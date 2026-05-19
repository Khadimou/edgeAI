"""
Rapport hebdomadaire du tracking, envoyé chaque jeudi à 21h Europe/Paris.

Contenu :
- KPIs de la semaine écoulée (lundi → dimanche précédent) : N paris, ROI, P&L, hit, CLV
- Comparaison vs semaine précédente (delta ROI, delta volume, etc.)
- Top 3 victoires + Top 3 défaites de la semaine
- État global du modèle (sample 730j) avec sweep edge condensé
- Matchs à venir 7j (prochaine semaine) avec value bets détectées
- Alertes auto si métrique anormale

Le pipeline tourne toutes les 6h. À un cycle dans la plage [jeudi 21h, vendredi 03h]
le rapport part. Lock Redis empêche les doublons (clé = numéro de semaine ISO).

Configuration via env (réutilise notifications.py) :
- BREVO_API_KEY
- NOTIFICATION_EMAIL_TO (= dioprassoul@gmail.com)
- NOTIFICATION_EMAIL_FROM (sender vérifié Brevo)
- APP_BASE_URL
"""
from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .notifications import _send_brevo_email

log = structlog.get_logger()

# Constantes mêmes que tracking.py (cohérence stratégie déployée)
INITIAL_BANKROLL = 100.0
KELLY_FRACTION = 0.25
MAX_STAKE_FRACTION = 0.05
EDGE_MAX = 0.20

# Lock Redis : 6 jours pour ne pas renvoyer si plusieurs cycles entre jeudi 21h
# et vendredi 03h. Le N° de semaine ISO change le lundi → la clé sera différente
# la semaine suivante donc auto-reset.
WEEKLY_REPORT_LOCK_TTL = 6 * 24 * 3600
PARIS_TZ = ZoneInfo("Europe/Paris")


def _is_send_window_now() -> bool:
    """True si on est jeudi 21h-23h Europe/Paris (avec marge avant minuit)."""
    now = datetime.now(PARIS_TZ)
    return now.weekday() == 3 and now.hour >= 21  # weekday() : lundi=0, jeudi=3


def _iso_week_key(dt: datetime) -> str:
    iso_year, iso_week, _ = dt.isocalendar()
    return f"weekly_report:{iso_year}-W{iso_week:02d}"


