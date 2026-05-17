"""
Backfill rétroactif des cotes 1X2 / Over-Under 2.5 / Asian Handicap dans la
table matches à partir des CSV football-data.co.uk.

Pourquoi : l'import historique des matchs (import_matches_to_prod.py) a populé
les scores mais pas les cotes. Sans cotes en DB, le tracking ne peut pas
calculer d'edge → 0 value bet sur les milliers de matchs backfillés. Ce script
remplit les colonnes home_odds/draw_odds/away_odds + over_25_odds/under_25_odds
+ ah_line/ah_home_odds/ah_away_odds depuis les CSV de référence.

Sources de cotes (par ordre de préférence) :
  - 1X2  : PSCH/D/A (Pinnacle closing) → PSH/D/A → B365CH/D/A → B365H/D/A → AvgCH/D/A
  - O/U  : PC>2.5/PC<2.5 → P>2.5/P<2.5 → B365C>2.5/B365C<2.5 → B365>2.5/B365<2.5
  - AH   : PCAHH/A → PAHH/A → B365CAHH/A → B365AHH/A → AvgCAHH/A
  - line : AHCh → AHh

Idempotent : on UPDATE seulement les colonnes NULL pour ne pas écraser les
cotes prod (ingérées en live par odds-api).

Usage (dans un container ml_worker éphémère) :
    docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm \\
        ml_worker python backfill_odds.py
"""
from __future__ import annotations

import argparse
import asyncio
import os
from io import StringIO

import httpx
import pandas as pd
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# Réutilise les constantes du backtest existant
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from backtest import LEAGUE_FD_CO_UK, TEAM_NAME_MAP


SEASONS = ["1920", "2021", "2122", "2223", "2324", "2425", "2526"]


def _build_url(raw: str) -> str:
    url = raw.split("?")[0]
    url = url.replace("postgresql://", "postgresql+asyncpg://")
    url = url.replace("postgres://", "postgresql+asyncpg://")
    return url


def _pick(row, candidates) -> float | None:
    """Renvoie la 1ère valeur > 1.0 trouvée parmi les colonnes candidates."""
    for col in candidates:
        if col in row.index and pd.notna(row[col]):
            try:
                v = float(row[col])
                if v > 1.0:
                    return v
            except (ValueError, TypeError):
                pass
    return None


def _pick_line(row) -> float | None:
    for col in ["AHCh", "AHh"]:
        if col in row.index and pd.notna(row[col]):
            try:
                return float(row[col])
            except (ValueError, TypeError):
                pass
    return None


def fetch_season(league_code: str, league_name: str, season: str) -> pd.DataFrame:
    url = f"https://www.football-data.co.uk/mmz4281/{season}/{league_code}.csv"
    try:
        r = httpx.get(url, timeout=30, follow_redirects=True)
        r.raise_for_status()
        df = pd.read_csv(StringIO(r.text))
    except Exception as e:
        print(f"  ! {league_name} {season}: {e}")
        return pd.DataFrame()

    if df.empty or "HomeTeam" not in df.columns:
        return pd.DataFrame()

    df["Date"] = pd.to_datetime(df["Date"], dayfirst=True, errors="coerce")
    df = df.dropna(subset=["Date", "HomeTeam", "AwayTeam"])

    rows = []
    for _, r_ in df.iterrows():
        home = TEAM_NAME_MAP.get(str(r_["HomeTeam"]).strip(), str(r_["HomeTeam"]).strip())
        away = TEAM_NAME_MAP.get(str(r_["AwayTeam"]).strip(), str(r_["AwayTeam"]).strip())

        # 1X2
        h_odds = _pick(r_, ["PSCH", "PSH", "B365CH", "B365H", "AvgCH", "AvgH"])
        d_odds = _pick(r_, ["PSCD", "PSD", "B365CD", "B365D", "AvgCD", "AvgD"])
        a_odds = _pick(r_, ["PSCA", "PSA", "B365CA", "B365A", "AvgCA", "AvgA"])

        # Over/Under 2.5
        o25 = _pick(r_, ["PC>2.5", "P>2.5", "B365C>2.5", "B365>2.5", "AvgC>2.5", "Avg>2.5"])
        u25 = _pick(r_, ["PC<2.5", "P<2.5", "B365C<2.5", "B365<2.5", "AvgC<2.5", "Avg<2.5"])

        # AH
        ah_line = _pick_line(r_)
        ah_h = _pick(r_, ["PCAHH", "PAHH", "B365CAHH", "B365AHH", "AvgCAHH", "AvgAHH"])
        ah_a = _pick(r_, ["PCAHA", "PAHA", "B365CAHA", "B365AHA", "AvgCAHA", "AvgAHA"])

        rows.append({
            "match_date": r_["Date"],
            "league": league_name,
            "home_team": home,
            "away_team": away,
            "home_odds": h_odds,
            "draw_odds": d_odds,
            "away_odds": a_odds,
            "over_25_odds": o25,
            "under_25_odds": u25,
            "ah_line": ah_line,
            "ah_home_odds": ah_h,
            "ah_away_odds": ah_a,
        })
    return pd.DataFrame(rows)


