"""
Construction du dataset de features pour l'entraînement XGBoost.
36 features dont classements dynamiques, cartons, mi-temps — sans data leakage temporel.

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
from pipeline.features import (
    compute_features_from_history,
    compute_standings_from_history,
    MatchFeatures,
    init_elo, update_elo, update_elo_venue,
)

OUTCOMES = ["HOME", "DRAW", "AWAY"]
DEFAULT_INPUT = Path(__file__).parent / "data" / "raw" / "matches.csv"
DEFAULT_OUTPUT = Path(__file__).parent / "data" / "features" / "dataset.csv"


def build(input_path: Path, output_path: Path, min_history: int = 3):
    print(f"Lecture : {input_path}")
    df = pd.read_csv(input_path, parse_dates=["match_date"])
    print(f"  {len(df)} matchs bruts")

    df = df.dropna(subset=["home_score", "away_score"])
    df["home_score"] = df["home_score"].astype(int)
    df["away_score"] = df["away_score"].astype(int)
    df = df.sort_values("match_date").reset_index(drop=True)
    print(f"  {len(df)} matchs avec résultats")

    # Colonnes optionnelles — remplir avec NaN si absentes
    for col in ["ht_home_score", "ht_away_score", "home_yellow_cards", "away_yellow_cards",
                # Phase 2 : shots/SOT/corners depuis fdco
                "home_shots", "away_shots", "home_shots_on_target", "away_shots_on_target",
                "home_corners", "away_corners", "home_fouls", "away_fouls"]:
        if col not in df.columns:
            df[col] = np.nan

    def label(row):
        if row["home_score"] > row["away_score"]:
            return 0
        elif row["home_score"] == row["away_score"]:
            return 1
        return 2

    df["label"] = df.apply(label, axis=1)

    if "date" not in df.columns:
        df["date"] = df["match_date"]

    # Déduire la ligue depuis la colonne "league" si disponible
    has_league = "league" in df.columns

    # ELO state maintenu chronologiquement (calculé avant le match, mis à jour après)
    elo_general = init_elo()
    elo_home_venue = init_elo()
    elo_away_venue = init_elo()

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
            # Encore mettre à jour ELO même si pas dans le dataset (apprentissage state)
            update_elo(elo_general, row["home_team"], row["away_team"],
                       int(row["home_score"]), int(row["away_score"]))
            update_elo_venue(elo_home_venue, elo_away_venue,
                             row["home_team"], row["away_team"],
                             int(row["home_score"]), int(row["away_score"]))
            skipped += 1
            continue

        # Classement dynamique sans data leakage
        league = row.get("league", "") if has_league else ""
        standings, total_teams = compute_standings_from_history(past, row["date"], league)

        # Pas d'odds_df : on entraîne le modèle SANS les market features.
        # Avec market features, le modèle apprend à imiter le marché → edge moyen faux.
        # Confirmé par le backtest : Ligue 1 et Bundesliga deviennent rentables sans.
        odds_df = None

        feat = compute_features_from_history(
            home_team=row["home_team"],
            away_team=row["away_team"],
            match_date=row["date"],
            historical_df=past,
            odds_df=odds_df,
            standings=standings,
            total_teams=total_teams,
            elo_general=elo_general,
            elo_home_venue=elo_home_venue,
            elo_away_venue=elo_away_venue,
        )

        # Update ELO state APRÈS calcul des features (pas de data leakage)
        update_elo(elo_general, row["home_team"], row["away_team"],
                   int(row["home_score"]), int(row["away_score"]))
        update_elo_venue(elo_home_venue, elo_away_venue,
                         row["home_team"], row["away_team"],
                         int(row["home_score"]), int(row["away_score"]))

        feat_dict = {name: val for name, val in zip(
            MatchFeatures.feature_names(), feat.to_array()
        )}
        feat_dict["label"] = row["label"]
        feat_dict["match_date"] = row["date"].isoformat()
        feat_dict["home_team"] = row["home_team"]
        feat_dict["away_team"] = row["away_team"]
        feat_dict["league"] = row.get("league", "") if has_league else ""

        feature_rows.append(feat_dict)

        if (i + 1) % 200 == 0:
            print(f"  {i+1}/{len(df)} matchs traités...")

    result = pd.DataFrame(feature_rows)
    print(f"\n  {len(result)} exemples générés ({skipped} ignorés — historique insuffisant)")

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
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--min-history", type=int, default=3)
    args = parser.parse_args()

    if not args.input.exists():
        print(f"ERREUR : fichier introuvable : {args.input}")
        print("Lancez d'abord : python collect_data.py")
        sys.exit(1)

    build(args.input, args.output, args.min_history)
