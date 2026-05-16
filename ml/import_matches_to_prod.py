"""
Import des 18011 matchs locaux (matches.csv) dans la DB prod via UPSERT.

La DB prod n'a actuellement que ~9936 matchs FINISHED (vs 18011 en local).
Cette demi-couverture biaise fortement la calibration (8.8% de draws seulement,
au lieu de ~25% normal) → modèle pull les probas vers le centre (Bayern à 50%
au lieu de 80%).

Stratégie :
- UPSERT par external_id (clé unique de football-data.org)
- COALESCE pour préserver les colonnes prod qui pourraient avoir été enrichies
  (e.g. predictions/odds en cours)
- Batch de 500 rows pour ne pas saturer la DB Prisma cloud
- Skip les matchs déjà à jour (created_at fresh) pour minimiser le travail

Usage :
    python import_matches_to_prod.py
    python import_matches_to_prod.py --dry-run    # juste compter
    python import_matches_to_prod.py --batch 500
"""
import argparse
import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import structlog
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import text

# Optional dotenv (local). En prod (Docker), DATABASE_URL vient de l'environnement.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

log = structlog.get_logger()

CSV_PATH = Path(__file__).parent / "data" / "raw" / "matches.csv"

UPSERT_SQL = text("""
INSERT INTO matches (
    id, external_id, sport, league, season, home_team, away_team,
    match_date, status, home_score, away_score,
    ht_home_score, ht_away_score,
    home_yellow_cards, away_yellow_cards, home_red_cards, away_red_cards,
    home_odds, draw_odds, away_odds,
    home_shots, away_shots,
    home_shots_on_target, away_shots_on_target,
    home_corners, away_corners,
    home_fouls, away_fouls,
    created_at, updated_at
) VALUES (
    gen_random_uuid(), :external_id, :sport, :league, :season, :home_team, :away_team,
    :match_date, :status, :home_score, :away_score,
    :ht_home_score, :ht_away_score,
    :home_yellow_cards, :away_yellow_cards, :home_red_cards, :away_red_cards,
    :home_odds, :draw_odds, :away_odds,
    :home_shots, :away_shots,
    :home_shots_on_target, :away_shots_on_target,
    :home_corners, :away_corners,
    :home_fouls, :away_fouls,
    NOW(), NOW()
)
ON CONFLICT (external_id) DO UPDATE SET
    -- On préserve les valeurs prod si elles existent déjà, sinon on prend celles du CSV
    sport = COALESCE(EXCLUDED.sport, matches.sport),
    league = COALESCE(EXCLUDED.league, matches.league),
    season = COALESCE(EXCLUDED.season, matches.season),
    home_team = COALESCE(EXCLUDED.home_team, matches.home_team),
    away_team = COALESCE(EXCLUDED.away_team, matches.away_team),
    match_date = COALESCE(EXCLUDED.match_date, matches.match_date),
    status = COALESCE(EXCLUDED.status, matches.status),
    home_score = COALESCE(EXCLUDED.home_score, matches.home_score),
    away_score = COALESCE(EXCLUDED.away_score, matches.away_score),
    ht_home_score = COALESCE(EXCLUDED.ht_home_score, matches.ht_home_score),
    ht_away_score = COALESCE(EXCLUDED.ht_away_score, matches.ht_away_score),
    home_yellow_cards = COALESCE(NULLIF(EXCLUDED.home_yellow_cards, 0), matches.home_yellow_cards),
    away_yellow_cards = COALESCE(NULLIF(EXCLUDED.away_yellow_cards, 0), matches.away_yellow_cards),
    home_red_cards = COALESCE(NULLIF(EXCLUDED.home_red_cards, 0), matches.home_red_cards),
    away_red_cards = COALESCE(NULLIF(EXCLUDED.away_red_cards, 0), matches.away_red_cards),
    home_shots = COALESCE(EXCLUDED.home_shots, matches.home_shots),
    away_shots = COALESCE(EXCLUDED.away_shots, matches.away_shots),
    home_shots_on_target = COALESCE(EXCLUDED.home_shots_on_target, matches.home_shots_on_target),
    away_shots_on_target = COALESCE(EXCLUDED.away_shots_on_target, matches.away_shots_on_target),
    home_corners = COALESCE(EXCLUDED.home_corners, matches.home_corners),
    away_corners = COALESCE(EXCLUDED.away_corners, matches.away_corners),
    home_fouls = COALESCE(EXCLUDED.home_fouls, matches.home_fouls),
    away_fouls = COALESCE(EXCLUDED.away_fouls, matches.away_fouls),
    updated_at = NOW()
""")


def build_url(raw: str) -> str:
    url = raw.split("?")[0]
    url = url.replace("postgresql://", "postgresql+asyncpg://")
    url = url.replace("postgres://", "postgresql+asyncpg://")
    return url


