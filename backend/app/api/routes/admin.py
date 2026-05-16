"""
Endpoint admin observabilité : vue d'ensemble du système.
Pipeline ML, credits API, modèles déployés, fraîcheur des données, drift.
"""
import json
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from app.core.config import settings
from app.core.deps import get_db, get_current_user
from app.core.redis import get_redis
from app.db.models import User

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/observability")
async def get_observability(
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
    _user: User = Depends(get_current_user),
):
    """Vue d'ensemble : pipeline, credits, modèles, drift, data freshness."""

    # ─── DB stats ────────────────────────────────────────────────
    db_stats = {}
    r = await db.execute(text("""
        SELECT sport, status, COUNT(*) FROM matches
        GROUP BY sport, status ORDER BY sport, status
    """))
    matches_breakdown = []
    total_matches = 0
    for row in r:
        matches_breakdown.append({"sport": row[0], "status": row[1], "count": row[2]})
        total_matches += row[2]
    db_stats["matches_total"] = total_matches
    db_stats["matches_breakdown"] = matches_breakdown

    r = await db.execute(text("SELECT COUNT(*) FROM predictions"))
    db_stats["predictions_total"] = r.scalar() or 0

    r = await db.execute(text("SELECT status, COUNT(*) FROM bets GROUP BY status"))
    db_stats["bets_by_status"] = [{"status": row[0], "count": row[1]} for row in r]

    # Last match update
    r = await db.execute(text("SELECT MAX(updated_at) FROM matches WHERE sport='FOOTBALL'"))
    last_match_update = r.scalar()
    r = await db.execute(text("SELECT MAX(computed_at) FROM predictions"))
    last_prediction = r.scalar()
    db_stats["last_match_update"] = last_match_update.isoformat() if last_match_update else None
    db_stats["last_prediction_at"] = last_prediction.isoformat() if last_prediction else None

    # ─── Models deployed ─────────────────────────────────────────
    r = await db.execute(text("""
        SELECT version, accuracy, log_loss, brier_score, features_hash,
               artifact_path, is_deployed, trained_at, deployed_at
        FROM model_versions
        WHERE is_deployed = TRUE
        ORDER BY deployed_at DESC NULLS LAST
        LIMIT 10
    """))
    deployed_models = []
    for row in r:
        deployed_models.append({
            "version": row[0],
            "accuracy": row[1],
            "log_loss": row[2],
            "brier_score": row[3],
            "features_hash": row[4],
            "artifact_path": row[5],
            "trained_at": row[7].isoformat() if row[7] else None,
            "deployed_at": row[8].isoformat() if row[8] else None,
        })

    # ─── Cache standings (Redis) ─────────────────────────────────
    standings_cache = {}
    for code in ["PL", "PD", "BL1", "SA", "FL1"]:
        ttl = await redis.ttl(f"standings:{code}")
        standings_cache[code] = {
            "cached": ttl > 0,
            "ttl_seconds": ttl if ttl > 0 else None,
            "ttl_hours": round(ttl / 3600, 1) if ttl > 0 else None,
        }

    # ─── Locks Redis ─────────────────────────────────────────────
    locks = {}
    for key in ["foot:odds:lock", "nba:ingest:lock"]:
        ttl = await redis.ttl(key)
        locks[key] = {
            "active": ttl > 0,
            "ttl_seconds": ttl if ttl > 0 else 0,
            "ttl_hours": round(ttl / 3600, 1) if ttl > 0 else 0,
        }

    # ─── API credits (the-odds-api) — lu depuis Redis ────────────
    odds_credits = await redis.get("odds_api:remaining")

    # ─── Data freshness par marché ───────────────────────────────
    r = await db.execute(text("""
        SELECT
            COUNT(*) AS total_scheduled,
            SUM(CASE WHEN home_odds IS NOT NULL THEN 1 ELSE 0 END) AS with_h2h,
            SUM(CASE WHEN over_25_odds IS NOT NULL THEN 1 ELSE 0 END) AS with_ou,
            SUM(CASE WHEN ah_line IS NOT NULL THEN 1 ELSE 0 END) AS with_ah,
            MAX(updated_at) AS last_update
        FROM matches
        WHERE sport='FOOTBALL' AND status='SCHEDULED'
    """))
    row = r.fetchone()
    foot_freshness = {
        "scheduled_matches": row[0] or 0,
        "with_h2h_odds": row[1] or 0,
        "with_ou_odds": row[2] or 0,
        "with_ah_odds": row[3] or 0,
        "last_update": row[4].isoformat() if row[4] else None,
    }

    # ─── Drift global (modèle 1X2 actuel) ────────────────────────
    r = await db.execute(text("""
        SELECT version, accuracy, log_loss FROM model_versions
        WHERE is_deployed = TRUE
        ORDER BY deployed_at DESC NULLS LAST LIMIT 1
    """))
    dep = r.fetchone()
    drift = None
    if dep:
        version, oof_acc, oof_ll = dep
        r = await db.execute(text("""
            SELECT COUNT(*) FROM predictions p
            JOIN matches m ON m.id = p.match_id
            WHERE p.model_version = :v AND m.status='FINISHED'
              AND m.home_score IS NOT NULL
        """), {"v": version})
        n_settled = r.scalar() or 0
        drift = {
            "deployed_model": version,
            "oof_accuracy": oof_acc,
            "oof_log_loss": oof_ll,
            "live_n_settled": n_settled,
            "ready_to_evaluate": n_settled >= 30,
        }

    # ─── Config whitelist actuelle ───────────────────────────────
    whitelists = {
        "value_bet_1x2_leagues": settings.value_bet_leagues,
        "value_bet_ou_leagues": settings.value_bet_ou_leagues,
        "value_bet_ah_leagues": settings.value_bet_ah_leagues,
        "per_league_model_leagues": settings.per_league_model_leagues,
    }

    return {
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "db_stats": db_stats,
        "deployed_models": deployed_models,
        "standings_cache": standings_cache,
        "locks": locks,
        "odds_api_remaining": int(odds_credits) if odds_credits else None,
        "foot_freshness": foot_freshness,
        "drift": drift,
        "whitelists": whitelists,
    }


