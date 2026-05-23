import json
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.deps import get_db, get_current_user
from app.core.redis import get_redis
from app.db.models import Match, Prediction, User
from app.services.kelly import calculate_kelly, RiskProfile

router = APIRouter(prefix="/matches", tags=["matches"])

CACHE_TTL = 300  # 5 min


@router.get("/upcoming")
async def get_upcoming_matches(
    league: str | None = Query(None),
    limit: int = Query(20, le=200),
    days: int = Query(2, ge=1, le=14),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
    _user: User = Depends(get_current_user),
):
    cache_key = f"matches:upcoming:{league or 'all'}:{limit}:{days}"
    cached = await redis.get(cache_key)
    if cached:
        return json.loads(cached)

    now = datetime.now(timezone.utc)
    stmt = (
        select(Match)
        .where(
            Match.status == "SCHEDULED",
            Match.match_date >= now,
            Match.match_date <= now + timedelta(days=days),
        )
        .options(selectinload(Match.predictions))
        .order_by(Match.match_date)
        .limit(limit)
    )
    if league:
        stmt = stmt.where(Match.league == league)

    result = await db.execute(stmt)
    matches = result.scalars().all()

    data = [_serialize_match(m) for m in matches]
    await redis.setex(cache_key, CACHE_TTL, json.dumps(data, default=str))
    return data


