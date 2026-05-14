"""
Entraînement de 5 modèles 1X2 par ligue (au lieu d'un global).

Hypothèse : chaque ligue a ses propres dynamiques (qualité de défense, % de nuls,
form recency-decay). Un modèle dédié devrait mieux capter ces patterns.

Usage:
    python train_per_league.py
    python train_per_league.py --tune --n-trials 20
"""
import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.metrics import log_loss, accuracy_score, brier_score_loss
from sklearn.model_selection import TimeSeriesSplit
from xgboost import XGBClassifier
import joblib

sys.path.insert(0, str(Path(__file__).parent))
from pipeline.features import MatchFeatures

DATA_DIR = Path(__file__).parent / "data"
INPUT = DATA_DIR / "features" / "dataset.csv"
ARTIFACTS_DIR = Path(__file__).parent / "artifacts" / "models"
FEATURE_COLS = MatchFeatures.feature_names()

LEAGUES = ["Premier League", "La Liga", "Bundesliga", "Serie A", "Ligue 1"]

DEFAULT_PARAMS = {
    "n_estimators": 300, "max_depth": 4, "learning_rate": 0.05,
    "subsample": 0.8, "colsample_bytree": 0.8,
    "objective": "multi:softprob", "num_class": 3,
    "eval_metric": "mlogloss",
    "random_state": 42, "n_jobs": -1,
}


def train_one_league(df: pd.DataFrame, league: str, n_trials: int = 0) -> tuple[object, dict]:
    sub = df[df["league"] == league].sort_values("match_date").reset_index(drop=True)
    if len(sub) < 500:
        print(f"  {league}: seulement {len(sub)} samples, skipped")
        return None, None

    X = sub[FEATURE_COLS].values.astype(np.float32)
    y = sub["label"].values.astype(int)

    print(f"  {league}: {len(sub)} samples, distribution {(y==0).mean()*100:.0f}/{(y==1).mean()*100:.0f}/{(y==2).mean()*100:.0f}")

    params = dict(DEFAULT_PARAMS)
    if n_trials > 0:
        params = _tune(X, y, n_trials)
        params.update({"objective": "multi:softprob", "num_class": 3,
                       "eval_metric": "mlogloss", "random_state": 42, "n_jobs": -1})

    tscv = TimeSeriesSplit(n_splits=5)
    oof = np.zeros((len(y), 3))
    for train_idx, val_idx in tscv.split(X):
        clf = CalibratedClassifierCV(XGBClassifier(**params), method="sigmoid", cv=3)
        clf.fit(X[train_idx], y[train_idx])
        oof[val_idx] = clf.predict_proba(X[val_idx])

    valid = oof.sum(axis=1) > 0
    ll = float(log_loss(y[valid], oof[valid]))
    acc = float(accuracy_score(y[valid], oof[valid].argmax(axis=1)))
    brier = float(brier_score_loss((y[valid] == 0).astype(int), oof[valid][:, 0]))

    # Modèle final sur tout
    version = f"perleague_{league.replace(' ', '_').lower()}_" + datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    final = CalibratedClassifierCV(XGBClassifier(**params), method="sigmoid", cv=3)
    final.fit(X, y)

    metrics = {
        "version": version,
        "league": league,
        "log_loss": round(ll, 4),
        "accuracy": round(acc, 4),
        "brier_score": round(brier, 4),
        "n_samples": len(X),
    }
    return final, metrics


def _tune(X, y, n_trials):
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    tscv = TimeSeriesSplit(n_splits=5)

    def objective(trial):
        p = {
            "n_estimators": trial.suggest_int("n_estimators", 150, 500),
            "max_depth": trial.suggest_int("max_depth", 3, 6),
            "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.15, log=True),
            "subsample": trial.suggest_float("subsample", 0.6, 1.0),
            "colsample_bytree": trial.suggest_float("colsample_bytree", 0.6, 1.0),
            "min_child_weight": trial.suggest_int("min_child_weight", 1, 10),
            "reg_alpha": trial.suggest_float("reg_alpha", 0.0, 1.0),
            "reg_lambda": trial.suggest_float("reg_lambda", 0.5, 2.0),
            "objective": "multi:softprob", "num_class": 3,
            "eval_metric": "mlogloss", "random_state": 42, "n_jobs": -1,
        }
        losses = []
        for ti, vi in tscv.split(X):
            clf = CalibratedClassifierCV(XGBClassifier(**p), method="sigmoid", cv=3)
            clf.fit(X[ti], y[ti])
            losses.append(log_loss(y[vi], clf.predict_proba(X[vi])))
        return float(np.mean(losses))

    study = optuna.create_study(direction="minimize")
    study.optimize(objective, n_trials=n_trials, show_progress_bar=False)
    return study.best_params


def save(model, metrics: dict):
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    league_slug = metrics["league"].replace(" ", "_").lower()
    path = ARTIFACTS_DIR / f"model_{metrics['version']}.joblib"
    joblib.dump({"model": model, "version": metrics["version"], "league": metrics["league"]}, path)
    (ARTIFACTS_DIR / f"metrics_{metrics['version']}.json").write_text(json.dumps(metrics, indent=2))
    # latest pointer
    shutil.copy2(path, ARTIFACTS_DIR / f"model_perleague_{league_slug}_latest.joblib")
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tune", action="store_true")
    parser.add_argument("--n-trials", type=int, default=20)
    args = parser.parse_args()

    print(f"Lecture : {INPUT}")
    df = pd.read_csv(INPUT, parse_dates=["match_date"])
    print(f"  {len(df)} samples, {df['league'].nunique()} ligues\n")

    results = {}
    for league in LEAGUES:
        print(f"\n── {league} ──")
        model, metrics = train_one_league(df, league, args.n_trials if args.tune else 0)
        if model:
            path = save(model, metrics)
            print(f"  log_loss={metrics['log_loss']:.4f}, acc={metrics['accuracy']:.4f}, brier={metrics['brier_score']:.4f}")
            print(f"  saved: {path.name}")
            results[league] = metrics

    print("\n" + "=" * 60)
    print("RÉSUMÉ PER-LEAGUE")
    print("=" * 60)
    print(f"{'Ligue':18} | log_loss | accuracy | brier | n")
    for league, m in results.items():
        print(f"  {league:18} | {m['log_loss']:.4f}   | {m['accuracy']:.4f}   | {m['brier_score']:.4f} | {m['n_samples']}")
