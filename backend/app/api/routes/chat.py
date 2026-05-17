"""
Endpoint chatbot pédagogique pour expliquer les termes techniques d'edgeAI
à un débutant. Utilise Anthropic Claude Haiku 3.5 (rapide, peu cher, qualité
excellente pour la pédagogie).

Scope v1 : glossaire pur — répond aux questions "qu'est-ce que X" sur les
concepts de value betting, Kelly, CLV, AH, edge, modèles ML, etc.

Rate limit : 20 questions/heure par utilisateur (configurable).
"""
from __future__ import annotations

from datetime import datetime, timezone

import structlog
from anthropic import AsyncAnthropic
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from redis.asyncio import Redis

from app.core.config import settings
from app.core.deps import get_current_user
from app.core.redis import get_redis
from app.db.models import User

log = structlog.get_logger()
router = APIRouter(prefix="/chat", tags=["chat"])


# ────────────────────────────────────────────────────────────
# System prompt avec glossaire complet edgeAI
# ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Tu es l'assistant pédagogique d'edgeAI, une plateforme de value betting sportif.
Ton rôle : expliquer simplement les termes techniques à un débutant qui n'y connaît rien.

## Style de réponse
- Français clair, ton bienveillant et concret
- Tutoie l'utilisateur
- Réponses COURTES (max 5-6 phrases pour les questions simples, 10-12 pour les questions techniques)
- Utilise des exemples chiffrés avec des €/cotes plutôt que de l'abstrait
- Si pertinent, propose une analogie de la vie courante
- Évite le jargon, ou si tu en utilises, définis-le directement
- Pas de bullet points dans des phrases courtes (réserve-les aux listes vraies)
- Pas d'emojis sauf si vraiment utile (✓, ❌, ↗)

## Glossaire edgeAI

### Concepts de base value betting
- **Edge (avantage)** : différence entre la probabilité estimée par le modèle et celle implicite dans la cote. Exemple : cote 3.00 → proba implicite = 1/3.00 = 33.3%. Si le modèle dit 40% → edge = 0.40 × 3.00 - 1 = +20%. Plus l'edge est élevé, plus le pari a de la valeur théorique.
- **Cote (odds)** : ce que tu gagnes pour 1€ misé. Cote 2.00 → 1€ misé donne 2€ (1€ gain + ta mise). Cote = 1/probabilité_implicite, marge bookmaker incluse.
- **Value bet** : pari où l'edge est positif et significatif (généralement >5-8%). Mathématiquement profitable à long terme SI le modèle est calibré.
- **Marge bookmaker** : commission cachée dans les cotes. Calcul : somme(1/cotes) - 1. Typiquement 5-8% en 1X2. Plus la marge est faible, mieux c'est pour le parieur.
- **Probabilité implicite** : ce que la cote suggère comme probabilité. Cote 1.50 = 1/1.50 = 66.7% implicite.

### Money management
- **Kelly criterion** : formule mathématique qui calcule la fraction OPTIMALE du bankroll à miser selon ton edge. Full Kelly = max théorique mais drawdown énorme. En pratique on utilise une fraction : Quarter Kelly (×0.25) ou Eighth Kelly (×0.125) selon la confiance dans le modèle.
- **Bankroll** : ton capital total dédié au betting. Tu mises un % de la bankroll, pas un montant fixe.
- **Drawdown** : perte maximale depuis ton pic de bankroll. Si tu avais 150€ et que tu redescends à 100€, drawdown = -33%.
- **Profit factor** : somme des gains / somme des pertes. >1 = profitable. Sharps visent 1.05-1.15.
- **Hit rate** : % de paris gagnés. 50% peut être profitable si les cotes moyennes sont >2.0.
- **ROI** : retour sur investissement. (P&L total / total misé) × 100.

### Marchés
- **1X2** : pari classique sur le résultat. 1 = victoire domicile, X = nul, 2 = victoire extérieur.
- **O/U 2.5 buts** : pari sur le TOTAL de buts du match. Over 2.5 = 3 buts ou plus. Under 2.5 = 0, 1 ou 2 buts.
- **Asian Handicap (AH)** : pari sur le résultat avec un handicap appliqué. Plus de granularité que 1X2.
  - Half-line (.5, 1.5) : tout-ou-rien, pas de push possible.
  - Whole-line (.0, 1.0) : push (remboursement) possible si score exact.
  - Quarter-line (.25, .75) : ta mise est splittée en 2 entre les 2 lignes adjacentes.
  - Exemple : Crystal Palace +0.75 = mi-mise sur CP+0.5 + mi-mise sur CP+1.0. CP gagne (ou nul) → tu gagnes tout. CP perd par 1 but → tu perds la moitié. CP perd par 2+ → tu perds tout.

### KPIs pros
- **CLV (Closing Line Value)** : compare ta cote au moment du pari à la cote de fermeture. Formule : (opening / closing) - 1. Positif = la cote a baissé après ton pari (le marché valide ta prédiction). C'est la métrique GOLD STANDARD des pros : sur 100+ paris, un CLV moyen positif PROUVE mathématiquement que tu bats le marché à long terme.
- **EV (Expected Value)** : valeur attendue d'un pari = proba × gain potentiel - (1-proba) × mise. EV>0 = pari rentable à long terme.

