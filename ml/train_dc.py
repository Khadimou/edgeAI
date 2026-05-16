"""
Entraîne Dixon-Coles sur tous les matchs FINISHED de la DB prod et sauvegarde.

Usage :
    docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm ml_worker python train_dc.py

Sortie : artifacts/models/model_dc_latest.joblib
"""
import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).parent))
from dixon_coles import DixonColes

log = structlog.get_logger()

MODEL_DIR = Path(os.getenv("MODEL_DIR", "/app/artifacts/models"))


def build_url(raw: str) -> str:
    url = raw.split("?")[0]
    url = url.replace("postgresql://", "postgresql+asyncpg://")
    url = url.replace("postgres://", "postgresql+asyncpg://")
    return url


async def main():
    raw = os.environ.get("DATABASE_URL")
    if not raw:
        log.error("missing_database_url")
        sys.exit(1)
    db_url = build_url(raw)
    connect_args = {"ssl": True} if "sslmode=require" in raw else {}
    engine = create_async_engine(db_url, connect_args=connect_args, pool_pre_ping=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    log.info("dc_train_start")
    async with Session() as session:
        result = await session.execute(text("""
            SELECT home_team, away_team, home_score, away_score, match_date, league
            FROM matches
            WHERE status = 'FINISHED'
              AND home_score IS NOT NULL
              AND away_score IS NOT NULL
              AND UPPER(sport) = 'FOOTBALL'
            ORDER BY match_date
        """))
        rows = result.fetchall()

    if not rows:
        log.error("dc_train_no_data")
        return
    df = pd.DataFrame(rows, columns=["home_team", "away_team",
                                     "home_score", "away_score",
                                     "match_date", "league"])
    df["match_date"] = pd.to_datetime(df["match_date"])
    log.info("dc_train_data_loaded", n_matches=len(df),
             leagues=df["league"].value_counts().to_dict())

    # Fit un seul DC global (toutes ligues confondues)
    dc = DixonColes()
    dc.fit(df, decay_half_life=180, verbose=True)

    # Save
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    version = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    path = MODEL_DIR / f"model_dc_{version}.joblib"
    latest = MODEL_DIR / "model_dc_latest.joblib"
    dc.save(path)
    import shutil
    shutil.copy2(path, latest)
    log.info("dc_train_saved", version=version, path=str(path),
             n_teams=len(dc.teams), home_adv=dc.home_adv, rho=dc.rho)

    # Sanity check : top teams
    top10_attack = sorted(dc.attack.items(), key=lambda x: -x[1])[:10]
    print("\nTop 10 attack ratings :")
    for team, score in top10_attack:
        print(f"  {team:35} {score:+.3f}")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
