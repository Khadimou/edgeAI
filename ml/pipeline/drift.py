"""
Détection de dérive + rollback automatique.

À la fin de chaque cycle du scheduler :
1. Calcule log-loss/accuracy live du modèle déployé sur les matchs FINISHED récents
2. Compare aux métriques OOF d'entraînement
3. Si dégradation marquée ET échantillon suffisant ET version antérieure dispo
   → rollback automatique vers la version précédente
"""
import os
import shutil
from datetime import datetime, timezone
from math import log as math_log
from pathlib import Path

import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

log = structlog.get_logger()

MODEL_DIR = Path(os.getenv("MODEL_DIR", "/app/artifacts/models"))

# Seuils de rollback — délibérément conservateurs pour éviter les faux positifs
DRIFT_MIN_SAMPLES = int(os.getenv("DRIFT_MIN_SAMPLES", "30"))
DRIFT_LOG_LOSS_DELTA = float(os.getenv("DRIFT_LOG_LOSS_DELTA", "0.15"))
DRIFT_ACCURACY_DELTA = float(os.getenv("DRIFT_ACCURACY_DELTA", "-0.06"))
DRIFT_DEPLOY_COOLDOWN_HOURS = int(os.getenv("DRIFT_DEPLOY_COOLDOWN_HOURS", "12"))

EPS = 1e-15


def _live_metrics(rows: list) -> dict:
    if not rows:
        return {"n": 0, "log_loss": None, "accuracy": None}
    correct = 0
    ll_sum = 0.0
    for ph, pd_, pa, hs, asc in rows:
        actual = 0 if hs > asc else (1 if hs == asc else 2)
        probs = [ph, pd_, pa]
        if max(range(3), key=lambda i: probs[i]) == actual:
            correct += 1
        p = max(min(probs[actual], 1 - EPS), EPS)
        ll_sum += -math_log(p)
    n = len(rows)
    return {
        "n": n,
        "log_loss": round(ll_sum / n, 4),
        "accuracy": round(correct / n, 4),
    }


async def check_drift_and_rollback(session: AsyncSession) -> dict:
    """
    Vérifie la dérive du modèle déployé et déclenche un rollback si nécessaire.
    Retourne un dict avec le statut et les actions effectuées.
    """
    # 1) Modèle actuellement déployé
    res = await session.execute(text("""
        SELECT version, accuracy, log_loss, brier_score, artifact_path,
               deployed_at, trained_at
        FROM model_versions
        WHERE is_deployed = TRUE
        ORDER BY deployed_at DESC NULLS LAST, trained_at DESC
        LIMIT 1
    """))
    deployed = res.fetchone()
    if not deployed:
        return {"status": "no_deployed_model"}

    version, oof_acc, oof_ll, _, artifact, deployed_at, _ = deployed

    # 2) Cooldown : on laisse le modèle 12h avant de pouvoir le rejeter
    if deployed_at:
        deployed_aware = deployed_at if deployed_at.tzinfo else deployed_at.replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - deployed_aware).total_seconds() / 3600
        if age_hours < DRIFT_DEPLOY_COOLDOWN_HOURS:
            return {
                "status": "cooldown",
                "version": version,
                "age_hours": round(age_hours, 1),
            }

    # 3) Métriques live du modèle déployé
    res = await session.execute(text("""
        SELECT p.prob_home, p.prob_draw, p.prob_away,
               m.home_score, m.away_score
        FROM predictions p
        JOIN matches m ON m.id = p.match_id
        WHERE p.model_version = :v
          AND m.status = 'FINISHED'
          AND m.home_score IS NOT NULL
          AND m.away_score IS NOT NULL
    """), {"v": version})
    rows = res.fetchall()
    live = _live_metrics([(r[0], r[1], r[2], r[3], r[4]) for r in rows])

    if live["n"] < DRIFT_MIN_SAMPLES:
        return {
            "status": "insufficient_data",
            "version": version,
            "n_samples": live["n"],
            "needed": DRIFT_MIN_SAMPLES,
        }

    log_loss_delta = live["log_loss"] - oof_ll
    accuracy_delta = live["accuracy"] - oof_acc

    is_degraded = (
        log_loss_delta > DRIFT_LOG_LOSS_DELTA
        or accuracy_delta < DRIFT_ACCURACY_DELTA
    )

    base_report = {
        "version": version,
        "live_log_loss": live["log_loss"],
        "oof_log_loss": oof_ll,
        "log_loss_delta": round(log_loss_delta, 4),
        "live_accuracy": live["accuracy"],
        "oof_accuracy": oof_acc,
        "accuracy_delta": round(accuracy_delta, 4),
        "n_samples": live["n"],
    }

    if not is_degraded:
        log.info("drift_check_healthy", **base_report)
        return {"status": "healthy", **base_report}

    # 4) Dégradé → tentative de rollback vers la version précédente
    res = await session.execute(text("""
        SELECT version, accuracy, log_loss, artifact_path, trained_at
        FROM model_versions
        WHERE is_deployed = FALSE
          AND is_shadow = FALSE
          AND log_loss < :ll
        ORDER BY trained_at DESC
        LIMIT 1
    """), {"ll": oof_ll + DRIFT_LOG_LOSS_DELTA})
    prev = res.fetchone()

    if not prev:
        log.warning("drift_degraded_no_rollback", **base_report)
        return {"status": "degraded_no_candidate", **base_report}

    prev_version, prev_acc, prev_ll, prev_path, _ = prev

    # 5) Effectuer le rollback : copier l'artefact + flip is_deployed
    if not _swap_active_artifact(prev_path):
        log.error("drift_rollback_artifact_missing", version=prev_version, path=prev_path)
        return {"status": "degraded_artifact_missing", **base_report, "candidate": prev_version}

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        async with session.begin_nested():
            await session.execute(text(
                "UPDATE model_versions SET is_deployed = FALSE WHERE version = :v"
            ), {"v": version})
            await session.execute(text("""
                UPDATE model_versions
                SET is_deployed = TRUE, deployed_at = :now
                WHERE version = :v
            """), {"v": prev_version, "now": now})
    except Exception as e:
        log.error("drift_rollback_db_error", error=str(e))
        return {"status": "degraded_db_error", **base_report, "candidate": prev_version}

    log.warning(
        "drift_rollback_done",
        from_version=version,
        to_version=prev_version,
        log_loss_delta=base_report["log_loss_delta"],
        n_samples=live["n"],
    )

    return {
        "status": "rolled_back",
        "from_version": version,
        "to_version": prev_version,
        "to_oof_log_loss": prev_ll,
        "to_oof_accuracy": prev_acc,
        **base_report,
    }


def _swap_active_artifact(target_path: str) -> bool:
    """Copie l'artefact cible vers model_latest.joblib."""
    src = Path(target_path)
    if not src.exists():
        return False
    dst = MODEL_DIR / "model_latest.joblib"
    shutil.copy2(src, dst)
    return True
