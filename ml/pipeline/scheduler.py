"""
Scheduler ML : orchestration du pipeline toutes les 6h.
Ingestion → features → prédictions → écriture en base + Redis.
Entraînement automatique quotidien si ≥ 50 nouveaux matchs terminés.
"""
import asyncio
import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

import structlog
import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text

from .ingestion import (
    FootballDataClient, OddsAPIClient, normalize_match, SUPPORTED_LEAGUES,
    ODDS_API_SOCCER_KEYS, extract_totals_25, extract_h2h, extract_spreads,
)
from .nba_ingestion import NBAOddsClient, normalize_nba_upcoming, normalize_nba_score
from .nba_features import compute_nba_features, NBAFeatures
from .nba_model import EdgeAIModelNBA
from .features import compute_features_from_history, MatchFeatures
from .football_inference import FOOT_STATE
from .model import EdgeAIModel
from .settle import settle_finished_bets
from .drift import check_drift_and_rollback
from .trainer import maybe_auto_retrain_all
from .notifications import notify_new_value_bets
from .weekly_report import send_weekly_report_if_due
import joblib

log = structlog.get_logger()

DB_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://edgeai:edgeai_secret@localhost:5432/edgeai")


def _build_db_url(raw: str) -> tuple[str, dict]:
    url = raw.replace("postgres://", "postgresql+asyncpg://")
    url = url.replace("postgresql://", "postgresql+asyncpg://")
    connect_args = {}
    if "sslmode=require" in url:
        url = url.split("?")[0]
        connect_args = {"ssl": True}
    return url, connect_args


REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
FOOTBALL_API_KEY = os.getenv("FOOTBALL_DATA_API_KEY", "")
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
MODEL_DIR = Path(os.getenv("MODEL_DIR", "/app/artifacts/models"))

# Ligues qui utilisent leur propre modèle plutôt que le global (config par env).
# Format : "Premier League,La Liga" (séparé par virgules)
PER_LEAGUE_MODEL_LEAGUES = set(
    league.strip() for league in os.getenv("PER_LEAGUE_MODEL_LEAGUES", "").split(",")
    if league.strip()
)
PREDICTION_TTL = 6 * 3600  # 6h
STANDINGS_TTL = 24 * 3600  # 24h — les classements changent au plus 1×/jour
NBA_INGEST_LOCK_TTL = 22 * 3600  # NBA fetché 1×/jour pour économiser les 500 req/mois
FOOT_ODDS_LOCK_TTL = 22 * 3600   # Odds foot via the-odds-api : idem 1×/jour


async def _ingest_nba(session, redis, nba_model=None, nba_totals_model=None) -> int:
    """
    Fetch NBA upcoming + scores via the-odds-api (1×/jour pour économiser les credits).
    Si nba_model est fourni, génère aussi les prédictions 1X2 sur les upcoming.
    Si nba_totals_model est fourni, ajoute les probas Over/Under sur la ligne du match.
    Retourne le nombre de matchs ingérés.
    """
    if not ODDS_API_KEY:
        log.info("nba_ingest_skip_no_key")
        return 0

    lock_key = "nba:ingest:lock"
    if await redis.get(lock_key):
        log.info("nba_ingest_skip_recent")
        return 0

    client = NBAOddsClient(ODDS_API_KEY)
    try:
        # 1) Scores des matchs récents → finalise les FINISHED
        scores = await client.get_scores(days_from=2)
        finished_count = 0
        for raw in scores:
            normalized = normalize_nba_score(raw)
            if normalized:
                normalized.update({
                    "sport": "NBA", "league": "NBA",
                    "season": "current",
                    "home_team": raw["home_team"], "away_team": raw["away_team"],
                    "match_date": raw.get("commence_time"),
                })
                await _upsert_match(session, normalized)
                finished_count += 1
        log.info("nba_scores_ingested", count=finished_count)

        # 2) Matchs upcoming avec cotes + prédictions
        upcoming = await client.get_upcoming()
        upcoming_count = 0
        pred_count = 0
        nba_history = None
        for raw in upcoming:
            normalized = normalize_nba_upcoming(raw)
            if not normalized:
                continue
            match_id = await _upsert_match(session, normalized)
            upcoming_count += 1

            if nba_model and match_id:
                # Charge l'historique NBA une seule fois (mis en cache)
                if nba_history is None:
                    nba_history = await _load_nba_history(session)
                prediction = await _generate_nba_prediction(
                    nba_model, normalized, nba_history
                )
                # Ajoute les probas Over/Under si modèle dispo + ligne capturée
                if prediction and nba_totals_model is not None and normalized.get("nba_total_line"):
                    totals = await _generate_nba_totals_prediction(
                        nba_totals_model, normalized, nba_history,
                    )
                    if totals:
                        prediction.update(totals)
                if prediction:
                    await _upsert_prediction(session, match_id, prediction)
                    pred_count += 1
        log.info("nba_upcoming_ingested", count=upcoming_count, predictions=pred_count)

        # Lock 22h pour éviter de retaper l'API
        await redis.setex(lock_key, NBA_INGEST_LOCK_TTL, "1")
        # Stocke le nb de credits restants pour la page admin
        if client.last_remaining is not None:
            await redis.set("odds_api:remaining", str(client.last_remaining))
        return upcoming_count + finished_count
    finally:
        await client.close()


def _load_nba_model() -> EdgeAIModelNBA | None:
    """Charge le modèle NBA déployé (model_nba_latest.joblib)."""
    latest = MODEL_DIR / "model_nba_latest.joblib"
    if not latest.exists():
        return None
    try:
        return EdgeAIModelNBA.load(latest)
    except Exception as e:
        log.error("nba_model_load_error", error=str(e))
        return None


def _load_ou_model():
    """Charge le modèle O/U 2.5 déployé (model_ou_latest.joblib)."""
    latest = MODEL_DIR / "model_ou_latest.joblib"
    if not latest.exists():
        return None
    try:
        return joblib.load(latest)
    except Exception as e:
        log.error("ou_model_load_error", error=str(e))
        return None


def _load_ah_model():
    """Charge le modèle Asian Handicap déployé (model_ah_latest.joblib)."""
    latest = MODEL_DIR / "model_ah_latest.joblib"
    if not latest.exists():
        return None
    try:
        return joblib.load(latest)
    except Exception as e:
        log.error("ah_model_load_error", error=str(e))
        return None


