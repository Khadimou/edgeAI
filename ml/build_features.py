"""
Construction du dataset de features pour l'entraînement XGBoost.
Calcule 29 features à partir de l'historique, sans aucun data leakage temporel.

Usage:
    python build_features.py --input data/raw/matches.csv --output data/features/dataset.csv
    python build_features.py  # utilise les chemins par défaut
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from pipeline.features import compute_features_from_history, MatchFeatures

OUTCOMES = ["HOME", "DRAW", "AWAY"]
DEFAULT_INPUT = Path(__file__).parent / "data" / "raw" / "matches.csv"
DEFAULT_OUTPUT = Path(__file__).parent / "data" / "features" / "dataset.csv"


def build(input_path: Path, output_path: Path, min_history: int = 3):
    print(f"Lecture : {input_path}")
    df = pd.read_csv(input_path, parse_dates=["match_date"])
    print(f"  {len(df)} matchs bruts")

    # Ne garder que les matchs avec résultat connu
    df = df.dropna(subset=["home_score", "away_score"])
    df["home_score"] = df["home_score"].astype(int)
    df["away_score"] = df["away_score"].astype(int)
    df = df.sort_values("match_date").reset_index(drop=True)
    print(f"  {len(df)} matchs avec résultats")

    # Label : 0=HOME, 1=DRAW, 2=AWAY
    def label(row):
        if row["home_score"] > row["away_score"]:
            return 0
        elif row["home_score"] == row["away_score"]:
            return 1
        return 2

    df["label"] = df.apply(label, axis=1)

    # Renommer 'date' si nécessaire (normalize_match utilise match_date)
    if "date" not in df.columns:
        df["date"] = df["match_date"]

    # Calcul des features — exclut les matchs sans assez d'historique
    feature_rows = []
    skipped = 0

    for i, row in df.iterrows():
        past = df[df["date"] < row["date"]]

        home_hist = past[
            (past["home_team"] == row["home_team"]) |
            (past["away_team"] == row["home_team"])
        ]
        away_hist = past[
            (past["home_team"] == row["away_team"]) |
            (past["away_team"] == row["away_team"])
        ]

        if len(home_hist) < min_history or len(away_hist) < min_history:
            skipped += 1
            continue

        feat = compute_features_from_history(
            home_team=row["home_team"],
            away_team=row["away_team"],
            match_date=row["date"],
            historical_df=past,
        )

        feat_dict = {name: val for name, val in zip(
            MatchFeatures.feature_names(), feat.to_array()
        )}
        feat_dict["label"] = row["label"]
        feat_dict["match_date"] = row["date"].isoformat()
        feat_dict["home_team"] = row["home_team"]
        feat_dict["away_team"] = row["away_team"]
        feat_dict["league"] = row.get("league", "")

        feature_rows.append(feat_dict)

        if (i + 1) % 200 == 0:
            print(f"  {i+1}/{len(df)} matchs traités...")

    result = pd.DataFrame(feature_rows)
    print(f"\n  {len(result)} exemples générés ({skipped} ignorés — historique insuffisant)")

    # Distribution des labels
    dist = result["label"].value_counts().sort_index()
    labels = ["Victoire dom.", "Nul", "Victoire ext."]
    for idx, count in dist.items():
        print(f"  {labels[idx]} : {count} ({100*count/len(result):.1f}%)")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(output_path, index=False)
    print(f"\n✓ Dataset exporté : {output_path}")
    print(f"  Shape : {result.shape}")

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Construit le dataset de features")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT,
                        help="CSV des matchs bruts")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                        help="CSV du dataset features")
    parser.add_argument("--min-history", type=int, default=3,
                        help="Nombre minimum de matchs historiques requis (défaut : 3)")
    args = parser.parse_args()

    if not args.input.exists():
        print(f"ERREUR : fichier introuvable : {args.input}")
        print("Lancez d'abord : python collect_data.py")
        sys.exit(1)

    build(args.input, args.output, args.min_history)
