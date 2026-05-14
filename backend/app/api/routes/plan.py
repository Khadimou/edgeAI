from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.deps import get_db, get_current_user
from app.core.redis import get_redis
from app.db.models import Match, User
from app.services.kelly import calculate_kelly, RiskProfile

router = APIRouter(prefix="/plan", tags=["plan"])

OUTCOME_LABELS = {"HOME": "Domicile", "DRAW": "Nul", "AWAY": "Extérieur"}


@router.get("")
async def get_plan(
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
    user: User = Depends(get_current_user),
):
    has_goal = user.goal_amount is not None and user.goal_timeframe_days is not None
    bankroll = user.bankroll or 0.0

    # Calcul de la progression vers l'objectif
    goal_summary = None
    if has_goal:
        start = user.goal_start_date or user.created_at
        now = datetime.now(timezone.utc)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        days_elapsed = max(0, (now - start).days)
        days_remaining = max(0, user.goal_timeframe_days - days_elapsed)
        target_bankroll = bankroll + user.goal_amount  # bankroll initiale + gain visé

        # P&L réel depuis le début de l'objectif
        from sqlalchemy import text
        start_naive = start.replace(tzinfo=None) if start.tzinfo else start
        r = await db.execute(
            text("SELECT COALESCE(SUM(profit_loss), 0) FROM bets WHERE user_id = :uid AND placed_at >= :since AND status IN ('WON','LOST')"),
            {"uid": user.id, "since": start_naive},
        )
        current_profit = float(r.scalar() or 0)
        progress_pct = min(100.0, round(current_profit / user.goal_amount * 100, 1)) if user.goal_amount > 0 else 0.0

        required_roi = round(user.goal_amount / bankroll * 100, 1) if bankroll > 0 else 0.0
        weeks_remaining = max(1, days_remaining / 7)
        weekly_roi_needed = round((((1 + required_roi / 100) ** (1 / max(1, user.goal_timeframe_days / 7))) - 1) * 100, 1)

        on_track = days_elapsed == 0 or (current_profit / max(1, days_elapsed) >= user.goal_amount / user.goal_timeframe_days)

        goal_summary = {
            "goal_amount": user.goal_amount,
            "goal_timeframe_days": user.goal_timeframe_days,
            "days_elapsed": days_elapsed,
            "days_remaining": days_remaining,
            "target_bankroll": round(target_bankroll, 2),
            "current_profit": round(current_profit, 2),
            "progress_percent": progress_pct,
            "required_roi_percent": required_roi,
            "weekly_roi_needed": weekly_roi_needed,
            "on_track": on_track,
        }

    # Récupérer les matchs à venir avec prédictions
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(Match)
        .where(
            Match.status == "SCHEDULED",
            Match.match_date >= now,
            Match.match_date <= now + timedelta(hours=72),
        )
        .options(selectinload(Match.predictions))
        .order_by(Match.match_date)
        .limit(30)
    )
    upcoming = result.scalars().all()

    # Générer les paris concrets
    bets = []
    for match in upcoming:
        if not match.predictions:
            continue
        pred = match.predictions[0]
        candidates = [
            ("HOME", pred.prob_home, match.home_odds),
            ("DRAW", pred.prob_draw, match.draw_odds),
            ("AWAY", pred.prob_away, match.away_odds),
        ]
        best_outcome = None
        best_kelly = None
        for outcome, prob, odds in candidates:
            if not odds or odds <= 1.0:
                continue
            kelly = calculate_kelly(
                prob=prob,
                odds=odds,
                bankroll=bankroll or 100.0,
                risk_profile=RiskProfile(user.risk_profile),
                kelly_user_fraction=user.kelly_fraction,
            )
            if kelly.is_value_bet and (best_kelly is None or kelly.edge > best_kelly.edge):
                best_kelly = kelly
                best_outcome = outcome

        if best_outcome and best_kelly:
            odds_val = {"HOME": match.home_odds, "DRAW": match.draw_odds, "AWAY": match.away_odds}[best_outcome]
            potential_gain = round((odds_val - 1) * best_kelly.recommended_amount, 2)
            bets.append({
                "match_id": match.id,
                "home_team": match.home_team,
                "away_team": match.away_team,
                "league": match.league,
                "match_date": match.match_date.isoformat(),
                "outcome": best_outcome,
                "outcome_label": OUTCOME_LABELS[best_outcome],
                "odds": round(odds_val, 2),
                "edge": round(best_kelly.edge, 4),
                "edge_percent": round(best_kelly.edge * 100, 1),
                "recommended_amount": best_kelly.recommended_amount,
                "potential_gain": potential_gain,
                "prob_home": pred.prob_home,
                "prob_draw": pred.prob_draw,
                "prob_away": pred.prob_away,
                "confidence": pred.confidence,
                "strategy": best_kelly.reason,
            })

    bets.sort(key=lambda x: x["edge"], reverse=True)

    # Message de contexte
    if not has_goal:
        message = "Définissez un objectif dans les paramètres pour obtenir un plan personnalisé."
    elif bankroll <= 0:
        message = "Ajoutez votre bankroll dans les paramètres pour que le plan calcule vos mises."
    elif not bets:
        message = "Aucune opportunité détectée sur les 72 prochaines heures. Le modèle analyse les matchs toutes les 6h."
    else:
        total_recommended = sum(b["recommended_amount"] for b in bets[:5])
        message = (
            f"Pour atteindre +{user.goal_amount:.0f}€ en {user.goal_timeframe_days} jours, "
            f"misez sur les {len(bets[:5])} meilleures opportunités — total recommandé : {total_recommended:.0f}€."
            if has_goal else
            f"{len(bets)} opportunité(s) détectée(s) sur les 72 prochaines heures."
        )

    return {
        "has_goal": has_goal,
        "bankroll": bankroll,
        "goal_summary": goal_summary,
        "bets": bets[:10],
        "message": message,
    }