def _load_dc_model():
    """Charge le modèle Dixon-Coles pour le 1X2 (model_dc_latest.joblib).

    DC remplace XGBoost pour le 1X2 quand dispo. Backtest a montré +4pts ROI
    grâce à des probas mieux discriminantes sur les favoris clairs.

    Format actuel : dict per-league {league: DixonColes}. Plus précis que un
    DC global car les équipes d'une ligue ne jouent qu'entre elles, donc le
    pool global biaise les attack ratings.
    """
    latest = MODEL_DIR / "model_dc_latest.joblib"
    if not latest.exists():
        return None
    try:
        import sys
        ml_root = Path(__file__).parent.parent
        if str(ml_root) not in sys.path:
            sys.path.insert(0, str(ml_root))
        from dixon_coles import DixonColes
        bundle = joblib.load(latest)

        # Détecte format : per_league dict ou single DC (rétrocompat)
        if isinstance(bundle, dict) and bundle.get("type") == "per_league":
            per_league = {}
            for league, data in bundle["per_league"].items():
                dc = DixonColes()
                dc.attack = data["attack"]
                dc.defense = data["defense"]
                dc.home_adv = data["home_adv"]
                dc.rho = data["rho"]
                dc.teams = data["teams"]
                dc._fitted = data["_fitted"]
                per_league[league] = dc
            return {"per_league": per_league, "type": "per_league"}
        else:
            # Format ancien single DC (fallback)
            return DixonColes.load(latest)
    except Exception as e:
        log.error("dc_model_load_error", error=str(e))
        return None


def _load_wc_inference():
    """Charge le modèle WC + initialise l'inférence (lazy load du CSV)."""
    latest = MODEL_DIR / "model_wc_latest.joblib"
    if not latest.exists():
        return None
    try:
        from .wc_inference import WCInference
        from pathlib import Path
        bundle = joblib.load(latest)
        csv_path = Path("/app/data/raw/international_matches.csv")
        # Modèle de buts Dixon-Coles (AH + O/U) — optionnel
        goals_model = None
        goals_path = MODEL_DIR / "model_wcgoals_latest.joblib"
        if goals_path.exists():
            try:
                from .wc_goals import WCGoalsModel
                gbundle = joblib.load(goals_path)
                goals_model = WCGoalsModel.from_dict(gbundle["goals_model"])
                log.info("wc_goals_model_loaded", n_teams=len(goals_model.attack))
            except Exception as ge:
                log.error("wc_goals_model_load_error", error=str(ge))
        return WCInference(bundle, csv_path, goals_model=goals_model)
    except Exception as e:
        log.error("wc_model_load_error", error=str(e))
        return None


async def _backfill_shots_daily(session: AsyncSession, redis) -> bool:
    """
    Backfill quotidien des shots/SOT/corners/fouls depuis football-data.co.uk.
    Lock 22h pour éviter de spam fdco. Ne fetch que les 2 dernières saisons
    (mode --recent) → ~10 CSV à télécharger, ~30s total.
    """
    lock_key = "shots:backfill:lock"
    if await redis.get(lock_key):
        return False
    try:
        import sys, importlib
        ml_root = Path(__file__).parent.parent
        if str(ml_root) not in sys.path:
            sys.path.insert(0, str(ml_root))
        cs = importlib.import_module("collect_shots")
        # Ne fetch que les 2 dernières saisons (saison en cours + précédente)
        seasons = cs.ALL_SEASONS[-2:]
        df = cs.fetch_all(seasons)
        if df.empty:
            log.warning("shots_backfill_no_data")
            return False
        # UPDATE DB seulement (pas le CSV local, on est en prod)
        stats = await cs.update_db(df)
        await redis.setex(lock_key, 22 * 3600, "1")
        log.info("shots_backfill_done", **stats, n_fetched=len(df))
        return True
    except Exception as e:
        log.error("shots_backfill_error", error=str(e))
        return False


async def _refresh_intl_matches_csv(redis) -> bool:
    """
    Re-fetch le CSV des matchs internationaux 1×/jour (lock 22h).
    Sources : github.com/martj42/international_results
    """
    lock_key = "intl:csv:lock"
    if await redis.get(lock_key):
        return False  # déjà fetché récemment

    from pathlib import Path
    import httpx
    url = "https://raw.githubusercontent.com/martj42/international_results/master/results.csv"
    csv_path = Path("/app/data/raw/international_matches.csv")
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()
            import pandas as pd
            from io import StringIO
            df = pd.read_csv(StringIO(r.text))
            df["date"] = pd.to_datetime(df["date"], errors="coerce")
            df = df.dropna(subset=["date", "home_team", "away_team", "home_score", "away_score"])
            df["home_score"] = df["home_score"].astype(int)
            df["away_score"] = df["away_score"].astype(int)
            df["is_wc"] = df["tournament"] == "FIFA World Cup"
            df["is_wc_qualifier"] = df["tournament"].str.contains("World Cup qualification", na=False, regex=False)
            df["is_friendly"] = df["tournament"] == "Friendly"
            csv_path.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(csv_path, index=False)
        await redis.setex(lock_key, 22 * 3600, "1")
        log.info("intl_csv_refreshed", n_matches=len(df), path=str(csv_path))
        return True
    except Exception as e:
        log.error("intl_csv_refresh_error", error=str(e))
        return False


