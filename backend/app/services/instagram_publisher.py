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
    """Caption Instagram grand public — zéro jargon, exemple concret."""
    home  = bet.get("home_team", "")
    away  = bet.get("away_team", "")
    league = bet.get("league", "")
    odds  = float(bet.get("odds") or 0)
    kelly = float(bet.get("kelly_stake") or 0)

    outcome_fr = {
        "HOME":    f"Victoire {home}",
        "DRAW":    "Match nul",
        "AWAY":    f"Victoire {away}",
        "OVER":    "Plus de 2.5 buts",
        "UNDER":   "Moins de 2.5 buts",
        "AH_HOME": f"{home} avec handicap",
        "AH_AWAY": f"{away} avec handicap",
    }.get(bet.get("outcome", ""), bet.get("outcome", "—"))

    try:
        from datetime import datetime
        match_date = bet.get("match_date", "")
        if isinstance(match_date, str):
            dt = datetime.fromisoformat(match_date.replace("Z", "+00:00"))
        else:
            dt = match_date
        date_str = dt.strftime("%d/%m à %Hh%M")
    except Exception:
        date_str = str(bet.get("match_date", ""))[:16]

    def fmt_eur(v: float) -> str:
        return f"{v:.2f}".replace(".", ",") + "€"

    # Exemples concrets : mise de 10€ (anchor universel)
    mise = 10.0
    gain_net = mise * (odds - 1)
    encaisse = mise * odds

    # Conseil de gestion : exemple sur un budget de 100€ avec ¼ Kelly traduit en €
    # (cap à 5% comme dans notre kelly engine pour rester prudent côté com)
    budget_ex = 100.0
    mise_conseillee = max(1.0, min(5.0, kelly * budget_ex))

    cote_fr = f"{odds:.2f}".replace(".", ",")
    return (
        f"🔥 LE COUP DU JOUR\n\n"
        f"⚽ {league}\n"
        f"{home} vs {away}\n"
        f"🕓 {date_str}\n\n"
        f"🎯 Notre pari : {outcome_fr}\n"
        f"💰 Cote : {cote_fr}\n\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"💸 EXEMPLE CONCRET\n\n"
        f"Si tu mises {fmt_eur(mise)} :\n"
        f"✅ Tu encaisses {fmt_eur(encaisse)} si ça passe\n"
        f"   (soit +{fmt_eur(gain_net)} de gain net)\n"
        f"❌ Tu perds {fmt_eur(mise)} sinon\n\n"
        f"💡 Notre conseil bankroll\n"
        f"Ne mise jamais plus de 1–2% de ton budget paris.\n"
        f"Avec {fmt_eur(budget_ex)} de budget → "
        f"environ {fmt_eur(mise_conseillee)} sur ce pari.\n"
        f"━━━━━━━━━━━━━━━━\n\n"
        f"🤔 Pourquoi ce match ?\n"
        f"Notre modèle estime que cette issue est plus probable que ce que "
        f"la cote indique. Quand on trouve un écart, on partage 👇\n\n"
        f"🔔 Résultat en story après le match\n"
        f"👉 Suis @edgebetfr pour les prochains coups\n\n"
        f"⚠️ Les paris sportifs comportent un risque. À 18+ uniquement. "
        f"Joue ce que tu peux te permettre de perdre.\n\n"
        f"#ChampionsLeague #PSG #Arsenal #Football #ParierMalin "
        f"#ValueBet #FootEuropéen #Tipster #ParisSportifs"
    )
