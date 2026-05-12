"""
Script de seed pour tester le flow complet sans pipeline ML.
Usage: docker exec -it edgeai-backend-1 python seed.py
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from app.db.session import AsyncSessionLocal
from app.db.models import Match, Prediction


MATCHES = [
    {
        "external_id": "seed_001",
        "league": "Premier League",
        "season": "2024-25",
        "home_team": "Arsenal",
        "away_team": "Manchester City",
        "match_date": datetime.now(timezone.utc) + timedelta(hours=24),
        "status": "SCHEDULED",
        "home_odds": 2.80,
        "draw_odds": 3.40,
        "away_odds": 2.50,
        "venue": "Emirates Stadium",
        "prediction": {"prob_home": 0.38, "prob_draw": 0.28, "prob_away": 0.34, "confidence": 0.71},
    },
    {
        "external_id": "seed_002",
        "league": "La Liga",
        "season": "2024-25",
        "home_team": "Real Madrid",
        "away_team": "Atlético Madrid",
        "match_date": datetime.now(timezone.utc) + timedelta(hours=30),
        "status": "SCHEDULED",
        "home_odds": 2.10,
        "draw_odds": 3.50,
        "away_odds": 3.60,
        "venue": "Santiago Bernabéu",
        "prediction": {"prob_home": 0.51, "prob_draw": 0.26, "prob_away": 0.23, "confidence": 0.78},
    },
    {
        "external_id": "seed_003",
        "league": "Ligue 1",
        "season": "2024-25",
        "home_team": "PSG",
        "away_team": "Olympique de Marseille",
        "match_date": datetime.now(timezone.utc) + timedelta(hours=40),
        "status": "SCHEDULED",
        "home_odds": 1.70,
        "draw_odds": 3.80,
        "away_odds": 5.20,
        "venue": "Parc des Princes",
        "prediction": {"prob_home": 0.62, "prob_draw": 0.22, "prob_away": 0.16, "confidence": 0.83},
    },
]


async def seed():
    async with AsyncSessionLocal() as db:
        for data in MATCHES:
            # Skip if already seeded
            from sqlalchemy import select
            existing = await db.execute(
                select(Match).where(Match.external_id == data["external_id"])
            )
            if existing.scalar_one_or_none():
                print(f"  → {data['home_team']} vs {data['away_team']} déjà en base, ignoré")
                continue

            pred_data = data.pop("prediction")
            match = Match(id=str(uuid.uuid4()), **data)
            db.add(match)
            await db.flush()

            prediction = Prediction(
                id=str(uuid.uuid4()),
                match_id=match.id,
                prob_home=pred_data["prob_home"],
                prob_draw=pred_data["prob_draw"],
                prob_away=pred_data["prob_away"],
                confidence=pred_data["confidence"],
                model_version="seed_v1",
                computed_at=datetime.now(timezone.utc),
            )
            db.add(prediction)
            print(f"  ✓ {match.home_team} vs {match.away_team} ({match.league})")

        await db.commit()
        print("\nSeed terminé — 3 matchs disponibles sur /matches/upcoming")


if __name__ == "__main__":
    asyncio.run(seed())
