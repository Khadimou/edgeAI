"""
Construit le dataset de features NBA pour entraîner le modèle.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from pipeline.nba_features import compute_nba_features, NBAFeatures

DATA_DIR = Path(__file__).parent / "data"
INPUT = DATA_DIR / "raw" / "nba_matches.csv"
OUTPUT = DATA_DIR / "features" / "nba_dataset.csv"
MIN_HISTORY = 10


def build():
    print(f"Lecture : {INPUT}")
    df = pd.read_csv(INPUT, parse_dates=["match_date"])
    df = df.sort_values("match_date").reset_index(drop=True)
    print(f"  {len(df)} matchs bruts")

    feature_rows = []
    skipped = 0

    for i, row in df.iterrows():
        past = df.iloc[:i]
        home_team = row["home_team"]
        away_team = row["away_team"]

        home_count = len(past[(past["home_team"] == home_team) | (past["away_team"] == home_team)])
        away_count = len(past[(past["home_team"] == away_team) | (past["away_team"] == away_team)])
        if home_count < MIN_HISTORY or away_count < MIN_HISTORY:
            skipped += 1
            continue

        feat = compute_nba_features(home_team, away_team, row["match_date"], past)
        feat_dict = dict(zip(NBAFeatures.feature_names(), feat.to_array()))
        feat_dict["label"] = int(row["label"])  # 0 = home win, 2 = away win
        feat_dict["match_date"] = row["match_date"].isoformat()
        feat_dict["home_team"] = home_team
        feat_dict["away_team"] = away_team
        feat_dict["season"] = row["season"]
        feature_rows.append(feat_dict)

        if (i + 1) % 500 == 0:
            print(f"  {i+1}/{len(df)} traités...")

    result = pd.DataFrame(feature_rows)
    print(f"\n  {len(result)} exemples ({skipped} ignorés, historique insuffisant)")

    # Distribution
    home_wins = (result["label"] == 0).sum()
    away_wins = (result["label"] == 2).sum()
    print(f"\nLabels :")
    print(f"  Victoire dom. : {home_wins} ({100*home_wins/len(result):.1f}%)")
    print(f"  Victoire ext. : {away_wins} ({100*away_wins/len(result):.1f}%)")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(OUTPUT, index=False)
    print(f"\n✓ Dataset : {OUTPUT}")
    print(f"  Shape : {result.shape}")


if __name__ == "__main__":
    build()
