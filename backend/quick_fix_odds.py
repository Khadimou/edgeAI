"""
Quick fix one-shot pour :
1. Wiper les cotes des matchs avec valeurs identiques (corruption)
2. Settle les matchs SCHEDULED dont la date est dans le passé > 6h

Tourne dans le container backend qui a déjà la config DB :
    docker compose -f docker-compose.yml -f docker-compose.prod.yml exec backend python quick_fix_odds.py
"""
import asyncio

from sqlalchemy import text

from app.db.session import engine


async def main():
    async with engine.begin() as c:
        # 1. Wipe cotes des matchs avec triplet (home_odds, draw_odds, away_odds) identique
        r = await c.execute(text("""
            UPDATE matches
            SET home_odds = NULL, draw_odds = NULL, away_odds = NULL,
                over_25_odds = NULL, under_25_odds = NULL,
                ah_line = NULL, ah_home_odds = NULL, ah_away_odds = NULL,
                opening_home_odds = NULL, opening_draw_odds = NULL, opening_away_odds = NULL,
                opening_over_25_odds = NULL, opening_under_25_odds = NULL,
                opening_ah_line = NULL, opening_ah_home_odds = NULL, opening_ah_away_odds = NULL,
                opening_captured_at = NULL,
                updated_at = NOW()
            WHERE sport = 'FOOTBALL' AND status = 'SCHEDULED'
              AND (home_odds, draw_odds, away_odds) IN (
                SELECT home_odds, draw_odds, away_odds
                FROM matches
                WHERE sport = 'FOOTBALL' AND status = 'SCHEDULED'
                  AND home_odds IS NOT NULL
                GROUP BY home_odds, draw_odds, away_odds
                HAVING COUNT(*) > 1
              )
            RETURNING home_team, away_team
        """))
        wiped = r.fetchall()
        print(f"=== {len(wiped)} matchs avec cotes corrompues wipés ===")
        for row in wiped:
            print(f"  {row.home_team} vs {row.away_team}")

        # 2. Settle les matchs en passé : FINISHED si score présent, CANCELLED sinon
        r = await c.execute(text("""
            UPDATE matches
            SET status = CASE
                  WHEN home_score IS NOT NULL AND away_score IS NOT NULL THEN 'FINISHED'
                  ELSE 'CANCELLED'
                END,
                updated_at = NOW()
            WHERE sport = 'FOOTBALL' AND status = 'SCHEDULED'
              AND match_date < NOW() - INTERVAL '6 hours'
            RETURNING home_team, away_team, status
        """))
        settled = r.fetchall()
        print(f"\n=== {len(settled)} matchs stale settled ===")
        for row in settled:
            print(f"  [{row.status}] {row.home_team} vs {row.away_team}")

    print("\nDone")


if __name__ == "__main__":
    asyncio.run(main())