async def _ingest_foot_odds(session, redis) -> int:
    """
    Fetch les cotes h2h + totals 2.5 + spreads (AH) pour les 5 ligues foot via the-odds-api.
    1×/jour (lock Redis). Update les colonnes home_odds, draw_odds, away_odds,
    over_25_odds, under_25_odds, ah_line, ah_home_odds, ah_away_odds.
    """
    if not ODDS_API_KEY:
        log.info("foot_odds_skip_no_key")
        return 0

    lock_key = "foot:odds:lock"
    if await redis.get(lock_key):
        log.info("foot_odds_skip_recent")
        return 0

    client = OddsAPIClient(ODDS_API_KEY)
    updated = 0
    try:
        for league_name, sport_key in ODDS_API_SOCCER_KEYS.items():
            raw = await client.get_odds(sport=sport_key, markets="h2h,totals,spreads")
            for game in raw:
                home = game.get("home_team", "")
                away = game.get("away_team", "")
                commence = game.get("commence_time", "")
                if not (home and away and commence):
                    continue
                bookmakers = game.get("bookmakers", [])
                h_odds, d_odds, a_odds = extract_h2h(bookmakers, home, away)
                o_odds, u_odds = extract_totals_25(bookmakers)
                ah_line, ah_h_odds, ah_a_odds = extract_spreads(bookmakers, home, away)

                # Skip si toutes les cotes sont NULL (rien à update)
                if not any([h_odds, d_odds, a_odds, o_odds, u_odds,
                            ah_line, ah_h_odds, ah_a_odds]):
                    continue

                # Update via team names + match_date (~même jour).
                # STRICT matching pour éviter de contaminer plusieurs matchs en 1 appel :
                # match exact si possible, fuzzy seulement avec une similarity score forte
                # (>= 0.6) via PostgreSQL similarity ou trigram, sinon EXACT match only.
                # Si fuzzy nécessaire, on requiert un overlap d'au moins 8 caractères.
                try:
                    from datetime import datetime as _dt
                    dt = _dt.fromisoformat(commence.replace("Z", "+00:00")).replace(tzinfo=None)
                    async with session.begin_nested():
                        result = await session.execute(text("""
                            UPDATE matches
                            SET home_odds = COALESCE(CAST(:h AS DOUBLE PRECISION), home_odds),
                                draw_odds = COALESCE(CAST(:d AS DOUBLE PRECISION), draw_odds),
                                away_odds = COALESCE(CAST(:a AS DOUBLE PRECISION), away_odds),
                                over_25_odds = COALESCE(CAST(:o AS DOUBLE PRECISION), over_25_odds),
                                under_25_odds = COALESCE(CAST(:u AS DOUBLE PRECISION), under_25_odds),
                                ah_line = COALESCE(CAST(:ahl AS DOUBLE PRECISION), ah_line),
                                ah_home_odds = COALESCE(CAST(:ahh AS DOUBLE PRECISION), ah_home_odds),
                                ah_away_odds = COALESCE(CAST(:aha AS DOUBLE PRECISION), ah_away_odds),
                                opening_home_odds = COALESCE(opening_home_odds, CAST(:h AS DOUBLE PRECISION)),
                                opening_draw_odds = COALESCE(opening_draw_odds, CAST(:d AS DOUBLE PRECISION)),
                                opening_away_odds = COALESCE(opening_away_odds, CAST(:a AS DOUBLE PRECISION)),
                                opening_over_25_odds = COALESCE(opening_over_25_odds, CAST(:o AS DOUBLE PRECISION)),
                                opening_under_25_odds = COALESCE(opening_under_25_odds, CAST(:u AS DOUBLE PRECISION)),
                                opening_ah_line = COALESCE(opening_ah_line, CAST(:ahl AS DOUBLE PRECISION)),
                                opening_ah_home_odds = COALESCE(opening_ah_home_odds, CAST(:ahh AS DOUBLE PRECISION)),
                                opening_ah_away_odds = COALESCE(opening_ah_away_odds, CAST(:aha AS DOUBLE PRECISION)),
                                opening_captured_at = COALESCE(opening_captured_at, NOW()),
                                updated_at = NOW()
                            WHERE id IN (
                                SELECT id FROM matches
                                WHERE sport = 'FOOTBALL'
                                  AND status = 'SCHEDULED'
                                  AND ABS(EXTRACT(EPOCH FROM (match_date - :dt))) < 7200
                                  AND (
                                    -- Exact match (case insensitive)
                                    LOWER(home_team) = LOWER(:home) OR
                                    -- Fuzzy : substring de >= 8 chars, dans les 2 sens
                                    (LENGTH(:home) >= 8 AND home_team ILIKE '%' || :home || '%') OR
                                    (LENGTH(home_team) >= 8 AND :home ILIKE '%' || home_team || '%')
                                  )
                                  AND (
                                    LOWER(away_team) = LOWER(:away) OR
                                    (LENGTH(:away) >= 8 AND away_team ILIKE '%' || :away || '%') OR
                                    (LENGTH(away_team) >= 8 AND :away ILIKE '%' || away_team || '%')
                                  )
                                ORDER BY ABS(EXTRACT(EPOCH FROM (match_date - :dt))) ASC
                                LIMIT 1
                            )
                            RETURNING id
                        """), {
                            "h": h_odds, "d": d_odds, "a": a_odds,
                            "o": o_odds, "u": u_odds,
                            "ahl": ah_line, "ahh": ah_h_odds, "aha": ah_a_odds,
                            "home": home, "away": away, "dt": dt,
                        })
                        # Safety : LIMIT 1 garantit au plus 1 row, mais on log si plus
                        if result.rowcount > 1:
                            log.warning("foot_odds_multi_match_warning",
                                        home=home, away=away, rows=result.rowcount,
                                        action="should_not_happen_with_LIMIT_1")
                        if result.rowcount > 0:
                            updated += result.rowcount
                except Exception as e:
                    log.error("foot_odds_upsert_error", home=home, away=away, error=str(e))

        await redis.setex(lock_key, FOOT_ODDS_LOCK_TTL, "1")
        log.info("foot_odds_ingested", matches_updated=updated)
        # Stocke le nb de credits restants pour la page admin
        if client.last_remaining is not None:
            await redis.set("odds_api:remaining", str(client.last_remaining))
        return updated
    finally:
        await client.close()


async def _load_nba_history(session: AsyncSession):
    """Charge l'historique NBA en DataFrame pour calcul de features."""
    import pandas as pd
    try:
        result = await session.execute(
            text("""
                SELECT home_team, away_team, home_score, away_score, match_date
                FROM matches
                WHERE sport = 'NBA' AND status = 'FINISHED'
                  AND home_score IS NOT NULL AND away_score IS NOT NULL
                ORDER BY match_date
            """)
        )
        rows = result.fetchall()
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows, columns=["home_team", "away_team", "home_score", "away_score", "match_date"])
        df["match_date"] = pd.to_datetime(df["match_date"])
        df["home_score"] = pd.to_numeric(df["home_score"], errors="coerce")
        df["away_score"] = pd.to_numeric(df["away_score"], errors="coerce")
        return df.dropna(subset=["home_score", "away_score"])
    except Exception as e:
        log.error("nba_history_load_error", error=str(e))
        import pandas as pd
        return pd.DataFrame()


async def _generate_nba_prediction(model: EdgeAIModelNBA, match_data: dict, history) -> dict | None:
    """Prédiction NBA depuis l'historique en DB."""
    import pandas as pd
    try:
        match_date_raw = match_data.get("match_date", "")
        if isinstance(match_date_raw, str):
            match_date = pd.Timestamp(match_date_raw.replace("Z", "+00:00")).tz_localize(None)
        else:
            match_date = pd.Timestamp(match_date_raw)

        if history is None or len(history) == 0:
            features = NBAFeatures()  # defaults
        else:
            features = compute_nba_features(
                match_data["home_team"], match_data["away_team"],
                match_date, history,
            )
        return model.predict(features)
    except Exception as e:
        log.error("nba_prediction_error", error=str(e))
        return None


def _load_nba_totals_model():
    """Charge le modèle NBA Totals (CalibratedClassifierCV binaire over/under)."""
    latest = MODEL_DIR / "model_nba_totals_latest.joblib"
    if not latest.exists():
        return None
    try:
        return joblib.load(latest)
    except Exception as e:
        log.error("nba_totals_model_load_error", error=str(e))
        return None


