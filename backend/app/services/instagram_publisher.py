"""
Client Instagram Graph API — publication de posts image.
Doc : https://developers.facebook.com/docs/instagram-api/guides/content-publishing
"""
from __future__ import annotations
import httpx
from app.core.config import settings


class InstagramPublisher:
    # Instagram Login Direct API (2024+) : tokens IGAA... via graph.instagram.com.
    # L'ancien flow Facebook Graph (graph.facebook.com + tokens EAA... via Page FB)
    # rejette les tokens IGAA avec "Cannot parse access token".
    BASE = "https://graph.instagram.com/v21.0"

    @property
    def _configured(self) -> bool:
        return bool(settings.instagram_access_token and settings.instagram_account_id)

    def _require_config(self) -> None:
        if not self._configured:
            raise RuntimeError(
                "Instagram non configuré. "
                "Renseigne INSTAGRAM_ACCESS_TOKEN et INSTAGRAM_ACCOUNT_ID dans .env"
            )

    async def _create_container(self, image_url: str, caption: str) -> str:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{self.BASE}/{settings.instagram_account_id}/media",
                params={
                    "image_url": image_url,
                    "caption": caption,
                    "access_token": settings.instagram_access_token,
                },
            )
            if r.status_code >= 400:
                # Surface le message d'erreur Meta (sinon raise_for_status masque tout)
                raise RuntimeError(
                    f"Meta /media {r.status_code} : {r.text[:500]} "
                    f"(image_url={image_url})"
                )
            return r.json()["id"]

    async def _publish_container(self, creation_id: str) -> str:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{self.BASE}/{settings.instagram_account_id}/media_publish",
                params={
                    "creation_id": creation_id,
                    "access_token": settings.instagram_access_token,
                },
            )
            if r.status_code >= 400:
                raise RuntimeError(
                    f"Meta /media_publish {r.status_code} : {r.text[:500]}"
                )
            return r.json()["id"]

    async def post(self, image_url: str, caption: str) -> str:
        """Publie un post image. Retourne l'instagram media_id."""
        self._require_config()
        container_id = await self._create_container(image_url, caption)
        return await self._publish_container(container_id)

    @property
    def is_configured(self) -> bool:
        return self._configured


publisher = InstagramPublisher()


def build_caption(bet: dict) -> str:
    """Formate le texte du post Instagram pour un value bet."""
    home  = bet.get("home_team", "")
    away  = bet.get("away_team", "")
    league = bet.get("league", "")
    odds  = float(bet.get("odds") or 0)
    edge  = float(bet.get("edge") or 0)
    kelly = float(bet.get("kelly_stake") or 0)

    outcome_fr = {
        "HOME":    f"Victoire {home}",
        "DRAW":    "Match nul",
        "AWAY":    f"Victoire {away}",
        "OVER":    "Plus de 2.5 buts",
        "UNDER":   "Moins de 2.5 buts",
        "AH_HOME": f"Asian Handicap {home}",
        "AH_AWAY": f"Asian Handicap {away}",
    }.get(bet.get("outcome", ""), bet.get("outcome", "—"))

    edge_pct    = round(edge * 100, 1)
    market_pct  = round((1 / odds * 100) if odds > 1 else 0, 1)
    units       = round(kelly * 100, 1)

    try:
        from datetime import datetime
        match_date = bet.get("match_date", "")
        if isinstance(match_date, str):
            dt = datetime.fromisoformat(match_date.replace("Z", "+00:00"))
        else:
            dt = match_date
        date_str = dt.strftime("%d/%m/%Y à %Hh%M")
    except Exception:
        date_str = str(bet.get("match_date", ""))[:16]

    return (
        f"🎯 VALUE BET DU JOUR\n\n"
        f"🏆 {league}\n"
        f"⚽ {home} vs {away}\n"
        f"📅 {date_str}\n\n"
        f"📊 Notre analyse :\n"
        f"• Pari : {outcome_fr}\n"
        f"• Cote : {odds:.2f} (marché implique {market_pct:.0f}%)\n"
        f"• Edge détecté : +{edge_pct:.1f}% ✅\n"
        f"• Mise recommandée : {units:.1f} unités (Kelly)\n\n"
        f"📈 Résultat en story après le match\n\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🔔 Suis @edgebet pour ne rien manquer\n"
        f"Investir, pas parier. 🧠\n\n"
        f"#ValueBet #EdgeBet #ParisResponsables #ParierMalin "
        f"#BettingStrategy #Football #Tipster"
    )