@router.get("/explain/{match_id}")
async def explain_prediction(
    match_id: str,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """
    Diagnostic d'une prédiction : SHAP values + top features + form récente
    des 2 équipes. Permet de comprendre POURQUOI le modèle prédit ce qu'il prédit.

    Utile pour les cas où l'intuition humaine diverge du modèle (ex : Bayern
    à 50% home vs cote market 2.96 → on veut savoir ce que le modèle "voit").
    """
    # 1. Match + latest prediction
    r = await db.execute(text("""
        SELECT m.id, m.home_team, m.away_team, m.match_date, m.league, m.season,
               m.status, m.home_score, m.away_score,
               m.home_odds, m.draw_odds, m.away_odds,
               m.over_25_odds, m.under_25_odds,
               m.ah_line, m.ah_home_odds, m.ah_away_odds,
               p.prob_home, p.prob_draw, p.prob_away,
               p.prob_over_25, p.prob_under_25,
               p.prob_ah_home, p.prob_ah_away,
               p.confidence, p.shap_values, p.model_version, p.computed_at
        FROM matches m
        LEFT JOIN LATERAL (
            SELECT * FROM predictions WHERE match_id = m.id
            ORDER BY computed_at DESC LIMIT 1
        ) p ON true
        WHERE m.id = :mid
    """), {"mid": match_id})
    row = r.fetchone()
    if not row:
        return {"error": "match_not_found", "match_id": match_id}

    home_team = row[1]
    away_team = row[2]
    match_date = row[3]
    league = row[4]

    # 2. SHAP values parsing + top features par |contribution|
    shap_raw = row[25]
    top_features = []
    if shap_raw:
        if isinstance(shap_raw, str):
            shap_raw = json.loads(shap_raw)
        if isinstance(shap_raw, dict):
            sorted_shap = sorted(shap_raw.items(),
                                 key=lambda x: abs(float(x[1])),
                                 reverse=True)
            top_features = [
                {"feature": k, "contribution": round(float(v), 4)}
                for k, v in sorted_shap[:15]
            ]

    # 3. Market implied probabilities pour comparaison
    home_odds = float(row[9]) if row[9] else None
    draw_odds = float(row[10]) if row[10] else None
    away_odds = float(row[11]) if row[11] else None
    market_implied = None
    if home_odds and draw_odds and away_odds:
        total = 1/home_odds + 1/draw_odds + 1/away_odds
        market_implied = {
            "home": round((1/home_odds) / total, 4),
            "draw": round((1/draw_odds) / total, 4),
            "away": round((1/away_odds) / total, 4),
            "margin": round(total - 1, 4),
        }

    # 4. Recent form : last 5 matches de chaque équipe avant match_date
    async def _recent_form(team: str, limit: int = 5):
        rr = await db.execute(text("""
            SELECT match_date, home_team, away_team, home_score, away_score, league
            FROM matches
            WHERE (home_team = :t OR away_team = :t)
              AND match_date < :d
              AND status = 'FINISHED'
              AND home_score IS NOT NULL AND away_score IS NOT NULL
            ORDER BY match_date DESC
            LIMIT :n
        """), {"t": team, "d": match_date, "n": limit})
        out = []
        for rrow in rr:
            home_t, away_t, hs, as_ = rrow[1], rrow[2], int(rrow[3]), int(rrow[4])
            if home_t == team:
                result = "W" if hs > as_ else ("D" if hs == as_ else "L")
                opp = away_t
                gf, ga = hs, as_
                venue = "H"
            else:
                result = "W" if as_ > hs else ("D" if hs == as_ else "L")
                opp = home_t
                gf, ga = as_, hs
                venue = "A"
            out.append({
                "date": rrow[0].isoformat() if rrow[0] else None,
                "venue": venue,
                "opponent": opp,
                "score": f"{gf}-{ga}",
                "result": result,
            })
        return out

    home_form = await _recent_form(home_team)
    away_form = await _recent_form(away_team)

    return {
        "match": {
            "id": row[0],
            "home_team": home_team,
            "away_team": away_team,
            "match_date": match_date.isoformat() if match_date else None,
            "league": league,
            "status": row[6],
            "score": f"{row[7]}-{row[8]}" if row[7] is not None else None,
        },
        "odds": {
            "1x2": {"home": home_odds, "draw": draw_odds, "away": away_odds},
            "ou_2_5": {"over": float(row[12]) if row[12] else None,
                       "under": float(row[13]) if row[13] else None},
            "ah": {"line": float(row[14]) if row[14] else None,
                   "home": float(row[15]) if row[15] else None,
                   "away": float(row[16]) if row[16] else None},
        },
        "market_implied_1x2": market_implied,
        "prediction": {
            "prob_home": float(row[17]) if row[17] else None,
            "prob_draw": float(row[18]) if row[18] else None,
            "prob_away": float(row[19]) if row[19] else None,
            "prob_over_25": float(row[20]) if row[20] else None,
            "prob_under_25": float(row[21]) if row[21] else None,
            "prob_ah_home": float(row[22]) if row[22] else None,
            "prob_ah_away": float(row[23]) if row[23] else None,
            "confidence": float(row[24]) if row[24] else None,
            "model_version": row[26],
            "computed_at": row[27].isoformat() if row[27] else None,
        },
        "shap_top_features": top_features,
        "home_recent_form": home_form,
        "away_recent_form": away_form,
    }
