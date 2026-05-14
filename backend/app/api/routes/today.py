from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.deps import get_db, get_current_user
from app.db.models import Match, User
from app.services.kelly import calculate_kelly, RiskProfile

router = APIRouter(prefix="/today", tags=["today"])

OUTCOME_LABELS = {
    "HOME": "Domicile",
    "DRAW": "Nul",
    "AWAY": "Extérieur",
    "OVER": "Plus de 2.5 buts",
    "UNDER": "Moins de 2.5 buts",
    "AH_HOME": "Domicile (handicap)",
    "AH_AWAY": "Extérieur (handicap)",
}

TIER_CONFIG = [
    {"min_edge": 0.80, "label": "Incontournable", "color": "green", "fire": 3},
    {"min_edge": 0.40, "label": "Très fort", "color": "yellow", "fire": 2},
    {"min_edge": 0.10, "label": "Intéressant", "color": "blue", "fire": 1},
    {"min_edge": 0.03, "label": "À surveiller", "color": "gray", "fire": 0},
]


def _get_tier(edge: float) -> dict:
    for t in TIER_CONFIG:
        if edge >= t["min_edge"]:
            return t
    return TIER_CONFIG[-1]


@router.get("")
async def get_today(
    sport: str = Query("FOOTBALL", pattern="^(FOOTBALL|NBA)$"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    # NBA : fenêtre élargie (les matchs commencent souvent tard la nuit UTC)
    horizon_days = 2 if sport == "NBA" else 1
    today_end = today_start + timedelta(days=horizon_days)

    result = await db.execute(
        select(Match)
        .where(
            Match.status == "SCHEDULED",
            Match.match_date >= today_start,
            Match.match_date < today_end,
            Match.sport == sport,
        )
        .options(selectinload(Match.predictions))
        .order_by(Match.match_date)
        .limit(100)
    )
    today_matches = result.scalars().all()

    bankroll = user.bankroll or 0.0
    picks = []

    league_whitelist = set(settings.value_bet_leagues)
    ou_whitelist = set(settings.value_bet_ou_leagues)
    ah_whitelist = set(settings.value_bet_ah_leagues)

    for match in today_matches:
        pred = match.predictions[0] if match.predictions else None
        is_nba = getattr(match, "sport", "FOOTBALL") == "NBA"
        # NBA : pas de filtre ligue (un seul "championnat") → toujours whitelistée
        is_whitelisted = is_nba or match.league in league_whitelist
        ou_whitelisted = (not is_nba) and (match.league in ou_whitelist)
        ah_whitelisted = (not is_nba) and (match.league in ah_whitelist)

        value_bets = []
        if pred and is_whitelisted:
            candidates = [
                ("HOME", pred.prob_home, match.home_odds),
                ("AWAY", pred.prob_away, match.away_odds),
            ]
            if not is_nba:
                candidates.insert(1, ("DRAW", pred.prob_draw, match.draw_odds))
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
                if kelly.is_value_bet:
                    value_bets.append({
                        "outcome": outcome,
                        "outcome_label": OUTCOME_LABELS[outcome],
                        "market": "1X2",
                        "odds": round(odds, 2),
                        "edge": round(kelly.edge, 4),
                        "edge_percent": round(kelly.edge * 100, 1),
                        "recommended_amount": kelly.recommended_amount,
                        "potential_gain": round((odds - 1) * kelly.recommended_amount, 2),
                        "prob": round(prob, 4),
                        "strategy": kelly.reason,
                    })

        # Marché O/U 2.5 (foot uniquement, ligues whitelistées O/U)
        prob_over = getattr(pred, "prob_over_25", None) if pred else None
        prob_under = getattr(pred, "prob_under_25", None) if pred else None
        if pred and ou_whitelisted and prob_over is not None and prob_under is not None:
            ou_candidates = [
                ("OVER", prob_over, match.over_25_odds),
                ("UNDER", prob_under, match.under_25_odds),
            ]
            for outcome, prob, odds in ou_candidates:
                if not odds or odds <= 1.0:
                    continue
                kelly = calculate_kelly(
                    prob=prob, odds=odds,
                    bankroll=bankroll or 100.0,
                    risk_profile=RiskProfile(user.risk_profile),
                    kelly_user_fraction=user.kelly_fraction,
                )
                if kelly.is_value_bet:
                    value_bets.append({
                        "outcome": outcome,
                        "outcome_label": OUTCOME_LABELS[outcome],
                        "market": "OU_2_5",
                        "odds": round(odds, 2),
                        "edge": round(kelly.edge, 4),
                        "edge_percent": round(kelly.edge * 100, 1),
                        "recommended_amount": kelly.recommended_amount,
                        "potential_gain": round((odds - 1) * kelly.recommended_amount, 2),
                        "prob": round(prob, 4),
                        "strategy": kelly.reason,
                    })

        # Marché Asian Handicap (foot uniquement, ligues whitelistées AH)
        prob_ah_h = getattr(pred, "prob_ah_home", None) if pred else None
        prob_ah_a = getattr(pred, "prob_ah_away", None) if pred else None
        if (pred and ah_whitelisted and prob_ah_h is not None and prob_ah_a is not None
                and match.ah_line is not None):
            ah_line = match.ah_line
            ah_label_home = f"Home {ah_line:+g}"
            ah_label_away = f"Away {-ah_line:+g}"
            ah_candidates = [
                ("AH_HOME", prob_ah_h, match.ah_home_odds, ah_label_home),
                ("AH_AWAY", prob_ah_a, match.ah_away_odds, ah_label_away),
            ]
            for outcome, prob, odds, label in ah_candidates:
                if not odds or odds <= 1.0:
                    continue
                kelly = calculate_kelly(
                    prob=prob, odds=odds,
                    bankroll=bankroll or 100.0,
                    risk_profile=RiskProfile(user.risk_profile),
                    kelly_user_fraction=user.kelly_fraction,
                )
                if kelly.is_value_bet:
                    team_name = match.home_team if outcome == "AH_HOME" else match.away_team
                    value_bets.append({
                        "outcome": outcome,
                        "outcome_label": f"{team_name} ({label.split(' ', 1)[1]})",
                        "market": "AH",
                        "ah_line": ah_line,
                        "odds": round(odds, 2),
                        "edge": round(kelly.edge, 4),
                        "edge_percent": round(kelly.edge * 100, 1),
                        "recommended_amount": kelly.recommended_amount,
                        "potential_gain": round((odds - 1) * kelly.recommended_amount, 2),
                        "prob": round(prob, 4),
                        "strategy": kelly.reason,
                    })

        value_bets.sort(key=lambda x: x["edge"], reverse=True)
        best = value_bets[0] if value_bets else None

        tier = _get_tier(best["edge"]) if best else None

        picks.append({
            "match_id": match.id,
            "sport": getattr(match, "sport", "FOOTBALL"),
            "home_team": match.home_team,
            "away_team": match.away_team,
            "league": match.league,
            "match_date": match.match_date.isoformat(),
            "kickoff_minutes": int((match.match_date.replace(tzinfo=timezone.utc) - now).total_seconds() / 60),
            "prob_home": round(pred.prob_home, 4) if pred else None,
            "prob_draw": round(pred.prob_draw, 4) if pred else None,
            "prob_away": round(pred.prob_away, 4) if pred else None,
            "prob_over_25": round(prob_over, 4) if (pred and prob_over is not None) else None,
            "prob_under_25": round(prob_under, 4) if (pred and prob_under is not None) else None,
            "prob_ah_home": round(prob_ah_h, 4) if (pred and prob_ah_h is not None) else None,
            "prob_ah_away": round(prob_ah_a, 4) if (pred and prob_ah_a is not None) else None,
            "confidence": round(pred.confidence, 4) if pred else None,
            "home_odds": match.home_odds,
            "draw_odds": match.draw_odds,
            "away_odds": match.away_odds,
            "over_25_odds": getattr(match, "over_25_odds", None),
            "under_25_odds": getattr(match, "under_25_odds", None),
            "ah_line": getattr(match, "ah_line", None),
            "ah_home_odds": getattr(match, "ah_home_odds", None),
            "ah_away_odds": getattr(match, "ah_away_odds", None),
            "best_bet": best,
            "all_value_bets": value_bets,
            "tier": tier,
            "has_value": best is not None,
            "league_whitelisted": is_whitelisted,
            "ou_whitelisted": ou_whitelisted,
            "ah_whitelisted": ah_whitelisted,
        })

    picks.sort(key=lambda x: (
        -(x["best_bet"]["edge"] if x["best_bet"] else -1),
        x["kickoff_minutes"],
    ))

    value_count = sum(1 for p in picks if p["has_value"])
    total_recommended = sum(
        p["best_bet"]["recommended_amount"] for p in picks if p["best_bet"]
    )

    return {
        "date": today_start.date().isoformat(),
        "sport": sport,
        "total_matches": len(picks),
        "value_matches": value_count,
        "total_recommended": round(total_recommended, 2),
        "bankroll": bankroll,
        "picks": picks,
    }
