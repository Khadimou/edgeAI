"""
Entraîne et sauvegarde les modèles 1X2 et OU 2.5 sur le dataset complet
(avec features Phase 1 : ELO + Pythag + streaks + form-vs-expected + BTTS).

Usage: python train_save_1x2_ou.py
"""
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.metrics import log_loss, accuracy_score, brier_score_loss
from sklearn.model_selection import TimeSeriesSplit
from xgboost import XGBClassifier

sys.path.insert(0, str(Path(__file__).parent))
from pipeline.features import MatchFeatures

DATA_DIR = Path(__file__).parent / "data"
MODEL_DIR = Path(__file__).parent / "artifacts" / "models"
MODEL_DIR.mkdir(parents=True, exist_ok=True)

# Params par défaut (PAS Optuna — la calibration log-loss dégrade le ROI)
PARAMS_1X2 = {
    "n_estimators": 300, "max_depth": 5, "learning_rate": 0.05,
    "subsample": 0.8, "colsample_bytree": 0.8,
    "objective": "multi:softprob", "num_class": 3,
    "eval_metric": "mlogloss", "random_state": 42, "n_jobs": -1,
}
PARAMS_OU = {
    "n_estimators": 300, "max_depth": 5, "learning_rate": 0.05,
    "subsample": 0.8, "colsample_bytree": 0.8,
    "objective": "binary:logistic", "eval_metric": "logloss",
    "random_state": 42, "n_jobs": -1,
}


def train_save(name: str, dataset_path: Path, params: dict,
               output_name: str, multi: bool = True):
    print(f"\n[{name}] Loading {dataset_path}...")
    df = pd.read_csv(dataset_path, parse_dates=["match_date"])
    df = df.sort_values("match_date").reset_index(drop=True)
    feature_cols = MatchFeatures.feature_names()
    X = df[feature_cols].values.astype(np.float32)
    y = df["label"].values.astype(int)
    print(f"  {len(X)} samples, {X.shape[1]} features, dist: {np.bincount(y)}")

    # OOF metrics
    n_classes = 3 if multi else 2
    oof = np.zeros((len(y), n_classes))
    tscv = TimeSeriesSplit(n_splits=5)
    for fold, (ti, vi) in enumerate(tscv.split(X)):
        clf = CalibratedClassifierCV(XGBClassifier(**params), method="sigmoid", cv=3)
        clf.fit(X[ti], y[ti])
        oof[vi] = clf.predict_proba(X[vi])
        print(f"  Fold {fold+1}/5 train={len(ti)} val={len(vi)} done")
    valid = oof.sum(axis=1) > 0
    ll = float(log_loss(y[valid], oof[valid]))
    acc = float(accuracy_score(y[valid], oof[valid].argmax(axis=1)))
    if multi:
        brier = float(brier_score_loss((y[valid] == 0).astype(int), oof[valid, 0]))
    else:
        brier = float(brier_score_loss(y[valid], oof[valid, 1]))
    print(f"  OOF metrics: log_loss={ll:.4f}, accuracy={acc:.4f}, brier={brier:.4f}")

    # Final model on ALL data
    final = CalibratedClassifierCV(XGBClassifier(**params), method="sigmoid", cv=3)
    final.fit(X, y)

    # SHAP explainer optional
    explainer = None
    try:
        import shap
        inner = final.calibrated_classifiers_[0].estimator
        explainer = shap.TreeExplainer(inner)
    except Exception:
        pass

    version = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    path = MODEL_DIR / f"model_{output_name}_{version}.joblib"
    payload = {"model": final, "version": version, "market": name}
    if explainer is not None:
        payload["explainer"] = explainer
    joblib.dump(payload, path)

    metrics = {
        "version": version, "market": name,
        "log_loss": round(ll, 4), "accuracy": round(acc, 4),
        "brier_score": round(brier, 4), "n_samples": len(X),
        "n_features": X.shape[1],
        "features_hash": _features_hash(feature_cols),
    }
    (MODEL_DIR / f"metrics_{output_name}_{version}.json").write_text(json.dumps(metrics, indent=2))

    # Symlink (copy) to latest
    latest = MODEL_DIR / f"model_{output_name}_latest.joblib"
    shutil.copy2(path, latest)
    print(f"  ✓ Saved: {path.name}")
    print(f"  ✓ Latest: {latest}")
    return metrics


def _features_hash(cols):
    import hashlib
    return hashlib.md5(str(cols).encode()).hexdigest()[:16]


if __name__ == "__main__":
    print("=" * 60)
    print("Train + save 1X2 et OU avec features Phase 1")
    print("=" * 60)
    print(f"  Features: {len(MatchFeatures.feature_names())} ({MatchFeatures.feature_names()[-5:]})")

    # 1X2 : utilise model_latest.joblib pour rétro-compat avec scheduler existant
    train_save("1X2", DATA_DIR / "features" / "dataset.csv",
               PARAMS_1X2, "", multi=True)

    # Renomme le fichier en model_latest.joblib (sans suffixe)
    src = MODEL_DIR / "model__latest.joblib"
    dst = MODEL_DIR / "model_latest.joblib"
    if src.exists():
        shutil.move(str(src), str(dst))
        print(f"  ✓ Renamed to {dst.name}")

    # OU 2.5
    train_save("OU_2_5", DATA_DIR / "features" / "ou_dataset.csv",
               PARAMS_OU, "ou", multi=False)

    print("\n" + "=" * 60)
    print("Done — modèles 1X2 + OU sauvegardés avec features Phase 1")
    print("=" * 60)