async def _kpis_window(
    session: AsyncSession,
    settings,
    since: datetime,
    until: datetime,
    edge_min: float,
) -> dict:
    """Calcule N paris, ROI, P&L, hit rate, CLV moyen pour la fenêtre [since, until]
    en utilisant les mêmes filtres que /tracking (whitelists + edge ∈ [min, 0.20]).

    Renvoie un dict avec les KPIs + la liste des paris settled (pour Top wins/losses).
    """
    league_wl_1x2 = set(settings.value_bet_leagues)
    league_wl_ou = set(settings.value_bet_ou_leagues)
    league_wl_ah = set(settings.value_bet_ah_leagues)

    rows = (await session.execute(text("""
        SELECT m.id, m.sport, m.league, m.home_team, m.away_team,
               m.match_date, m.status, m.home_score, m.away_score,
               m.home_odds, m.draw_odds, m.away_odds,
               m.over_25_odds, m.under_25_odds, m.nba_total_line,
               m.ah_line, m.ah_home_odds, m.ah_away_odds,
               m.opening_home_odds, m.opening_draw_odds, m.opening_away_odds,
               m.opening_over_25_odds, m.opening_under_25_odds,
               m.opening_ah_home_odds, m.opening_ah_away_odds,
               p.prob_home, p.prob_draw, p.prob_away,
               p.prob_over_25, p.prob_under_25,
               p.prob_ah_home, p.prob_ah_away
        FROM matches m
        JOIN LATERAL (
            SELECT * FROM predictions
            WHERE match_id = m.id
            ORDER BY computed_at DESC LIMIT 1
        ) p ON TRUE
        WHERE m.match_date >= :since
          AND m.match_date < :until
          AND m.status NOT IN ('CANCELLED', 'POSTPONED')
        ORDER BY m.match_date ASC
    """), {"since": since.replace(tzinfo=None),
           "until": until.replace(tzinfo=None)})).fetchall()

    bankroll = INITIAL_BANKROLL
    bets_settled = []
    n_pending = 0
    clv_values = []

    for r in rows:
        (mid, sport, league, home, away, mdate, status,
         hs, as_, ho, do, ao, o25, u25, nba_line,
         ahl, aho, aao,
         oho, odo, oao, oo25, ou25, oaho, oaao,
         ph, pd_, pa, po, pu, pah, paa) = r

        # Collecte candidats par marché (mêmes règles que tracking.py)
        candidates = []
        if sport == "NBA":
            for outcome, prob, odds in [("HOME", ph, ho), ("AWAY", pa, ao)]:
                if odds and prob:
                    edge = prob * odds - 1
                    if edge_min <= edge <= EDGE_MAX:
                        candidates.append(("NBA", outcome, prob, odds, edge))
            if po and pu and o25 and u25 and nba_line is not None:
                for outcome, prob, odds in [("OVER", po, o25), ("UNDER", pu, u25)]:
                    edge = prob * odds - 1
                    if edge_min <= edge <= EDGE_MAX:
                        candidates.append(("NBA_TOTALS", outcome, prob, odds, edge))
        else:
            if league in league_wl_1x2:
                for outcome, prob, odds in [("HOME", ph, ho), ("DRAW", pd_, do), ("AWAY", pa, ao)]:
                    if odds and prob:
                        edge = prob * odds - 1
                        if edge_min <= edge <= EDGE_MAX:
                            candidates.append(("FOOTBALL_1X2", outcome, prob, odds, edge))
            if league in league_wl_ou and po and pu:
                for outcome, prob, odds in [("OVER", po, o25), ("UNDER", pu, u25)]:
                    if odds:
                        edge = prob * odds - 1
                        if edge_min <= edge <= EDGE_MAX:
                            candidates.append(("FOOTBALL_OU", outcome, prob, odds, edge))
            if league in league_wl_ah and pah and paa and ahl is not None:
                for outcome, prob, odds in [("AH_HOME", pah, aho), ("AH_AWAY", paa, aao)]:
                    if odds:
                        edge = prob * odds - 1
                        if edge_min <= edge <= EDGE_MAX:
                            candidates.append(("FOOTBALL_AH", outcome, prob, odds, edge))

        if not candidates:
            continue
        # On garde le meilleur edge par match (cohérent avec tracking)
        best = max(candidates, key=lambda c: c[4])
        market, outcome, prob, odds, edge = best

        # Mise Kelly
        b = odds - 1
        f_star = (prob * b - (1 - prob)) / b
        if f_star <= 0:
            continue
        stake = round(bankroll * min(f_star * KELLY_FRACTION, MAX_STAKE_FRACTION), 2)
        if stake < 1:
            continue

        settled = status == "FINISHED" and hs is not None and as_ is not None
        if not settled:
            n_pending += 1
            continue

        # P&L
        if market == "FOOTBALL_OU":
            actual = "OVER" if (hs + as_) > 2.5 else "UNDER"
            won = outcome == actual
        elif market == "NBA_TOTALS" and nba_line is not None:
            total = hs + as_
            if abs(total - nba_line) < 1e-9:
                won = None  # push
            else:
                actual = "OVER" if total > nba_line else "UNDER"
                won = outcome == actual
        elif market == "FOOTBALL_AH":
            # Simplification : on traite comme win/loss strict pour le rapport.
            # (le calcul push/quarter-line de tracking.py est plus précis)
            diff = hs - as_ + (ahl if outcome == "AH_HOME" else -ahl)
            won = diff > 0 if diff != 0 else None
        else:
            actual = "HOME" if hs > as_ else ("AWAY" if hs < as_ else "DRAW")
            won = outcome == actual

        if won is None:
            profit = 0.0
        elif won:
            profit = round(stake * (odds - 1), 2)
        else:
            profit = -stake
        bankroll = round(bankroll + profit, 2)

        # CLV (mêmes filtres que tracking : |CLV| ≤ 30%)
        opening = {
            "HOME": oho, "DRAW": odo, "AWAY": oao,
            "OVER": oo25, "UNDER": ou25,
            "AH_HOME": oaho, "AH_AWAY": oaao,
        }.get(outcome)
        if opening and odds and opening != odds and opening > 0:
            clv_raw = (opening / odds - 1) * 100
            if abs(clv_raw) <= 30:
                clv_values.append(clv_raw)

        bets_settled.append({
            "match_date": mdate,
            "home": home, "away": away,
            "league": league, "market": market, "outcome": outcome,
            "odds": odds, "edge": edge, "stake": stake, "profit": profit,
            "won": won, "score": f"{hs}-{as_}",
        })

    n_settled = len(bets_settled)
    n_wins = sum(1 for b in bets_settled if b["won"] is True)
    total_staked = sum(b["stake"] for b in bets_settled)
    total_pnl = sum(b["profit"] for b in bets_settled)
    roi = (total_pnl / total_staked * 100) if total_staked > 0 else 0.0
    hit = (n_wins / n_settled) if n_settled > 0 else 0.0
    clv_avg = (sum(clv_values) / len(clv_values)) if clv_values else None

    return {
        "n_settled": n_settled,
        "n_pending": n_pending,
        "n_wins": n_wins,
        "hit_rate": hit,
        "total_staked": round(total_staked, 2),
        "total_pnl": round(total_pnl, 2),
        "roi_percent": round(roi, 2),
        "bankroll": round(bankroll, 2),
        "clv_avg_percent": round(clv_avg, 2) if clv_avg is not None else None,
        "clv_sample": len(clv_values),
        "bets": bets_settled,
    }


