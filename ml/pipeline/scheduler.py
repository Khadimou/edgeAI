"""
Scheduler ML : orchestration du pipeline toutes les 6h.
Ingestion → features → prédictions → écriture en base + Redis.
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

from .ingestion import FootballDataClient, OddsAPIClient, normalize_match, SUPPORTED_LEAGUES
from .features import compute_features_from_history, MatchFeatures
from .model import EdgeAIModel

log = structlog.get_logger()

DB_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://edgeai:edgeai_secret@localhost:5432/edgeai")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
FOOTBALL_API_KEY = os.getenv("FOOTBALL_DATA_API_KEY", "")
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
MODEL_DIR = Path(os.getenv("MODEL_DIR", "/artifacts/models"))
PREDICTION_TTL = 6 * 3600  # 6h


async def run_pipeline():
    log.info("pipeline_start", timestamp=datetime.now(timezone.utc).isoformat())

    engine = create_async_engine(DB_URL.replace("postgresql://", "postgresql+asyncpg://"))
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    redis = await aioredis.from_url(REDIS_URL, decode_responses=True)

    # Charger le modèle actif
    model = _load_active_model()
    if model is None:
        log.warning("no_model_available", msg="Pas de modèle déployé — prédictions désactivées")

    football_client = FootballDataClient(FOOTBALL_API_KEY)
    odds_client = OddsAPIClient(ODDS_API_KEY)

    try:
        async with async_session() as session:
            for code, league_name in SUPPORTED_LEAGUES.items():
                await _process_league(
                    code, league_name, session, redis, football_client, odds_client, model
                )
        await session.commit()
    finally:
        await football_client.close()
        await odds_client.close()
        await redis.aclose()
        await engine.dispose()

    log.info("pipeline_done", timestamp=datetime.now(timezone.utc).isoformat())


async def _process_league(code, league_name, session, redis, football_client, odds_client, model):
    raw_matches = await football_client.get_upcoming_matches(code, days=3)
    log.info("matches_fetched", league=code, count=len(raw_matches))

    for raw in raw_matches:
        normalized = normalize_match(raw, league_name)
        match_id = await _upsert_match(session, normalized)

        if model and match_id:
            prediction = _generate_prediction(model, normalized)
            await _upsert_prediction(session, match_id, prediction)

            cache_key = f"prediction:{match_id}"
            await redis.setex(cache_key, PREDICTION_TTL, json.dumps(prediction))


async def _upsert_match(session: AsyncSession, data: dict) -> str | None:
    try:
        result = await session.execute(
            text("""
                INSERT INTO matches (id, external_id, league, season, home_team, away_team,
                                     match_date, status, home_score, away_score,
                                     created_at, updated_at)
                VALUES (gen_random_uuid(), :external_id, :league, :season, :home_team, :away_team,
                        :match_date::timestamptz, :status, :home_score, :away_score, NOW(), NOW())
                ON CONFLICT (external_id) DO UPDATE
                    SET status = EXCLUDED.status,
                        home_score = EXCLUDED.home_score,
                        away_score = EXCLUDED.away_score,
                        updated_at = NOW()
                RETURNING id
            """),
            data,
        )
        row = result.fetchone()
        return str(row[0]) if row else None
    except Exception as e:
        log.error("upsert_match_error", error=str(e))
        return None


async def _upsert_prediction(session: AsyncSession, match_id: str, pred: dict):
    try:
        await session.execute(
            text("""
                INSERT INTO predictions (id, match_id, model_version, prob_home, prob_draw, prob_away,
                                          confidence, shap_values, computed_at)
                VALUES (gen_random_uuid(), :match_id, :model_version,
                        :prob_home, :prob_draw, :prob_away,
                        :confidence, :shap_values::jsonb, NOW())
                ON CONFLICT DO NOTHING
            """),
            {
                "match_id": match_id,
                "model_version": pred["model_version"],
                "prob_home": pred["prob_home"],
                "prob_draw": pred["prob_draw"],
                "prob_away": pred["prob_away"],
                "confidence": pred["confidence"],
                "shap_values": json.dumps(pred.get("shap_values")),
            },
        )
    except Exception as e:
        log.error("upsert_prediction_error", match_id=match_id, error=str(e))


def _generate_prediction(model: EdgeAIModel, match_data: dict) -> dict:
    # Sans données historiques en temps réel → features par défaut
    # En production : récupérer l'historique depuis la base
    features = MatchFeatures()
    features.is_home_advantage = 1.0
    return model.predict(features)


def _load_active_model() -> EdgeAIModel | None:
    model_files = sorted(MODEL_DIR.glob("model_*.joblib"), reverse=True)
    if not model_files:
        return None
    try:
        return EdgeAIModel.load(model_files[0])
    except Exception as e:
        log.error("model_load_error", error=str(e))
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
