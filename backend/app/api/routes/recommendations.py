from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db, get_current_user, require_pro
from app.core.redis import get_redis
from app.db.models import User
from app.services.recommendations import get_user_recommendations

router = APIRouter(prefix="/recommendations", tags=["recommendations"])


@router.get("/")
async def list_recommendations(
    limit: int = Query(10, le=20),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
    user: User = Depends(require_pro),
):
    return await get_user_recommendations(user, db, redis, limit=limit)


@router.get("/preview")
async def preview_recommendations(
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
    user: User = Depends(get_current_user),
):
    recs = await get_user_recommendations(user, db, redis, limit=1)
    if recs and user.plan == "FREE":
        rec = recs[0]
        rec["recommended_amount"] = None
        rec["kelly_stake"] = None
        rec["strategy"] = "Passe à Pro pour voir la recommandation complète"
    return recs