def _delta_str(curr: float | None, prev: float | None, suffix: str = "") -> str:
    """Renvoie '+1.2pp' ou '-3.4pp' ou '—' si une valeur manque."""
    if curr is None or prev is None:
        return "—"
    d = curr - prev
    sign = "+" if d >= 0 else ""
    return f"{sign}{d:.1f}{suffix}"


def _delta_color(curr: float | None, prev: float | None) -> str:
    if curr is None or prev is None:
        return "#9ca3af"
    if curr > prev:
        return "#059669"
    if curr < prev:
        return "#dc2626"
    return "#6b7280"


def _build_report_html(
    week_kpis: dict,
    prev_kpis: dict,
    global_kpis: dict,
    upcoming_bets: list[dict],
    app_url: str,
    week_label: str,
) -> str:
    w = week_kpis
    p = prev_kpis

    # Top 3 victoires + défaites
    wins = sorted(
        [b for b in w["bets"] if b["won"] is True and b["profit"] > 0],
        key=lambda b: -b["profit"],
    )[:3]
    losses = sorted(
        [b for b in w["bets"] if b["won"] is False],
        key=lambda b: b["profit"],
    )[:3]

    def fmt_bet_row(b: dict, is_win: bool) -> str:
        color = "#059669" if is_win else "#dc2626"
        sign = "+" if is_win else ""
        return f"""
        <tr>
          <td style="padding:6px 8px;font-size:13px">{b['home']} vs {b['away']}</td>
          <td style="padding:6px 8px;font-size:12px;color:#6b7280">{b['score']}</td>
          <td style="padding:6px 8px;text-align:right;font-weight:bold;color:{color}">{sign}{b['profit']:.2f}€</td>
        </tr>
        """

    wins_html = "".join(fmt_bet_row(b, True) for b in wins) or '<tr><td colspan="3" style="padding:6px 8px;color:#9ca3af;font-size:12px;text-align:center">Aucune victoire cette semaine</td></tr>'
    losses_html = "".join(fmt_bet_row(b, False) for b in losses) or '<tr><td colspan="3" style="padding:6px 8px;color:#9ca3af;font-size:12px;text-align:center">Aucune perte cette semaine</td></tr>'

    # Recommandations à venir
    upcoming_html = ""
    if upcoming_bets:
        upcoming_rows = []
        for b in upcoming_bets[:10]:
            edge_pct = b["edge"] * 100
            dt = b["match_date"].strftime("%a %d/%m %H:%M") if b["match_date"] else "—"
            upcoming_rows.append(f"""
            <tr style="border-bottom:1px solid #e5e7eb">
              <td style="padding:6px 8px;font-size:12px;color:#6b7280">{dt}</td>
              <td style="padding:6px 8px;font-size:13px">{b['home']} vs {b['away']}</td>
              <td style="padding:6px 8px;font-size:11px;color:#6b7280">{b['market']}</td>
              <td style="padding:6px 8px;font-family:monospace;font-size:12px">{b['odds']:.2f}</td>
              <td style="padding:6px 8px;text-align:right;font-weight:bold;color:#059669;font-size:12px">+{edge_pct:.1f}%</td>
            </tr>
            """)
        upcoming_html = f"""
        <h2 style="margin:32px 0 12px 0;font-size:16px;color:#111827">📅 Recommandations à venir ({len(upcoming_bets)})</h2>
        <table style="width:100%;border-collapse:collapse;background:#f9fafb;border-radius:8px;overflow:hidden">
          <thead>
            <tr style="background:#e5e7eb"><th style="padding:8px;text-align:left;font-size:10px;text-transform:uppercase">Date</th><th style="padding:8px;text-align:left;font-size:10px;text-transform:uppercase">Match</th><th style="padding:8px;text-align:left;font-size:10px;text-transform:uppercase">Marché</th><th style="padding:8px;text-align:left;font-size:10px;text-transform:uppercase">Cote</th><th style="padding:8px;text-align:right;font-size:10px;text-transform:uppercase">Edge</th></tr>
          </thead>
          <tbody>{"".join(upcoming_rows)}</tbody>
        </table>
        """

    # Alertes auto
    alerts = []
    if w["n_settled"] >= 5 and w["roi_percent"] < -10:
        alerts.append(f"⚠ ROI semaine très négatif ({w['roi_percent']:+.1f}%) — vérifier les modèles")
    if global_kpis["max_drawdown_pct"] > 70:
        alerts.append(f"⚠ Drawdown max global > 70% ({global_kpis['max_drawdown_pct']:.1f}%) — risque tilt")
    if w["clv_sample"] >= 10 and w["clv_avg_percent"] is not None and w["clv_avg_percent"] < -2:
        alerts.append(f"⚠ CLV moyen semaine très négatif ({w['clv_avg_percent']:+.2f}%) — modèle dérive ?")
    alerts_html = ""
    if alerts:
        alerts_html = '<div style="background:#fef2f2;border:1px solid #fecaca;border-radius:8px;padding:12px;margin-bottom:24px"><strong style="color:#991b1b">Alertes</strong><ul style="margin:8px 0 0 0;padding-left:20px;color:#7f1d1d;font-size:13px">' + "".join(f"<li>{a}</li>" for a in alerts) + "</ul></div>"

    # Couleurs
    roi_color = "#059669" if w["roi_percent"] >= 0 else "#dc2626"
    pnl_color = "#059669" if w["total_pnl"] >= 0 else "#dc2626"
    pnl_sign = "+" if w["total_pnl"] >= 0 else ""
    roi_sign = "+" if w["roi_percent"] >= 0 else ""

    clv_str = f"{w['clv_avg_percent']:+.2f}%" if w["clv_avg_percent"] is not None else "—"
    prev_clv_str = f"{p['clv_avg_percent']:+.2f}%" if p["clv_avg_percent"] is not None else "—"

    return f"""
<html>
<body style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;background:#f9fafb;margin:0;padding:20px">
  <div style="max-width:680px;margin:0 auto;background:#fff;border-radius:12px;padding:28px;border:1px solid #e5e7eb">
    <h1 style="margin:0 0 4px 0;color:#111827;font-size:22px">📊 Rapport hebdo edgeAI</h1>
    <p style="color:#6b7280;margin:0 0 24px 0;font-size:14px">Semaine {week_label}</p>

    {alerts_html}

    <h2 style="margin:0 0 12px 0;font-size:16px;color:#111827">📈 KPIs de la semaine</h2>
    <table style="width:100%;border-collapse:collapse;margin-bottom:24px">
      <tr>
        <td style="padding:10px;background:#f3f4f6;border-radius:8px 0 0 8px">
          <div style="font-size:11px;color:#6b7280;text-transform:uppercase">ROI</div>
          <div style="font-size:24px;font-weight:bold;color:{roi_color}">{roi_sign}{w['roi_percent']:.1f}%</div>
          <div style="font-size:11px;color:{_delta_color(w['roi_percent'], p['roi_percent'])}">vs sem. -1 : {_delta_str(w['roi_percent'], p['roi_percent'], 'pp')}</div>
        </td>
        <td style="padding:10px;background:#f3f4f6">
          <div style="font-size:11px;color:#6b7280;text-transform:uppercase">P&L</div>
          <div style="font-size:24px;font-weight:bold;color:{pnl_color}">{pnl_sign}{w['total_pnl']:.0f}€</div>
          <div style="font-size:11px;color:#6b7280">sur {w['n_settled']} paris settled</div>
        </td>
        <td style="padding:10px;background:#f3f4f6">
          <div style="font-size:11px;color:#6b7280;text-transform:uppercase">Hit rate</div>
          <div style="font-size:24px;font-weight:bold;color:#111827">{w['hit_rate']*100:.1f}%</div>
          <div style="font-size:11px;color:#6b7280">{w['n_wins']}/{w['n_settled']} gagnés</div>
        </td>
        <td style="padding:10px;background:#f3f4f6;border-radius:0 8px 8px 0">
          <div style="font-size:11px;color:#6b7280;text-transform:uppercase">CLV moyen</div>
          <div style="font-size:24px;font-weight:bold;color:#111827">{clv_str}</div>
          <div style="font-size:11px;color:#6b7280">{w['clv_sample']} paris mesurés</div>
        </td>
      </tr>
    </table>

    <h2 style="margin:24px 0 12px 0;font-size:16px;color:#111827">🏆 État global du modèle (730 jours)</h2>
    <table style="width:100%;border-collapse:collapse;background:#fefce8;border-radius:8px;padding:12px;margin-bottom:24px">
      <tr>
        <td style="padding:12px;font-size:13px">
          <strong>ROI cumul</strong> : <span style="color:{'#059669' if global_kpis['roi_percent'] >= 0 else '#dc2626'};font-weight:bold">{('+' if global_kpis['roi_percent']>=0 else '')}{global_kpis['roi_percent']:.1f}%</span><br/>
          <strong>P&L cumul</strong> : <span style="font-weight:bold">{('+' if global_kpis['total_pnl']>=0 else '')}{global_kpis['total_pnl']:.0f}€</span> sur {global_kpis['n_settled']} paris settled<br/>
          <strong>Drawdown max</strong> : <span style="color:{'#dc2626' if global_kpis['max_drawdown_pct'] > 50 else '#6b7280'}">{global_kpis['max_drawdown_pct']:.1f}%</span><br/>
          <strong>CLV moyen</strong> : {global_kpis['clv_avg_percent']:+.2f}% sur {global_kpis['clv_sample']} paris<br/>
          <strong>Edge config</strong> : {global_kpis['edge_min_pct']:.0f}% — sweep complet sur <a href="{app_url}/tracking" style="color:#2563eb">/tracking</a>
        </td>
      </tr>
    </table>

    <table style="width:100%;border-collapse:collapse">
      <tr>
        <td style="vertical-align:top;width:50%;padding-right:8px">
          <h3 style="margin:0 0 8px 0;font-size:14px;color:#059669">✅ Top victoires</h3>
          <table style="width:100%;border-collapse:collapse">{wins_html}</table>
        </td>
        <td style="vertical-align:top;width:50%;padding-left:8px">
          <h3 style="margin:0 0 8px 0;font-size:14px;color:#dc2626">❌ Top défaites</h3>
          <table style="width:100%;border-collapse:collapse">{losses_html}</table>
        </td>
      </tr>
    </table>

    {upcoming_html}

    <p style="margin-top:32px;padding-top:16px;border-top:1px solid #e5e7eb;text-align:center">
      <a href="{app_url}/tracking" style="color:#2563eb;text-decoration:none;font-weight:500">Voir le tracking complet →</a>
    </p>
    <p style="color:#9ca3af;font-size:11px;margin-top:8px;text-align:center">
      edgeAI · Rapport généré automatiquement chaque jeudi 21h
    </p>
  </div>
</body>
</html>
"""


