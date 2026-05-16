"""
Cleanup des cotes corrompues : détecte les groupes de matchs SCHEDULED qui
ont les MEMES (home_odds, draw_odds, away_odds) — symptôme du bug d'ingestion
qui contaminait plusieurs rows en 1 appel.

Aussi : settle les matchs SCHEDULED dont match_date est dans le passé > 6h
(les forcer en FINISHED avec score 0-0 si pas de score, OU nullifier pour
qu'ils sortent du flux upcoming).

Usage :
    python cleanup_corrupted_odds.py              # dry-run (juste lister)
    python cleanup_corrupted_odds.py --wipe       # nullifie les cotes en doublon
    python cleanup_corrupted_odds.py --settle     # status SCHEDULED + passé → FINISHED ou cancelled
    python cleanup_corrupted_odds.py --wipe --settle  # tout faire
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

import structlog
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import text

# Optional dotenv (en local). En prod (Docker), DATABASE_URL vient déjà de l'environnement
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

log = structlog.get_logger()


def build_url(raw: str) -> str:
    url = raw.split("?")[0]
    url = url.replace("postgresql://", "postgresql+asyncpg://")
    url = url.replace("postgres://", "postgresql+asyncpg://")
    return url


async def detect_corrupted_odds(session) -> list[dict]:
    """Détecte les groupes de matchs SCHEDULED FOOTBALL avec cotes identiques (suspect)."""
    r = await session.execute(text("""
        SELECT home_odds, draw_odds, away_odds,
               COUNT(*) AS n_matches,
               ARRAY_AGG(home_team || ' vs ' || away_team) AS matches,
               ARRAY_AGG(id) AS ids
        FROM matches
        WHERE sport = 'FOOTBALL'
          AND status = 'SCHEDULED'
          AND home_odds IS NOT NULL
          AND draw_odds IS NOT NULL
          AND away_odds IS NOT NULL
        GROUP BY home_odds, draw_odds, away_odds
        HAVING COUNT(*) > 1
        ORDER BY n_matches DESC
    """))
    groups = []
    for row in r:
        groups.append({
            "odds": (float(row.home_odds), float(row.draw_odds), float(row.away_odds)),
            "n_matches": row.n_matches,
            "matches": row.matches,
            "ids": row.ids,
        })
    return groups


async def wipe_corrupted(session, groups: list[dict]) -> int:
    """Nullifie les cotes des groupes corrompus."""
    total_wiped = 0
    for g in groups:
        ids = g["ids"]
        r = await session.execute(text("""
            UPDATE matches
            SET home_odds = NULL, draw_odds = NULL, away_odds = NULL,
                over_25_odds = NULL, under_25_odds = NULL,
                ah_line = NULL, ah_home_odds = NULL, ah_away_odds = NULL,
                opening_home_odds = NULL, opening_draw_odds = NULL, opening_away_odds = NULL,
                opening_over_25_odds = NULL, opening_under_25_odds = NULL,
                opening_ah_line = NULL, opening_ah_home_odds = NULL, opening_ah_away_odds = NULL,
                opening_captured_at = NULL,
                updated_at = NOW()
            WHERE id = ANY(:ids)
        """), {"ids": ids})
        total_wiped += r.rowcount
        log.info("wiped_group", odds=g["odds"], n=len(ids))
    await session.commit()
    return total_wiped


async def detect_stale_scheduled(session) -> list:
    """Détecte les matchs SCHEDULED dans le passé > 6h (probablement déjà joués mais pas settled)."""
    r = await session.execute(text("""
        SELECT id, home_team, away_team, match_date, league, home_score, away_score
        FROM matches
        WHERE sport = 'FOOTBALL'
          AND status = 'SCHEDULED'
          AND match_date < NOW() - INTERVAL '6 hours'
        ORDER BY match_date DESC
    """))
    return [dict(row._mapping) for row in r]


async def fix_stale(session, stale: list, mark_cancelled: bool = True) -> int:
    """
    Pour les matchs stale (SCHEDULED + passé > 6h) :
    - Si home_score + away_score présents → FINISHED
    - Sinon → CANCELLED (sortent du flux upcoming sans polluer)
    """
    n_finished = 0
    n_cancelled = 0
    for m in stale:
        if m["home_score"] is not None and m["away_score"] is not None:
            await session.execute(text("UPDATE matches SET status = 'FINISHED', updated_at = NOW() WHERE id = :id"),
                                  {"id": m["id"]})
            n_finished += 1
        elif mark_cancelled:
            await session.execute(text("UPDATE matches SET status = 'CANCELLED', updated_at = NOW() WHERE id = :id"),
                                  {"id": m["id"]})
            n_cancelled += 1
    await session.commit()
    log.info("stale_fixed", finished=n_finished, cancelled=n_cancelled, total=len(stale))
    return n_finished + n_cancelled


async def main(wipe: bool, settle: bool):
    raw = os.environ.get("DATABASE_URL")
    if not raw:
        log.error("missing_database_url"); sys.exit(1)
    db_url = build_url(raw)
    connect_args = {"ssl": True} if "sslmode=require" in raw else {}
    engine = create_async_engine(db_url, connect_args=connect_args, pool_pre_ping=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as session:
        # 1. Détection cotes corrompues
        groups = await detect_corrupted_odds(session)
        print(f"\n=== Cotes corrompues (groupes avec mêmes cotes) ===")
        print(f"Total groupes : {len(groups)}")
        for g in groups[:10]:
            print(f"\n  Cotes {g['odds']} → {g['n_matches']} matchs :")
            for m in g["matches"]:
                print(f"    - {m}")
        total_corrupted = sum(g["n_matches"] for g in groups)
        print(f"\nTotal matchs avec cotes douteuses : {total_corrupted}")

        if wipe and groups:
            n = await wipe_corrupted(session, groups)
            print(f"\n✓ Wiped cotes pour {n} matchs")

        # 2. Détection matchs stale
        stale = await detect_stale_scheduled(session)
        print(f"\n=== Matchs SCHEDULED dans le passé > 6h ===")
        print(f"Total : {len(stale)}")
        for m in stale[:10]:
            score = f"{m['home_score']}-{m['away_score']}" if m['home_score'] is not None else "no_score"
            print(f"  {m['match_date']}  {m['league']:15} {m['home_team']:25} vs {m['away_team']:25}  [{score}]")

        if settle and stale:
            n = await fix_stale(session, stale)
            print(f"\n✓ Fixed status pour {n} matchs")

    await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--wipe", action="store_true",
                        help="Nullifie les cotes des matchs avec cotes identiques (suspect)")
    parser.add_argument("--settle", action="store_true",
                        help="Force status FINISHED/CANCELLED pour les matchs SCHEDULED en passé > 6h")
    args = parser.parse_args()
    asyncio.run(main(args.wipe, args.settle))