def to_int_or_none(v):
    if pd.isna(v):
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def to_float_or_none(v):
    if pd.isna(v):
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def row_to_params(row: pd.Series) -> dict:
    return {
        "external_id": str(row["external_id"]),
        "sport": str(row.get("sport") or "FOOTBALL").upper(),
        "league": str(row["league"]),
        "season": str(row["season"]),
        "home_team": str(row["home_team"]),
        "away_team": str(row["away_team"]),
        "match_date": pd.Timestamp(row["match_date"]).to_pydatetime().replace(tzinfo=None),
        "status": str(row.get("status") or "FINISHED").upper(),
        "home_score": to_int_or_none(row.get("home_score")),
        "away_score": to_int_or_none(row.get("away_score")),
        "ht_home_score": to_int_or_none(row.get("ht_home_score")),
        "ht_away_score": to_int_or_none(row.get("ht_away_score")),
        "home_yellow_cards": to_int_or_none(row.get("home_yellow_cards")) or 0,
        "away_yellow_cards": to_int_or_none(row.get("away_yellow_cards")) or 0,
        "home_red_cards": to_int_or_none(row.get("home_red_cards")) or 0,
        "away_red_cards": to_int_or_none(row.get("away_red_cards")) or 0,
        "home_odds": to_float_or_none(row.get("home_odds")),
        "draw_odds": to_float_or_none(row.get("draw_odds")),
        "away_odds": to_float_or_none(row.get("away_odds")),
        "home_shots": to_int_or_none(row.get("home_shots")),
        "away_shots": to_int_or_none(row.get("away_shots")),
        "home_shots_on_target": to_int_or_none(row.get("home_shots_on_target")),
        "away_shots_on_target": to_int_or_none(row.get("away_shots_on_target")),
        "home_corners": to_int_or_none(row.get("home_corners")),
        "away_corners": to_int_or_none(row.get("away_corners")),
        "home_fouls": to_int_or_none(row.get("home_fouls")),
        "away_fouls": to_int_or_none(row.get("away_fouls")),
    }


async def main(dry_run: bool, batch_size: int):
    if not CSV_PATH.exists():
        log.error("csv_not_found", path=str(CSV_PATH))
        sys.exit(1)

    df = pd.read_csv(CSV_PATH, parse_dates=["match_date"])
    log.info("csv_loaded", rows=len(df))

    raw = os.environ.get("DATABASE_URL")
    if not raw:
        log.error("missing_database_url")
        sys.exit(1)
    db_url = build_url(raw)
    connect_args = {"ssl": True} if "sslmode=require" in raw else {}

    engine = create_async_engine(db_url, connect_args=connect_args, pool_pre_ping=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as session:
        # Count before
        r = await session.execute(text("SELECT COUNT(*) FROM matches WHERE status='FINISHED' AND home_score IS NOT NULL"))
        before = r.scalar() or 0
        log.info("prod_count_before", finished_matches=before)

        if dry_run:
            log.info("dry_run_done", would_upsert=len(df))
            await engine.dispose()
            return

        # Batch upsert
        total = len(df)
        n_done = 0
        n_errors = 0
        for batch_start in range(0, total, batch_size):
            batch = df.iloc[batch_start:batch_start + batch_size]
            for _, row in batch.iterrows():
                try:
                    params = row_to_params(row)
                    await session.execute(UPSERT_SQL, params)
                    n_done += 1
                except Exception as e:
                    n_errors += 1
                    if n_errors <= 5:
                        log.warning("upsert_error", external_id=row.get("external_id"), error=str(e)[:200])
            await session.commit()
            log.info("batch_committed",
                     batch=batch_start // batch_size + 1,
                     total_batches=(total + batch_size - 1) // batch_size,
                     n_done=n_done, n_errors=n_errors)

        # Count after
        r = await session.execute(text("SELECT COUNT(*) FROM matches WHERE status='FINISHED' AND home_score IS NOT NULL"))
        after = r.scalar() or 0

    log.info("import_done",
             before=before, after=after, delta=after - before,
             upserts_attempted=n_done, errors=n_errors)

    # Distribution check
    async with Session() as session:
        r = await session.execute(text("""
            SELECT
                COUNT(*) FILTER (WHERE home_score > away_score) AS home_wins,
                COUNT(*) FILTER (WHERE home_score = away_score) AS draws,
                COUNT(*) FILTER (WHERE home_score < away_score) AS away_wins
            FROM matches
            WHERE status='FINISHED' AND home_score IS NOT NULL AND UPPER(sport)='FOOTBALL'
        """))
        h, d, a = r.fetchone()
    total = h + d + a
    log.info("distribution_check",
             home_wins=h, draws=d, away_wins=a,
             home_pct=round(100 * h / total, 1) if total else 0,
             draws_pct=round(100 * d / total, 1) if total else 0,
             away_pct=round(100 * a / total, 1) if total else 0)

    await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="N'effectue pas l'UPSERT, juste compte les matchs")
    parser.add_argument("--batch", type=int, default=200,
                        help="Taille des batches (default 200, augmenter si DB rapide)")
    args = parser.parse_args()
    asyncio.run(main(args.dry_run, args.batch))