async def _fetch_upcoming_value_bets(session: AsyncSession, settings, edge_min: float) -> list[dict]:
    """Liste les value bets prévues dans les 7 prochains jours (FOOT, ligues whitelistées)."""
    league_wl_1x2 = set(settings.value_bet_leagues)
    league_wl_ah = set(settings.value_bet_ah_leagues)

    rows = (await session.execute(text("""
        SELECT m.id, m.league, m.home_team, m.away_team, m.match_date,
               m.home_odds, m.draw_odds, m.away_odds,
               m.ah_line, m.ah_home_odds, m.ah_away_odds,
               p.prob_home, p.prob_draw, p.prob_away,
               p.prob_ah_home, p.prob_ah_away
        FROM matches m
        JOIN LATERAL (
            SELECT * FROM predictions WHERE match_id = m.id
            ORDER BY computed_at DESC LIMIT 1
        ) p ON TRUE
        WHERE m.status = 'SCHEDULED'
          AND m.match_date BETWEEN NOW() AND NOW() + interval '7 days'
        ORDER BY m.match_date ASC
    """))).fetchall()

    out = []
    for r in rows:
        (mid, league, home, away, mdate, ho, do, ao, ahl, aho, aao,
         ph, pd_, pa, pah, paa) = r
        if league in league_wl_1x2:
            for outcome, prob, odds in [("1X2-HOME", ph, ho), ("1X2-DRAW", pd_, do), ("1X2-AWAY", pa, ao)]:
                if odds and prob:
                    edge = prob * odds - 1
                    if edge_min <= edge <= EDGE_MAX:
                        out.append({
                            "match_date": mdate, "home": home, "away": away,
                            "market": outcome, "odds": odds, "edge": edge,
                        })
        if league in league_wl_ah and pah and paa and ahl is not None:
            for outcome, prob, odds in [("AH-HOME", pah, aho), ("AH-AWAY", paa, aao)]:
                if odds:
                    edge = prob * odds - 1
                    if edge_min <= edge <= EDGE_MAX:
                        out.append({
                            "match_date": mdate, "home": home, "away": away,
                            "market": outcome, "odds": odds, "edge": edge,
                        })

    # Garde le meilleur edge par match
    seen = {}
    for vb in out:
        key = (vb["home"], vb["away"], vb["match_date"])
        if key not in seen or vb["edge"] > seen[key]["edge"]:
            seen[key] = vb
    return sorted(seen.values(), key=lambda v: -v["edge"])


