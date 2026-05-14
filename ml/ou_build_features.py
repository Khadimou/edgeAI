"""
Construit le dataset O/U 2.5 buts en réutilisant les 36 features du dataset 1X2.
Le target devient : 1 si total goals > 2.5, sinon 0.
"""
import sys
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).parent / "data"
INPUT_FEATURES = DATA_DIR / "features" / "dataset.csv"
INPUT_RAW = DATA_DIR / "raw" / "matches.csv"
OUTPUT = DATA_DIR / "features" / "ou_dataset.csv"


def build():
    if not INPUT_FEATURES.exists():
        print(f"ERREUR : {INPUT_FEATURES} introuvable. Lancez build_features.py d'abord.")
        sys.exit(1)
    if not INPUT_RAW.exists():
        print(f"ERREUR : {INPUT_RAW} introuvable.")
        sys.exit(1)

    features = pd.read_csv(INPUT_FEATURES, parse_dates=["match_date"])
    raw = pd.read_csv(INPUT_RAW, parse_dates=["match_date"])

    print(f"Features dataset : {len(features)} matchs")
    print(f"Raw matches      : {len(raw)} matchs")

    # Calcule le total de goals + label O/U 2.5
    raw["total_goals"] = raw["home_score"] + raw["away_score"]
    raw["ou_label"] = (raw["total_goals"] > 2.5).astype(int)

    # Merge sur (match_date, home_team, away_team)
    raw["date"] = pd.to_datetime(raw["match_date"]).dt.date
    features["date"] = pd.to_datetime(features["match_date"]).dt.date

    merged = features.merge(
        raw[["date", "home_team", "away_team", "ou_label", "total_goals"]],
        on=["date", "home_team", "away_team"],
        how="inner",
    )

    # Remplace la colonne label (3-class) par ou_label (binaire)
    merged["label_1x2"] = merged["label"]
    merged["label"] = merged["ou_label"]
    merged = merged.drop(columns=["ou_label", "date"])

    print(f"\n  {len(merged)} exemples mergés")
    print(f"  Total goals moyen : {merged['total_goals'].mean():.2f}")
    print(f"  Distribution :")
    overs = (merged["label"] == 1).sum()
    print(f"    Over 2.5  : {overs} ({100*overs/len(merged):.1f}%)")
    print(f"    Under 2.5 : {len(merged)-overs} ({100*(len(merged)-overs)/len(merged):.1f}%)")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(OUTPUT, index=False)
    print(f"\n✓ Dataset O/U : {OUTPUT}")
    print(f"  Shape : {merged.shape}")


if __name__ == "__main__":
    build()
