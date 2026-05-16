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
    init_elo, update_elo, update_elo_venue,
)
from .model import EdgeAIModel

log = structlog.get_logger()

MODEL_DIR = Path(os.getenv("MODEL_DIR", "/app/artifacts/models"))
STATE_FILE = MODEL_DIR / "training_state.json"
RETRAIN_MIN_SAMPLES = int(os.getenv("RETRAIN_MIN_SAMPLES", "50"))
RETRAIN_COOLDOWN_HOURS = int(os.getenv("RETRAIN_COOLDOWN_HOURS", "24"))
FEATURE_COLS = MatchFeatures.feature_names()

# Gate de déploiement : ne deploie que si log-loss n'empire pas trop
# ET si les seuils minimaux (du model.py) sont respectés
MAX_LOG_LOSS_REGRESSION = float(os.getenv("MAX_LOG_LOSS_REGRESSION", "0.05"))  # 5% pire max
MIN_DEPLOY_ACCURACY = float(os.getenv("MIN_DEPLOY_ACCURACY", "0.44"))
MAX_DEPLOY_LOG_LOSS = float(os.getenv("MAX_DEPLOY_LOG_LOSS", "1.10"))


# ── State helpers ──────────────────────────────────────────────────────────────

def _state_path(market: str = "1x2") -> Path:
    """Path du state file pour un marché donné."""
    if market == "1x2":
        return STATE_FILE  # backward compat : training_state.json
    return MODEL_DIR / f"training_state_{market}.json"


def _load_state(market: str = "1x2") -> dict:
    path = _state_path(market)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {
        "market": market,
        "last_trained": "2020-01-01T00:00:00",
        "current_log_loss": 9999.0,
        "current_accuracy": 0.0,
        "samples_count": 0,
        "best_optuna_params": None,
    }


def _save_state(state: dict, market: str = "1x2"):
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    _state_path(market).write_text(json.dumps(state, indent=2))


# ── Feature builder (in-memory, mirrors build_features.py) ────────────────────

def _label_1x2(hs: int, as_: int) -> int:
    if hs > as_: return 0  # HOME
    if hs == as_: return 1  # DRAW
    return 2  # AWAY


def _label_ou_25(hs: int, as_: int) -> int:
    return 1 if hs + as_ > 2.5 else 0  # 1 = Over


def _build_dataset(rows: list, min_history: int = 3,
                   label_func=_label_1x2,
                   use_phase1_only: bool = False) -> tuple[np.ndarray, np.ndarray]:
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
        # Phase 2 : shots/SOT/corners (peuvent être NULL si pas encore backfillé)
        "home_shots", "away_shots",
        "home_shots_on_target", "away_shots_on_target",
        "home_corners", "away_corners",
    ])
    df["date"] = pd.to_datetime(df["date"])
    df["home_score"] = pd.to_numeric(df["home_score"], errors="coerce")
    df["away_score"] = pd.to_numeric(df["away_score"], errors="coerce")
    df = df.dropna(subset=["home_score", "away_score"])
    df["home_score"] = df["home_score"].astype(int)
    df["away_score"] = df["away_score"].astype(int)
    df = df.sort_values("date").reset_index(drop=True)

    # ELO state chronologique (Phase 1)
    elo_general = init_elo()
    elo_home_venue = init_elo()
    elo_away_venue = init_elo()

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
            # Update ELO même si on n'inclut pas dans le dataset
            update_elo(elo_general, row["home_team"], row["away_team"],
                       int(row["home_score"]), int(row["away_score"]))
            update_elo_venue(elo_home_venue, elo_away_venue,
                             row["home_team"], row["away_team"],
                             int(row["home_score"]), int(row["away_score"]))
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
            elo_general=elo_general,
            elo_home_venue=elo_home_venue,
            elo_away_venue=elo_away_venue,
        )

        # Slice features selon le subset demandé (phase1 = 52 sans shots, full = 67)
        arr = feat.to_array_phase1() if use_phase1_only else feat.to_array()
        feature_rows.append(arr)

        hs, as_ = int(row["home_score"]), int(row["away_score"])
        labels.append(label_func(hs, as_))

        # Update ELO APRÈS calcul features (pas de data leakage)
        update_elo(elo_general, row["home_team"], row["away_team"], hs, as_)
        update_elo_venue(elo_home_venue, elo_away_venue,
                         row["home_team"], row["away_team"], hs, as_)

    log.info("dataset_built", total=len(df), examples=len(feature_rows), skipped=skipped,
             use_phase1_only=use_phase1_only)

    n_features = len(MatchFeatures.feature_names_phase1()) if use_phase1_only else len(FEATURE_COLS)
    if not feature_rows:
        return np.zeros((0, n_features), dtype=np.float32), np.zeros(0, dtype=int)

    return np.array(feature_rows, dtype=np.float32), np.array(labels, dtype=int)


