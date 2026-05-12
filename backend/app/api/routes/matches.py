import json
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.deps import get_db, get_current_user
from app.core.redis import get_redis
from app.db.models import Match, Prediction, User
from app.services.kelly import calculate_kelly, RiskProfile

router = APIRouter(prefix="/matches", tags=["matches"])

CACHE_TTL = 300  # 5 min


@router.get("/upcoming")
async def get_upcoming_matches(
    league: str | None = Query(None),
    limit: int = Query(20, le=50),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
    _user: User = Depends(get_current_user),
):
    cache_key = f"matches:upcoming:{league or 'all'}:{limit}"
    cached = await redis.get(cache_key)
    if cached:
        return json.loads(cached)

    now = datetime.now(timezone.utc)
    stmt = (
        select(Match)
        .where(
            Match.status == "SCHEDULED",
            Match.match_date >= now,
            Match.match_date <= now + timedelta(hours=48),
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

    if prediction and user.bankroll > 0:
        candidates = [
            ("HOME", prediction.prob_home, match.home_odds),
            ("DRAW", prediction.prob_draw, match.draw_odds),
            ("AWAY", prediction.prob_away, match.away_odds),
        ]
        best = max(
            [(o, calculate_kelly(p, odd or 1.5, user.bankroll, RiskProfile(user.risk_profile)))
             for o, p, odd in candidates if odd],
            key=lambda x: x[1].edge,
            default=None,
        )
        if best and best[1].is_value_bet:
            outcome, kelly = best
            recommendation = {
                "id": f"temp_{match_id}",
                "match_id": match_id,
                "outcome": outcome,
                "edge": round(kelly.edge, 4),
                "kelly_stake": round(kelly.adjusted_fraction, 4),
                "recommended_amount": kelly.recommended_amount,
                "odds": _get_odds(match, outcome),
                "strategy": kelly.reason,
                "expires_at": None,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

    analysis = {
        "match": _serialize_match(match),
        "prediction": _serialize_prediction(prediction) if prediction else None,
        "recommendation": recommendation,
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
        "confidence": p.confidence,
        "shap_values": p.shap_values,
        "model_version": p.model_version,
        "computed_at": p.computed_at.isoformat(),
    }


def _get_odds(match: Match, outcome: str) -> float:
    return {"HOME": match.home_odds, "DRAW": match.draw_odds, "AWAY": match.away_odds}.get(outcome) or 0.0