async def send_weekly_report_if_due(session: AsyncSession, redis, settings) -> bool:
    """Envoie le rapport hebdo si : on est jeudi >21h Europe/Paris ET pas déjà envoyé cette semaine.

    Renvoie True si envoyé, False sinon.
    """
    if not _is_send_window_now():
        return False

    api_key = os.getenv("BREVO_API_KEY", "")
    to_email = os.getenv("NOTIFICATION_EMAIL_TO", "")
    from_email = os.getenv("NOTIFICATION_EMAIL_FROM", "")
    app_url = os.getenv("APP_BASE_URL", "https://edgeai-betting.duckdns.org")
    if not (api_key and to_email and from_email):
        log.info("weekly_report_skip_no_config")
        return False

    # Lock Redis : si déjà envoyé cette semaine ISO, on skip
    now = datetime.now(PARIS_TZ)
    lock_key = _iso_week_key(now)
    if await redis.get(lock_key):
        return False

    edge_min = settings.value_bet_edge_min

    # Fenêtres : semaine en cours (lundi 00h → jeudi 21h ~ now)
    # et semaine précédente (lundi-7j → lundi)
    monday_this = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    monday_prev = monday_this - timedelta(days=7)
    # Convertit en UTC naive pour la query (les dates DB sont stockées en UTC naive)
    monday_this_utc = monday_this.astimezone(timezone.utc).replace(tzinfo=None)
    monday_prev_utc = monday_prev.astimezone(timezone.utc).replace(tzinfo=None)
    now_utc = now.astimezone(timezone.utc).replace(tzinfo=None)

    week_kpis = await _kpis_window(session, settings,
                                   monday_this.astimezone(timezone.utc),
                                   now.astimezone(timezone.utc), edge_min)
    prev_kpis = await _kpis_window(session, settings,
                                   monday_prev.astimezone(timezone.utc),
                                   monday_this.astimezone(timezone.utc), edge_min)
    global_kpis = await _kpis_window(session, settings,
                                     now.astimezone(timezone.utc) - timedelta(days=730),
                                     now.astimezone(timezone.utc), edge_min)
    global_kpis["edge_min_pct"] = edge_min * 100

    # Drawdown max global (calcul rapide sur les bets settled)
    bets = sorted(global_kpis["bets"], key=lambda b: b["match_date"] or datetime.min)
    bankroll = INITIAL_BANKROLL
    peak = bankroll
    max_dd = 0.0
    for b in bets:
        bankroll += b["profit"]
        peak = max(peak, bankroll)
        if peak > 0:
            dd = (peak - bankroll) / peak
            if dd > max_dd:
                max_dd = dd
    global_kpis["max_drawdown_pct"] = round(max_dd * 100, 2)

    # Recommandations 7 prochains jours
    upcoming = await _fetch_upcoming_value_bets(session, settings, edge_min)

    week_label = f"du {monday_this.strftime('%d/%m')} au {now.strftime('%d/%m/%Y')}"
    html = _build_report_html(week_kpis, prev_kpis, global_kpis, upcoming, app_url, week_label)

    roi_sign = "+" if week_kpis["roi_percent"] >= 0 else ""
    subject = f"📊 edgeAI hebdo : ROI {roi_sign}{week_kpis['roi_percent']:.1f}% ({week_kpis['n_settled']} paris)"

    ok = await _send_brevo_email(api_key, from_email, to_email, subject, html)
    if ok:
        await redis.setex(lock_key, WEEKLY_REPORT_LOCK_TTL, "1")
        log.info("weekly_report_sent", to=to_email,
                 n_settled=week_kpis["n_settled"], roi=week_kpis["roi_percent"])
        return True
    return False
