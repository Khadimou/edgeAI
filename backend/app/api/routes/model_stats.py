"""
Monitoring live des performances du modèle ML.
Compare les prédictions historiques aux résultats réels pour détecter dérive et régression.
"""
from datetime import datetime, timezone, timedelta
from math import log

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from app.core.deps import get_db, get_current_user
from app.db.models import User

router = APIRouter(prefix="/model", tags=["model"])

EPS = 1e-15  # clipping pour log-loss


def _actual_outcome(home_score: int, away_score: int) -> int:
    """0 = HOME win, 1 = DRAW, 2 = AWAY win."""
    if home_score > away_score:
        return 0
    if home_score == away_score:
        return 1
    return 2


def _compute_metrics(rows: list) -> dict:
    """rows : list of (prob_home, prob_draw, prob_away, home_score, away_score, model_version)."""
    if not rows:
        return {
            "n": 0,
            "accuracy": None,
            "log_loss": None,
            "brier_score": None,
            "draw_accuracy": None,
            "home_accuracy": None,
            "away_accuracy": None,
        }

    correct = 0
    home_correct = home_total = 0
    draw_correct = draw_total = 0
    away_correct = away_total = 0
    log_loss_sum = 0.0
    brier_sum = 0.0

    for ph, pd_, pa, hs, asco, _ in rows:
        actual = _actual_outcome(hs, asco)
        probs = [ph, pd_, pa]
        pred = max(range(3), key=lambda i: probs[i])

        if pred == actual:
            correct += 1

        if actual == 0:
            home_total += 1
            if pred == 0:
                home_correct += 1
        elif actual == 1:
            draw_total += 1
            if pred == 1:
                draw_correct += 1
        else:
            away_total += 1
            if pred == 2:
                away_correct += 1

        # log-loss multi-classe
        p_actual = max(min(probs[actual], 1 - EPS), EPS)
        log_loss_sum += -log(p_actual)

        # Brier multi-classe (sum of squared errors)
        for i in range(3):
            target = 1.0 if i == actual else 0.0
            brier_sum += (probs[i] - target) ** 2

    n = len(rows)
    return {
        "n": n,
        "accuracy": round(correct / n, 4),
        "log_loss": round(log_loss_sum / n, 4),
        "brier_score": round(brier_sum / n, 4),
        "home_accuracy": round(home_correct / home_total, 4) if home_total else None,
        "draw_accuracy": round(draw_correct / draw_total, 4) if draw_total else None,
        "away_accuracy": round(away_correct / away_total, 4) if away_total else None,
        "outcome_distribution": {
            "home_pct": round(home_total / n * 100, 1),
            "draw_pct": round(draw_total / n * 100, 1),
            "away_pct": round(away_total / n * 100, 1),
        },
    }


@router.get("/performance")
async def get_model_performance(
    days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """
    Métriques live de prédictions vs résultats réels.
    Renvoie : global (days), par modèle (version), par jour (timeline).
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    since_naive = since.replace(tzinfo=None)

    # 1) Performance globale sur la fenêtre
    result = await db.execute(text("""
        SELECT p.prob_home, p.prob_draw, p.prob_away,
               m.home_score, m.away_score, p.model_version, m.match_date
        FROM predictions p
        JOIN matches m ON m.id = p.match_id
        WHERE m.status = 'FINISHED'
          AND m.home_score IS NOT NULL
          AND m.away_score IS NOT NULL
          AND m.match_date >= :since
        ORDER BY m.match_date DESC
    """), {"since": since_naive})
    rows = result.fetchall()
    rows_list = [(r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows]

    overall = _compute_metrics(rows_list)
    overall["window_days"] = days
    overall["latest_match_date"] = rows[0][6].isoformat() if rows else None

    # 2) Par version de modèle
    by_version_raw: dict[str, list] = {}
    for r in rows_list:
        by_version_raw.setdefault(r[5], []).append(r)
    by_version = [
        {"model_version": v, **_compute_metrics(group)}
        for v, group in by_version_raw.items()
    ]
    by_version.sort(key=lambda x: x["model_version"], reverse=True)

    # 3) Daily timeline (pour graphe)
    daily_raw: dict[str, list] = {}
    for r, ts in zip(rows_list, [r[6] for r in rows]):
        day = ts.date().isoformat()
        daily_raw.setdefault(day, []).append(r)
    daily = [
        {"date": d, **_compute_metrics(group)}
        for d, group in sorted(daily_raw.items())
    ]

    # 4) Comparaison avec OOF du modèle déployé
    deployed = await db.execute(text("""
        SELECT version, accuracy, log_loss, brier_score, trained_at
        FROM model_versions
        WHERE is_deployed = TRUE
        ORDER BY deployed_at DESC NULLS LAST, trained_at DESC
        LIMIT 1
    """))
    dep = deployed.fetchone()
    deployed_info = None
    drift = None
    if dep:
        deployed_info = {
            "version": dep[0],
            "oof_accuracy": round(dep[1], 4),
            "oof_log_loss": round(dep[2], 4),
            "oof_brier_score": round(dep[3], 4),
            "trained_at": dep[4].isoformat() if dep[4] else None,
        }
        # Métriques du modèle déployé en production
        dep_rows = [r for r in rows_list if r[5] == dep[0]]
        dep_metrics = _compute_metrics(dep_rows)
        if dep_metrics["n"] >= 10 and dep_metrics["log_loss"] is not None:
            log_loss_delta = dep_metrics["log_loss"] - dep[2]
            accuracy_delta = dep_metrics["accuracy"] - dep[1]
            # drift score : positif = pire qu'à l'entraînement
            drift = {
                "live_log_loss": dep_metrics["log_loss"],
                "live_accuracy": dep_metrics["accuracy"],
                "log_loss_delta": round(log_loss_delta, 4),
                "accuracy_delta": round(accuracy_delta, 4),
                "n_samples": dep_metrics["n"],
                "status": _drift_status(log_loss_delta, accuracy_delta, dep_metrics["n"]),
            }

    return {
        "overall": overall,
        "by_version": by_version,
        "daily": daily,
        "deployed_model": deployed_info,
        "drift": drift,
    }


def _drift_status(log_loss_delta: float, accuracy_delta: float, n: int) -> str:
    """Catégorise la dérive."""
    if n < 20:
        return "insufficient_data"
    # Dégradation marquée
    if log_loss_delta > 0.10 or accuracy_delta < -0.05:
        return "degraded"
    if log_loss_delta > 0.05 or accuracy_delta < -0.02:
        return "warning"
    return "healthy"