# ── Training ───────────────────────────────────────────────────────────────────

def _train_with_params(X: np.ndarray, y: np.ndarray, params: dict | None,
                       multi: bool = True) -> tuple[EdgeAIModel, dict]:
    """Train XGBoost + sigmoid calibration. multi=True → 3-class softprob, sinon binary.

    Robuste contre les TSCV folds qui n'ont pas toutes les classes :
    - Si un fold de train n'a pas toutes les classes attendues → skip le fold
    - Métriques OOF calculées uniquement sur les rows avec prédictions valides
    """
    from xgboost import XGBClassifier
    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.model_selection import TimeSeriesSplit
    from sklearn.metrics import log_loss, accuracy_score, brier_score_loss

    if multi:
        objective_params = {"objective": "multi:softprob", "num_class": 3,
                            "eval_metric": "mlogloss"}
        n_classes = 3
        expected_classes = {0, 1, 2}
    else:
        objective_params = {"objective": "binary:logistic", "eval_metric": "logloss"}
        n_classes = 2
        expected_classes = {0, 1}

    # Sanity check global avant tout : si le dataset entier n'a pas toutes les classes,
    # le modèle ne peut pas apprendre, on remonte vite une erreur claire
    unique_global = set(np.unique(y).tolist())
    counts_global = np.bincount(y.astype(int), minlength=n_classes).tolist()
    log.info("training_y_distribution",
             unique=sorted(unique_global), counts=counts_global,
             n_samples=int(len(y)), multi=multi)
    if not expected_classes.issubset(unique_global):
        missing = sorted(expected_classes - unique_global)
        log.error("training_missing_classes_global", missing=missing,
                  unique=sorted(unique_global))
        raise ValueError(f"Dataset n'a pas toutes les classes : manquantes={missing}")

    default_params = {
        "n_estimators": 300,
        "max_depth": 5,
        "learning_rate": 0.05,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        **objective_params,
        "random_state": 42,
        "n_jobs": -1,
    }
    if params:
        for k, v in params.items():
            default_params[k] = v
        default_params.update({**objective_params, "random_state": 42, "n_jobs": -1})

    # OOF cross-validation temporelle : 3 splits (vs 5) pour avoir des folds plus
    # gros = plus de chances que toutes les classes soient présentes en train
    tscv = TimeSeriesSplit(n_splits=3)
    oof = np.zeros((len(y), n_classes))
    folds_trained = 0

    for fold_idx, (train_idx, val_idx) in enumerate(tscv.split(X), start=1):
        y_train = y[train_idx]
        train_classes = set(np.unique(y_train).tolist())
        if not expected_classes.issubset(train_classes):
            missing = sorted(expected_classes - train_classes)
            log.warning("training_fold_skipped_missing_classes",
                        fold=fold_idx, missing=missing,
                        train_classes=sorted(train_classes))
            continue
        try:
            clf = CalibratedClassifierCV(XGBClassifier(**default_params), method="sigmoid", cv=3)
            clf.fit(X[train_idx], y_train)
            oof[val_idx] = clf.predict_proba(X[val_idx])
            folds_trained += 1
        except Exception as e:
            log.warning("training_fold_failed", fold=fold_idx, error=str(e))

    if folds_trained == 0:
        raise RuntimeError("Aucun fold de TSCV n'a pu être entraîné — dataset trop petit ou déséquilibré")

    log.info("training_oof_done", folds_trained=folds_trained, total_folds=3)

    # Métriques uniquement sur les rows avec OOF prediction valide (sum > 0)
    valid = oof.sum(axis=1) > 0
    y_valid = y[valid]
    oof_valid = oof[valid]
    ll = float(log_loss(y_valid, oof_valid))
    acc = float(accuracy_score(y_valid, oof_valid.argmax(axis=1)))
    if multi:
        brier = float(brier_score_loss((y_valid == 0).astype(int), oof_valid[:, 0]))
    else:
        brier = float(brier_score_loss(y_valid, oof_valid[:, 1]))

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

