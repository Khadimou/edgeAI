from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.deps import get_db, get_current_user
from app.db.models import Bet, BankrollHistory, Match, User
from app.models.schemas import BetCreate, BetResultUpdate

router = APIRouter(prefix="/bets", tags=["bets"])


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_bet(
    payload: BetCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(select(Match).where(Match.id == payload.match_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Match introuvable")

    if user.bankroll < payload.amount:
        raise HTTPException(status_code=400, detail="Bankroll insuffisante")

    rec_id = payload.recommendation_id
    if rec_id and rec_id.startswith("temp_"):
        rec_id = None

    bet = Bet(
        user_id=user.id,
        match_id=payload.match_id,
        recommendation_id=rec_id,
        outcome=payload.outcome,
        amount=payload.amount,
        odds=payload.odds,
        bookmaker=payload.bookmaker,
        notes=payload.notes,
        status="PENDING",
    )
    db.add(bet)

    user.bankroll -= payload.amount
    db.add(BankrollHistory(
        user_id=user.id,
        amount=-payload.amount,
        balance=user.bankroll,
        event_type="BET_PLACED",
        reference_id=bet.id,
    ))

    await db.commit()
    await db.refresh(bet)

    result = await db.execute(
        select(Bet).where(Bet.id == bet.id).options(selectinload(Bet.match))
    )
    return _serialize_bet(result.scalar_one())


@router.patch("/{bet_id}/result")
async def update_bet_result(
    bet_id: str,
    payload: BetResultUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Bet).where(Bet.id == bet_id).options(selectinload(Bet.match))
    )
    bet = result.scalar_one_or_none()
    if not bet or bet.user_id != user.id:
        raise HTTPException(status_code=404, detail="Pari introuvable")
    if bet.status != "PENDING":
        raise HTTPException(status_code=400, detail="Ce pari a déjà été réglé")

    if payload.status == "WON":
        profit_loss = round(bet.amount * (bet.odds - 1), 2)
        credit = bet.amount + profit_loss
        event_type = "BET_WON"
    elif payload.status == "LOST":
        profit_loss = -bet.amount
        credit = 0.0
        event_type = "BET_LOST"
    else:
        profit_loss = 0.0
        credit = bet.amount
        event_type = "BET_VOID"

    bet.status = payload.status
    bet.profit_loss = profit_loss
    bet.settled_at = datetime.now(timezone.utc)

    if credit > 0:
        user.bankroll += credit
        db.add(BankrollHistory(
            user_id=user.id,
            amount=credit,
            balance=user.bankroll,
            event_type=event_type,
            reference_id=bet_id,
        ))

    await db.commit()
    await db.refresh(bet)
    return _serialize_bet(bet)


@router.get("/")
async def list_bets(
    status: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    stmt = (
        select(Bet)
        .where(Bet.user_id == user.id)
        .options(selectinload(Bet.match))
        .order_by(Bet.placed_at.desc())
        .limit(200)
    )
    if status:
        stmt = stmt.where(Bet.status == status)

    result = await db.execute(stmt)
    return [_serialize_bet(b) for b in result.scalars().all()]


def _serialize_bet(b: Bet) -> dict:
    match = b.match
    return {
        "id": b.id,
        "match_id": b.match_id,
        "recommendation_id": b.recommendation_id,
        "outcome": b.outcome,
        "amount": b.amount,
        "odds": b.odds,
        "status": b.status,
        "profit_loss": b.profit_loss,
        "bookmaker": b.bookmaker,
        "notes": b.notes,
        "placed_at": b.placed_at.isoformat(),
        "settled_at": b.settled_at.isoformat() if b.settled_at else None,
        "match": {
            "id": match.id,
            "home_team": match.home_team,
            "away_team": match.away_team,
            "league": match.league,
            "match_date": match.match_date.isoformat(),
            "status": match.status,
        } if match else None,
    }
