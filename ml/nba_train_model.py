"""
Entraînement du modèle NBA binaire avec validation temporelle + Optuna.

Usage:
    python nba_train_model.py
    python nba_train_model.py --tune --n-trials 50
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from pipeline.nba_features import NBAFeatures
from pipeline.nba_model import EdgeAIModelNBA, NBA_MAX_LOG_LOSS, NBA_MIN_ACCURACY

from sklearn.calibration import CalibratedClassifierCV
from sklearn.metrics import log_loss, accuracy_score, brier_score_loss
from sklearn.model_selection import TimeSeriesSplit
from xgboost import XGBClassifier

DATA_DIR = Path(__file__).parent / "data"
INPUT = DATA_DIR / "features" / "nba_dataset.csv"
ARTIFACTS_DIR = Path(__file__).parent / "artifacts" / "models"
FEATURE_COLS = NBAFeatures.feature_names()

DEFAULT_PARAMS = {
    "n_estimators": 400, "max_depth": 5, "learning_rate": 0.05,
    "subsample": 0.8, "colsample_bytree": 0.8,
    "objective": "binary:logistic", "eval_metric": "logloss",
    "random_state": 42, "n_jobs": -1,
}


def load_dataset(path: Path):
    df = pd.read_csv(path, parse_dates=["match_date"]).sort_values("match_date").reset_index(drop=True)
    X = df[FEATURE_COLS].values.astype(np.float32)
    # 0 = home, 2 = away dans le CSV → on binarise : 0 = home win, 1 = away win
    y = (df["label"].values == 2).astype(int)
    print(f"  {len(df)} exemples | {X.shape[1]} features")
    print(f"  Période : {df['match_date'].min().date()} → {df['match_date'].max().date()}")
    print(f"  Distribution : HOME {(y==0).sum()} ({100*(y==0).mean():.1f}%), AWAY {(y==1).sum()} ({100*(y==1).mean():.1f}%)")
    return X, y


def train_with_params(X, y, params):
    p = {**DEFAULT_PARAMS, **(params or {})}
    p.update({"objective": "binary:logistic", "eval_metric": "logloss", "random_state": 42, "n_jobs": -1})

    tscv = TimeSeriesSplit(n_splits=5)
    oof = np.zeros((len(y), 2))

    for fold, (train_idx, val_idx) in enumerate(tscv.split(X)):
        clf = CalibratedClassifierCV(XGBClassifier(**p), method="sigmoid", cv=3)
        clf.fit(X[train_idx], y[train_idx])
        oof[val_idx] = clf.predict_proba(X[val_idx])

    valid = oof.sum(axis=1) > 0
    ll = log_loss(y[valid], oof[valid])
    acc = accuracy_score(y[valid], oof[valid].argmax(axis=1))
    brier = brier_score_loss(y[valid], oof[valid][:, 1])

    # Modèle final sur tout le dataset
    model = EdgeAIModelNBA()
    model.model = CalibratedClassifierCV(XGBClassifier(**p), method="sigmoid", cv=3)
    model.model.fit(X, y)

    metrics = {
        "version": model.version,
        "log_loss": round(float(ll), 4),
        "accuracy": round(float(acc), 4),
        "brier_score": round(float(brier), 4),
        "n_samples": len(X),
        "passes_threshold": float(ll) < NBA_MAX_LOG_LOSS and float(acc) > NBA_MIN_ACCURACY,
    }
    return model, metrics


def tune(X, y, n_trials: int):
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)

    tscv = TimeSeriesSplit(n_splits=5)

    def objective(trial):
        p = {
            "n_estimators": trial.suggest_int("n_estimators", 200, 700),
            "max_depth": trial.suggest_int("max_depth", 3, 7),
            "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.15, log=True),
            "subsample": trial.suggest_float("subsample", 0.6, 1.0),
            "colsample_bytree": trial.suggest_float("colsample_bytree", 0.6, 1.0),
            "min_child_weight": trial.suggest_int("min_child_weight", 1, 10),
            "reg_alpha": trial.suggest_float("reg_alpha", 0.0, 1.0),
            "reg_lambda": trial.suggest_float("reg_lambda", 0.5, 2.0),
            "objective": "binary:logistic", "eval_metric": "logloss",
            "random_state": 42, "n_jobs": -1,
        }
        losses = []
        for train_idx, val_idx in tscv.split(X):
            clf = CalibratedClassifierCV(XGBClassifier(**p), method="sigmoid", cv=3)
            clf.fit(X[train_idx], y[train_idx])
            losses.append(log_loss(y[val_idx], clf.predict_proba(X[val_idx])))
        return float(np.mean(losses))

    study = optuna.create_study(direction="minimize")
    study.optimize(objective, n_trials=n_trials, show_progress_bar=True)
    print(f"\n  Best log-loss : {study.best_value:.4f}")
    return study.best_params


def save_model(model, metrics):
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    path = ARTIFACTS_DIR / f"model_{model.version}.joblib"
    model.save(path)
    (ARTIFACTS_DIR / f"metrics_{model.version}.json").write_text(json.dumps(metrics, indent=2))
    import shutil
    shutil.copy2(path, ARTIFACTS_DIR / "model_nba_latest.joblib")
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=INPUT)
    parser.add_argument("--tune", action="store_true")
    parser.add_argument("--n-trials", type=int, default=30)
    args = parser.parse_args()

    if not args.input.exists():
        print(f"ERREUR : {args.input} introuvable. Lancez nba_build_features.py d'abord.")
        sys.exit(1)

    print(f"Lecture : {args.input}")
    X, y = load_dataset(args.input)

    if args.tune:
        print(f"\n── Optuna ({args.n_trials} trials) ──")
        best_params = tune(X, y, args.n_trials)
        print(f"\n── Entraînement final ──")
        model, metrics = train_with_params(X, y, best_params)
        metrics["best_optuna_params"] = best_params
    else:
        print(f"\n── Entraînement baseline ──")
        model, metrics = train_with_params(X, y, None)

    print(f"\n── Résultats ──")
    print(f"  Log-loss    : {metrics['log_loss']:.4f}  (cible < {NBA_MAX_LOG_LOSS})")
    print(f"  Accuracy    : {metrics['accuracy']:.4f}  (cible > {NBA_MIN_ACCURACY})")
    print(f"  Brier score : {metrics['brier_score']:.4f}")
    print(f"  Samples     : {metrics['n_samples']}")

    path = save_model(model, metrics)
    if metrics["passes_threshold"]:
        print(f"\n✓ Modèle NBA déployé : {path}")
    else:
        print(f"\n⚠ Seuils non atteints — modèle sauvegardé pour inspection : {path}")