async def _generate_nba_totals_prediction(
    bundle: dict, match_data: dict, history
) -> dict | None:
    """Prédiction NBA Over/Under sur la ligne du match (nba_total_line).

    Le modèle a appris à prédire P(total_points > closing_line). On lui passe les
    features NBA + la ligne en feature implicite (déjà encodée par compute_nba_features
    si elle utilise la ligne, sinon le modèle est neutre vs la ligne et compare
    juste sa prédiction de points totaux à la ligne du marché).

    Renvoie {prob_over_25, prob_under_25} (clés réutilisées pour stockage en DB).
    """
    import numpy as np
    import pandas as pd
    try:
        cal = bundle.get("model") if isinstance(bundle, dict) else bundle
        if cal is None:
            return None

        match_date_raw = match_data.get("match_date", "")
        if isinstance(match_date_raw, str):
            match_date = pd.Timestamp(match_date_raw.replace("Z", "+00:00")).tz_localize(None)
        else:
            match_date = pd.Timestamp(match_date_raw)

        if history is None or len(history) == 0:
            features = NBAFeatures()
        else:
            features = compute_nba_features(
                match_data["home_team"], match_data["away_team"],
                match_date, history,
            )

        X = features.to_array().reshape(1, -1)
        proba = cal.predict_proba(X)[0]
        # label=1 → Over, label=0 → Under
        return {
            "prob_under_25": round(float(proba[0]), 4),
            "prob_over_25": round(float(proba[1]), 4),
        }
    except Exception as e:
        log.error("nba_totals_predict_error", error=str(e))
        return None


async def _get_cached_standings(redis, football_client, league_code: str) -> tuple[dict, int, bool]:
    """
    Renvoie (standings, total_teams, was_api_call). Cache Redis 24h.
    Évite ~15 appels/jour à l'API → moins de 429.
    """
    cache_key = f"standings:{league_code}"
    cached = await redis.get(cache_key)
    if cached:
        try:
            data = json.loads(cached)
            log.info("standings_cache_hit", league=league_code)
            return data.get("standings", {}), data.get("total_teams", 20), False
        except Exception:
            pass

    standings = await football_client.get_standings(league_code)
    total = len(standings) if standings else 20
    if standings:
        await redis.setex(
            cache_key, STANDINGS_TTL,
            json.dumps({"standings": standings, "total_teams": total}),
        )
        log.info("standings_cached", league=league_code, n_teams=total)
    return standings, total, True


async def run_pipeline():
    log.info("pipeline_start", timestamp=datetime.now(timezone.utc).isoformat())

    db_url, connect_args = _build_db_url(DB_URL)
    engine = create_async_engine(db_url, connect_args=connect_args)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    redis = await aioredis.from_url(REDIS_URL, decode_responses=True)

    model = _load_active_model()
    if model is None:
        log.warning("no_model_available", msg="Pas de modèle football déployé")

    nba_model = _load_nba_model()
    if nba_model is None:
        log.info("no_nba_model_available")

    nba_totals_model = _load_nba_totals_model()
    if nba_totals_model is None:
        log.info("no_nba_totals_model_available")

    ou_model = _load_ou_model()
    if ou_model is None:
        log.info("no_ou_model_available")

    ah_model = _load_ah_model()
    if ah_model is None:
        log.info("no_ah_model_available")

    # Dixon-Coles : modèle alternatif pour 1X2 (backtest +1pt ROI vs XGB -3pt)
    # Si dispo + équipes connues, remplace XGB pour le 1X2. Fallback XGB sinon.
    dc_model = _load_dc_model()
    if dc_model is None:
        log.info("no_dc_model_available_fallback_xgb")
    elif isinstance(dc_model, dict) and dc_model.get("type") == "per_league":
        log.info("dc_model_loaded_per_league",
                 leagues={l: {"n_teams": len(dc.teams),
                              "home_adv": round(dc.home_adv, 3)}
                          for l, dc in dc_model["per_league"].items()})
    else:
        log.info("dc_model_loaded", n_teams=len(dc_model.teams),
                 home_adv=round(dc_model.home_adv, 3))

    # Refresh CSV intl matches 1×/jour avant de charger le modèle WC
    await _refresh_intl_matches_csv(redis)
    wc_inference = _load_wc_inference()
    if wc_inference is None:
        log.info("no_wc_model_available")
    else:
        # Statut WC publié dans Redis pour la page admin (le backend n'a pas accès
        # aux .joblib, montés seulement côté ml_worker).
        gm = wc_inference.goals_model
        try:
            await redis.set("wc:status", json.dumps({
                "x12_model": wc_inference.x12_version,
                "x12_loaded": wc_inference.model is not None,
                "goals_model_loaded": gm is not None,
                "goals_n_teams": len(gm.attack) if gm else 0,
                "goals_trained_through": getattr(gm, "trained_through", None) if gm else None,
                "goals_home_adv": round(gm.home_adv, 3) if gm else None,
                "goals_rho": round(gm.rho, 3) if gm else None,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }))
        except Exception as e:
            log.warning("wc_status_redis_write_error", error=str(e))

    football_client = FootballDataClient(FOOTBALL_API_KEY)
    odds_client = OddsAPIClient(ODDS_API_KEY)

    try:
        async with async_session() as session:
            for code, league_name in SUPPORTED_LEAGUES.items():
                # World Cup : modèle dédié (wc_inference), pas de standings (poules).
                # Validé walk-forward sur 4 WC : +12.9 pts d'accuracy vs baseline.
                if league_name == "World Cup":
                    await _process_world_cup(
                        code, league_name, session, redis, football_client,
                        wc_inference=wc_inference,
                    )
                    await asyncio.sleep(7)
                    continue

                # Classement caché 24h pour éviter les 429 (free tier 10 req/min)
                standings, total_teams, was_api_call = await _get_cached_standings(
                    redis, football_client, code
                )
                if was_api_call:
                    await asyncio.sleep(7)  # rate limit gap après vrai appel

                await _process_league(
                    code, league_name, session, redis,
                    football_client, odds_client, model,
                    standings, total_teams,
                    ou_model=ou_model, ah_model=ah_model, dc_model=dc_model,
                )
                await asyncio.sleep(7)  # gap entre ligues

            # Fetch cotes foot (h2h + O/U 2.5 + AH) 1×/jour via the-odds-api.
            # Inclut la Coupe du Monde (soccer_fifa_world_cup est dans ODDS_API_SOCCER_KEYS
            # et les matchs WC sont sport=FOOTBALL/SCHEDULED → mêmes colonnes mises à jour).
            foot_odds_count = await _ingest_foot_odds(session, redis)
            if foot_odds_count:
                log.info("foot_odds_pipeline_done", updated=foot_odds_count)

            # Prédictions WC APRÈS les cotes : l'AH est calculé à la ligne du book.
            wc_pred_count = await _generate_wc_predictions(session, wc_inference)
            if wc_pred_count:
                log.info("wc_predictions_pipeline_done", count=wc_pred_count)

            # Ingestion NBA via the-odds-api (1×/jour, lock Redis)
            nba_count = await _ingest_nba(
                session, redis,
                nba_model=nba_model,
                nba_totals_model=nba_totals_model,
            )
            if nba_count:
                log.info("nba_pipeline_done", matches=nba_count)

            # Commit IMMÉDIAT de toute l'ingestion (foot + NBA + odds + prédictions)
            # AVANT l'auto-retrain qui est long (~5-7 min). Sinon une interruption
            # (restart, crash, OOM) pendant le retrain annule TOUTE l'ingestion non
            # commitée — bug observé le 21/05/2026 (NBA jamais persistée).
            await session.commit()

            # Settlement automatique des paris sur matchs terminés
            settled = await settle_finished_bets(session)
            if settled:
                log.info("auto_settlement_done", bets_settled=settled)

            # Détection de dérive + rollback si modèle dégradé
            drift_report = await check_drift_and_rollback(session)
            log.info("drift_check", **{k: v for k, v in drift_report.items() if v is not None})

            # Backfill quotidien shots/SOT depuis fdco (Phase 2 features)
            # 22h lock → ~1×/jour, ~30s d'API calls
            await _backfill_shots_daily(session, redis)

            # Réentraînement automatique quotidien (1X2 + OU + AH, gates inclus)
            retrain_results = await maybe_auto_retrain_all(session)
            log.info("auto_retrain_cycle_done", **retrain_results)

            await session.commit()

            # Notifications email Brevo des nouveaux value bets (après commit)
            try:
                from app.core.config import settings as backend_settings
            except Exception:
                # ml_worker tourne sans le backend, on importe les settings minimum
                from types import SimpleNamespace
                import os as _os
                backend_settings = SimpleNamespace(
                    value_bet_leagues=_os.getenv("VALUE_BET_LEAGUES", "Ligue 1,Bundesliga,Serie A").split(","),
                    value_bet_ou_leagues=[x for x in _os.getenv("VALUE_BET_OU_LEAGUES", "").split(",") if x],
                    value_bet_ah_leagues=_os.getenv("VALUE_BET_AH_LEAGUES", "Ligue 1,Premier League,Serie A").split(","),
                    value_bet_edge_min=float(_os.getenv("VALUE_BET_EDGE_MIN", "0.05")),
                    value_bet_edge_max=float(_os.getenv("VALUE_BET_EDGE_MAX", "0.20")),
                )
            async with async_session() as notif_session:
                try:
                    n_notified = await notify_new_value_bets(notif_session, redis, backend_settings)
                    if n_notified:
                        log.info("notifications_done", count=n_notified)
                except Exception as e:
                    log.error("notifications_error", error=str(e))

                # Rapport hebdo (jeudi >21h Europe/Paris, lock Redis 1×/semaine)
                try:
                    sent = await send_weekly_report_if_due(notif_session, redis, backend_settings)
                    if sent:
                        log.info("weekly_report_done")
                except Exception as e:
                    log.error("weekly_report_error", error=str(e))
    finally:
        await football_client.close()
        await odds_client.close()
        await redis.aclose()
        await engine.dispose()

    log.info("pipeline_done", timestamp=datetime.now(timezone.utc).isoformat())


