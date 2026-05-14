"""
Entraînement du modèle XGBoost edgeAI avec validation temporelle.
Optionnel : optimisation des hyperparamètres via Optuna.

Usage:
    python train_model.py
    python train_model.py --tune --n-trials 100
    python train_model.py --input data/features/dataset.csv
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from pipeline.features import MatchFeatures
from pipeline.model import EdgeAIModel, MAX_LOG_LOSS, MIN_ACCURACY

DEFAULT_INPUT = Path(__file__).parent / "data" / "features" / "dataset.csv"
ARTIFACTS_DIR = Path(__file__).parent / "artifacts" / "models"
FEATURE_COLS = MatchFeatures.feature_names()


def load_dataset(path: Path):
    print(f"Lecture : {path}")
    df = pd.read_csv(path, parse_dates=["match_date"])
    df = df.sort_values("match_date").reset_index(drop=True)

    X = df[FEATURE_COLS].values.astype(np.float32)
    y = df["label"].values.astype(int)

    print(f"  {len(df)} exemples | {X.shape[1]} features")
    print(f"  Période : {df['match_date'].min().date()} → {df['match_date'].max().date()}")

    dist = np.bincount(y)
    labels = ["HOME", "DRAW", "AWAY"]
    for i, c in enumerate(dist):
        print(f"  {labels[i]} : {c} ({100*c/len(y):.1f}%)")

    return X, y


def train_baseline(X: np.ndarray, y: np.ndarray) -> dict:
    print("\n── Entraînement baseline ──")
    model = EdgeAIModel()
    metrics = model.train(X, y)
    return metrics, model


def tune_and_train(X: np.ndarray, y: np.ndarray, n_trials: int) -> dict:
    try:
        import optuna
        optuna.logging.set_verbosity(optuna.logging.WARNING)
    except ImportError:
        print("optuna non installé — pip install optuna")
        return train_baseline(X, y)

    from sklearn.model_selection import TimeSeriesSplit
    from sklearn.metrics import log_loss
    from xgboost import XGBClassifier
    from sklearn.calibration import CalibratedClassifierCV

    print(f"\n── Optimisation Optuna ({n_trials} trials) ──")

    tscv = TimeSeriesSplit(n_splits=5)

    def objective(trial):
        params = {
            "n_estimators": trial.suggest_int("n_estimators", 200, 800),
            "max_depth": trial.suggest_int("max_depth", 3, 8),
            "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.2, log=True),
            "subsample": trial.suggest_float("subsample", 0.6, 1.0),
            "colsample_bytree": trial.suggest_float("colsample_bytree", 0.6, 1.0),
            "min_child_weight": trial.suggest_int("min_child_weight", 1, 10),
            "gamma": trial.suggest_float("gamma", 0.0, 1.0),
            "reg_alpha": trial.suggest_float("reg_alpha", 0.0, 1.0),
            "reg_lambda": trial.suggest_float("reg_lambda", 0.5, 2.0),
            "objective": "multi:softprob",
            "num_class": 3,
            "eval_metric": "mlogloss",
            "random_state": 42,
            "n_jobs": -1,
        }

        losses = []
        for train_idx, val_idx in tscv.split(X):
            clf = CalibratedClassifierCV(XGBClassifier(**params), method="sigmoid", cv=3)
            clf.fit(X[train_idx], y[train_idx])
            preds = clf.predict_proba(X[val_idx])
            losses.append(log_loss(y[val_idx], preds))

        return np.mean(losses)

    study = optuna.create_study(direction="minimize")
    study.optimize(objective, n_trials=n_trials, show_progress_bar=True)

    best = study.best_params
    print(f"\nMeilleurs params (log_loss={study.best_value:.4f}) :")
    for k, v in best.items():
        print(f"  {k}: {v}")

    # Entraîner le modèle final avec les meilleurs params
    from xgboost import XGBClassifier
    from sklearn.calibration import CalibratedClassifierCV
    import joblib

    print("\n── Entraînement final avec params optimisés ──")
    model = EdgeAIModel()

    xgb_params = {**best, "objective": "multi:softprob", "num_class": 3,
                  "eval_metric": "mlogloss", "random_state": 42, "n_jobs": -1}

    base = XGBClassifier(**xgb_params)
    model.model = CalibratedClassifierCV(base, method="sigmoid", cv=3)
    model.model.fit(X, y)

    try:
        import shap
        inner = model.model.calibrated_classifiers_[0].estimator
        model.explainer = shap.TreeExplainer(inner)
    except Exception:
        model.explainer = None

    # Métriques OOF sur les derniers splits
    from sklearn.metrics import accuracy_score, brier_score_loss
    from sklearn.model_selection import TimeSeriesSplit

    tscv2 = TimeSeriesSplit(n_splits=5)
    oof = np.zeros((len(y), 3))
    clf_final = CalibratedClassifierCV(XGBClassifier(**xgb_params), method="sigmoid", cv=3)
    for train_idx, val_idx in tscv2.split(X):
        clf_final.fit(X[train_idx], y[train_idx])
        oof[val_idx] = clf_final.predict_proba(X[val_idx])

    valid = oof.sum(axis=1) > 0
    from sklearn.metrics import log_loss as sk_log_loss
    ll = sk_log_loss(y[valid], oof[valid])
    acc = accuracy_score(y[valid], oof[valid].argmax(axis=1))

    metrics = {
        "version": model.version,
        "log_loss": round(ll, 4),
        "accuracy": round(acc, 4),
        "brier_score": round(brier_score_loss((y[valid] == 0).astype(int), oof[valid, 0]), 4),
        "n_samples": len(X),
        "passes_threshold": ll < MAX_LOG_LOSS and acc > MIN_ACCURACY,
        "best_optuna_params": best,
    }

    return metrics, model


def save_model(model: EdgeAIModel, metrics: dict):
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    path = ARTIFACTS_DIR / f"model_{model.version}.joblib"
    model.save(path)

    metrics_path = ARTIFACTS_DIR / f"metrics_{model.version}.json"
    with open(metrics_path, "w") as f:
        json.dump(metrics, f, indent=2)

    # Copie vers model_latest.joblib (symlink requiert droits admin sur Windows)
    import shutil
    latest = ARTIFACTS_DIR / "model_latest.joblib"
    shutil.copy2(path, latest)

    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Entraîne le modèle XGBoost edgeAI")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT,
                        help="CSV du dataset features")
    parser.add_argument("--tune", action="store_true",
                        help="Active l'optimisation Optuna des hyperparamètres")
    parser.add_argument("--n-trials", type=int, default=50,
                        help="Nombre de trials Optuna (défaut : 50)")
    args = parser.parse_args()

    if not args.input.exists():
        print(f"ERREUR : fichier introuvable : {args.input}")
        print("Lancez d'abord : python build_features.py")
        sys.exit(1)

    X, y = load_dataset(args.input)

    if len(X) < 200:
        print(f"\n⚠ Seulement {len(X)} exemples — entraînement possible mais modèle peu fiable.")
        print("  Recommandé : 2000+ exemples (5 ligues × 3-4 saisons)")

    if args.tune:
        metrics, model = tune_and_train(X, y, args.n_trials)
    else:
        metrics, model = train_baseline(X, y)

    print(f"\n── Résultats ──")
    print(f"  Log-loss    : {metrics['log_loss']:.4f}  (cible < {MAX_LOG_LOSS})")
    print(f"  Accuracy    : {metrics['accuracy']:.4f}  (cible > {MIN_ACCURACY})")
    print(f"  Brier score : {metrics['brier_score']:.4f}")
    print(f"  Samples     : {metrics['n_samples']}")

    if metrics.get("passes_threshold"):
        path = save_model(model, metrics)
        print(f"\n✓ Modèle sauvegardé : {path}")
        print(f"✓ Prêt pour le déploiement")
    else:
        print(f"\n✗ Modèle sous le seuil de qualité — non déployé")
        print(f"  Collectez plus de données ou ajustez les hyperparamètres.")
        # Sauvegarde quand même pour inspection
        path = save_model(model, metrics)
        print(f"  Fichier brut : {path}")
