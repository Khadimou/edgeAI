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

    # Fit UN DC PAR LIGUE : équipes d'une ligue jouent uniquement entre elles,
    # donc un global pool est biaisé. Per-league : ~20-30 teams × 3-5k matchs,
    # convergence rapide (<10s par ligue), attack ratings plus précis.
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    version = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    import shutil

    leagues = sorted(df["league"].dropna().unique())
    print(f"\n=== Fitting DC per league ({len(leagues)} ligues) ===\n")
    per_league_models = {}

    for league in leagues:
        sub = df[df["league"] == league].copy()
        if len(sub) < 200:
            print(f"  Skip {league}: only {len(sub)} matches")
            continue
        print(f"\n--- {league} ({len(sub)} matchs) ---")
        dc = DixonColes()
        dc.fit(sub, decay_half_life=180, verbose=True)
        per_league_models[league] = dc

        # Sanity check : top 5 attack
        top5 = sorted(dc.attack.items(), key=lambda x: -x[1])[:5]
        print(f"  Top 5 attack:")
        for team, score in top5:
            print(f"    {team:35} {score:+.3f}")

    # Save : bundle dict {league: DixonColes} dans un seul joblib
    import joblib
    path = MODEL_DIR / f"model_dc_{version}.joblib"
    latest = MODEL_DIR / "model_dc_latest.joblib"
    bundle = {
        "per_league": {
            league: {
                "attack": dc.attack, "defense": dc.defense,
                "home_adv": dc.home_adv, "rho": dc.rho,
                "teams": dc.teams, "_fitted": dc._fitted,
            } for league, dc in per_league_models.items()
        },
        "version": version,
        "type": "per_league",
    }
    joblib.dump(bundle, path)
    shutil.copy2(path, latest)
    log.info("dc_train_saved_per_league",
             version=version, path=str(path),
             n_leagues=len(per_league_models),
             leagues={l: {"n_teams": len(m.teams), "home_adv": round(m.home_adv, 3),
                          "rho": round(m.rho, 3)}
                      for l, m in per_league_models.items()})

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
