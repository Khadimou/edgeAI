"""
Notifications de value bets via Brevo (transactional email API).

Logique :
- À chaque fin de pipeline, on liste les value bets sur matchs SCHEDULED dans 48h
- On compare avec un set Redis 'notified' qui stocke les paris déjà signalés
- Pour les nouveaux : on envoie 1 email digest avec tout

Configuration via env :
- BREVO_API_KEY : la clé API Brevo (xkeysib-...)
- NOTIFICATION_EMAIL_TO : destinataire (ex: dioprassoul@gmail.com)
- NOTIFICATION_EMAIL_FROM : expéditeur (doit être un sender vérifié dans Brevo)
- APP_BASE_URL : pour les liens dans l'email (ex: https://edgeai-betting.duckdns.org)
"""
import os
import json
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

log = structlog.get_logger()

BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"
NOTIFIED_KEY_TTL = 7 * 24 * 3600  # garde le memo 7 jours

# Mêmes seuils Kelly que la prod
EDGE_MIN = 0.08
EDGE_MAX = 0.20


def _actual_edge(prob: float | None, odds: float | None) -> float | None:
    if not prob or not odds or odds <= 1.0:
        return None
    return prob * odds - 1


async def _find_current_value_bets(session: AsyncSession, settings) -> list[dict]:
    """
    Liste les value bets actuels (matchs SCHEDULED dans 48h, dans les whitelists).
    Renvoie une liste de dicts avec match_id, market, outcome, odds, edge, etc.
    """
    league_wl_1x2 = set(settings.value_bet_leagues)
    league_wl_ou = set(settings.value_bet_ou_leagues)
    league_wl_ah = set(settings.value_bet_ah_leagues)

    result = await session.execute(text("""
        SELECT m.id, m.league, m.home_team, m.away_team, m.match_date,
               m.home_odds, m.draw_odds, m.away_odds,
               m.over_25_odds, m.under_25_odds,
               m.ah_line, m.ah_home_odds, m.ah_away_odds,
               p.prob_home, p.prob_draw, p.prob_away,
               p.prob_over_25, p.prob_under_25,
               p.prob_ah_home, p.prob_ah_away
        FROM matches m
        JOIN LATERAL (
            SELECT * FROM predictions
            WHERE match_id = m.id
            ORDER BY computed_at DESC LIMIT 1
        ) p ON TRUE
        WHERE m.sport = 'FOOTBALL' AND m.status = 'SCHEDULED'
          AND m.match_date BETWEEN NOW() AND NOW() + interval '48 hours'
    """))
    rows = result.fetchall()

    value_bets = []
    for r in rows:
        match_id = r[0]
        league = r[1]
        home, away = r[2], r[3]
        match_date = r[4]

        # 1X2 (foot whitelisté)
        if league in league_wl_1x2:
            for outcome, prob, odds, label in [
                ("HOME", r[13], r[5], home),
                ("DRAW", r[14], r[6], "Match nul"),
                ("AWAY", r[15], r[7], away),
            ]:
                edge = _actual_edge(prob, odds)
                if edge is not None and EDGE_MIN <= edge <= EDGE_MAX:
                    value_bets.append({
                        "match_id": match_id, "league": league,
                        "home": home, "away": away,
                        "match_date": match_date.isoformat() if match_date else None,
                        "market": "1X2", "outcome": outcome, "label": label,
                        "prob": prob, "odds": odds, "edge": edge,
                    })

        # O/U 2.5 (foot whitelisté O/U)
        if league in league_wl_ou:
            for outcome, prob, odds, label in [
                ("OVER", r[16], r[8], "+2.5 buts"),
                ("UNDER", r[17], r[9], "-2.5 buts"),
            ]:
                edge = _actual_edge(prob, odds)
                if edge is not None and EDGE_MIN <= edge <= EDGE_MAX:
                    value_bets.append({
                        "match_id": match_id, "league": league,
                        "home": home, "away": away,
                        "match_date": match_date.isoformat() if match_date else None,
                        "market": "OU_2_5", "outcome": outcome, "label": label,
                        "prob": prob, "odds": odds, "edge": edge,
                    })

        # AH (foot whitelisté AH)
        if league in league_wl_ah and r[10] is not None:
            ah_line = r[10]
            for outcome, prob, odds, suffix in [
                ("AH_HOME", r[18], r[11], f"{home} ({ah_line:+g})"),
                ("AH_AWAY", r[19], r[12], f"{away} ({-ah_line:+g})"),
            ]:
                edge = _actual_edge(prob, odds)
                if edge is not None and EDGE_MIN <= edge <= EDGE_MAX:
                    value_bets.append({
                        "match_id": match_id, "league": league,
                        "home": home, "away": away,
                        "match_date": match_date.isoformat() if match_date else None,
                        "market": "AH", "outcome": outcome, "label": suffix,
                        "prob": prob, "odds": odds, "edge": edge, "ah_line": ah_line,
                    })

    return value_bets


def _vb_key(vb: dict) -> str:
    """Clé unique pour le dedup Redis."""
    return f"vb:notified:{vb['match_id']}:{vb['market']}:{vb['outcome']}"


