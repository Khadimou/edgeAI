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
from .model import EdgeAIModel
from .settle import settle_finished_bets
from .drift import check_drift_and_rollback
from .trainer import maybe_auto_retrain
from .notifications import notify_new_value_bets
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


async def _ingest_nba(session, redis, nba_model=None) -> int:
    """
    Fetch NBA upcoming + scores via the-odds-api (1×/jour pour économiser les credits).
    Si nba_model est fourni, génère aussi les prédictions sur les matchs upcoming.
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

                # Update via team names + match_date (~même jour)
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
                            WHERE sport = 'FOOTBALL'
                              AND status = 'SCHEDULED'
                              AND (
                                home_team = :home OR
                                home_team ILIKE '%' || :home || '%' OR
                                :home ILIKE '%' || home_team || '%'
                              )
                              AND (
                                away_team = :away OR
                                away_team ILIKE '%' || :away || '%' OR
                                :away ILIKE '%' || away_team || '%'
                              )
                              AND ABS(EXTRACT(EPOCH FROM (match_date - :dt))) < 7200
                            RETURNING id
                        """), {
                            "h": h_odds, "d": d_odds, "a": a_odds,
                            "o": o_odds, "u": u_odds,
                            "ahl": ah_line, "ahh": ah_h_odds, "aha": ah_a_odds,
                            "home": home, "away": away, "dt": dt,
                        })
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

    ou_model = _load_ou_model()
    if ou_model is None:
        log.info("no_ou_model_available")

    ah_model = _load_ah_model()
    if ah_model is None:
        log.info("no_ah_model_available")

    football_client = FootballDataClient(FOOTBALL_API_KEY)
    odds_client = OddsAPIClient(ODDS_API_KEY)

    try:
        async with async_session() as session:
            for code, league_name in SUPPORTED_LEAGUES.items():
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
                    ou_model=ou_model, ah_model=ah_model,
                )
                await asyncio.sleep(7)  # gap entre ligues

            # Fetch cotes foot (h2h + O/U 2.5) 1×/jour via the-odds-api
            foot_odds_count = await _ingest_foot_odds(session, redis)
            if foot_odds_count:
                log.info("foot_odds_pipeline_done", updated=foot_odds_count)

            # Ingestion NBA via the-odds-api (1×/jour, lock Redis)
            nba_count = await _ingest_nba(session, redis, nba_model=nba_model)
            if nba_count:
                log.info("nba_pipeline_done", matches=nba_count)

            # Settlement automatique des paris sur matchs terminés
            settled = await settle_finished_bets(session)
            if settled:
                log.info("auto_settlement_done", bets_settled=settled)

            # Détection de dérive + rollback si modèle dégradé
            drift_report = await check_drift_and_rollback(session)
            log.info("drift_check", **{k: v for k, v in drift_report.items() if v is not None})

            # Réentraînement automatique quotidien
            await maybe_auto_retrain(session)

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
                    value_bet_ou_leagues=_os.getenv("VALUE_BET_OU_LEAGUES", "Premier League").split(","),
                    value_bet_ah_leagues=_os.getenv("VALUE_BET_AH_LEAGUES", "Ligue 1,Premier League,Serie A").split(","),
                )
            async with async_session() as notif_session:
                try:
                    n_notified = await notify_new_value_bets(notif_session, redis, backend_settings)
                    if n_notified:
                        log.info("notifications_done", count=n_notified)
                except Exception as e:
                    log.error("notifications_error", error=str(e))
    finally:
        await football_client.close()
        await odds_client.close()
        await redis.aclose()
        await engine.dispose()

    log.info("pipeline_done", timestamp=datetime.now(timezone.utc).isoformat())


async def _process_league(
    code, league_name, session, redis,
    football_client, odds_client, model,
    standings, total_teams,
    ou_model=None, ah_model=None,
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
                effective_model, normalized, session, standings, total_teams
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
            await _upsert_odds_from_prediction(session, match_id, prediction)

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
                        opening_home_odds, opening_draw_odds, opening_away_odds,
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
                        CAST(:home_odds AS DOUBLE PRECISION),
                        CAST(:draw_odds AS DOUBLE PRECISION),
                        CAST(:away_odds AS DOUBLE PRECISION),
                        CASE WHEN CAST(:home_odds AS DOUBLE PRECISION) IS NOT NULL THEN NOW() ELSE NULL END,
                        NOW(), NOW()
                    )
                    ON CONFLICT (external_id) DO UPDATE
                        SET status            = EXCLUDED.status,
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
                            -- Opening : seulement si pas déjà fixé (jamais modifié)
                            opening_home_odds = COALESCE(matches.opening_home_odds, EXCLUDED.home_odds),
                            opening_draw_odds = COALESCE(matches.opening_draw_odds, EXCLUDED.draw_odds),
                            opening_away_odds = COALESCE(matches.opening_away_odds, EXCLUDED.away_odds),
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


