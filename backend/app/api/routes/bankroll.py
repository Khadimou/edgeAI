from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.deps import get_db, get_current_user
from app.db.models import BankrollHistory, User

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
    total_pnl = sum(h.amount for h in history if h.event_type in ("BET_WON", "BET_LOST"))
    roi = (total_pnl / total_deposited * 100) if total_deposited > 0 else 0.0

    return {
        "current_balance": user.bankroll,
        "total_deposited": total_deposited,
        "total_profit_loss": total_pnl,
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
