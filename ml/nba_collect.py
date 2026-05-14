"""
Collecte des matchs NBA historiques via nba_api (stats.nba.com).

Récupère 5 saisons NBA en saison régulière + playoffs, pivote pour avoir
1 ligne par match (home + away en colonnes), sauve en CSV.

Usage:
    python nba_collect.py
    python nba_collect.py --seasons 2020-21 2021-22 2022-23 2023-24 2024-25
"""
import argparse
import sys
import time
from pathlib import Path

import pandas as pd

from nba_api.stats.endpoints import leaguegamefinder

DATA_DIR = Path(__file__).parent / "data" / "raw"
DEFAULT_SEASONS = ["2020-21", "2021-22", "2022-23", "2023-24", "2024-25"]
REQUEST_DELAY = 1.5  # nba_api est strict sur le rate limit


def fetch_season(season: str, season_type: str = "Regular Season") -> pd.DataFrame:
    """Récupère tous les game logs d'une saison. 2 rows par match."""
    gf = leaguegamefinder.LeagueGameFinder(
        season_nullable=season,
        season_type_nullable=season_type,
        timeout=60,
    )
    df = gf.get_data_frames()[0]
    return df


def pivot_to_match(rows: pd.DataFrame, season: str, season_type: str) -> pd.DataFrame:
    """
    Pivote les 2 rows/match en 1 row avec home/away.
    MATCHUP format : 'IND vs. ATL' = IND est home, 'IND @ ATL' = IND est away.
    """
    rows = rows.copy()
    rows["is_home"] = rows["MATCHUP"].str.contains(" vs. ")
    home = rows[rows["is_home"]].rename(columns={
        "TEAM_NAME": "home_team", "TEAM_ABBREVIATION": "home_abbr",
        "PTS": "home_score", "FG_PCT": "home_fg_pct", "FG3_PCT": "home_fg3_pct",
        "FT_PCT": "home_ft_pct", "REB": "home_reb", "AST": "home_ast",
        "STL": "home_stl", "BLK": "home_blk", "TOV": "home_tov",
        "PLUS_MINUS": "home_plus_minus",
    })
    away = rows[~rows["is_home"]].rename(columns={
        "TEAM_NAME": "away_team", "TEAM_ABBREVIATION": "away_abbr",
        "PTS": "away_score", "FG_PCT": "away_fg_pct", "FG3_PCT": "away_fg3_pct",
        "FT_PCT": "away_ft_pct", "REB": "away_reb", "AST": "away_ast",
        "STL": "away_stl", "BLK": "away_blk", "TOV": "away_tov",
        "PLUS_MINUS": "away_plus_minus",
    })

    keep_cols_home = [
        "GAME_ID", "GAME_DATE", "home_team", "home_abbr", "home_score",
        "home_fg_pct", "home_fg3_pct", "home_ft_pct",
        "home_reb", "home_ast", "home_stl", "home_blk", "home_tov", "home_plus_minus",
    ]
    keep_cols_away = [
        "GAME_ID", "away_team", "away_abbr", "away_score",
        "away_fg_pct", "away_fg3_pct", "away_ft_pct",
        "away_reb", "away_ast", "away_stl", "away_blk", "away_tov", "away_plus_minus",
    ]

    merged = home[keep_cols_home].merge(away[keep_cols_away], on="GAME_ID", how="inner")
    merged["match_date"] = pd.to_datetime(merged["GAME_DATE"])
    merged["external_id"] = "nba:" + merged["GAME_ID"].astype(str)
    merged["season"] = season
    merged["season_type"] = season_type
    merged["sport"] = "NBA"
    merged["league"] = "NBA"
    merged["status"] = "FINISHED"

    # Outcome : 0 = home win, 2 = away win (pas de draw en NBA)
    merged["label"] = (merged["away_score"] > merged["home_score"]).astype(int) * 2

    return merged.drop(columns=["GAME_ID", "GAME_DATE"])


def collect(seasons: list[str]) -> pd.DataFrame:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    all_dfs = []

    for season in seasons:
        for season_type in ["Regular Season", "Playoffs"]:
            print(f"  {season} {season_type}...", end=" ", flush=True)
            try:
                raw = fetch_season(season, season_type)
                if raw.empty:
                    print("0 matchs")
                    continue
                pivoted = pivot_to_match(raw, season, season_type)
                print(f"{len(pivoted)} matchs")
                all_dfs.append(pivoted)
            except Exception as e:
                print(f"ERREUR: {type(e).__name__}: {e}")
            time.sleep(REQUEST_DELAY)

    if not all_dfs:
        return pd.DataFrame()
    return pd.concat(all_dfs, ignore_index=True).sort_values("match_date").reset_index(drop=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--seasons", nargs="+", default=DEFAULT_SEASONS)
    args = parser.parse_args()

    print(f"Collecte NBA — {len(args.seasons)} saisons")
    print(f"  {args.seasons}\n")

    df = collect(args.seasons)
    if df.empty:
        print("\n✗ Aucun match collecté")
        sys.exit(1)

    output = DATA_DIR / "nba_matches.csv"
    df.to_csv(output, index=False)
    print(f"\n✓ Total : {len(df)} matchs")
    print(f"✓ Exporté : {output}")

    # Distribution outcomes
    home_wins = (df["label"] == 0).sum()
    away_wins = (df["label"] == 2).sum()
    print(f"\nDistribution :")
    print(f"  Victoire dom. : {home_wins} ({100*home_wins/len(df):.1f}%)")
    print(f"  Victoire ext. : {away_wins} ({100*away_wins/len(df):.1f}%)")
    print(f"\nPériode : {df['match_date'].min().date()} → {df['match_date'].max().date()}")
