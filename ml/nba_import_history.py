"""
Importe les matchs NBA historiques (data/raw/nba_matches.csv) dans la table matches.
À lancer une seule fois après la première collecte.
"""
import asyncio
import os
import sys
from pathlib import Path

import pandas as pd
import asyncpg
from dotenv import load_dotenv

load_dotenv()

CSV = Path(__file__).parent / "data" / "raw" / "nba_matches.csv"
DATABASE_URL = os.getenv("DATABASE_URL", "")


async def main():
    if not CSV.exists():
        print(f"ERREUR : {CSV} introuvable")
        sys.exit(1)
    df = pd.read_csv(CSV, parse_dates=["match_date"])
    print(f"Lecture : {len(df)} matchs NBA")

    url = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://").split("?")[0]
    ssl = "require" if "sslmode=require" in DATABASE_URL else None
    conn = await asyncpg.connect(url, ssl=ssl)

    inserted = 0
    skipped = 0
    for _, row in df.iterrows():
        try:
            await conn.execute("""
                INSERT INTO matches (
                    id, external_id, sport, league, season, home_team, away_team,
                    match_date, status, home_score, away_score,
                    created_at, updated_at
                )
                VALUES (
                    gen_random_uuid()::text, $1, 'NBA', 'NBA', $2, $3, $4,
                    $5, 'FINISHED', $6, $7, NOW(), NOW()
                )
                ON CONFLICT (external_id) DO UPDATE
                    SET home_score = EXCLUDED.home_score,
                        away_score = EXCLUDED.away_score,
                        status = 'FINISHED',
                        updated_at = NOW()
            """,
                row["external_id"], str(row["season"]),
                row["home_team"], row["away_team"],
                row["match_date"].to_pydatetime().replace(tzinfo=None),
                int(row["home_score"]), int(row["away_score"]),
            )
            inserted += 1
        except Exception as e:
            print(f"  ⚠ {row['external_id']}: {e}")
            skipped += 1
        if inserted % 500 == 0 and inserted > 0:
            print(f"  {inserted} importés...")

    await conn.close()
    print(f"\n✓ {inserted} matchs importés (NBA)")
    if skipped:
        print(f"  {skipped} ignorés")


if __name__ == "__main__":
    asyncio.run(main())