def _deploy(model: EdgeAIModel, metrics: dict, suffix: str = "", market: str = "1X2"):
    """suffix: '' (1X2), '_ou' (OU 2.5), '_ah' (AH). Met à jour model{suffix}_latest.joblib."""
    import joblib
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    prefix = "model" if not suffix else f"model_{suffix.lstrip('_')}"
    path = MODEL_DIR / f"{prefix}_{model.version}.joblib"
    # Use joblib with market info embedded (consistent avec les autres modèles)
    payload = {"model": model.model, "version": model.version, "market": market}
    if model.explainer is not None:
        payload["explainer"] = model.explainer
    joblib.dump(payload, path)
    (MODEL_DIR / f"metrics_{prefix.replace('model', '').lstrip('_') or '1x2'}_{model.version}.json").write_text(
        json.dumps({**metrics, "market": market}, indent=2))
    latest = MODEL_DIR / f"{prefix}_latest.joblib"
    shutil.copy2(path, latest)
    log.info("model_deployed", market=market, version=model.version,
             log_loss=metrics["log_loss"], accuracy=metrics["accuracy"], path=str(latest))
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

async def maybe_auto_retrain(session: AsyncSession, force: bool = False) -> bool:
    """
    Vérifie si un réentraînement est nécessaire et le lance si oui.
    Retourne True si un nouveau modèle a été déployé.

    force=True : bypass cooldown + RETRAIN_MIN_SAMPLES (utile au déploiement
    initial après un schema change pour forcer le 1er entraînement).
    """
    state = _load_state()
    last_trained = datetime.fromisoformat(state["last_trained"]).replace(tzinfo=timezone.utc)
    cooldown = timedelta(hours=RETRAIN_COOLDOWN_HOURS)

    if not force and datetime.now(timezone.utc) - last_trained < cooldown:
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

    if not force and new_samples < RETRAIN_MIN_SAMPLES:
        log.info("auto_retrain_skip", new_samples=new_samples, threshold=RETRAIN_MIN_SAMPLES)
        return False

    log.info("auto_retrain_start", new_samples=new_samples, force=force)

    # Charge tous les matchs FINISHED depuis la DB
    result = await session.execute(text("""
        SELECT home_team, away_team, home_score, away_score, match_date, league,
               ht_home_score, ht_away_score,
               COALESCE(home_yellow_cards, 0), COALESCE(away_yellow_cards, 0),
               home_shots, away_shots,
               home_shots_on_target, away_shots_on_target,
               home_corners, away_corners
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
    # 1X2 utilise les 52 features Phase 1 (shots/SOT dégradent le directionnel)
    X, y = _build_dataset(rows, use_phase1_only=True)

    if len(X) < 100:
        log.warning("auto_retrain_insufficient_features", count=len(X))
        return False

    # Entraîne avec les meilleurs params Optuna connus
    best_params = state.get("best_optuna_params")
    new_model, metrics = _train_with_params(X, y, best_params)

    current_loss = float(state.get("current_log_loss", 9999))
    new_loss = metrics["log_loss"]
    new_acc = metrics["accuracy"]
    current_features_hash = state.get("features_hash")
    new_features_hash = hashlib.md5(str(MatchFeatures.feature_names()).encode()).hexdigest()[:16]
    schema_changed = (current_features_hash is not None and current_features_hash != new_features_hash)

    # ── Gate de déploiement ──────────────────────────────────────
    # 1. Bypass complet si le schema des features a changé (nouveau modèle obligatoire)
    # 2. Sinon : threshold absolus + protection contre régression > 5%
    fails_threshold = (new_loss > MAX_DEPLOY_LOG_LOSS or new_acc < MIN_DEPLOY_ACCURACY)
    regression = (not schema_changed and current_loss < 9000
                  and new_loss > current_loss * (1 + MAX_LOG_LOSS_REGRESSION))

    if fails_threshold or regression:
        log.warning("auto_retrain_rejected",
                    new_loss=new_loss, current_loss=current_loss,
                    new_acc=new_acc, samples=len(X),
                    fails_threshold=fails_threshold, regression=regression,
                    schema_changed=schema_changed,
                    reason="below_min_threshold" if fails_threshold else "log_loss_regression")
        state["last_trained"] = datetime.now(timezone.utc).isoformat()
        _save_state(state)
        return False
    if schema_changed:
        log.info("auto_retrain_schema_change_bypass",
                 old_hash=current_features_hash, new_hash=new_features_hash)

    improved = new_loss < current_loss
    log.info("auto_retrain_result",
             new_loss=new_loss, current_loss=current_loss,
             improved=improved, samples=len(X))

    artifact_path = _deploy(new_model, metrics)
    await _register_in_db(session, metrics, artifact_path)

    state.update({
        "last_trained": datetime.now(timezone.utc).isoformat(),
        "current_log_loss": new_loss,
        "current_accuracy": new_acc,
        "samples_count": len(X),
        "features_hash": new_features_hash,
    })
    _save_state(state)

    return True


# ── OU 2.5 auto-retrain ────────────────────────────────────────────────────────

async def maybe_auto_retrain_ou(session: AsyncSession, force: bool = False) -> bool:
    """Retrain OU 2.5 : mêmes features que 1X2, label binaire (total > 2.5)."""
    state = _load_state(market="ou")
    last_trained = datetime.fromisoformat(state["last_trained"]).replace(tzinfo=timezone.utc)
    cooldown = timedelta(hours=RETRAIN_COOLDOWN_HOURS)

    if not force and datetime.now(timezone.utc) - last_trained < cooldown:
        log.info("auto_retrain_ou_cooldown", hours_remaining=round(
            (cooldown - (datetime.now(timezone.utc) - last_trained)).seconds / 3600, 1))
        return False

    since_naive = last_trained.replace(tzinfo=None)
    result = await session.execute(text("""
        SELECT COUNT(*) FROM matches
        WHERE status = 'FINISHED'
          AND home_score IS NOT NULL AND away_score IS NOT NULL
          AND match_date > :since
    """), {"since": since_naive})
    new_samples = result.scalar() or 0

    if not force and new_samples < RETRAIN_MIN_SAMPLES:
        log.info("auto_retrain_ou_skip", new_samples=new_samples, threshold=RETRAIN_MIN_SAMPLES)
        return False

    log.info("auto_retrain_ou_start", new_samples=new_samples, force=force)
    result = await session.execute(text("""
        SELECT home_team, away_team, home_score, away_score, match_date, league,
               ht_home_score, ht_away_score,
               COALESCE(home_yellow_cards, 0), COALESCE(away_yellow_cards, 0),
               home_shots, away_shots,
               home_shots_on_target, away_shots_on_target,
               home_corners, away_corners
        FROM matches
        WHERE status = 'FINISHED'
          AND home_score IS NOT NULL AND away_score IS NOT NULL
        ORDER BY match_date
    """))
    rows = result.fetchall()
    if len(rows) < 200:
        log.warning("auto_retrain_ou_insufficient_total", count=len(rows))
        return False

    # OU utilise les 52 features Phase 1 (shots dégradent l'OU de -12pts ROI)
    X, y = _build_dataset(rows, label_func=_label_ou_25, use_phase1_only=True)
    if len(X) < 100:
        log.warning("auto_retrain_ou_insufficient_features", count=len(X))
        return False

    best_params = state.get("best_optuna_params")
    new_model, metrics = _train_with_params(X, y, best_params, multi=False)
    current_loss = float(state.get("current_log_loss", 9999))
    new_loss = metrics["log_loss"]
    new_acc = metrics["accuracy"]
    current_features_hash = state.get("features_hash")
    new_features_hash = hashlib.md5(str(MatchFeatures.feature_names()).encode()).hexdigest()[:16]
    schema_changed = (current_features_hash is not None and current_features_hash != new_features_hash)

    fails_threshold = (new_loss > 0.72 or new_acc < 0.50)
    regression = (not schema_changed and current_loss < 9000
                  and new_loss > current_loss * (1 + MAX_LOG_LOSS_REGRESSION))
    if fails_threshold or regression:
        log.warning("auto_retrain_ou_rejected",
                    new_loss=new_loss, current_loss=current_loss, new_acc=new_acc,
                    fails_threshold=fails_threshold, regression=regression,
                    schema_changed=schema_changed)
        state["last_trained"] = datetime.now(timezone.utc).isoformat()
        _save_state(state, market="ou")
        return False

    log.info("auto_retrain_ou_result", new_loss=new_loss, current_loss=current_loss,
             improved=new_loss < current_loss, samples=len(X),
             schema_changed=schema_changed)
    _deploy(new_model, metrics, suffix="ou", market="OU_2_5")
    state.update({
        "last_trained": datetime.now(timezone.utc).isoformat(),
        "current_log_loss": new_loss,
        "current_accuracy": new_acc,
        "samples_count": len(X),
        "features_hash": new_features_hash,
    })
    _save_state(state, market="ou")
    return True


# ── AH auto-retrain ────────────────────────────────────────────────────────────

async def maybe_auto_retrain_ah(session: AsyncSession, force: bool = False) -> bool:
    """Retrain AH : fetch fdco lines + merge avec features DB + train binary.

    Coût : ~30s pour fetch fdco (6 saisons × 5 ligues = ~30 CSVs), tolérable
    en daily cycle. Cooldown identique aux autres marchés.
    """
    state = _load_state(market="ah")
    last_trained = datetime.fromisoformat(state["last_trained"]).replace(tzinfo=timezone.utc)
    cooldown = timedelta(hours=RETRAIN_COOLDOWN_HOURS)

    if not force and datetime.now(timezone.utc) - last_trained < cooldown:
        log.info("auto_retrain_ah_cooldown", hours_remaining=round(
            (cooldown - (datetime.now(timezone.utc) - last_trained)).seconds / 3600, 1))
        return False

    since_naive = last_trained.replace(tzinfo=None)
    result = await session.execute(text("""
        SELECT COUNT(*) FROM matches
        WHERE status = 'FINISHED'
          AND home_score IS NOT NULL AND away_score IS NOT NULL
          AND match_date > :since
    """), {"since": since_naive})
    new_samples = result.scalar() or 0

    if not force and new_samples < RETRAIN_MIN_SAMPLES:
        log.info("auto_retrain_ah_skip", new_samples=new_samples, threshold=RETRAIN_MIN_SAMPLES)
        return False

    log.info("auto_retrain_ah_start", new_samples=new_samples, force=force)

    # 1. Fetch AH lines depuis fdco (réutilise la logique d'ah_pipeline.py)
    try:
        # Import lazy : ah_pipeline.py est en racine ml/, pas dans pipeline/
        import sys, importlib
        ml_root = Path(__file__).parent.parent
        if str(ml_root) not in sys.path:
            sys.path.insert(0, str(ml_root))
        ah_pipeline = importlib.import_module("ah_pipeline")
        ah_df = ah_pipeline.fetch_all_ah()
    except Exception as e:
        log.error("auto_retrain_ah_fetch_failed", error=str(e))
        return False

    if ah_df is None or ah_df.empty:
        log.warning("auto_retrain_ah_no_data")
        return False

    # 2. Build features depuis DB (mêmes features que 1X2)
    result = await session.execute(text("""
        SELECT home_team, away_team, home_score, away_score, match_date, league,
               ht_home_score, ht_away_score,
               COALESCE(home_yellow_cards, 0), COALESCE(away_yellow_cards, 0),
               home_shots, away_shots,
               home_shots_on_target, away_shots_on_target,
               home_corners, away_corners
        FROM matches
        WHERE status = 'FINISHED'
          AND home_score IS NOT NULL AND away_score IS NOT NULL
        ORDER BY match_date
    """))
    rows = result.fetchall()
    if len(rows) < 200:
        log.warning("auto_retrain_ah_insufficient_total", count=len(rows))
        return False

    # On rebuild un DataFrame avec features ET keys (date, home_team, away_team)
    df = pd.DataFrame(rows, columns=[
        "home_team", "away_team", "home_score", "away_score", "date",
        "league", "ht_home_score", "ht_away_score",
        "home_yellow_cards", "away_yellow_cards",
        # Phase 2 : shots/SOT/corners (peuvent être NULL si pas encore backfillé)
        "home_shots", "away_shots",
        "home_shots_on_target", "away_shots_on_target",
        "home_corners", "away_corners",
    ])
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna(subset=["home_score", "away_score"])
    df["home_score"] = df["home_score"].astype(int)
    df["away_score"] = df["away_score"].astype(int)
    df = df.sort_values("date").reset_index(drop=True)

    # ELO state chronologique
    elo_general = init_elo()
    elo_home_venue = init_elo()
    elo_away_venue = init_elo()
    feat_records = []  # liste de dict avec features + date + teams (pour merge AH)
    for i, row in df.iterrows():
        past = df.iloc[:i]
        if len(past) < 3:
            update_elo(elo_general, row["home_team"], row["away_team"],
                       int(row["home_score"]), int(row["away_score"]))
            update_elo_venue(elo_home_venue, elo_away_venue,
                             row["home_team"], row["away_team"],
                             int(row["home_score"]), int(row["away_score"]))
            continue
        standings, total_teams = compute_standings_from_history(past, row["date"], row["league"])
        feat = compute_features_from_history(
            home_team=row["home_team"], away_team=row["away_team"],
            match_date=row["date"], historical_df=past,
            standings=standings, total_teams=total_teams,
            elo_general=elo_general,
            elo_home_venue=elo_home_venue,
            elo_away_venue=elo_away_venue,
        )
        d = {name: val for name, val in zip(FEATURE_COLS, feat.to_array())}
        d["match_date_dt"] = row["date"]
        d["home_team"] = row["home_team"]
        d["away_team"] = row["away_team"]
        feat_records.append(d)
        update_elo(elo_general, row["home_team"], row["away_team"],
                   int(row["home_score"]), int(row["away_score"]))
        update_elo_venue(elo_home_venue, elo_away_venue,
                         row["home_team"], row["away_team"],
                         int(row["home_score"]), int(row["away_score"]))

    feat_df = pd.DataFrame(feat_records)
    if feat_df.empty:
        log.warning("auto_retrain_ah_no_features")
        return False

    # 3. Merge features avec AH lines + outcome
    ah_df = ah_df.copy()
    ah_df["match_date"] = pd.to_datetime(ah_df["match_date"])
    # Normalize au jour : les heures peuvent différer (UTC vs local)
    ah_df["match_date_dt"] = ah_df["match_date"].dt.normalize()
    feat_df["match_date_dt"] = pd.to_datetime(feat_df["match_date_dt"]).dt.normalize()

    merged = ah_df.merge(
        feat_df, on=["match_date_dt", "home_team", "away_team"], how="inner",
    )
    if len(merged) < 200:
        # Debug : pourquoi 0 ? sample des 2 côtés pour voir les noms/dates
        ah_sample = ah_df[["match_date_dt", "home_team", "away_team"]].head(5).to_dict("records")
        feat_sample = feat_df[["match_date_dt", "home_team", "away_team"]].head(5).to_dict("records")
        log.warning("auto_retrain_ah_insufficient_merged",
                    count=len(merged),
                    n_ah=len(ah_df), n_feat=len(feat_df),
                    ah_sample=ah_sample, feat_sample=feat_sample)
        return False
    # Compute label
    merged["home_pnl"] = merged.apply(
        lambda r: ah_pipeline.compute_ah_outcome(int(r["home_score"]), int(r["away_score"]), float(r["ah_line"]))[0],
        axis=1,
    )
    merged["label"] = (merged["home_pnl"] > 0).astype(int)
    merged = merged.sort_values("match_date").reset_index(drop=True)

    X = merged[FEATURE_COLS].values.astype(np.float32)
    y = merged["label"].values.astype(int)

    best_params = state.get("best_optuna_params")
    new_model, metrics = _train_with_params(X, y, best_params, multi=False)
    current_loss = float(state.get("current_log_loss", 9999))
    new_loss = metrics["log_loss"]
    new_acc = metrics["accuracy"]
    current_features_hash = state.get("features_hash")
    new_features_hash = hashlib.md5(str(MatchFeatures.feature_names()).encode()).hexdigest()[:16]
    schema_changed = (current_features_hash is not None and current_features_hash != new_features_hash)

    fails_threshold = (new_loss > 0.72 or new_acc < 0.48)
    regression = (not schema_changed and current_loss < 9000
                  and new_loss > current_loss * (1 + MAX_LOG_LOSS_REGRESSION))
    if fails_threshold or regression:
        log.warning("auto_retrain_ah_rejected",
                    new_loss=new_loss, current_loss=current_loss, new_acc=new_acc,
                    fails_threshold=fails_threshold, regression=regression,
                    schema_changed=schema_changed)
        state["last_trained"] = datetime.now(timezone.utc).isoformat()
        _save_state(state, market="ah")
        return False

    log.info("auto_retrain_ah_result", new_loss=new_loss, current_loss=current_loss,
             improved=new_loss < current_loss, samples=len(X),
             schema_changed=schema_changed)
    _deploy(new_model, metrics, suffix="ah", market="AH")
    state.update({
        "last_trained": datetime.now(timezone.utc).isoformat(),
        "current_log_loss": new_loss,
        "current_accuracy": new_acc,
        "samples_count": len(X),
        "features_hash": new_features_hash,
    })
    _save_state(state, market="ah")
    return True


# ── Orchestrateur 3 marchés ────────────────────────────────────────────────────

async def maybe_auto_retrain_all(session: AsyncSession, force: bool = False) -> dict:
    """Orchestrate les 3 retrains. Retourne {market: bool deployed}.

    force=True : bypass cooldown + min_samples (utile au déploiement initial
    après changement de schema features).
    """
    results = {}
    try:
        results["1x2"] = await maybe_auto_retrain(session, force=force)
    except Exception as e:
        log.error("auto_retrain_1x2_error", error=str(e))
        results["1x2"] = False
    try:
        results["ou"] = await maybe_auto_retrain_ou(session, force=force)
    except Exception as e:
        log.error("auto_retrain_ou_error", error=str(e))
        results["ou"] = False
    try:
        results["ah"] = await maybe_auto_retrain_ah(session, force=force)
    except Exception as e:
        log.error("auto_retrain_ah_error", error=str(e))
        results["ah"] = False
    return results
