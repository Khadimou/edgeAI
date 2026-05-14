import json
from datetime import datetime, timedelta, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.db.models import Match, User
from app.services.kelly import calculate_kelly, RiskProfile

CACHE_TTL = 30 * 60  # 30 min — invalide si bankroll change


async def get_user_recommendations(
    user: User,
    db: AsyncSession,
    redis,
    limit: int = 10,
) -> list[dict]:
    bankroll_bucket = int((user.bankroll or 0) // 10)  # recompute si bankroll change de 10€+
    cache_key = f"recommendations:{user.id}:{bankroll_bucket}:{user.kelly_fraction}:{user.risk_profile}"
    cached = await redis.get(cache_key)
    if cached:
        return json.loads(cached)

    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(Match)
        .where(
            Match.status == "SCHEDULED",
            Match.match_date >= now,
            Match.match_date <= now + timedelta(hours=48),
            Match.league.in_(settings.value_bet_leagues),
        )
        .options(selectinload(Match.predictions))
        .order_by(Match.match_date)
        .limit(20)
    )
    upcoming = result.scalars().all()

    recs = []
    for match in upcoming:
        if not match.predictions:
            continue
        prediction = match.predictions[0]
        outcome, kelly = _find_best_bet(prediction, match, user)
        if kelly and kelly.is_value_bet:
            recs.append({
                "match_id": match.id,
                "home_team": match.home_team,
                "away_team": match.away_team,
                "league": match.league,
                "match_date": match.match_date.isoformat(),
                "outcome": outcome,
                "edge": round(kelly.edge, 4),
                "kelly_stake": round(kelly.adjusted_fraction, 4),
                "recommended_amount": kelly.recommended_amount,
                "odds": _get_odds(match, outcome),
                "prob_home": prediction.prob_home,
                "prob_draw": prediction.prob_draw,
                "prob_away": prediction.prob_away,
                "confidence": prediction.confidence,
                "strategy": kelly.reason,
                "model_version": prediction.model_version,
            })

    recs.sort(key=lambda x: x["edge"], reverse=True)
    recs = recs[:limit]

    await redis.setex(cache_key, CACHE_TTL, json.dumps(recs, default=str))
    return recs


def _find_best_bet(prediction, match: Match, user: User):
    candidates = [
        ("HOME", prediction.prob_home, match.home_odds),
        ("DRAW", prediction.prob_draw, match.draw_odds),
        ("AWAY", prediction.prob_away, match.away_odds),
    ]
    best_outcome = None
    best_kelly = None

    for outcome, prob, odds in candidates:
        if not odds or odds <= 1.0:
            continue
        kelly = calculate_kelly(
            prob=prob,
            odds=odds,
            bankroll=user.bankroll or 100.0,
            risk_profile=RiskProfile(user.risk_profile),
            kelly_user_fraction=user.kelly_fraction,
        )
        if kelly.is_value_bet and (best_kelly is None or kelly.edge > best_kelly.edge):
            best_kelly = kelly
            best_outcome = outcome

    return best_outcome, best_kelly


def _get_odds(match: Match, outcome: str) -> float:
    return {"HOME": match.home_odds, "DRAW": match.draw_odds, "AWAY": match.away_odds}.get(outcome) or 0.0