async def _generate_prediction(
    model: EdgeAIModel,
    match_data: dict,
    session: AsyncSession,
    standings: dict | None = None,
    total_teams: int = 20,
) -> dict:
    import pandas as pd

    home_team = match_data.get("home_team", "")
    away_team = match_data.get("away_team", "")
    match_date_raw = match_data.get("match_date", "")

    try:
        if isinstance(match_date_raw, str):
            match_date = pd.Timestamp(match_date_raw.replace("Z", "+00:00")).tz_localize(None)
        else:
            match_date = pd.Timestamp(match_date_raw)

        result = await session.execute(
            text("""
                SELECT home_team, away_team, home_score, away_score, match_date, league,
                       ht_home_score, ht_away_score,
                       COALESCE(home_yellow_cards, 0), COALESCE(away_yellow_cards, 0)
                FROM matches
                WHERE status = 'FINISHED'
                  AND (home_team = :home OR away_team = :home
                       OR home_team = :away OR away_team = :away)
                ORDER BY match_date DESC
                LIMIT 40
            """),
            {"home": home_team, "away": away_team},
        )
        rows = result.fetchall()
    except Exception:
        rows = []

    if len(rows) >= 3:
        df = pd.DataFrame(rows, columns=[
            "home_team", "away_team", "home_score", "away_score", "date", "league",
            "ht_home_score", "ht_away_score", "home_yellow_cards", "away_yellow_cards",
        ])
        df["date"] = pd.to_datetime(df["date"])
        df["home_score"] = pd.to_numeric(df["home_score"], errors="coerce").fillna(0)
        df["away_score"] = pd.to_numeric(df["away_score"], errors="coerce").fillna(0)
        features = compute_features_from_history(
            home_team, away_team, match_date, df,
            standings=standings, total_teams=total_teams,
        )
    else:
        features = MatchFeatures()

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
    """Génère les probas O/U pour un match foot."""
    import pandas as pd
    cal = ou_bundle.get("model")
    if cal is None:
        return None

    home_team = match_data.get("home_team", "")
    away_team = match_data.get("away_team", "")
    match_date_raw = match_data.get("match_date", "")
    try:
        if isinstance(match_date_raw, str):
            match_date = pd.Timestamp(match_date_raw.replace("Z", "+00:00")).tz_localize(None)
        else:
            match_date = pd.Timestamp(match_date_raw)

        result = await session.execute(text("""
            SELECT home_team, away_team, home_score, away_score, match_date, league,
                   ht_home_score, ht_away_score,
                   COALESCE(home_yellow_cards, 0), COALESCE(away_yellow_cards, 0)
            FROM matches
            WHERE status = 'FINISHED' AND sport = 'FOOTBALL'
              AND (home_team = :home OR away_team = :home
                   OR home_team = :away OR away_team = :away)
            ORDER BY match_date DESC LIMIT 40
        """), {"home": home_team, "away": away_team})
        rows = result.fetchall()
    except Exception:
        rows = []

    if len(rows) >= 3:
        df = pd.DataFrame(rows, columns=[
            "home_team", "away_team", "home_score", "away_score", "date", "league",
            "ht_home_score", "ht_away_score", "home_yellow_cards", "away_yellow_cards",
        ])
        df["date"] = pd.to_datetime(df["date"])
        df["home_score"] = pd.to_numeric(df["home_score"], errors="coerce").fillna(0)
        df["away_score"] = pd.to_numeric(df["away_score"], errors="coerce").fillna(0)
        features = compute_features_from_history(
            home_team, away_team, match_date, df,
            standings=standings, total_teams=total_teams,
        )
    else:
        features = MatchFeatures()

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
    """Génère les probas Asian Handicap (P(home covers) / P(away covers))."""
    import pandas as pd
    cal = ah_bundle.get("model")
    if cal is None:
        return None

    home_team = match_data.get("home_team", "")
    away_team = match_data.get("away_team", "")
    match_date_raw = match_data.get("match_date", "")
    try:
        if isinstance(match_date_raw, str):
            match_date = pd.Timestamp(match_date_raw.replace("Z", "+00:00")).tz_localize(None)
        else:
            match_date = pd.Timestamp(match_date_raw)

        result = await session.execute(text("""
            SELECT home_team, away_team, home_score, away_score, match_date, league,
                   ht_home_score, ht_away_score,
                   COALESCE(home_yellow_cards, 0), COALESCE(away_yellow_cards, 0)
            FROM matches
            WHERE status = 'FINISHED' AND sport = 'FOOTBALL'
              AND (home_team = :home OR away_team = :home
                   OR home_team = :away OR away_team = :away)
            ORDER BY match_date DESC LIMIT 40
        """), {"home": home_team, "away": away_team})
        rows = result.fetchall()
    except Exception:
        rows = []

    if len(rows) >= 3:
        df = pd.DataFrame(rows, columns=[
            "home_team", "away_team", "home_score", "away_score", "date", "league",
            "ht_home_score", "ht_away_score", "home_yellow_cards", "away_yellow_cards",
        ])
        df["date"] = pd.to_datetime(df["date"])
        df["home_score"] = pd.to_numeric(df["home_score"], errors="coerce").fillna(0)
        df["away_score"] = pd.to_numeric(df["away_score"], errors="coerce").fillna(0)
        features = compute_features_from_history(
            home_team, away_team, match_date, df,
            standings=standings, total_teams=total_teams,
        )
    else:
        features = MatchFeatures()

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
    """Charge le modèle FOOTBALL le plus récent (exclut les modèles NBA + O/U + per-league)."""
    all_files = sorted(MODEL_DIR.glob("model_*.joblib"), reverse=True)
    model_files = [
        f for f in all_files
        if "nba" not in f.name.lower()
        and "ou" not in f.name.lower()
        and "ah" not in f.name.lower()
        and "perleague" not in f.name.lower()
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
    """Charge le modèle dédié pour une ligue. Renvoie None si absent."""
    if league in _PER_LEAGUE_CACHE:
        return _PER_LEAGUE_CACHE[league]
    slug = league.replace(" ", "_").lower()
    path = MODEL_DIR / f"model_perleague_{slug}_latest.joblib"
    if not path.exists():
        return None
    try:
        bundle = joblib.load(path)
        model = EdgeAIModel(version=bundle.get("version", f"perleague_{slug}"))
        model.model = bundle["model"]
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
