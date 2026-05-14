"""
Auto-trainer quotidien : déclenché après chaque pipeline, réentraîne le modèle
si assez de nouveaux matchs terminés sont disponibles (défaut : 50).

Flux :
  1. Lit training_state.json (dernière version, log-loss, meilleurs params Optuna)
  2. Vérifie cooldown (24h) + seuil de nouveaux samples
  3. Charge tous les matchs FINISHED depuis la DB
  4. Reconstruit le dataset de features en mémoire
  5. Entraîne XGBoost avec les meilleurs params connus (pas d'Optuna)
  6. Déploie le nouveau modèle si log-loss s'améliore
  7. Écrit dans la table model_versions
"""
import json
import os
import shutil
import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from .features import (
    compute_features_from_history,
    compute_standings_from_history,
    MatchFeatures,
)
from .model import EdgeAIModel

log = structlog.get_logger()

MODEL_DIR = Path(os.getenv("MODEL_DIR", "/app/artifacts/models"))
STATE_FILE = MODEL_DIR / "training_state.json"
RETRAIN_MIN_SAMPLES = int(os.getenv("RETRAIN_MIN_SAMPLES", "50"))
RETRAIN_COOLDOWN_HOURS = int(os.getenv("RETRAIN_COOLDOWN_HOURS", "24"))
FEATURE_COLS = MatchFeatures.feature_names()


# ── State helpers ──────────────────────────────────────────────────────────────

def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {
        "last_trained": "2020-01-01T00:00:00",
        "current_log_loss": 9999.0,
        "current_accuracy": 0.0,
        "samples_count": 0,
        "best_optuna_params": None,
    }


def _save_state(state: dict):
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Feature builder (in-memory, mirrors build_features.py) ────────────────────

def _build_dataset(rows: list, min_history: int = 3) -> tuple[np.ndarray, np.ndarray]:
    """
    Construit X, y depuis une liste de tuples DB.
    Colonnes attendues : home_team, away_team, home_score, away_score, match_date,
                        league, ht_home_score, ht_away_score,
                        home_yellow_cards, away_yellow_cards
    """
    df = pd.DataFrame(rows, columns=[
        "home_team", "away_team", "home_score", "away_score", "date",
        "league", "ht_home_score", "ht_away_score",
        "home_yellow_cards", "away_yellow_cards",
    ])
    df["date"] = pd.to_datetime(df["date"])
    df["home_score"] = pd.to_numeric(df["home_score"], errors="coerce")
    df["away_score"] = pd.to_numeric(df["away_score"], errors="coerce")
    df = df.dropna(subset=["home_score", "away_score"])
    df["home_score"] = df["home_score"].astype(int)
    df["away_score"] = df["away_score"].astype(int)
    df = df.sort_values("date").reset_index(drop=True)

    feature_rows = []
    labels = []
    skipped = 0

    for i, row in df.iterrows():
        past = df[df["date"] < row["date"]]

        home_hist_count = len(past[
            (past["home_team"] == row["home_team"]) | (past["away_team"] == row["home_team"])
        ])
        away_hist_count = len(past[
            (past["home_team"] == row["away_team"]) | (past["away_team"] == row["away_team"])
        ])

        if home_hist_count < min_history or away_hist_count < min_history:
            skipped += 1
            continue

        # Classement dynamique (sans data leakage)
        standings, total_teams = compute_standings_from_history(past, row["date"], row["league"])

        # Cotes pour ce match (si disponibles dans le passé — non pour l'instant)
        feat = compute_features_from_history(
            home_team=row["home_team"],
            away_team=row["away_team"],
            match_date=row["date"],
            historical_df=past,
            standings=standings,
            total_teams=total_teams,
        )

        feature_rows.append(feat.to_array())

        hs, as_ = row["home_score"], row["away_score"]
        if hs > as_:
            labels.append(0)
        elif hs == as_:
            labels.append(1)
        else:
            labels.append(2)

    log.info("dataset_built", total=len(df), examples=len(feature_rows), skipped=skipped)

    if not feature_rows:
        return np.zeros((0, len(FEATURE_COLS)), dtype=np.float32), np.zeros(0, dtype=int)

    return np.array(feature_rows, dtype=np.float32), np.array(labels, dtype=int)


# ── Training ───────────────────────────────────────────────────────────────────

def _train_with_params(X: np.ndarray, y: np.ndarray, params: dict | None) -> tuple[EdgeAIModel, dict]:
    from xgboost import XGBClassifier
    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.model_selection import TimeSeriesSplit
    from sklearn.metrics import log_loss, accuracy_score, brier_score_loss

    default_params = {
        "n_estimators": 300,
        "max_depth": 5,
        "learning_rate": 0.05,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "objective": "multi:softprob",
        "num_class": 3,
        "eval_metric": "mlogloss",
        "random_state": 42,
        "n_jobs": -1,
    }
    if params:
        for k, v in params.items():
            default_params[k] = v
        default_params.update({"objective": "multi:softprob", "num_class": 3,
                                "eval_metric": "mlogloss", "random_state": 42, "n_jobs": -1})

    # OOF cross-validation temporelle
    tscv = TimeSeriesSplit(n_splits=5)
    oof = np.zeros((len(y), 3))

    for train_idx, val_idx in tscv.split(X):
        clf = CalibratedClassifierCV(XGBClassifier(**default_params), method="sigmoid", cv=3)
        clf.fit(X[train_idx], y[train_idx])
        oof[val_idx] = clf.predict_proba(X[val_idx])

    ll = float(log_loss(y, oof))
    acc = float(accuracy_score(y, oof.argmax(axis=1)))
    brier = float(brier_score_loss((y == 0).astype(int), oof[:, 0]))

    # Modèle final sur tout le dataset
    model = EdgeAIModel()
    model.model = CalibratedClassifierCV(XGBClassifier(**default_params), method="sigmoid", cv=3)
    model.model.fit(X, y)

    try:
        import shap
        inner = model.model.calibrated_classifiers_[0].estimator
        model.explainer = shap.TreeExplainer(inner)
    except Exception:
        model.explainer = None

    metrics = {
        "version": model.version,
        "log_loss": round(ll, 4),
        "accuracy": round(acc, 4),
        "brier_score": round(brier, 4),
        "n_samples": len(X),
        "passes_threshold": True,
    }
    return model, metrics


