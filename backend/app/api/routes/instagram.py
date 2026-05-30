"""
Endpoints admin pour publier des value bets sur Instagram.
Nécessite INSTAGRAM_ACCESS_TOKEN + INSTAGRAM_ACCOUNT_ID dans .env
"""
import logging
from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.deps import get_db, get_current_user
from app.core.redis import get_redis
from app.db.models import User, Match
from app.services.instagram_image import generate_value_bet_image
from app.services.instagram_publisher import publisher, build_caption
from app.services.recommendations import get_user_recommendations

logger = logging.getLogger("edgeai")

router = APIRouter(prefix="/instagram", tags=["instagram"])


def _image_public_url(filename: str) -> str:
    base = (settings.api_base_url or "").rstrip("/")
    # /api/v1/static = chemin servi par le backend (nginx route /api/* → backend).
    # /static au root tombait sur le frontend Next.js (404 côté Meta).
    return f"{base}/api/v1/static/instagram/{filename}"


@router.get("/status")
async def instagram_status(_user: User = Depends(get_current_user)):
    """Vérifie si Instagram est configuré."""
    return {
        "configured": publisher.is_configured,
        "account_id": settings.instagram_account_id or None,
        "api_base_url": settings.api_base_url or None,
    }


@router.post("/post/top-pick")
async def post_top_pick(
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
    user: User = Depends(get_current_user),
):
    """
    Publie le meilleur value bet du moment sur Instagram.
    Génère l'image + caption et appelle l'API Meta.
    """
    if not publisher.is_configured:
        raise HTTPException(
            status_code=503,
            detail="Instagram non configuré. Renseigne INSTAGRAM_ACCESS_TOKEN et INSTAGRAM_ACCOUNT_ID."
        )
    if not settings.api_base_url:
        raise HTTPException(
            status_code=503,
            detail="API_BASE_URL manquant dans .env (nécessaire pour l'URL publique de l'image)."
        )

    recs = await get_user_recommendations(user, db, redis, limit=1)
    if not recs:
        raise HTTPException(status_code=404, detail="Aucun value bet disponible pour le moment.")

    bet = recs[0]
    return await _publish_bet(bet)


@router.post("/post/match/{match_id}")
async def post_match_bet(
    match_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Publie le value bet d'un match spécifique sur Instagram.
    Utile pour choisir manuellement quel match mettre en avant.
    """
    if not publisher.is_configured:
        raise HTTPException(status_code=503, detail="Instagram non configuré.")
    if not settings.api_base_url:
        raise HTTPException(status_code=503, detail="API_BASE_URL manquant dans .env.")

    result = await db.execute(
        select(Match)
        .where(Match.id == match_id)
        .options(selectinload(Match.predictions))
    )
    match = result.scalar_one_or_none()
    if not match:
        raise HTTPException(status_code=404, detail="Match introuvable.")
    if not match.predictions:
        raise HTTPException(status_code=404, detail="Aucune prédiction pour ce match.")

    from app.services.recommendations import _find_best_bet
    outcome, kelly = _find_best_bet(match.predictions[0], match, user)

    if not kelly or not kelly.is_value_bet:
        raise HTTPException(status_code=422, detail="Ce match n'a pas de value bet détecté.")

    pred = match.predictions[0]
    bet = {
        "home_team": match.home_team,
        "away_team": match.away_team,
        "league": match.league,
        "match_date": match.match_date.isoformat(),
        "outcome": outcome,
        "edge": round(kelly.edge, 4),
        "kelly_stake": round(kelly.adjusted_fraction, 4),
        "odds": {"HOME": match.home_odds, "DRAW": match.draw_odds,
                 "AWAY": match.away_odds}.get(outcome) or 0,
        "prob_home": pred.prob_home,
        "prob_draw": pred.prob_draw,
        "prob_away": pred.prob_away,
        "confidence": pred.confidence,
    }
    return await _publish_bet(bet)


class AutoPostPayload(BaseModel):
    bet: dict


@router.post("/post/auto")
async def post_auto(
    payload: AutoPostPayload,
    x_service_token: str = Header(..., alias="X-Service-Token"),
):
    """
    Endpoint interne déclenché par le ML pipeline après détection de nouveaux value bets.
    Authentifié par X-Service-Token (partagé via INSTAGRAM_SERVICE_TOKEN dans .env).
    """
    if not settings.instagram_service_token:
        raise HTTPException(status_code=503, detail="INSTAGRAM_SERVICE_TOKEN non configuré.")
    if x_service_token != settings.instagram_service_token:
        raise HTTPException(status_code=401, detail="Token invalide.")
    if not publisher.is_configured:
        raise HTTPException(status_code=503, detail="Instagram non configuré.")
    if not settings.api_base_url:
        raise HTTPException(status_code=503, detail="API_BASE_URL manquant dans .env.")

    return await _publish_bet(payload.bet)


async def _publish_bet(bet: dict) -> dict:
    """Génère l'image, l'uploade et publie sur Instagram."""
    try:
        filepath = generate_value_bet_image(bet)
    except Exception as e:
        logger.error("instagram_image_error", exc_info=e)
        raise HTTPException(status_code=500, detail=f"Erreur génération image : {e}")

    image_url = _image_public_url(filepath.name)
    caption   = build_caption(bet)

    try:
        media_id = await publisher.post(image_url, caption)
    except Exception as e:
        logger.error("instagram_publish_error", exc_info=e)
        raise HTTPException(status_code=502, detail=f"Erreur publication Instagram : {e}")

    logger.info("instagram_published", media_id=media_id, match=f"{bet['home_team']} vs {bet['away_team']}")
    return {
        "published": True,
        "instagram_media_id": media_id,
        "image_url": image_url,
        "match": f"{bet['home_team']} vs {bet['away_team']}",
        "edge": bet.get("edge"),
        "odds": bet.get("odds"),
    }