async def _process_world_cup(code, league_name, session, redis, football_client, wc_inference=None):
    """
    Ingestion des matchs Coupe du Monde (récents + à venir) UNIQUEMENT.

    Les prédictions WC sont générées séparément par _generate_wc_predictions(),
    APRÈS l'ingestion des cotes (_ingest_foot_odds), pour que l'Asian Handicap soit
    calculé à la ligne réelle du bookmaker (ah_line). Sinon, au 1er passage ah_line
    serait NULL → pas d'AH, et la prédiction ne se rafraîchirait jamais.
    """
    try:
        recent_finished = await football_client.get_recently_finished(code, days=2)
        log.info("wc_finished_fetched", count=len(recent_finished))
        for raw in recent_finished:
            normalized = normalize_match(raw, league_name)
            await _upsert_match(session, normalized)

        await asyncio.sleep(7)

        upcoming = await football_client.get_upcoming_matches(code, days=14)
        log.info("wc_matches_fetched", count=len(upcoming))
        for raw in upcoming:
            normalized = normalize_match(raw, league_name)
            await _upsert_match(session, normalized)
    except Exception as e:
        log.error("wc_process_error", error=str(e))


async def _generate_wc_predictions(session, wc_inference) -> int:
    """
    Génère les prédictions WC (1X2 + O/U + AH) sur les matchs WC à venir, APRÈS que
    les cotes (dont ah_line) aient été ingérées. Remplace les prédictions existantes
    (delete + insert) pour que l'API serve toujours la plus récente, avec l'AH.

    AH calculé à la ligne du bookmaker (ah_line en DB). Matchs WC = terrain neutre.
    """
    if wc_inference is None:
        return 0
    try:
        rows = (await session.execute(text("""
            SELECT id, home_team, away_team, match_date, ah_line
            FROM matches
            WHERE league = 'World Cup' AND status = 'SCHEDULED'
              AND match_date >= NOW() AND match_date <= NOW() + INTERVAL '14 days'
        """))).mappings().all()

        count = 0
        for m in rows:
            pred = wc_inference.predict(m["home_team"], m["away_team"], m["match_date"])
            if not pred:
                continue
            pred["model_version"] = "wc_intl"
            markets = wc_inference.goals_markets(
                m["home_team"], m["away_team"],
                neutral=True, ah_line=m["ah_line"],
            )
            pred.update(markets)
            # Remplace les prédictions existantes du match (pas de contrainte unique
            # sur match_id → on purge avant d'insérer la fraîche pour éviter les stale).
            async with session.begin_nested():
                await session.execute(
                    text("DELETE FROM predictions WHERE match_id = :id"),
                    {"id": m["id"]},
                )
            await _upsert_prediction(session, m["id"], pred)
            count += 1
        if count:
            log.info("wc_predictions_generated", count=count)
        return count
    except Exception as e:
        log.error("wc_generate_predictions_error", error=str(e))
        return 0