# ── Deploy ─────────────────────────────────────────────────────────────────────

def _deploy(model: EdgeAIModel, metrics: dict):
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    path = MODEL_DIR / f"model_{model.version}.joblib"
    model.save(path)
    (MODEL_DIR / f"metrics_{model.version}.json").write_text(json.dumps(metrics, indent=2))
    shutil.copy2(path, MODEL_DIR / "model_latest.joblib")
    log.info("model_deployed", version=model.version, log_loss=metrics["log_loss"],
             accuracy=metrics["accuracy"])
    return str(path)


async def _register_in_db(session: AsyncSession, metrics: dict, artifact_path: str):
    features_hash = hashlib.md5(str(MatchFeatures.feature_names()).encode()).hexdigest()[:16]
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        async with session.begin_nested():
            # Marquer toutes les versions précédentes comme non déployées
            await session.execute(text("UPDATE model_versions SET is_deployed = false"))
            await session.execute(text("""
                INSERT INTO model_versions
                    (id, version, accuracy, log_loss, brier_score, features_hash,
                     artifact_path, is_deployed, is_shadow, trained_at, deployed_at)
                VALUES
                    (gen_random_uuid(), :version, :accuracy, :log_loss, :brier,
                     :fhash, :path, true, false, :now, :now)
                ON CONFLICT (version) DO UPDATE
                    SET is_deployed = true, deployed_at = :now
            """), {
                "version": metrics["version"],
                "accuracy": metrics["accuracy"],
                "log_loss": metrics["log_loss"],
                "brier": metrics["brier_score"],
                "fhash": features_hash,
                "path": artifact_path,
                "now": now,
            })
    except Exception as e:
        log.error("model_version_register_error", error=str(e))


# ── Main entry point ───────────────────────────────────────────────────────────

async def maybe_auto_retrain(session: AsyncSession) -> bool:
    """
    Vérifie si un réentraînement est nécessaire et le lance si oui.
    Retourne True si un nouveau modèle a été déployé.
    """
    state = _load_state()
    last_trained = datetime.fromisoformat(state["last_trained"]).replace(tzinfo=timezone.utc)
    cooldown = timedelta(hours=RETRAIN_COOLDOWN_HOURS)

    if datetime.now(timezone.utc) - last_trained < cooldown:
        log.info("auto_retrain_cooldown", hours_remaining=round(
            (cooldown - (datetime.now(timezone.utc) - last_trained)).seconds / 3600, 1))
        return False

    # Compte les nouveaux matchs depuis le dernier entraînement
    since_naive = last_trained.replace(tzinfo=None)
    result = await session.execute(text("""
        SELECT COUNT(*) FROM matches
        WHERE status = 'FINISHED'
          AND home_score IS NOT NULL
          AND away_score IS NOT NULL
          AND match_date > :since
    """), {"since": since_naive})
    new_samples = result.scalar() or 0

    if new_samples < RETRAIN_MIN_SAMPLES:
        log.info("auto_retrain_skip", new_samples=new_samples, threshold=RETRAIN_MIN_SAMPLES)
        return False

    log.info("auto_retrain_start", new_samples=new_samples)

    # Charge tous les matchs FINISHED depuis la DB
    result = await session.execute(text("""
        SELECT home_team, away_team, home_score, away_score, match_date, league,
               ht_home_score, ht_away_score,
               COALESCE(home_yellow_cards, 0), COALESCE(away_yellow_cards, 0)
        FROM matches
        WHERE status = 'FINISHED'
          AND home_score IS NOT NULL
          AND away_score IS NOT NULL
        ORDER BY match_date
    """))
    rows = result.fetchall()

    if len(rows) < 200:
        log.warning("auto_retrain_insufficient_total", count=len(rows))
        return False

    # Construit le dataset de features
    X, y = _build_dataset(rows)

    if len(X) < 100:
        log.warning("auto_retrain_insufficient_features", count=len(X))
        return False

    # Entraîne avec les meilleurs params Optuna connus
    best_params = state.get("best_optuna_params")
    new_model, metrics = _train_with_params(X, y, best_params)

    current_loss = float(state.get("current_log_loss", 9999))
    improved = metrics["log_loss"] < current_loss

    log.info("auto_retrain_result",
             new_loss=metrics["log_loss"], current_loss=current_loss,
             improved=improved, samples=len(X))

    artifact_path = _deploy(new_model, metrics)
    await _register_in_db(session, metrics, artifact_path)

    state.update({
        "last_trained": datetime.now(timezone.utc).isoformat(),
        "current_log_loss": metrics["log_loss"],
        "current_accuracy": metrics["accuracy"],
        "samples_count": len(X),
    })
    _save_state(state)

    return True
