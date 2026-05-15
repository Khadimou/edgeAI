"""
Évaluation walk-forward du modèle WC sur les 4 dernières éditions.

Pour CHAQUE WC (2010, 2014, 2018, 2022) :
- Train : tous les matchs internationaux compétitifs avant la WC
- Eval  : 64 matchs de cette WC

Si le modèle bat la baseline naïve (45% home) de >5 pts sur PLUSIEURS WC,
on a une vraie validation. Si c'est juste 2022, c'est peut-être de la chance.

Usage:
    python wc_eval_history.py
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.metrics import log_loss, accuracy_score
from xgboost import XGBClassifier

sys.path.insert(0, str(Path(__file__).parent))
from pipeline.wc_features import WCFeatures

DATA_DIR = Path(__file__).parent / "data"
WC_DATASET = DATA_DIR / "features" / "wc_dataset.csv"

# Fenêtres temporelles : (year, train_cutoff_date, eval_start, eval_end)
WC_EDITIONS = [
    (2010, "2010-06-01", "2010-06-11", "2010-07-12"),
    (2014, "2014-06-01", "2014-06-12", "2014-07-14"),
    (2018, "2018-06-01", "2018-06-14", "2018-07-16"),
    (2022, "2022-11-01", "2022-11-20", "2022-12-19"),
]

PARAMS = {
    "n_estimators": 227, "max_depth": 3, "learning_rate": 0.019,
    "subsample": 0.62, "colsample_bytree": 0.81,
    "min_child_weight": 3, "reg_alpha": 0.31, "reg_lambda": 1.16,
    "objective": "multi:softprob", "num_class": 3,
    "eval_metric": "mlogloss", "random_state": 42, "n_jobs": -1,
}


def eval_one_wc(df: pd.DataFrame, year: int, cutoff: str, eval_start: str, eval_end: str) -> dict:
    """Train < cutoff, eval sur la WC en [eval_start, eval_end]."""
    feature_cols = WCFeatures.feature_names()
    df["date"] = pd.to_datetime(df["date"])

    # Train : compétitions sérieuses, après 1990, AVANT cutoff
    train_df = df[
        (df["date"] >= "1990-01-01") &
        (df["date"] < cutoff) &
        (df["tournament"] != "Friendly")
    ].sort_values("date").reset_index(drop=True)

    # Eval : tous les matchs WC de cette édition
    eval_df = df[
        (df["tournament"] == "FIFA World Cup") &
        (df["date"] >= eval_start) &
        (df["date"] <= eval_end)
    ].sort_values("date").reset_index(drop=True)

    if len(eval_df) < 20:
        return {"year": year, "skip": True, "reason": f"eval has {len(eval_df)} matches"}

    X_train = train_df[feature_cols].values.astype(np.float32)
    y_train = train_df["label"].values.astype(int)
    X_eval = eval_df[feature_cols].values.astype(np.float32)
    y_eval = eval_df["label"].values.astype(int)

    model = CalibratedClassifierCV(XGBClassifier(**PARAMS), method="sigmoid", cv=3)
    model.fit(X_train, y_train)
    proba = model.predict_proba(X_eval)
    preds = proba.argmax(axis=1)
    acc = float(accuracy_score(y_eval, preds))
    ll = float(log_loss(y_eval, proba))
    naive = float((y_eval == 0).mean())

    return {
        "year": year,
        "train_n": len(train_df),
        "eval_n": len(eval_df),
        "accuracy": round(acc, 4),
        "naive_baseline": round(naive, 4),
        "improvement_pts": round((acc - naive) * 100, 2),
        "log_loss": round(ll, 4),
    }


def main():
    if not WC_DATASET.exists():
        print(f"ERREUR : {WC_DATASET} introuvable. Lance wc_pipeline.py d'abord.")
        sys.exit(1)

    print("Lecture wc_dataset.csv...")
    df = pd.read_csv(WC_DATASET, parse_dates=["date"])
    df["is_wc"] = df["is_wc"].astype(bool)
    print(f"  {len(df)} matchs au total\n")

    print("=" * 70)
    print(f"{'Édition':10} | {'Train':>7} | {'Eval':>5} | {'Acc':>7} | {'Naïve':>7} | {'Δ pts':>7} | {'LogL':>6}")
    print("=" * 70)

    results = []
    for year, cutoff, eval_start, eval_end in WC_EDITIONS:
        r = eval_one_wc(df, year, cutoff, eval_start, eval_end)
        if r.get("skip"):
            print(f"  {year} skipped : {r['reason']}")
            continue
        print(f"  {r['year']:8} | {r['train_n']:>7} | {r['eval_n']:>5} | "
              f"{r['accuracy']*100:>6.1f}% | {r['naive_baseline']*100:>6.1f}% | "
              f"{r['improvement_pts']:>+6.1f} | {r['log_loss']:.4f}")
        results.append(r)

    if results:
        avg_acc = np.mean([r["accuracy"] for r in results])
        avg_naive = np.mean([r["naive_baseline"] for r in results])
        avg_imp = avg_acc - avg_naive
        print("-" * 70)
        print(f"  {'MOYENNE':8} |         |       | "
              f"{avg_acc*100:>6.1f}% | {avg_naive*100:>6.1f}% | "
              f"{avg_imp*100:>+6.1f} |")
        print()

        # Verdict global
        positives = sum(1 for r in results if r["improvement_pts"] > 5)
        if positives >= 3:
            print(f"  ✅ ROBUSTE : {positives}/{len(results)} WC où le modèle bat la baseline de >5pts")
        elif positives >= 2:
            print(f"  🟡 MIXTE : {positives}/{len(results)} WC où le modèle bat la baseline de >5pts")
        else:
            print(f"  ❌ FRAGILE : seulement {positives}/{len(results)} WC où le modèle bat la baseline")


if __name__ == "__main__":
    main()