async def update_match_odds(session, row) -> bool:
    """UPDATE 1 match. Ne touche que les colonnes NULL (pas d'écrasement prod).

    Match key : (league, home_team, away_team, |match_date - csv_date| < 2 days)
    pour absorber les variations de fuseau horaire et de jour reporté.
    """
    try:
        # UPDATE conditionnel : COALESCE pour ne pas écraser
        result = await session.execute(
            text("""
                UPDATE matches m
                SET home_odds         = COALESCE(m.home_odds, :h_odds),
                    draw_odds         = COALESCE(m.draw_odds, :d_odds),
                    away_odds         = COALESCE(m.away_odds, :a_odds),
                    over_25_odds      = COALESCE(m.over_25_odds, :o25),
                    under_25_odds     = COALESCE(m.under_25_odds, :u25),
                    ah_line           = COALESCE(m.ah_line, :ah_line),
                    ah_home_odds      = COALESCE(m.ah_home_odds, :ah_h),
                    ah_away_odds      = COALESCE(m.ah_away_odds, :ah_a),
                    opening_home_odds = COALESCE(m.opening_home_odds, :h_odds),
                    opening_draw_odds = COALESCE(m.opening_draw_odds, :d_odds),
                    opening_away_odds = COALESCE(m.opening_away_odds, :a_odds),
                    opening_over_25_odds  = COALESCE(m.opening_over_25_odds, :o25),
                    opening_under_25_odds = COALESCE(m.opening_under_25_odds, :u25),
                    opening_ah_line       = COALESCE(m.opening_ah_line, :ah_line),
                    opening_ah_home_odds  = COALESCE(m.opening_ah_home_odds, :ah_h),
                    opening_ah_away_odds  = COALESCE(m.opening_ah_away_odds, :ah_a)
                WHERE m.league = :league
                  AND m.home_team = :home
                  AND m.away_team = :away
                  AND m.match_date >= :date_min
                  AND m.match_date <= :date_max
                  AND m.status = 'FINISHED'
                RETURNING m.id
            """),
            {
                "h_odds": row["home_odds"], "d_odds": row["draw_odds"], "a_odds": row["away_odds"],
                "o25": row["over_25_odds"], "u25": row["under_25_odds"],
                "ah_line": row["ah_line"], "ah_h": row["ah_home_odds"], "ah_a": row["ah_away_odds"],
                "league": row["league"], "home": row["home_team"], "away": row["away_team"],
                "date_min": (row["match_date"] - pd.Timedelta(days=2)).to_pydatetime().replace(tzinfo=None),
                "date_max": (row["match_date"] + pd.Timedelta(days=2)).to_pydatetime().replace(tzinfo=None),
            },
        )
        return result.fetchone() is not None
    except Exception as e:
        # On loggue mais on ne s'arrête pas (encodage de team peut différer)
        print(f"  ⚠ update error {row['home_team']} vs {row['away_team']}: {e}")
        return False


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", type=int, default=200,
                        help="Commit chaque N updates")
    args = parser.parse_args()

    print(f"⚙ Backfill cotes historiques : {len(LEAGUE_FD_CO_UK)} ligues × {len(SEASONS)} saisons")

    # Connexion DB
    raw = os.environ["DATABASE_URL"]
    db_url = _build_url(raw)
    connect_args = {"ssl": True} if "sslmode=require" in raw else {}
    engine = create_async_engine(db_url, connect_args=connect_args, pool_pre_ping=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    total_matched = 0
    total_attempted = 0
    per_league = {}

    async with Session() as session:
        for league_name, league_code in LEAGUE_FD_CO_UK.items():
            print(f"\n── {league_name} ──")
            league_matched = 0
            for season in SEASONS:
                print(f"  {season}...", end=" ", flush=True)
                df = fetch_season(league_code, league_name, season)
                if df.empty:
                    print("vide ou indisponible")
                    continue
                # On ne garde que les rows qui ont au moins UNE cote utile
                df = df.dropna(subset=["home_odds", "over_25_odds", "ah_home_odds"], how="all")
                print(f"{len(df)} rows", end=" → ", flush=True)

                matched = 0
                for i, row_dict in enumerate(df.to_dict("records"), 1):
                    total_attempted += 1
                    if await update_match_odds(session, row_dict):
                        matched += 1
                        total_matched += 1
                    if i % args.batch == 0:
                        await session.commit()
                await session.commit()
                print(f"{matched} matchés")
                league_matched += matched
            per_league[league_name] = league_matched

    await engine.dispose()

    print("\n" + "=" * 60)
    print("RÉSUMÉ")
    print("=" * 60)
    for league, n in per_league.items():
        print(f"  {league:18} : {n} matchs mis à jour")
    print(f"\n✓ Total : {total_matched}/{total_attempted} cotes ajoutées en DB")
    if total_matched < total_attempted * 0.5:
        print("⚠ < 50% match — vérifier TEAM_NAME_MAP pour les mismatches de noms")


if __name__ == "__main__":
    asyncio.run(main())