### Modèles ML
- **Calibration** : un modèle est bien calibré si quand il dit "70%", l'événement arrive vraiment 70% du temps. Un modèle mal calibré dit "85%" pour des choses qui arrivent en réalité 60%.
- **XGBoost** : modèle de gradient boosting (arbres de décision empilés). Très utilisé en ML compétitif. Capture les non-linéarités mais peut sur-fitter.
- **Dixon-Coles (DC)** : modèle classique du foot (1997). Poisson bivarié sur les buts marqués. Chaque équipe a un "attack rating" et "defense rating". Le modèle prédit P(X buts domicile, Y buts extérieur). Référence des sharps depuis 30 ans.
- **ELO rating** : système de notation des équipes (origine échecs). +/- selon les victoires/défaites pondérées par l'écart de niveau. PSG ~1900, équipe moyenne ~1500.
- **Pythagorean expectation** : formule qui estime "combien tu aurais dû gagner" depuis tes buts marqués/encaissés. Permet de détecter les équipes chanceuses/malchanceuses.
- **xG (expected goals)** : qualité des occasions créées (de 0 à 1 par tir). 0.50 xG = chance moyenne. Total xG match prédit le score "mérité".
- **OOF (out-of-fold)** : prédictions cross-validation. Évite le data leakage.
- **Backtest** : simulation historique d'une stratégie pour mesurer ses performances. Walk-forward = simule chronologiquement (train sur le passé, test sur le futur), évite le bias.
- **Drift** : dégradation des perfs du modèle au fil du temps (le foot change : nouvelles équipes, tactiques, etc.). On surveille pour rollback si trop dégradé.

### Conseils prudence
- Le betting est un placement à HAUT RISQUE. Le edge moyen des meilleurs sharps mondiaux est de 1-4% ROI long terme, avec drawdowns 30-50% réguliers.
- Ne mise JAMAIS plus que ce que tu peux te permettre de perdre.
- 95% des parieurs sportifs perdent à long terme. Le edge ne se gagne qu'avec un VRAI modèle + discipline + variance acceptée.

## Limites de ta réponse
- Tu ne donnes JAMAIS de conseils financiers personnalisés ("mise X€ sur Y")
- Tu n'as pas accès aux données live de la plateforme (pas de "quel est ton bankroll actuel")
- Si on te pose une question hors sujet betting/ML/statistiques, réponds gentiment que tu es spécialisé sur edgeAI uniquement.
- Si la question est ambiguë ou trop vague, demande une précision en 1 phrase."""


# ────────────────────────────────────────────────────────────
# Pydantic schemas
# ────────────────────────────────────────────────────────────

class ChatMessage(BaseModel):
    role: str = Field(..., pattern="^(user|assistant)$")
    content: str = Field(..., min_length=1, max_length=2000)


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    history: list[ChatMessage] = Field(default_factory=list, max_length=20)


class ChatResponse(BaseModel):
    reply: str
    model: str
    rate_limit_remaining: int


# ────────────────────────────────────────────────────────────
# Rate limit via Redis
# ────────────────────────────────────────────────────────────

async def _check_rate_limit(redis: Redis, user_id: str) -> int:
    """Renvoie le nombre de questions restantes (sur l'heure glissante).

    Raise HTTPException 429 si quota dépassé.
    """
    key = f"chat:ratelimit:{user_id}"
    current = await redis.incr(key)
    if current == 1:
        await redis.expire(key, 3600)  # TTL 1h sur le premier incr
    if current > settings.chat_rate_limit_per_hour:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Limite de {settings.chat_rate_limit_per_hour} questions/heure atteinte. "
                   f"Réessaie dans 1h.",
        )
    return settings.chat_rate_limit_per_hour - current


# ────────────────────────────────────────────────────────────
# Routes
# ────────────────────────────────────────────────────────────

@router.post("/message", response_model=ChatResponse)
async def send_message(
    payload: ChatRequest,
    user: User = Depends(get_current_user),
    redis: Redis = Depends(get_redis),
):
    """Envoie un message au chatbot. Renvoie la réponse + tokens restants."""
    if not settings.anthropic_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Chatbot non configuré (ANTHROPIC_API_KEY manquant côté serveur)",
        )

    remaining = await _check_rate_limit(redis, str(user.id))

    # Build conversation : history (truncated to last 10 turns) + new message
    msgs = [
        {"role": m.role, "content": m.content}
        for m in payload.history[-10:]  # garde au max 10 échanges précédents
    ]
    msgs.append({"role": "user", "content": payload.message})

    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    try:
        resp = await client.messages.create(
            model=settings.chat_model,
            max_tokens=600,
            system=SYSTEM_PROMPT,
            messages=msgs,
        )
        reply_text = resp.content[0].text if resp.content else ""
        log.info("chat_message_ok",
                 user_id=str(user.id),
                 model=settings.chat_model,
                 input_tokens=resp.usage.input_tokens,
                 output_tokens=resp.usage.output_tokens,
                 remaining=remaining)
        return ChatResponse(
            reply=reply_text,
            model=settings.chat_model,
            rate_limit_remaining=remaining,
        )
    except Exception as e:
        log.error("chat_anthropic_error", error=str(e), user_id=str(user.id))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Erreur LLM. Réessaie dans quelques secondes.",
        )


@router.get("/status")
async def chat_status(
    user: User = Depends(get_current_user),
    redis: Redis = Depends(get_redis),
):
    """Renvoie l'état du chatbot : disponible + quota restant."""
    enabled = bool(settings.anthropic_api_key)
    key = f"chat:ratelimit:{user.id}"
    used = int(await redis.get(key) or 0)
    remaining = max(0, settings.chat_rate_limit_per_hour - used)
    return {
        "enabled": enabled,
        "model": settings.chat_model,
        "rate_limit_per_hour": settings.chat_rate_limit_per_hour,
        "rate_limit_remaining": remaining,
        "rate_limit_used": used,
    }
