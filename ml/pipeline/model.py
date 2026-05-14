"""
Pipeline ML : XGBoost multi-classe + calibration isotonique.
Validation via TimeSeriesSplit pour éviter le data leakage temporel.
"""
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import shap

try:
    import mlflow
    import mlflow.xgboost
    _MLFLOW_AVAILABLE = True
except ImportError:
    _MLFLOW_AVAILABLE = False
from sklearn.calibration import CalibratedClassifierCV
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import log_loss, brier_score_loss, accuracy_score
from xgboost import XGBClassifier

from .features import MatchFeatures, compute_features_from_history

OUTCOMES = ["HOME", "DRAW", "AWAY"]
MODEL_DIR = Path(os.getenv("MODEL_DIR", "/artifacts/models"))
MODEL_DIR.mkdir(parents=True, exist_ok=True)

# Seuils de qualité minimaux (le modèle doit battre la baseline naïve)
# Seuils phase 1 (données limitées) — montez à 0.54 / 0.95 avec 6000+ matchs
MIN_ACCURACY = 0.44
MAX_LOG_LOSS = 1.10
MAX_BRIER_SCORE = 0.26


class EdgeAIModel:
    def __init__(self, version: str | None = None):
        self.version = version or datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        self.model: CalibratedClassifierCV | None = None
        self.explainer: shap.TreeExplainer | None = None
        self._feature_names = MatchFeatures.feature_names()

    def train(self, X: np.ndarray, y: np.ndarray, n_splits: int = 5) -> dict:
        """
        Entraîne XGBoost avec TimeSeriesSplit et calibration isotonique.
        Retourne les métriques d'évaluation.
        """
        tscv = TimeSeriesSplit(n_splits=n_splits)
        oof_preds = np.zeros((len(y), 3))

        xgb_params = {
            "n_estimators": 500,
            "max_depth": 5,
            "learning_rate": 0.05,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "min_child_weight": 3,
            "gamma": 0.1,
            "reg_alpha": 0.1,
            "reg_lambda": 1.0,
            "objective": "multi:softprob",
            "num_class": 3,
            "eval_metric": "mlogloss",
            "random_state": 42,
            "n_jobs": -1,
        }

        for fold, (train_idx, val_idx) in enumerate(tscv.split(X)):
            X_train, X_val = X[train_idx], X[val_idx]
            y_train, y_val = y[train_idx], y[val_idx]

            base = XGBClassifier(**xgb_params)
            cal = CalibratedClassifierCV(base, method="sigmoid", cv=3)
            cal.fit(X_train, y_train)
            oof_preds[val_idx] = cal.predict_proba(X_val)

        # Entraînement final sur tout le jeu
        base_final = XGBClassifier(**xgb_params)
        self.model = CalibratedClassifierCV(base_final, method="sigmoid", cv=3)
        self.model.fit(X, y)

        # SHAP explainer sur le modèle de base XGBoost
        try:
            inner = self.model.calibrated_classifiers_[0].estimator
            self.explainer = shap.TreeExplainer(inner)
        except Exception:
            self.explainer = None

        # Métriques OOF
        valid_mask = oof_preds.sum(axis=1) > 0
        y_valid = y[valid_mask]
        preds_valid = oof_preds[valid_mask]

        ll = log_loss(y_valid, preds_valid)
        acc = accuracy_score(y_valid, preds_valid.argmax(axis=1))
        bs = brier_score_loss(
            (y_valid == 0).astype(int),
            preds_valid[:, 0],
        )

        features_hash = hashlib.md5(
            json.dumps(self._feature_names).encode()
        ).hexdigest()

        return {
            "version": self.version,
            "log_loss": round(ll, 4),
            "accuracy": round(acc, 4),
            "brier_score": round(bs, 4),
            "features_hash": features_hash,
            "n_samples": len(X),
            "passes_threshold": bool(ll < MAX_LOG_LOSS and acc > MIN_ACCURACY and bs < MAX_BRIER_SCORE),
        }

    def predict(self, features: MatchFeatures) -> dict:
        """Génère les probabilités H/D/A + valeurs SHAP."""
        if self.model is None:
            raise RuntimeError("Modèle non chargé")

        X = features.to_array().reshape(1, -1)
        proba = self.model.predict_proba(X)[0]

        shap_values = None
        if self.explainer is not None:
            try:
                sv = self.explainer.shap_values(X)
                if isinstance(sv, list):
                    sv = sv[0]
                shap_values = {
                    name: round(float(val), 4)
                    for name, val in zip(self._feature_names, sv[0])
                }
            except Exception:
                pass

        return {
            "prob_home": round(float(proba[0]), 4),
            "prob_draw": round(float(proba[1]), 4),
            "prob_away": round(float(proba[2]), 4),
            "confidence": round(float(max(proba)), 4),
            "shap_values": shap_values,
            "model_version": self.version,
        }

    def save(self, path: Path | None = None) -> Path:
        import joblib
        path = path or MODEL_DIR / f"model_{self.version}.joblib"
        joblib.dump({"model": self.model, "explainer": self.explainer, "version": self.version}, path)
        return path

    @classmethod
    def load(cls, path: Path) -> "EdgeAIModel":
        import joblib
        data = joblib.load(path)
        instance = cls(version=data["version"])
        instance.model = data["model"]
        instance.explainer = data.get("explainer")
        return instance


def train_and_log(X: np.ndarray, y: np.ndarray, tracking_uri: str | None = None) -> dict:
    """Entraîne avec MLflow si disponible, sinon entraîne directement."""
    model = EdgeAIModel()
    metrics = model.train(X, y)

    if _MLFLOW_AVAILABLE:
        if tracking_uri:
            mlflow.set_tracking_uri(tracking_uri)
        mlflow.set_experiment("edgeai_football_predictions")
        with mlflow.start_run():
            mlflow.log_metrics({
                "log_loss": metrics["log_loss"],
                "accuracy": metrics["accuracy"],
                "brier_score": metrics["brier_score"],
            })
            mlflow.log_param("n_samples", metrics["n_samples"])
            mlflow.log_param("version", metrics["version"])
            if metrics["passes_threshold"]:
                artifact_path = model.save()
                mlflow.log_artifact(str(artifact_path))
                mlflow.set_tag("status", "promoted")
            else:
                mlflow.set_tag("status", "rejected")

    return {**metrics, "model": model if metrics["passes_threshold"] else None}