async def _process_league(
    code, league_name, session, redis,
    football_client, odds_client, model,
    standings, total_teams,
    ou_model=None, ah_model=None, dc_model=None,
):
    # 1. Mettre à jour les matchs récemment terminés
    recent_finished = await football_client.get_recently_finished(code, days=2)
    log.info("finished_fetched", league=code, count=len(recent_finished))
    for raw in recent_finished:
        normalized = normalize_match(raw, league_name)
        await _upsert_match(session, normalized)

    await asyncio.sleep(7)  # rate limit between calls

    # 2. Prédictions sur les matchs à venir
    raw_matches = await football_client.get_upcoming_matches(code, days=3)
    log.info("matches_fetched", league=code, count=len(raw_matches))

    # Per-league routing : si cette ligue est configurée pour utiliser son modèle dédié
    effective_model = model
    if league_name in PER_LEAGUE_MODEL_LEAGUES:
        pl_model = _load_per_league_model(league_name)
        if pl_model:
            effective_model = pl_model
            log.info("using_per_league_model", league=league_name, version=pl_model.version)

    for raw in raw_matches:
        normalized = normalize_match(raw, league_name)
        match_id = await _upsert_match(session, normalized)

        if effective_model and match_id:
            prediction = await _generate_prediction(
                effective_model, normalized, session, standings, total_teams,
                dc_model=dc_model,
            )
            # Ajoute les probas O/U si le modèle O/U est dispo
            if ou_model is not None:
                ou_probs = await _generate_ou_prediction(
                    ou_model, normalized, session, standings, total_teams
                )
                if ou_probs:
                    prediction.update(ou_probs)
            # Ajoute les probas AH si le modèle AH est dispo
            if ah_model is not None:
                ah_probs = await _generate_ah_prediction(
                    ah_model, normalized, session, standings, total_teams
                )
                if ah_probs:
                    prediction.update(ah_probs)
            await _upsert_prediction(session, match_id, prediction)
            # NE PLUS auto-générer les cotes à partir du modèle (tautologique :
            # affiche "marché = modèle" car cotes = inverse proba modèle). Les
            # vraies cotes viennent uniquement de _ingest_foot_odds via odds-api.
            # await _upsert_odds_from_prediction(session, match_id, prediction)

            cache_key = f"prediction:{match_id}"
            await redis.setex(cache_key, PREDICTION_TTL, json.dumps(prediction))


async def _upsert_match(session: AsyncSession, data: dict) -> str | None:
    from datetime import datetime as _dt
    row_data = dict(data)
    if isinstance(row_data.get("match_date"), str):
        dt = _dt.fromisoformat(row_data["match_date"].replace("Z", "+00:00"))
        row_data["match_date"] = dt.replace(tzinfo=None)

    # Valeurs par défaut pour les colonnes nullable
    row_data.setdefault("sport", "FOOTBALL")
    row_data.setdefault("home_score", None)
    row_data.setdefault("away_score", None)
    row_data.setdefault("ht_home_score", None)
    row_data.setdefault("ht_away_score", None)
    row_data.setdefault("home_yellow_cards", 0)
    row_data.setdefault("away_yellow_cards", 0)
    row_data.setdefault("home_red_cards", 0)
    row_data.setdefault("away_red_cards", 0)
    row_data.setdefault("home_odds", None)
    row_data.setdefault("draw_odds", None)
    row_data.setdefault("away_odds", None)
    # NBA Totals : ligne + cotes Over/Under (vide pour foot et NBA sans totals dispo)
    row_data.setdefault("nba_total_line", None)
    row_data.setdefault("over_25_odds", None)
    row_data.setdefault("under_25_odds", None)

    try:
        async with session.begin_nested():
            result = await session.execute(
                text("""
                    INSERT INTO matches (
                        id, external_id, sport, league, season, home_team, away_team,
                        match_date, status, home_score, away_score,
                        ht_home_score, ht_away_score,
                        home_yellow_cards, away_yellow_cards,
                        home_red_cards, away_red_cards,
                        home_odds, draw_odds, away_odds,
                        over_25_odds, under_25_odds, nba_total_line,
                        opening_home_odds, opening_draw_odds, opening_away_odds,
                        opening_over_25_odds, opening_under_25_odds, opening_nba_total_line,
                        opening_captured_at,
                        created_at, updated_at
                    )
                    VALUES (
                        gen_random_uuid(), :external_id, :sport, :league, :season,
                        :home_team, :away_team, :match_date, :status,
                        :home_score, :away_score,
                        :ht_home_score, :ht_away_score,
                        :home_yellow_cards, :away_yellow_cards,
                        :home_red_cards, :away_red_cards,
                        CAST(:home_odds AS DOUBLE PRECISION),
                        CAST(:draw_odds AS DOUBLE PRECISION),
                        CAST(:away_odds AS DOUBLE PRECISION),
                        CAST(:over_25_odds AS DOUBLE PRECISION),
                        CAST(:under_25_odds AS DOUBLE PRECISION),
                        CAST(:nba_total_line AS DOUBLE PRECISION),
                        CAST(:home_odds AS DOUBLE PRECISION),
                        CAST(:draw_odds AS DOUBLE PRECISION),
                        CAST(:away_odds AS DOUBLE PRECISION),
                        CAST(:over_25_odds AS DOUBLE PRECISION),
                        CAST(:under_25_odds AS DOUBLE PRECISION),
                        CAST(:nba_total_line AS DOUBLE PRECISION),
                        CASE WHEN CAST(:home_odds AS DOUBLE PRECISION) IS NOT NULL THEN NOW() ELSE NULL END,
                        NOW(), NOW()
                    )
                    ON CONFLICT (external_id) DO UPDATE
                        SET status            = EXCLUDED.status,
                            -- Met à jour la date si elle change (matchs reportés,
                            -- ou même id the-odds-api ré-annoncé avec une nouvelle
                            -- commence_time). Sinon des matchs restaient bloqués à
                            -- une date passée → invisibles dans le tracking "à venir".
                            match_date        = EXCLUDED.match_date,
                            home_score        = COALESCE(EXCLUDED.home_score, matches.home_score),
                            away_score        = COALESCE(EXCLUDED.away_score, matches.away_score),
                            ht_home_score     = COALESCE(EXCLUDED.ht_home_score, matches.ht_home_score),
                            ht_away_score     = COALESCE(EXCLUDED.ht_away_score, matches.ht_away_score),
                            home_yellow_cards = EXCLUDED.home_yellow_cards,
                            away_yellow_cards = EXCLUDED.away_yellow_cards,
                            home_red_cards    = EXCLUDED.home_red_cards,
                            away_red_cards    = EXCLUDED.away_red_cards,
                            home_odds         = COALESCE(EXCLUDED.home_odds, matches.home_odds),
                            draw_odds         = COALESCE(EXCLUDED.draw_odds, matches.draw_odds),
                            away_odds         = COALESCE(EXCLUDED.away_odds, matches.away_odds),
                            over_25_odds      = COALESCE(EXCLUDED.over_25_odds, matches.over_25_odds),
                            under_25_odds     = COALESCE(EXCLUDED.under_25_odds, matches.under_25_odds),
                            nba_total_line    = COALESCE(EXCLUDED.nba_total_line, matches.nba_total_line),
                            -- Opening : seulement si pas déjà fixé (jamais modifié)
                            opening_home_odds = COALESCE(matches.opening_home_odds, EXCLUDED.home_odds),
                            opening_draw_odds = COALESCE(matches.opening_draw_odds, EXCLUDED.draw_odds),
                            opening_away_odds = COALESCE(matches.opening_away_odds, EXCLUDED.away_odds),
                            opening_over_25_odds = COALESCE(matches.opening_over_25_odds, EXCLUDED.over_25_odds),
                            opening_under_25_odds = COALESCE(matches.opening_under_25_odds, EXCLUDED.under_25_odds),
                            opening_nba_total_line = COALESCE(matches.opening_nba_total_line, EXCLUDED.nba_total_line),
                            opening_captured_at = COALESCE(
                                matches.opening_captured_at,
                                CASE WHEN EXCLUDED.home_odds IS NOT NULL THEN NOW() ELSE NULL END
                            ),
                            updated_at        = NOW()
                    RETURNING id
                """),
                row_data,
            )
            row = result.fetchone()
            return str(row[0]) if row else None
    except Exception as e:
        log.error("upsert_match_error", error=str(e))
        return None


