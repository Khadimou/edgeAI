from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.core.deps import get_db, get_current_user
from app.db.models import BankrollHistory, Bet, User

router = APIRouter(prefix="/bankroll", tags=["bankroll"])


@router.get("/history")
async def get_bankroll_history(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(BankrollHistory)
        .where(BankrollHistory.user_id == user.id)
        .order_by(BankrollHistory.timestamp.asc())
        .limit(365)
    )
    history = result.scalars().all()

    total_deposited = sum(h.amount for h in history if h.event_type == "DEPOSIT" and h.amount > 0)

    # P&L réel : somme des profit_loss des paris settled (WON / LOST).
    # bankroll_history n'est PAS fiable comme source : les paris LOST n'y créent
    # aucune entrée (credit=0 → skip), et les WON stockent credit = mise + gain.
    pnl_result = await db.execute(
        select(func.coalesce(func.sum(Bet.profit_loss), 0.0))
        .where(
            Bet.user_id == user.id,
            Bet.status.in_(["WON", "LOST"]),
            Bet.profit_loss.is_not(None),
        )
    )
    total_pnl = float(pnl_result.scalar() or 0.0)

    # ROI sur mise totale settled (et pas sur deposits, qui peut être 0 si l'user
    # a juste rentré son bankroll sans transaction)
    staked_result = await db.execute(
        select(func.coalesce(func.sum(Bet.amount), 0.0))
        .where(
            Bet.user_id == user.id,
            Bet.status.in_(["WON", "LOST"]),
        )
    )
    total_staked = float(staked_result.scalar() or 0.0)
    roi = (total_pnl / total_staked * 100) if total_staked > 0 else 0.0

    return {
        "current_balance": user.bankroll,
        "total_deposited": total_deposited,
        "total_profit_loss": round(total_pnl, 2),
        "total_staked": round(total_staked, 2),
        "roi_percent": round(roi, 2),
        "history": [
            {
                "id": h.id,
                "amount": h.amount,
                "balance": h.balance,
                "event_type": h.event_type,
                "reference_id": h.reference_id,
                "timestamp": h.timestamp.isoformat(),
            }
            for h in history
        ],
    }
