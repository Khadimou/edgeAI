"""
Fetch des matchs foot historiques depuis football-data.co.uk (gratuit).
10 saisons × 5 ligues = ~17 000 matchs avec scores + cotes Bet365/Pinnacle.

Cette source est BIEN PLUS GÉNÉREUSE que football-data.org free tier :
- Saisons illimitées (vs 2 saisons max)
- Cotes 1X2 + O/U incluses (vs aucune)
- Yellow/red cards, half-time scores inclus

Usage:
    python fetch_full_history.py
    python fetch_full_history.py --seasons 1920 2021 2122 2223 2324 2425
"""
import argparse
import sys
from io import StringIO
from pathlib import Path

import httpx
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from backtest import LEAGUE_FD_CO_UK, TEAM_NAME_MAP

DATA_DIR = Path(__file__).parent / "data" / "raw"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_SEASONS = ["1516", "1617", "1718", "1819", "1920", "2021", "2122", "2223", "2324", "2425"]

# Inverse du mapping de backtest.py pour normaliser vers nos noms longs
TEAM_NAME_MAP_FULL = dict(TEAM_NAME_MAP)


def fetch_season(league_code: str, league_name: str, season: str) -> pd.DataFrame:
    """Télécharge un CSV foot + normalise vers notre format edgeAI."""
    url = f"https://www.football-data.co.uk/mmz4281/{season}/{league_code}.csv"
    try:
        r = httpx.get(url, timeout=20, follow_redirects=True)
        r.raise_for_status()
        df = pd.read_csv(StringIO(r.text))
    except Exception as e:
        print(f"  ! {league_name} {season}: {e}")
        return pd.DataFrame()

    if df.empty or "HomeTeam" not in df.columns:
        return pd.DataFrame()

    # Normalisation
    df["Date"] = pd.to_datetime(df["Date"], dayfirst=True, errors="coerce")
    df = df.dropna(subset=["Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG"])

    def pick_odds(row, side):
        """Préférence Pinnacle > Bet365 > Avg."""
        for prefix in ["PS", "B365", "Avg"]:
            col = f"{prefix}{side}"
            if col in row.index and pd.notna(row[col]):
                try:
                    val = float(row[col])
                    if val > 1:
                        return val
                except (ValueError, TypeError):
                    pass
        return None

    rows = []
    for _, r_ in df.iterrows():
        home_long = TEAM_NAME_MAP_FULL.get(str(r_["HomeTeam"]).strip(), str(r_["HomeTeam"]).strip())
        away_long = TEAM_NAME_MAP_FULL.get(str(r_["AwayTeam"]).strip(), str(r_["AwayTeam"]).strip())

        ht_home = r_.get("HTHG")
        ht_away = r_.get("HTAG")
        try:
            ht_home = int(ht_home) if pd.notna(ht_home) else None
        except (ValueError, TypeError):
            ht_home = None
        try:
            ht_away = int(ht_away) if pd.notna(ht_away) else None
        except (ValueError, TypeError):
            ht_away = None

        # Cards (souvent absent sur saisons anciennes)
        def to_int(v):
            try:
                return int(v) if pd.notna(v) else 0
            except (ValueError, TypeError):
                return 0

        external_id = f"fdcouk:{league_code}:{season}:{r_['HomeTeam']}:{r_['AwayTeam']}:{r_['Date'].strftime('%Y%m%d')}"

        rows.append({
            "external_id": external_id,
            "sport": "FOOTBALL",
            "league": league_name,
            "season": "20" + season[:2],  # ex: "2023"
            "home_team": home_long,
            "away_team": away_long,
            "match_date": r_["Date"].isoformat(),
            "status": "FINISHED",
            "home_score": int(r_["FTHG"]),
            "away_score": int(r_["FTAG"]),
            "ht_home_score": ht_home,
            "ht_away_score": ht_away,
            "home_yellow_cards": to_int(r_.get("HY")),
            "away_yellow_cards": to_int(r_.get("AY")),
            "home_red_cards": to_int(r_.get("HR")),
            "away_red_cards": to_int(r_.get("AR")),
            "home_odds": pick_odds(r_, "H"),
            "draw_odds": pick_odds(r_, "D"),
            "away_odds": pick_odds(r_, "A"),
        })

    return pd.DataFrame(rows)


def main(seasons: list[str], output_path: Path):
    all_dfs = []
    total = 0
    for league_name, code in LEAGUE_FD_CO_UK.items():
        for season in seasons:
            print(f"  {league_name} {season}...", end=" ", flush=True)
            df = fetch_season(code, league_name, season)
            if not df.empty:
                all_dfs.append(df)
                print(f"{len(df)} matchs")
                total += len(df)
            else:
                print("vide")

    if not all_dfs:
        print("\n✗ Aucune donnée collectée")
        sys.exit(1)

    full = pd.concat(all_dfs, ignore_index=True)
    full["match_date"] = pd.to_datetime(full["match_date"])
    full = full.sort_values("match_date").reset_index(drop=True)
    # Dédupe par external_id (au cas où)
    full = full.drop_duplicates(subset=["external_id"], keep="last")

    full.to_csv(output_path, index=False)
    print(f"\n✓ {len(full)} matchs uniques exportés vers {output_path}")
    print(f"  Période : {full['match_date'].min().date()} → {full['match_date'].max().date()}")
    print(f"  Avec cotes : {full['home_odds'].notna().sum()} ({100 * full['home_odds'].notna().mean():.1f}%)")
    print(f"  Avec HT scores : {full['ht_home_score'].notna().sum()} ({100 * full['ht_home_score'].notna().mean():.1f}%)")
    print(f"  Avec cards : {(full['home_yellow_cards'] > 0).sum()} matchs avec YC > 0")
    print(f"\nPar ligue :")
    for league, sub in full.groupby("league"):
        print(f"  {league:18} : {len(sub)} matchs")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--seasons", nargs="+", default=DEFAULT_SEASONS)
    parser.add_argument("--output", type=Path, default=DATA_DIR / "matches.csv")
    args = parser.parse_args()
    print(f"Fetch foot history — {len(args.seasons)} saisons × {len(LEAGUE_FD_CO_UK)} ligues\n")
    main(args.seasons, args.output)