def _build_email_html(bets: list[dict], app_url: str) -> str:
    """Construit le HTML du digest email."""
    if not bets:
        return ""
    # Tri par date puis edge
    bets = sorted(bets, key=lambda b: (b["match_date"] or "", -b["edge"]))

    rows_html = []
    for b in bets:
        edge_pct = b["edge"] * 100
        market_emoji = {"1X2": "⚽", "OU_2_5": "🎯", "AH": "📈"}.get(b["market"], "⚽")
        date_short = (b["match_date"] or "")[:16].replace("T", " ")
        match_url = f"{app_url}/match/{b['match_id']}"
        rows_html.append(f"""
        <tr style="border-bottom:1px solid #e5e7eb">
          <td style="padding:12px 8px;font-size:13px;color:#6b7280">{date_short}</td>
          <td style="padding:12px 8px;font-size:14px">
            <strong>{b['home']}</strong> vs <strong>{b['away']}</strong>
            <div style="color:#9ca3af;font-size:12px">{b['league']}</div>
          </td>
          <td style="padding:12px 8px">
            <span style="background:#dbeafe;color:#1e40af;padding:2px 8px;border-radius:4px;font-size:12px">
              {market_emoji} {b['market']}
            </span>
          </td>
          <td style="padding:12px 8px;font-size:13px"><strong>{b['label']}</strong></td>
          <td style="padding:12px 8px;text-align:right;font-family:monospace">{b['odds']:.2f}</td>
          <td style="padding:12px 8px;text-align:right;font-weight:bold;color:#059669">+{edge_pct:.1f}%</td>
          <td style="padding:12px 8px"><a href="{match_url}" style="color:#2563eb;text-decoration:none">Voir →</a></td>
        </tr>
        """)

    return f"""
    <html>
    <body style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;background:#f9fafb;margin:0;padding:20px">
      <div style="max-width:720px;margin:0 auto;background:#fff;border-radius:12px;padding:24px;border:1px solid #e5e7eb">
        <h1 style="margin:0 0 8px 0;color:#111827">⚡ {len(bets)} nouveau{'x' if len(bets)>1 else ''} value bet{'s' if len(bets)>1 else ''}</h1>
        <p style="color:#6b7280;margin:0 0 24px 0">Edge entre 8% et 20%, sur les ligues whitelistées par le backtest.</p>
        <table style="width:100%;border-collapse:collapse">
          <thead>
            <tr style="background:#f3f4f6">
              <th style="padding:10px 8px;text-align:left;font-size:11px;text-transform:uppercase;color:#6b7280">Date</th>
              <th style="padding:10px 8px;text-align:left;font-size:11px;text-transform:uppercase;color:#6b7280">Match</th>
              <th style="padding:10px 8px;text-align:left;font-size:11px;text-transform:uppercase;color:#6b7280">Marché</th>
              <th style="padding:10px 8px;text-align:left;font-size:11px;text-transform:uppercase;color:#6b7280">Pari</th>
              <th style="padding:10px 8px;text-align:right;font-size:11px;text-transform:uppercase;color:#6b7280">Cote</th>
              <th style="padding:10px 8px;text-align:right;font-size:11px;text-transform:uppercase;color:#6b7280">Edge</th>
              <th></th>
            </tr>
          </thead>
          <tbody>{"".join(rows_html)}</tbody>
        </table>
        <p style="color:#9ca3af;font-size:12px;margin-top:24px">
          Notification générée par edgeAI · <a href="{app_url}/today" style="color:#9ca3af">Tout voir</a>
        </p>
      </div>
    </body>
    </html>
    """


async def _send_brevo_email(api_key: str, sender: str, to: str, subject: str, html: str) -> bool:
    """Envoie un email transactionnel via Brevo API."""
    payload = {
        "sender": {"email": sender, "name": "edgeAI"},
        "to": [{"email": to}],
        "subject": subject,
        "htmlContent": html,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                BREVO_API_URL,
                headers={"api-key": api_key, "content-type": "application/json"},
                json=payload,
            )
            r.raise_for_status()
            log.info("brevo_email_sent", to=to, message_id=r.json().get("messageId"))
            return True
    except Exception as e:
        log.error("brevo_email_error", error=str(e))
        return False


async def notify_new_value_bets(session: AsyncSession, redis, settings) -> int:
    """
    Trouve les nouveaux value bets (jamais notifiés) et envoie un email digest.
    Renvoie le nombre de paris notifiés.
    """
    api_key = os.getenv("BREVO_API_KEY", "")
    to_email = os.getenv("NOTIFICATION_EMAIL_TO", "")
    from_email = os.getenv("NOTIFICATION_EMAIL_FROM", "")
    app_url = os.getenv("APP_BASE_URL", "https://edgeai-betting.duckdns.org")

    if not (api_key and to_email and from_email):
        log.info("notifications_skip_no_config")
        return 0

    current_bets = await _find_current_value_bets(session, settings)
    if not current_bets:
        return 0

    # Dedup via Redis : ne notifie que les nouveaux
    new_bets = []
    for vb in current_bets:
        key = _vb_key(vb)
        if not await redis.get(key):
            new_bets.append(vb)
            await redis.setex(key, NOTIFIED_KEY_TTL, "1")

    if not new_bets:
        log.info("notifications_nothing_new", current=len(current_bets))
        return 0

    subject = f"⚡ {len(new_bets)} value bet{'s' if len(new_bets) > 1 else ''} détecté{'s' if len(new_bets) > 1 else ''}"
    html = _build_email_html(new_bets, app_url)
    ok = await _send_brevo_email(api_key, from_email, to_email, subject, html)
    if ok:
        log.info("notifications_sent", count=len(new_bets), total=len(current_bets))
        return len(new_bets)
    return 0