async def _upsert_prediction(session: AsyncSession, match_id: str, pred: dict):
    try:
        async with session.begin_nested():
            await session.execute(
                text("""
                    INSERT INTO predictions (
                        id, match_id, model_version, prob_home, prob_draw, prob_away,
                        prob_over_25, prob_under_25,
                        prob_ah_home, prob_ah_away,
                        confidence, shap_values, computed_at
                    )
                    VALUES (
                        gen_random_uuid(), :match_id, :model_version,
                        :prob_home, :prob_draw, :prob_away,
                        :prob_over_25, :prob_under_25,
                        :prob_ah_home, :prob_ah_away,
                        :confidence, CAST(:shap_values AS jsonb), NOW()
                    )
                    ON CONFLICT DO NOTHING
                """),
                {
                    "match_id": match_id,
                    "model_version": pred["model_version"],
                    "prob_home": pred["prob_home"],
                    "prob_draw": pred["prob_draw"],
                    "prob_away": pred["prob_away"],
                    "prob_over_25": pred.get("prob_over_25"),
                    "prob_under_25": pred.get("prob_under_25"),
                    "prob_ah_home": pred.get("prob_ah_home"),
                    "prob_ah_away": pred.get("prob_ah_away"),
                    "confidence": pred["confidence"],
                    "shap_values": json.dumps(pred.get("shap_values")),
                },
            )
    except Exception as e:
        log.error("upsert_prediction_error", match_id=match_id, error=str(e))


async def _upsert_odds_from_prediction(session: AsyncSession, match_id: str, pred: dict):
    margin = 1.06
    ph, pd_, pa = pred["prob_home"], pred["prob_draw"], pred["prob_away"]
    if ph <= 0 or pd_ <= 0 or pa <= 0:
        return
    home_odds = round((1 / ph) * margin, 2)
    draw_odds = round((1 / pd_) * margin, 2)
    away_odds = round((1 / pa) * margin, 2)
    try:
        async with session.begin_nested():
            await session.execute(
                text("""
                    UPDATE matches
                    SET home_odds = COALESCE(home_odds, :home_odds),
                        draw_odds = COALESCE(draw_odds, :draw_odds),
                        away_odds = COALESCE(away_odds, :away_odds)
                    WHERE id = :match_id
                      AND home_odds IS NULL
                """),
                {"match_id": match_id, "home_odds": home_odds,
                 "draw_odds": draw_odds, "away_odds": away_odds},
            )
    except Exception as e:
        log.error("upsert_odds_error", match_id=match_id, error=str(e))


async def _build_foot_features(
    match_data: dict,
    session: AsyncSession,
    standings: dict | None = None,
    total_teams: int = 20,
) -> MatchFeatures:
    """Build MatchFeatures (52 fields, Phase 1) using global FOOT_STATE.

    Charge l'état ELO global si périmé, puis calcule les features. Fallback :
    si FOOT_STATE est inutilisable, retourne MatchFeatures() avec defaults.
    """
    import pandas as pd

    home_team = match_data.get("home_team", "")
    away_team = match_data.get("away_team", "")
    match_date_raw = match_data.get("match_date", "")
    league = match_data.get("league", "")

    try:
        if isinstance(match_date_raw, str):
            match_date = pd.Timestamp(match_date_raw.replace("Z", "+00:00")).tz_localize(None)
        else:
            match_date = pd.Timestamp(match_date_raw)
    except Exception:
        return MatchFeatures()

    ok = await FOOT_STATE.ensure_loaded(session)
    if not ok or FOOT_STATE.historical_df is None:
        # Fallback : pas de FOOT_STATE → features par défaut (ELO=1500)
        return MatchFeatures()

    # Si standings non fournis, FOOT_STATE.compute_features_sync les calculera
    # depuis son historical_df interne (sans data leakage, < match_date)
    if standings is not None:
        # Override standings dans la fct sync nécessite paramétrage. Pour rester
        # simple, on délègue à FOOT_STATE.compute_features_sync qui recalcule.
        pass

    return FOOT_STATE.compute_features_sync(home_team, away_team, match_date, league)


async def _generate_prediction(
    model: EdgeAIModel,
    match_data: dict,
    session: AsyncSession,
    standings: dict | None = None,
    total_teams: int = 20,
    dc_model=None,
) -> dict:
    """Génère la prédiction 1X2 pour un match foot.

    Si dc_model (Dixon-Coles) est fourni et que les 2 équipes y sont connues
    (dans la même ligue), on utilise DC (backtest +4pts ROI vs XGBoost).
    Sinon fallback XGB.

    Format dc_model : {'per_league': {league_name: DixonColes}, 'type': 'per_league'}
    """
    home_team = match_data.get("home_team", "")
    away_team = match_data.get("away_team", "")
    league = match_data.get("league", "")

    # Priorité 1 : Dixon-Coles si dispo et équipes connues dans la bonne ligue
    dc_for_league = None
    if dc_model is not None:
        if isinstance(dc_model, dict) and dc_model.get("type") == "per_league":
            # Format per-league : on récupère le DC de la ligue du match
            dc_for_league = dc_model["per_league"].get(league)
        else:
            # Format ancien single DC
            dc_for_league = dc_model

    if (dc_for_league is not None
            and home_team in dc_for_league.attack
            and away_team in dc_for_league.attack):
        try:
            p = dc_for_league.predict(home_team, away_team)
            return {
                "prob_home": round(float(p["prob_home"]), 4),
                "prob_draw": round(float(p["prob_draw"]), 4),
                "prob_away": round(float(p["prob_away"]), 4),
                "confidence": round(float(max(p["prob_home"], p["prob_draw"], p["prob_away"])), 4),
                "shap_values": None,
                "model_version": "dc_" + datetime.now(timezone.utc).strftime("%Y%m%d"),
            }
        except Exception as e:
            log.warning("dc_predict_error_fallback_xgb",
                        home=home_team, away=away_team, league=league, error=str(e))

    # Fallback : XGBoost
    features = await _build_foot_features(match_data, session, standings, total_teams)
    return model.predict(features)


