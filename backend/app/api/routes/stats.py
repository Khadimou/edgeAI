from collections import defaultdict
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.deps import get_db, get_current_user
from app.db.models import Bet, User

router = APIRouter(prefix="/stats", tags=["stats"])


@router.get("/performance")
async def get_performance(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Bet)
        .where(Bet.user_id == user.id)
        .options(selectinload(Bet.match))
        .order_by(Bet.placed_at.asc())
    )
    bets = result.scalars().all()

    total = len(bets)
    won = sum(1 for b in bets if b.status == "WON")
    lost = sum(1 for b in bets if b.status == "LOST")
    pending = sum(1 for b in bets if b.status == "PENDING")
    total_pnl = sum(b.profit_loss or 0 for b in bets)
    total_staked = sum(b.amount for b in bets if b.status != "VOID")
    avg_odds = (sum(b.odds for b in bets) / total) if total > 0 else 0.0

    win_rate = (won / (won + lost)) if (won + lost) > 0 else 0.0
    roi = (total_pnl / total_staked * 100) if total_staked > 0 else 0.0

    settled = [b for b in bets if b.status in ("WON", "LOST")]
    best_streak, current_streak = _compute_streaks(settled)

    by_league: dict = defaultdict(lambda: {"bets": 0, "won": 0, "pnl": 0.0})
    for b in bets:
        if b.match:
            lg = b.match.league
            by_league[lg]["bets"] += 1
            if b.status == "WON":
                by_league[lg]["won"] += 1
            by_league[lg]["pnl"] = round(by_league[lg]["pnl"] + (b.profit_loss or 0), 2)

    by_outcome: dict = defaultdict(lambda: {"bets": 0, "won": 0, "pnl": 0.0})
    for b in bets:
        out = b.outcome
        by_outcome[out]["bets"] += 1
        if b.status == "WON":
            by_outcome[out]["won"] += 1
        by_outcome[out]["pnl"] = round(by_outcome[out]["pnl"] + (b.profit_loss or 0), 2)

    monthly: dict = defaultdict(float)
    for b in bets:
        monthly[b.placed_at.strftime("%Y-%m")] += b.profit_loss or 0

    return {
        "total_bets": total,
        "won": won,
        "lost": lost,
        "pending": pending,
        "win_rate": round(win_rate, 4),
        "roi_percent": round(roi, 2),
        "total_profit_loss": round(total_pnl, 2),
        "avg_odds": round(avg_odds, 2),
        "expected_value_realized": round(total_pnl, 2),
        "best_streak": best_streak,
        "current_streak": current_streak,
        "by_league": dict(by_league),
        "by_outcome": dict(by_outcome),
        "monthly_pnl": [{"month": k, "pnl": round(v, 2)} for k, v in sorted(monthly.items())],
    }


def _compute_streaks(settled: list[Bet]) -> tuple[int, int]:
    if not settled:
        return 0, 0
    best = current = 0
    last = None
    for b in reversed(settled):
        if b.status == last or last is None:
            current += 1
        else:
            current = 1
        last = b.status
        best = max(best, current)
    return best, current