@router.get("/{match_id}/analysis")
async def get_match_analysis(
    match_id: str,
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
    user: User = Depends(get_current_user),
):
    cache_key = f"analysis:{match_id}:{user.id}"
    cached = await redis.get(cache_key)
    if cached:
        return json.loads(cached)

    result = await db.execute(
        select(Match)
        .where(Match.id == match_id)
        .options(selectinload(Match.predictions))
    )
    match = result.scalar_one_or_none()
    if not match:
        raise HTTPException(status_code=404, detail="Match introuvable")

    prediction = match.predictions[0] if match.predictions else None
    recommendation = None
    ou_recommendation = None
    ah_recommendation = None
    is_whitelisted = match.league in set(settings.value_bet_leagues)
    ou_whitelisted = match.league in set(settings.value_bet_ou_leagues)
    ah_whitelisted = match.league in set(settings.value_bet_ah_leagues)

    if prediction and user.bankroll > 0 and is_whitelisted:
        candidates = [
            ("HOME", prediction.prob_home, match.home_odds),
            ("DRAW", prediction.prob_draw, match.draw_odds),
            ("AWAY", prediction.prob_away, match.away_odds),
        ]
        best = max(
            [(o, calculate_kelly(
                prob=p,
                odds=odd or 1.5,
                bankroll=user.bankroll or 100.0,
                risk_profile=RiskProfile(user.risk_profile),
                kelly_user_fraction=user.kelly_fraction,
                market="1X2",
            )) for o, p, odd in candidates if odd],
            key=lambda x: x[1].edge,
            default=None,
        )
        if best and best[1].is_value_bet:
            outcome, kelly = best
            recommendation = {
                "id": f"temp_{match_id}",
                "match_id": match_id,
                "outcome": outcome,
                "market": "1X2",
                "edge": round(kelly.edge, 4),
                "kelly_stake": round(kelly.adjusted_fraction, 4),
                "recommended_amount": kelly.recommended_amount,
                "odds": _get_odds(match, outcome),
                "strategy": kelly.reason,
                "expires_at": None,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

    # Marché O/U 2.5
    prob_over = getattr(prediction, "prob_over_25", None) if prediction else None
    prob_under = getattr(prediction, "prob_under_25", None) if prediction else None
    if (prediction and user.bankroll > 0 and ou_whitelisted
            and prob_over is not None and prob_under is not None):
        ou_cands = [
            ("OVER", prob_over, match.over_25_odds),
            ("UNDER", prob_under, match.under_25_odds),
        ]
        best_ou = max(
            [(o, calculate_kelly(
                prob=p, odds=odd or 1.5,
                bankroll=user.bankroll or 100.0,
                risk_profile=RiskProfile(user.risk_profile),
                kelly_user_fraction=user.kelly_fraction,
                market="OU_2_5",
            )) for o, p, odd in ou_cands if odd],
            key=lambda x: x[1].edge,
            default=None,
        )
        if best_ou and best_ou[1].is_value_bet:
            outcome, kelly = best_ou
            ou_recommendation = {
                "id": f"temp_ou_{match_id}",
                "match_id": match_id,
                "outcome": outcome,
                "market": "OU_2_5",
                "edge": round(kelly.edge, 4),
                "kelly_stake": round(kelly.adjusted_fraction, 4),
                "recommended_amount": kelly.recommended_amount,
                "odds": match.over_25_odds if outcome == "OVER" else match.under_25_odds,
                "strategy": kelly.reason,
                "expires_at": None,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

    # Marché Asian Handicap
    prob_ah_h = getattr(prediction, "prob_ah_home", None) if prediction else None
    prob_ah_a = getattr(prediction, "prob_ah_away", None) if prediction else None
    if (prediction and user.bankroll > 0 and ah_whitelisted
            and prob_ah_h is not None and prob_ah_a is not None
            and match.ah_line is not None):
        ah_cands = [
            ("AH_HOME", prob_ah_h, match.ah_home_odds),
            ("AH_AWAY", prob_ah_a, match.ah_away_odds),
        ]
        best_ah = max(
            [(o, calculate_kelly(
                prob=p, odds=odd or 1.5,
                bankroll=user.bankroll or 100.0,
                risk_profile=RiskProfile(user.risk_profile),
                kelly_user_fraction=user.kelly_fraction,
                market="AH",
            )) for o, p, odd in ah_cands if odd],
            key=lambda x: x[1].edge,
            default=None,
        )
        if best_ah and best_ah[1].is_value_bet:
            outcome, kelly = best_ah
            team = match.home_team if outcome == "AH_HOME" else match.away_team
            line = match.ah_line if outcome == "AH_HOME" else -match.ah_line
            ah_recommendation = {
                "id": f"temp_ah_{match_id}",
                "match_id": match_id,
                "outcome": outcome,
                "market": "AH",
                "ah_line": match.ah_line,
                "team_name": team,
                "handicap": f"{line:+g}",
                "edge": round(kelly.edge, 4),
                "kelly_stake": round(kelly.adjusted_fraction, 4),
                "recommended_amount": kelly.recommended_amount,
                "odds": match.ah_home_odds if outcome == "AH_HOME" else match.ah_away_odds,
                "strategy": kelly.reason,
                "expires_at": None,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

    analysis = {
        "match": _serialize_match(match),
        "prediction": _serialize_prediction(prediction) if prediction else None,
        "recommendation": recommendation,
        "ou_recommendation": ou_recommendation,
        "ah_recommendation": ah_recommendation,
        "league_whitelisted": is_whitelisted,
        "ou_whitelisted": ou_whitelisted,
        "ah_whitelisted": ah_whitelisted,
        "home_form": {"recent": "N/A"},
        "away_form": {"recent": "N/A"},
        "h2h": {"last5": []},
        "value_assessment": {"market_efficiency": "medium", "closing_line_value": None},
    }

    await redis.setex(cache_key, CACHE_TTL, json.dumps(analysis, default=str))
    return analysis


def _serialize_match(m: Match) -> dict:
    pred = m.predictions[0] if m.predictions else None
    return {
        "id": m.id,
        "external_id": m.external_id,
        "sport": getattr(m, "sport", "FOOTBALL"),
        "league": m.league,
        "season": m.season,
        "home_team": m.home_team,
        "away_team": m.away_team,
        "match_date": m.match_date.isoformat(),
        "status": m.status,
        "home_score": m.home_score,
        "away_score": m.away_score,
        "home_odds": m.home_odds,
        "draw_odds": m.draw_odds,
        "away_odds": m.away_odds,
        "over_25_odds": getattr(m, "over_25_odds", None),
        "under_25_odds": getattr(m, "under_25_odds", None),
        "ah_line": getattr(m, "ah_line", None),
        "ah_home_odds": getattr(m, "ah_home_odds", None),
        "ah_away_odds": getattr(m, "ah_away_odds", None),
        "venue": m.venue,
        "prediction": _serialize_prediction(pred) if pred else None,
    }


def _serialize_prediction(p: Prediction | None) -> dict | None:
    if not p:
        return None
    return {
        "prob_home": p.prob_home,
        "prob_draw": p.prob_draw,
        "prob_away": p.prob_away,
        "prob_over_25": getattr(p, "prob_over_25", None),
        "prob_under_25": getattr(p, "prob_under_25", None),
        "prob_ah_home": getattr(p, "prob_ah_home", None),
        "prob_ah_away": getattr(p, "prob_ah_away", None),
        "confidence": p.confidence,
        "shap_values": p.shap_values,
        "model_version": p.model_version,
        "computed_at": p.computed_at.isoformat(),
    }


def _get_odds(match: Match, outcome: str) -> float:
    return {"HOME": match.home_odds, "DRAW": match.draw_odds, "AWAY": match.away_odds}.get(outcome) or 0.0