def _predict_ou(ou_bundle: dict, match_data: dict, session: AsyncSession,
                standings: dict | None = None, total_teams: int = 20) -> dict | None:
    """Renvoie {prob_over_25, prob_under_25} pour un match foot. Utilise les mêmes
    features que le modèle 1X2 (le bundle contient un CalibratedClassifierCV)."""
    import pandas as pd
    try:
        cal = ou_bundle.get("model")
        if cal is None:
            return None
        home_team = match_data.get("home_team", "")
        away_team = match_data.get("away_team", "")
        match_date_raw = match_data.get("match_date", "")
        if isinstance(match_date_raw, str):
            match_date = pd.Timestamp(match_date_raw.replace("Z", "+00:00")).tz_localize(None)
        else:
            match_date = pd.Timestamp(match_date_raw)

        # Charge l'historique (synchrone côté DataFrame mais on est async côté DB)
        # On utilise les mêmes features que le modèle 1X2 — il faut donc reproduire
        # ce que fait _generate_prediction. Pour éviter de doubler la requête DB,
        # on utilise asyncio inline.
        import asyncio
        # On est déjà dans un await, donc on appelle un sub-helper
        # qui fait la requête.
        # NOTE: pour simplifier on ne fait que MatchFeatures() par défaut si
        # pas assez d'historique. Sinon on calcule les features.
        # Pour éviter de retaper la DB, on recalcule comme _generate_prediction.
    except Exception:
        pass

    # Reset & redo synchronously: appel séparé pour récupérer rows
    return None


async def _generate_ou_prediction(
    ou_bundle: dict,
    match_data: dict,
    session: AsyncSession,
    standings: dict | None = None,
    total_teams: int = 20,
) -> dict | None:
    """Génère les probas O/U pour un match foot.

    OU utilise les 52 features Phase 1 (sans shots/SOT) — backtest a montré
    que les features Phase 2 (shots) dégradent l'OU (-12pts ROI).
    """
    cal = ou_bundle.get("model")
    if cal is None:
        return None
    features = await _build_foot_features(match_data, session, standings, total_teams)
    # Slice selon le schema attendu par le modèle (52 phase1 ou 67 full)
    try:
        inner = cal.calibrated_classifiers_[0].estimator
        n_expected = int(getattr(inner, "n_features_in_", 52))
    except Exception:
        n_expected = 52
    if n_expected == 52:
        X = features.to_array_phase1().reshape(1, -1)
    else:
        X = features.to_array().reshape(1, -1)
    try:
        proba = cal.predict_proba(X)[0]
        # Le modèle est entraîné avec label=1 → Over
        return {
            "prob_under_25": round(float(proba[0]), 4),
            "prob_over_25": round(float(proba[1]), 4),
        }
    except Exception as e:
        log.error("ou_predict_error", error=str(e))
        return None


async def _generate_ah_prediction(
    ah_bundle: dict,
    match_data: dict,
    session: AsyncSession,
    standings: dict | None = None,
    total_teams: int = 20,
) -> dict | None:
    """Génère les probas Asian Handicap (P(home covers) / P(away covers)).

    AH utilise les 67 features complètes (Phase 1 + Phase 2 shots/SOT) —
    backtest a montré que les shots boostent l'AH de +2.1pts ROI (+2.79% → +4.87%).
    Fallback : si modèle ancien (52 ou 36), slice approprié.
    """
    cal = ah_bundle.get("model")
    if cal is None:
        return None
    features = await _build_foot_features(match_data, session, standings, total_teams)
    try:
        inner = cal.calibrated_classifiers_[0].estimator
        n_expected = int(getattr(inner, "n_features_in_", 67))
    except Exception:
        n_expected = 67
    if n_expected == 52:
        X = features.to_array_phase1().reshape(1, -1)
    else:
        X = features.to_array().reshape(1, -1)
    try:
        proba = cal.predict_proba(X)[0]
        # Le modèle est entraîné avec label=1 → Home couvre
        return {
            "prob_ah_away": round(float(proba[0]), 4),
            "prob_ah_home": round(float(proba[1]), 4),
        }
    except Exception as e:
        log.error("ah_predict_error", error=str(e))
        return None


def _load_active_model() -> EdgeAIModel | None:
    """Charge le modèle FOOTBALL 1X2 le plus récent.

    Stratégie :
    1. Privilégie model_latest.joblib (pointeur canonique mis à jour par auto-retrain)
    2. Fallback : glob model_*.joblib en excluant nba/ou/ah/perleague/wc/tennis
    """
    canonical = MODEL_DIR / "model_latest.joblib"
    if canonical.exists():
        try:
            return EdgeAIModel.load(canonical)
        except Exception as e:
            log.error("model_load_error_canonical", error=str(e))
            # Si canonical foire, on continue vers le fallback

    all_files = sorted(MODEL_DIR.glob("model_*.joblib"), reverse=True)
    EXCLUDE = ("nba", "ou", "ah", "perleague", "wc", "tennis")
    model_files = [
        f for f in all_files
        if not any(token in f.name.lower() for token in EXCLUDE)
    ]
    if not model_files:
        return None
    try:
        return EdgeAIModel.load(model_files[0])
    except Exception as e:
        log.error("model_load_error", error=str(e))
        return None


# Cache des modèles per-league chargés
_PER_LEAGUE_CACHE: dict[str, EdgeAIModel] = {}


def _load_per_league_model(league: str) -> EdgeAIModel | None:
    """Charge le modèle dédié pour une ligue. Renvoie None si absent ou si le
    schema de features ne correspond pas à MatchFeatures actuel (modèle obsolète)."""
    if league in _PER_LEAGUE_CACHE:
        return _PER_LEAGUE_CACHE[league]
    slug = league.replace(" ", "_").lower()
    path = MODEL_DIR / f"model_perleague_{slug}_latest.joblib"
    if not path.exists():
        return None
    try:
        bundle = joblib.load(path)
        cal = bundle["model"]
        # Vérifie que le nombre de features attendu matche le schema actuel
        expected_n = len(MatchFeatures.feature_names())
        try:
            inner = cal.calibrated_classifiers_[0].estimator
            model_n = int(getattr(inner, "n_features_in_", expected_n))
        except Exception:
            model_n = expected_n
        if model_n != expected_n:
            log.warning("per_league_model_schema_mismatch_skipped",
                        league=league, model_features=model_n,
                        expected_features=expected_n,
                        action="falling_back_to_global_model")
            _PER_LEAGUE_CACHE[league] = None
            return None
        model = EdgeAIModel(version=bundle.get("version", f"perleague_{slug}"))
        model.model = cal
        _PER_LEAGUE_CACHE[league] = model
        return model
    except Exception as e:
        log.error("per_league_model_load_error", league=league, error=str(e))
        return None


async def main():
    """Boucle principale : toutes les 6h."""
    while True:
        try:
            await run_pipeline()
        except Exception as e:
            log.error("pipeline_error", error=str(e))
        await asyncio.sleep(6 * 3600)


if __name__ == "__main__":
    asyncio.run(main())
