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
