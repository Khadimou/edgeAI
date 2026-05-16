"""
Backtest ensemble XGBoost + Dixon-Coles pour le 1X2.

Stratégie :
- Pour chaque fold TimeSeriesSplit, on entraîne XGB sur train, on fit DC sur train
- Sur val, on génère 2 probabilités (XGB, DC) puis on blend :
  proba_final = w * XGB + (1-w) * DC  (puis renormaliser)
- On simule le Kelly betting sur les cotes pour mesurer ROI
- On compare 3 scénarios :
  1. XGB alone (baseline actuelle)
  2. DC alone
  3. Ensemble w=0.5

Usage :
    python backtest_ensemble.py --weight 0.5
    python backtest_ensemble.py --sweep   # essaie w in [0.0, 0.25, 0.5, 0.75, 1.0]
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.metrics import log_loss, accuracy_score
from sklearn.model_selection import TimeSeriesSplit
from xgboost import XGBClassifier

sys.path.insert(0, str(Path(__file__).parent))
from dixon_coles import DixonColes
from pipeline.features import MatchFeatures
from backtest import (
    LEAGUE_FD_CO_UK, fetch_all_historical_odds,
    merge_with_features, simulate,
)


def train_xgb_oof(X: np.ndarray, y: np.ndarray, n_splits: int = 3) -> np.ndarray:
    """Returns OOF probas (N, 3) for HOME/DRAW/AWAY."""
    params = {
        "n_estimators": 300, "max_depth": 5, "learning_rate": 0.05,
        "subsample": 0.8, "colsample_bytree": 0.8,
        "objective": "multi:softprob", "num_class": 3,
        "eval_metric": "mlogloss", "random_state": 42, "n_jobs": -1,
    }
    oof = np.zeros((len(y), 3))
    tscv = TimeSeriesSplit(n_splits=n_splits)
    for fold, (train_idx, val_idx) in enumerate(tscv.split(X), 1):
        y_train = y[train_idx]
        if len(np.unique(y_train)) < 3:
            print(f"  [XGB] fold {fold}: missing classes in train, skip")
            continue
        clf = CalibratedClassifierCV(XGBClassifier(**params), method="sigmoid", cv=3)
        clf.fit(X[train_idx], y_train)
        oof[val_idx] = clf.predict_proba(X[val_idx])
        print(f"  [XGB] fold {fold}/{n_splits} done")
    return oof


def predict_dc_oof(merged_df: pd.DataFrame, n_splits: int = 3) -> np.ndarray:
    """Returns OOF probas (N, 3) using Dixon-Coles fit on train portion.

    Pour chaque fold, on fit DC sur les matchs train (chronologiquement avant val),
    puis on predict sur val matches.

    Si merged_df n'a pas home_score/away_score (dataset features sans scores bruts),
    on les recupere depuis matches.csv via (date, home_team, away_team).
    """
    merged_df = merged_df.sort_values("match_date").reset_index(drop=True)
    if "home_score" not in merged_df.columns or "away_score" not in merged_df.columns:
        raw = pd.read_csv(Path(__file__).parent / "data" / "raw" / "matches.csv",
                          parse_dates=["match_date"])
        raw["_date"] = pd.to_datetime(raw["match_date"]).dt.date
        raw_keys = raw[["_date", "home_team", "away_team", "home_score", "away_score"]]
        merged_df["_date"] = pd.to_datetime(merged_df["match_date"]).dt.date
        merged_df = merged_df.merge(raw_keys, on=["_date", "home_team", "away_team"], how="left")
        merged_df = merged_df.drop(columns=["_date"])
        merged_df = merged_df.dropna(subset=["home_score", "away_score"])
        merged_df = merged_df.sort_values("match_date").reset_index(drop=True)
    n = len(merged_df)
    oof = np.zeros((n, 3))
    tscv = TimeSeriesSplit(n_splits=n_splits)
    for fold, (train_idx, val_idx) in enumerate(tscv.split(np.arange(n)), 1):
        train_df = merged_df.iloc[train_idx]
        val_df = merged_df.iloc[val_idx]
        dc = DixonColes()
        dc.fit(train_df, decay_half_life=180, verbose=False)
        # Predict each val match
        for local_i, global_i in enumerate(val_idx):
            row = val_df.iloc[local_i]
            try:
                p = dc.predict(row["home_team"], row["away_team"])
                oof[global_i] = [p["prob_home"], p["prob_draw"], p["prob_away"]]
            except Exception:
                oof[global_i] = [0.45, 0.25, 0.30]  # fallback à priors
        print(f"  [DC] fold {fold}/{n_splits} done ({len(val_idx)} predictions)")
    return oof


def blend(p_xgb: np.ndarray, p_dc: np.ndarray, w: float) -> np.ndarray:
    """proba_final = w * XGB + (1-w) * DC, puis renormalise."""
    blended = w * p_xgb + (1 - w) * p_dc
    sums = blended.sum(axis=1, keepdims=True)
    sums = np.where(sums > 0, sums, 1)
    return blended / sums


def metrics_summary(name: str, probas: np.ndarray, y_true: np.ndarray) -> dict:
    valid = probas.sum(axis=1) > 0
    if valid.sum() == 0:
        return {"name": name, "log_loss": None, "accuracy": None, "n": 0}
    y_pred = probas[valid].argmax(axis=1)
    ll = log_loss(y_true[valid], probas[valid])
    acc = accuracy_score(y_true[valid], y_pred)
    return {"name": name, "log_loss": round(ll, 4), "accuracy": round(acc, 4),
            "n": int(valid.sum())}


def backtest_with_probas(merged: pd.DataFrame, probas: np.ndarray,
                          edge_threshold: float = 0.08, edge_max: float = 0.20,
                          kelly_fraction: float = 0.25) -> dict:
    """Simulate Kelly betting on cotes given probas array (N, 3)."""
    df = merged.copy().reset_index(drop=True)
    df["prob_home"] = probas[:, 0]
    df["prob_draw"] = probas[:, 1]
    df["prob_away"] = probas[:, 2]
    df = df[probas.sum(axis=1) > 0]
    if df.empty:
        return {"n_bets": 0, "roi_percent": 0}
    bets_df, summary = simulate(df, 100.0, edge_threshold, edge_max,
                                 kelly_fraction=kelly_fraction)
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--weight", type=float, default=0.5,
                        help="Poids du XGBoost dans l'ensemble (0=DC pur, 1=XGB pur)")
    parser.add_argument("--sweep", action="store_true",
                        help="Sweep w in [0.0, 0.25, 0.5, 0.75, 1.0]")
    parser.add_argument("--edge-threshold", type=float, default=0.08)
    parser.add_argument("--edge-max", type=float, default=0.20)
    args = parser.parse_args()

    DATA_DIR = Path(__file__).parent / "data"

    print("[1/4] Load dataset...")
    features = pd.read_csv(DATA_DIR / "features" / "dataset.csv", parse_dates=["match_date"])
    print(f"  features: {len(features)} rows")

    print("\n[2/4] Fetch cotes football-data.co.uk (2 dernières saisons)...")
    odds = fetch_all_historical_odds()
    print(f"  odds: {len(odds)} matchs")

    print("\n[3/4] Merge features + cotes...")
    merged = merge_with_features(features, odds)
    merged = merged.sort_values("match_date").reset_index(drop=True)
    print(f"  merged: {len(merged)} matchs")
    if len(merged) < 200:
        print("  Trop peu de matchs, abort")
        return

    feature_cols = MatchFeatures.feature_names_phase1()
    X = merged[feature_cols].values.astype(np.float32)
    y = merged["label"].values.astype(int)

    print("\n[4/4] OOF predictions XGBoost + DC...")
    print("\n--- XGBoost ---")
    p_xgb = train_xgb_oof(X, y, n_splits=3)
    print("\n--- Dixon-Coles ---")
    p_dc = predict_dc_oof(merged, n_splits=3)

    valid_mask = (p_xgb.sum(axis=1) > 0) & (p_dc.sum(axis=1) > 0)
    print(f"\n  Valid predictions (both XGB and DC): {valid_mask.sum()}")

    # Metrics comparison
    print("\n" + "=" * 60)
    print("LOG-LOSS / ACCURACY COMPARISON")
    print("=" * 60)
    weights_to_test = [0.0, 0.25, 0.5, 0.75, 1.0] if args.sweep else [0.0, args.weight, 1.0]
    for w in weights_to_test:
        p_blend = blend(p_xgb, p_dc, w)
        m = metrics_summary(f"w={w:.2f}", p_blend, y)
        print(f"  w={w:.2f}: log_loss={m['log_loss']}, accuracy={m['accuracy']}, n={m['n']}")

    # ROI backtest pour chaque w
    print("\n" + "=" * 60)
    print("ROI BACKTEST (edge [8%, 20%], Kelly 0.25)")
    print("=" * 60)
    for w in weights_to_test:
        p_blend = blend(p_xgb, p_dc, w)
        summary = backtest_with_probas(merged, p_blend,
                                       edge_threshold=args.edge_threshold,
                                       edge_max=args.edge_max)
        if summary and summary.get("n_bets", 0) > 0:
            print(f"  w={w:.2f}: ROI={summary['roi_percent']:+6.2f}%  "
                  f"n_bets={summary['n_bets']}  "
                  f"hit_rate={summary['hit_rate']*100:.1f}%  "
                  f"PF={summary.get('profit_factor', 0):.2f}")
        else:
            print(f"  w={w:.2f}: no bets")


if __name__ == "__main__":
    main()
