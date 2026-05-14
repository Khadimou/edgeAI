"""
Modèle NBA : XGBoost binaire (HOME win vs AWAY win) + calibration isotonique.
"""
import os
from datetime import datetime
from pathlib import Path

import numpy as np
from sklearn.calibration import CalibratedClassifierCV
from xgboost import XGBClassifier

from .nba_features import NBAFeatures

MODEL_DIR = Path(os.getenv("MODEL_DIR", "/app/artifacts/models"))
MODEL_DIR.mkdir(parents=True, exist_ok=True)

# Seuils plus stricts qu'en foot — NBA est plus prévisible
NBA_MIN_ACCURACY = 0.60
NBA_MAX_LOG_LOSS = 0.67


class EdgeAIModelNBA:
    """Modèle binaire pour la NBA. Sortie : (prob_home, prob_away)."""

    def __init__(self, version: str | None = None):
        self.version = version or "nba_" + datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        self.model: CalibratedClassifierCV | None = None
        self._feature_names = NBAFeatures.feature_names()

    def predict(self, features: NBAFeatures) -> dict:
        if self.model is None:
            raise RuntimeError("Modèle NBA non chargé")
        X = features.to_array().reshape(1, -1)
        # Le modèle a été entraîné avec labels {0=HOME, 1=AWAY}
        proba = self.model.predict_proba(X)[0]
        return {
            "prob_home": round(float(proba[0]), 4),
            "prob_away": round(float(proba[1]), 4),
            "prob_draw": 0.0,  # toujours 0 en NBA
            "confidence": round(float(max(proba)), 4),
            "shap_values": None,
            "model_version": self.version,
            "sport": "NBA",
        }

    def save(self, path: Path | None = None) -> Path:
        import joblib
        path = path or MODEL_DIR / f"model_{self.version}.joblib"
        joblib.dump({"model": self.model, "version": self.version, "sport": "NBA"}, path)
        return path

    @classmethod
    def load(cls, path: Path) -> "EdgeAIModelNBA":
        import joblib
        data = joblib.load(path)
        instance = cls(version=data["version"])
        instance.model = data["model"]
        return instance
