from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db, get_current_user
from app.db.models import User
from app.models.schemas import UserProfileUpdate, UserResponse

router = APIRouter(prefix="/user", tags=["user"])


def _user_to_dict(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "plan": user.plan,
        "bankroll": user.bankroll,
        "risk_profile": user.risk_profile,
        "kelly_fraction": user.kelly_fraction,
        "max_bets_per_day": user.max_bets_per_day,
        "alerts_enabled": user.alerts_enabled,
        "referral_code": user.referral_code,
        "created_at": user.created_at,
    }


@router.get("/me", response_model=UserResponse)
async def get_me(user: User = Depends(get_current_user)):
    return _user_to_dict(user)


@router.post("/profile", response_model=UserResponse)
async def update_profile(
    payload: UserProfileUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    update_data = payload.model_dump(exclude_none=True)
    for key, value in update_data.items():
        setattr(user, key, value)

    await db.commit()
    await db.refresh(user)
    return _user_to_dict(user)
