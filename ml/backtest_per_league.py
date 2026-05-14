"""
Backtest des modèles per-league : chaque ligue a son propre modèle entraîné
uniquement sur ses matchs. Compare aux résultats du modèle global.

Méthode : pour chaque ligue, on charge le modèle dédié, on génère des OOF
predictions sur les seuls matchs de cette ligue (5-fold TimeSeriesSplit),
on simule Kelly avec les cotes football-data.co.uk.

Usage:
    python backtest_per_league.py
    python backtest_per_league.py --edge-threshold 0.08 --edge-max 0.20
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from pipeline.features import MatchFeatures
from backtest import (
    fetch_all_historical_odds, merge_with_features,
    simulate, normalize_team,
)

DATA_DIR = Path(__file__).parent / "data"
ARTIFACTS_DIR = Path(__file__).parent / "artifacts" / "backtest"
ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

LEAGUES = ["Premier League", "La Liga", "Bundesliga", "Serie A", "Ligue 1"]


def compute_oof_per_league(X, y):
    """Mêmes params que train_per_league pour cohérence."""
    from sklearn.model_selection import TimeSeriesSplit
    from sklearn.calibration import CalibratedClassifierCV
    from xgboost import XGBClassifier
    params = {
        "n_estimators": 300, "max_depth": 4, "learning_rate": 0.05,
        "subsample": 0.8, "colsample_bytree": 0.8,
        "objective": "multi:softprob", "num_class": 3,
        "eval_metric": "mlogloss", "random_state": 42, "n_jobs": -1,
    }
    oof = np.zeros((len(y), 3))
    tscv = TimeSeriesSplit(n_splits=5)
    for fold, (ti, vi) in enumerate(tscv.split(X)):
        print(f"    Fold {fold+1}/5 train={len(ti)} val={len(vi)}", flush=True)
        clf = CalibratedClassifierCV(XGBClassifier(**params), method="sigmoid", cv=3)
        clf.fit(X[ti], y[ti])
        oof[vi] = clf.predict_proba(X[vi])
    return oof


def run(edge_threshold=0.08, edge_max=0.20, kelly_fraction=0.25, bankroll=100.0):
    print("─" * 60)
    print("Backtest PER-LEAGUE — 1 modèle par ligue")
    print("─" * 60)

    features_path = DATA_DIR / "features" / "dataset.csv"
    features = pd.read_csv(features_path, parse_dates=["match_date"])
    print(f"\n✓ Dataset : {len(features)} matchs")

    print("\n[1/3] Téléchargement des cotes football-data.co.uk...")
    odds = fetch_all_historical_odds()
    print(f"  → {len(odds)} matchs avec cotes")

    print("\n[2/3] Merge features ↔ cotes...")
    merged = merge_with_features(features, odds)
    print(f"  → {len(merged)} matchs matchés")

    print(f"\n[3/3] Backtest par ligue (edge ∈ [{edge_threshold:.0%}, {edge_max:.0%}])...")

    all_bets = []
    per_league_results = {}
    feature_cols = MatchFeatures.feature_names()

    for league in LEAGUES:
        print(f"\n── {league} ──")
        sub = merged[merged["league"] == league].sort_values("match_date").reset_index(drop=True)
        if len(sub) < 500:
            print(f"  Skip ({len(sub)} samples)")
            continue

        X = sub[feature_cols].values.astype(np.float32)
        y = sub["label"].values.astype(int)
        oof = compute_oof_per_league(X, y)
        sub["prob_home"] = oof[:, 0]
        sub["prob_draw"] = oof[:, 1]
        sub["prob_away"] = oof[:, 2]
        valid = oof.sum(axis=1) > 0
        sub_valid = sub[valid].reset_index(drop=True)
        print(f"  → {len(sub_valid)} matchs avec OOF preds")

        bets_df, summary = simulate(
            sub_valid,
            initial_bankroll=bankroll,
            edge_threshold=edge_threshold,
            edge_max=edge_max,
            kelly_fraction=kelly_fraction,
        )
        if bets_df.empty:
            print(f"  Aucun value bet trouvé")
            per_league_results[league] = {"n_bets": 0, "roi_percent": 0, "hit_rate": 0, "total_pnl": 0}
            continue

        per_league_results[league] = {
            "n_bets": summary["n_bets"],
            "hit_rate": summary["hit_rate"],
            "roi_percent": summary["roi_percent"],
            "total_pnl": summary["total_pnl"],
            "drawdown_pct": summary["max_drawdown_pct"],
            "avg_edge_pct": summary["avg_edge_pct"],
            "avg_odds": summary["avg_odds"],
            "profit_factor": summary["profit_factor"],
        }
        print(f"  ROI {summary['roi_percent']:+.1f}% sur {summary['n_bets']} paris, hit {summary['hit_rate']*100:.1f}%, DD {summary['max_drawdown_pct']:.1f}%")
        all_bets.append(bets_df)

    print("\n" + "=" * 60)
    print("RÉSULTATS PER-LEAGUE")
    print("=" * 60)
    print(f"{'Ligue':18} | N | hit | ROI | P&L | edge moy | DD")
    for league, r in per_league_results.items():
        print(f"  {league:18} | {r['n_bets']:4d} | {r.get('hit_rate', 0)*100:5.1f}% | {r['roi_percent']:+6.1f}% | {r['total_pnl']:+7.0f} | {r.get('avg_edge_pct', 0):.1f}% | {r.get('drawdown_pct', 0):.1f}%")

    # Sauvegarde
    out = {
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "params": {
            "edge_threshold": edge_threshold, "edge_max": edge_max,
            "kelly_fraction": kelly_fraction, "bankroll": bankroll,
        },
        "per_league": per_league_results,
    }
    (ARTIFACTS_DIR / "per_league_summary.json").write_text(json.dumps(out, indent=2))
    print(f"\n✓ Saved : {ARTIFACTS_DIR / 'per_league_summary.json'}")

    if all_bets:
        all_bets_df = pd.concat(all_bets, ignore_index=True)
        all_bets_df.to_csv(ARTIFACTS_DIR / "per_league_bets.csv", index=False)
        print(f"✓ Bets   : {ARTIFACTS_DIR / 'per_league_bets.csv'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--edge-threshold", type=float, default=0.08)
    parser.add_argument("--edge-max", type=float, default=0.20)
    parser.add_argument("--kelly-fraction", type=float, default=0.25)
    args = parser.parse_args()
    run(args.edge_threshold, args.edge_max, args.kelly_fraction)
