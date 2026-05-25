"""
Rapport hebdomadaire du tracking, envoyé chaque jeudi à 21h Europe/Paris.

Contenu :
- KPIs de la semaine écoulée (lundi → dimanche précédent) : N paris, ROI, P&L, hit, CLV
- Comparaison vs semaine précédente (delta ROI, delta volume, etc.)
- Top 3 victoires + Top 3 défaites de la semaine
- État global du modèle (sample 730j) avec sweep edge condensé
- Matchs à venir 7j (prochaine semaine) avec value bets détectées
- Alertes auto si métrique anormale
- 🆕 Analyse IA générée par Claude Haiku (commentaire 200-300 mots avec
  interprétation des chiffres et suggestions d'amélioration)
- 🆕 Breakdown ROI par marché (1X2 / AH / OU / NBA) et par ligue
- 🆕 Indicateurs avancés : profit factor, Sharpe-like, cote moyenne pariée
- 🆕 Statut système : quota odds-api, dernière date de retrain

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

import httpx
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


def _model_metrics_from_rows(rows: list) -> dict:
    """Métriques prédictives 1X2 (mêmes formules que /model/performance).
    rows : list de (prob_home, prob_draw, prob_away, home_score, away_score).
    """
    from math import log
    EPS = 1e-15
    if not rows:
        return {"n": 0, "accuracy": None, "log_loss": None, "brier_score": None,
                "home_accuracy": None, "draw_accuracy": None, "away_accuracy": None}
    correct = 0
    hc = ht = dc = dt = ac = at = 0
    ll_sum = brier_sum = 0.0
    for ph, pd_, pa, hs, as_ in rows:
        if None in (ph, pd_, pa, hs, as_):
            continue
        actual = 0 if hs > as_ else (1 if hs == as_ else 2)
        probs = [ph, pd_, pa]
        pred = max(range(3), key=lambda i: probs[i])
        if pred == actual:
            correct += 1
        if actual == 0:
            ht += 1; hc += (pred == 0)
        elif actual == 1:
            dt += 1; dc += (pred == 1)
        else:
            at += 1; ac += (pred == 2)
        p_act = max(min(probs[actual], 1 - EPS), EPS)
        ll_sum += -log(p_act)
        for i in range(3):
            brier_sum += (probs[i] - (1.0 if i == actual else 0.0)) ** 2
    n = sum(1 for r in rows if None not in r)
    if n == 0:
        return {"n": 0, "accuracy": None, "log_loss": None, "brier_score": None,
                "home_accuracy": None, "draw_accuracy": None, "away_accuracy": None}
    return {
        "n": n,
        "accuracy": round(correct / n, 4),
        "log_loss": round(ll_sum / n, 4),
        "brier_score": round(brier_sum / n, 4),
        "home_accuracy": round(hc / ht, 4) if ht else None,
        "draw_accuracy": round(dc / dt, 4) if dt else None,
        "away_accuracy": round(ac / at, 4) if at else None,
    }


async def _model_perf_window(session: AsyncSession, since: datetime, until: datetime) -> dict:
    """Performance prédictive du modèle 1X2 sur les matchs FINISHED de la fenêtre.

    Mesure la qualité des prédictions (toutes, pas seulement les value bets) vs
    résultats réels. Exclut les prédictions de backfill (data leak). Ajoute aussi
    une calibration O/U 2.5 simple (prob over moyenne prédite vs taux over réel).
    """
    rows = (await session.execute(text("""
        SELECT p.prob_home, p.prob_draw, p.prob_away,
               m.home_score, m.away_score,
               p.prob_over_25, m.sport,
               m.ah_line, p.prob_ah_home, p.prob_ah_away
        FROM matches m
        JOIN LATERAL (
            SELECT * FROM predictions
            WHERE match_id = m.id AND model_version NOT LIKE 'backfill_%'
            ORDER BY computed_at DESC LIMIT 1
        ) p ON TRUE
        WHERE m.status = 'FINISHED'
          AND m.home_score IS NOT NULL AND m.away_score IS NOT NULL
          AND m.match_date >= :since AND m.match_date < :until
    """), {"since": since.replace(tzinfo=None),
           "until": until.replace(tzinfo=None)})).fetchall()

    metrics = _model_metrics_from_rows([(r[0], r[1], r[2], r[3], r[4]) for r in rows])

    # Calibration O/U 2.5 (foot uniquement, ligne fixe 2.5)
    ou_pred, ou_actual = [], []
    # AH : calibration (P(home couvre) prédite vs réelle) + accuracy du côté prédit.
    # Settlement simplifié home : margin = (hs - as) + ah_line ; push (margin==0) exclu.
    ah_pred, ah_actual, ah_correct, ah_total = [], [], 0, 0
    for r in rows:
        po, sport = r[5], r[6]
        hs, as_ = r[3], r[4]
        if po is not None and sport != "NBA":
            ou_pred.append(float(po))
            ou_actual.append(1.0 if (hs + as_) > 2.5 else 0.0)

        ahl, pah, paa = r[7], r[8], r[9]
        if sport != "NBA" and ahl is not None and pah is not None and paa is not None:
            margin = (hs - as_) + ahl
            if abs(margin) < 1e-9:
                continue  # push : exclu de la calibration AH
            home_covered = margin > 0
            ah_pred.append(float(pah))
            ah_actual.append(1.0 if home_covered else 0.0)
            ah_total += 1
            if (pah >= paa) == home_covered:  # côté le plus probable = côté qui a couvert
                ah_correct += 1

    if ou_pred:
        metrics["ou_n"] = len(ou_pred)
        metrics["ou_pred_over"] = round(sum(ou_pred) / len(ou_pred), 4)
        metrics["ou_actual_over"] = round(sum(ou_actual) / len(ou_actual), 4)
        metrics["ou_calib_gap"] = round(metrics["ou_pred_over"] - metrics["ou_actual_over"], 4)
    else:
        metrics["ou_n"] = 0
        metrics["ou_pred_over"] = metrics["ou_actual_over"] = metrics["ou_calib_gap"] = None

    if ah_total:
        metrics["ah_n"] = ah_total
        metrics["ah_accuracy"] = round(ah_correct / ah_total, 4)
        metrics["ah_pred_home"] = round(sum(ah_pred) / len(ah_pred), 4)
        metrics["ah_actual_home"] = round(sum(ah_actual) / len(ah_actual), 4)
        metrics["ah_calib_gap"] = round(metrics["ah_pred_home"] - metrics["ah_actual_home"], 4)
    else:
        metrics["ah_n"] = 0
        metrics["ah_accuracy"] = metrics["ah_pred_home"] = None
        metrics["ah_actual_home"] = metrics["ah_calib_gap"] = None
    return metrics


def _breakdown_by_dimension(bets: list[dict], dim: str) -> list[dict]:
    """Aggrège les paris par marché ou par ligue. Renvoie une liste triée par ROI desc."""
    buckets: dict[str, dict] = {}
    for b in bets:
        if b["won"] is None:  # push, ignore
            continue
        key = b.get(dim, "—")
        bk = buckets.setdefault(key, {"n": 0, "wins": 0, "staked": 0.0, "pnl": 0.0})
        bk["n"] += 1
        if b["won"]:
            bk["wins"] += 1
        bk["staked"] += b["stake"]
        bk["pnl"] += b["profit"]
    rows = []
    for key, bk in buckets.items():
        roi = (bk["pnl"] / bk["staked"] * 100) if bk["staked"] > 0 else 0.0
        rows.append({
            "label": key,
            "n": bk["n"],
            "hit_rate": (bk["wins"] / bk["n"]) if bk["n"] > 0 else 0.0,
            "pnl": round(bk["pnl"], 2),
            "roi": round(roi, 1),
        })
    return sorted(rows, key=lambda r: -r["roi"])


def _advanced_indicators(bets: list[dict]) -> dict:
    """Calcule indicateurs financiers avancés sur les bets settled."""
    settled = [b for b in bets if b["won"] is not None]
    if not settled:
        return {
            "profit_factor": None,
            "sharpe_like": None,
            "avg_odds": None,
            "max_win_streak": 0,
            "max_loss_streak": 0,
        }
    gross_wins = sum(b["profit"] for b in settled if b["profit"] > 0)
    gross_losses = sum(-b["profit"] for b in settled if b["profit"] < 0)
    profit_factor = (gross_wins / gross_losses) if gross_losses > 0 else None

    # Sharpe-like : ROI / std des profits unitaires (par euro misé)
    unit_returns = [b["profit"] / b["stake"] for b in settled if b["stake"] > 0]
    if len(unit_returns) >= 2:
        mean_r = sum(unit_returns) / len(unit_returns)
        var = sum((r - mean_r) ** 2 for r in unit_returns) / (len(unit_returns) - 1)
        std = var ** 0.5
        sharpe = mean_r / std if std > 0 else None
    else:
        sharpe = None

    avg_odds = sum(b["odds"] for b in settled) / len(settled)

    # Streaks (par ordre chronologique)
    settled_sorted = sorted(settled, key=lambda b: b["match_date"] or datetime.min)
    max_w = max_l = cur_w = cur_l = 0
    for b in settled_sorted:
        if b["won"] is True:
            cur_w += 1
            cur_l = 0
            max_w = max(max_w, cur_w)
        elif b["won"] is False:
            cur_l += 1
            cur_w = 0
            max_l = max(max_l, cur_l)

    return {
        "profit_factor": round(profit_factor, 2) if profit_factor is not None else None,
        "sharpe_like": round(sharpe, 3) if sharpe is not None else None,
        "avg_odds": round(avg_odds, 2),
        "max_win_streak": max_w,
        "max_loss_streak": max_l,
    }


def _system_status_html(odds_remaining: int | None, retrain_dates: dict) -> str:
    """Petite section footer avec statut système."""
    quota_color = "#dc2626" if (odds_remaining is None or odds_remaining == 0) else \
                  "#f59e0b" if odds_remaining < 50 else "#059669"
    quota_str = f"{odds_remaining} req restantes" if odds_remaining is not None else "inconnu"

    retrain_lines = []
    for label, dt_str in retrain_dates.items():
        retrain_lines.append(f"<li>{label} : {dt_str or '—'}</li>")
    retrain_html = "<ul style='margin:4px 0 0 0;padding-left:20px;font-size:12px;color:#6b7280'>" + "".join(retrain_lines) + "</ul>"

    return f"""
<h3 style="margin:24px 0 8px 0;font-size:14px;color:#111827">🔧 Statut système</h3>
<div style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px;padding:12px;font-size:13px">
  <strong>Quota odds-api :</strong> <span style="color:{quota_color};font-weight:bold">{quota_str}</span>
  <div style="margin-top:8px"><strong>Dernier retrain :</strong>{retrain_html}</div>
</div>
"""


async def _generate_ai_analysis(
    week_kpis: dict,
    prev_kpis: dict,
    global_kpis: dict,
    per_market: list[dict],
    per_league: list[dict],
    advanced: dict,
    model_perf: dict | None = None,
    model_perf_prev: dict | None = None,
) -> str:
    """Génère un commentaire d'analyse via Claude Haiku 4.5.

    Coût ~$0.002 par appel = $0.10/an pour 52 rapports. Si la clé manque
    ou l'API échoue, renvoie un fallback texte basique.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return _fallback_analysis(week_kpis, prev_kpis, global_kpis)

    # Compose le contexte data pour Claude
    pf = advanced["profit_factor"]
    sharpe = advanced["sharpe_like"]
    data_context = f"""SEMAINE EN COURS
- N paris settled : {week_kpis['n_settled']} (en attente : {week_kpis['n_pending']})
- ROI : {week_kpis['roi_percent']:+.1f}%
- P&L : {week_kpis['total_pnl']:+.2f}€ sur {week_kpis['total_staked']:.0f}€ misés
- Hit rate : {week_kpis['hit_rate']*100:.1f}% ({week_kpis['n_wins']}/{week_kpis['n_settled']})
- CLV moyen : {week_kpis['clv_avg_percent']:+.2f}% si non-None (sample {week_kpis['clv_sample']})

SEMAINE PRÉCÉDENTE (comparaison)
- N paris settled : {prev_kpis['n_settled']}
- ROI : {prev_kpis['roi_percent']:+.1f}%
- Hit rate : {prev_kpis['hit_rate']*100:.1f}%

CUMUL 730 JOURS (état global du modèle)
- N paris settled : {global_kpis['n_settled']}
- ROI : {global_kpis['roi_percent']:+.1f}%
- Drawdown max : {global_kpis['max_drawdown_pct']:.1f}%
- CLV moyen cumul : {global_kpis['clv_avg_percent']:+.2f}% si non-None

INDICATEURS AVANCÉS (sur la semaine)
- Profit factor : {pf if pf is not None else 'N/A'} (=somme gains / somme pertes ; >1 = profitable)
- Sharpe-like : {sharpe if sharpe is not None else 'N/A'} (return moyen normalisé par variance)
- Cote moyenne pariée : {advanced['avg_odds']}
- Plus longue série de victoires : {advanced['max_win_streak']}
- Plus longue série de défaites : {advanced['max_loss_streak']}

BREAKDOWN PAR MARCHÉ
""" + "\n".join(f"- {m['label']} : {m['n']} paris, ROI {m['roi']:+.1f}%, hit {m['hit_rate']*100:.0f}%"
               for m in per_market[:6]) + """

BREAKDOWN PAR LIGUE
""" + "\n".join(f"- {l['label']} : {l['n']} paris, ROI {l['roi']:+.1f}%, hit {l['hit_rate']*100:.0f}%"
               for l in per_league[:6])

    # Performance PRÉDICTIVE du modèle (distincte du ROI des paris)
    mp = model_perf or {}
    mpp = model_perf_prev or {}
    if mp.get("n"):
        prev_acc = mpp.get("accuracy")
        prev_acc_txt = f"{prev_acc*100:.1f}%" if prev_acc is not None else "N/A"
        ou_txt = ""
        if mp.get("ou_n"):
            ou_txt = (f"\n- Calibration O/U 2.5 : prédit {mp['ou_pred_over']*100:.0f}% Over vs "
                      f"{mp['ou_actual_over']*100:.0f}% réel (écart {mp['ou_calib_gap']*100:+.1f}pp, {mp['ou_n']} matchs)")
        if mp.get("ah_n"):
            ou_txt += (f"\n- Handicap asiatique : accuracy {mp['ah_accuracy']*100:.0f}%, "
                       f"couverture home prédite {mp['ah_pred_home']*100:.0f}% vs réelle "
                       f"{mp['ah_actual_home']*100:.0f}% (écart {mp['ah_calib_gap']*100:+.1f}pp, {mp['ah_n']} matchs, push exclu)")
        data_context += f"""

PERFORMANCE PRÉDICTIVE DU MODÈLE (1X2, TOUS les matchs joués cette semaine, pas seulement les value bets)
- N matchs évalués : {mp['n']}
- Accuracy 1X2 : {mp['accuracy']*100:.1f}% (semaine précédente : {prev_acc_txt})
- Log-loss : {mp['log_loss']} (sem. préc. {mpp.get('log_loss', 'N/A')} ; plus bas = meilleure calibration)
- Brier score : {mp['brier_score']} (plus bas = mieux)
- Accuracy par issue : dom {_pct(mp['home_accuracy'])}, nul {_pct(mp['draw_accuracy'])}, ext {_pct(mp['away_accuracy'])}{ou_txt}"""

    system_prompt = """Tu es un analyste senior en paris sportifs et value betting,
expert en machine learning appliqué au sport (Dixon-Coles, XGBoost, calibration).

Le contexte : tu rédiges un commentaire d'analyse pour le rapport hebdo d'edgeAI,
une plateforme qui détecte les value bets sur foot et NBA via modèles ML (DC pour
1X2 foot, XGB calibré pour AH/OU/NBA). Mises Kelly fractionnel (¼), edge ∈ [5%, 20%].
Le tracking est sur backfill 730j avec data leak partiel (DC vu les matchs) — donc
le ROI absolu cumul est légèrement gonflé mais le ranking inter-edge reste valide.

Ton ROLE : produire un commentaire de 220-320 mots en français qui :
1. Interprète les KPIs de la semaine en contexte (variance courte vs perf long terme,
   sample size, CLV comme signal indépendant du résultat).
2. Commente la PERFORMANCE PRÉDICTIVE DU MODÈLE séparément du ROI : accuracy/log-loss/
   Brier et calibration O/U. Point clé à expliquer : le modèle peut bien prédire
   (bonne accuracy/calibration) tout en ayant un ROI négatif sur la semaine (variance),
   ou l'inverse. Si log-loss/Brier se dégradent vs la semaine précédente, signale une
   possible dérive du modèle ; si la calibration O/U dérape (|écart| > 10pp), dis-le.
3. Identifie les patterns intéressants (marché/ligue qui surperforme ou pas).
4. Donne 1-2 suggestions concrètes d'amélioration ou alerte si nécessaire
   (ex: si une ligue/marché a -20% ROI sur >30 paris, suggérer de la retirer
   de la whitelist ; ou si le modèle dérive, suggérer un retrain).

Ton STYLE : sobre, factuel, comme un partenaire qui revoit la performance.
Pas de surenchère ("excellent !", "fantastique !"). Pas de jargon inutile.
Cite des chiffres précis du contexte. Si la semaine est mauvaise mais le cumul
ok, rappelle que la variance court terme est attendue avec ¼ Kelly. Si tu vois
un signal préoccupant, dis-le clairement.

Format : 2-3 paragraphes séparés par des lignes vides. Pas de titres ni listes.
Pas de markdown (le texte sera rendu en HTML <p>)."""

    user_message = f"""Voici les données du rapport :

{data_context}

Rédige le commentaire d'analyse."""

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5",
                    "max_tokens": 800,
                    "system": system_prompt,
                    "messages": [{"role": "user", "content": user_message}],
                },
            )
            r.raise_for_status()
            data = r.json()
            content = data.get("content", [{}])[0].get("text", "")
            log.info("weekly_report_ai_generated", tokens=data.get("usage", {}))
            return content.strip()
    except Exception as e:
        log.error("weekly_report_ai_error", error=str(e))
        return _fallback_analysis(week_kpis, prev_kpis, global_kpis)


def _fallback_analysis(week_kpis: dict, prev_kpis: dict, global_kpis: dict) -> str:
    """Texte basique si Claude est indisponible."""
    roi_w = week_kpis["roi_percent"]
    roi_g = global_kpis["roi_percent"]
    n = week_kpis["n_settled"]
    parts = []
    if n < 5:
        parts.append(f"Semaine à faible volume ({n} paris settled) — résultats peu significatifs statistiquement.")
    elif roi_w > 10:
        parts.append(f"Bonne semaine ({roi_w:+.1f}% ROI) — mais attention à la variance courte avec {n} paris seulement.")
    elif roi_w < -10:
        parts.append(f"Semaine difficile ({roi_w:+.1f}% ROI). Sur {n} paris c'est dans la variance attendue de ¼ Kelly, le ROI cumul reste à {roi_g:+.1f}%.")
    else:
        parts.append(f"Performance dans la moyenne cette semaine ({roi_w:+.1f}% ROI). Le cumul 730j reste à {roi_g:+.1f}% sur {global_kpis['n_settled']} paris.")
    parts.append(f"Drawdown max cumulé : {global_kpis['max_drawdown_pct']:.1f}%. Edge min config : 5%.")
    return "\n\n".join(parts)


async def _fetch_system_status(session: AsyncSession, redis) -> dict:
    """Quota odds-api restant + dernières dates de retrain des modèles."""
    odds_remaining = None
    try:
        raw = await redis.get("odds_api:remaining")
        if raw:
            odds_remaining = int(raw)
    except Exception:
        pass

    # Dernier model_version par sport (proxy de date retrain)
    retrain_dates: dict = {"1X2 foot (DC)": None, "OU foot": None, "AH foot": None,
                           "NBA 1X2": None, "NBA Totals": None}
    try:
        rows = (await session.execute(text("""
            SELECT model_version, MAX(computed_at) as last_used
            FROM predictions
            WHERE model_version IS NOT NULL
              AND model_version NOT LIKE 'backfill_%'
            GROUP BY model_version
            ORDER BY last_used DESC
            LIMIT 20
        """))).fetchall()
        for row in rows:
            mv = row[0]
            last = row[1]
            if last is None:
                continue
            dt_str = last.strftime("%Y-%m-%d")
            if mv.startswith("dc_") and retrain_dates["1X2 foot (DC)"] is None:
                retrain_dates["1X2 foot (DC)"] = dt_str
    except Exception as e:
        log.warning("weekly_report_retrain_dates_error", error=str(e))

    return {"odds_remaining": odds_remaining, "retrain_dates": retrain_dates}


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


def _model_perf_html(mp: dict, mp_prev: dict) -> str:
    """Section performance prédictive du modèle (distincte du ROI des paris)."""
    if not mp or mp.get("n", 0) == 0:
        return """
<h2 style="margin:24px 0 12px 0;font-size:16px;color:#111827">🎯 Performance du modèle (semaine)</h2>
<p style="font-size:13px;color:#9ca3af;background:#f9fafb;border-radius:8px;padding:12px;margin-bottom:24px">
Aucun match terminé avec prédiction live cette semaine — pas de mesure de performance modèle.
</p>"""

    acc = mp["accuracy"]
    ll = mp["log_loss"]
    brier = mp["brier_score"]
    # Deltas vs semaine précédente (accuracy ↑ = mieux ; log-loss/brier ↓ = mieux)
    acc_d = _delta_str(acc * 100 if acc is not None else None,
                       mp_prev.get("accuracy") * 100 if mp_prev.get("accuracy") is not None else None, "pp")
    acc_dc = _delta_color(acc, mp_prev.get("accuracy"))
    ll_d = _delta_str(ll, mp_prev.get("log_loss"))
    ll_dc = _delta_color(mp_prev.get("log_loss"), ll)  # inversé : baisse = vert

    ou_line = ""
    if mp.get("ou_n"):
        gap = mp["ou_calib_gap"]
        gap_color = "#059669" if abs(gap) <= 0.05 else "#f59e0b" if abs(gap) <= 0.10 else "#dc2626"
        ou_line = f"""
      <div style="margin-top:10px;font-size:12px;color:#374151">
        <strong>Calibration O/U 2.5</strong> ({mp['ou_n']} matchs) :
        prédit {mp['ou_pred_over']*100:.0f}% Over, réel {mp['ou_actual_over']*100:.0f}% Over,
        écart <span style="color:{gap_color};font-weight:bold">{gap*100:+.1f}pp</span>
        <span style="color:#9ca3af">(|écart| ≤ 5pp = bien calibré)</span>
      </div>"""

    ah_line = ""
    if mp.get("ah_n"):
        agap = mp["ah_calib_gap"]
        agap_color = "#059669" if abs(agap) <= 0.05 else "#f59e0b" if abs(agap) <= 0.10 else "#dc2626"
        ah_line = f"""
      <div style="margin-top:8px;font-size:12px;color:#374151">
        <strong>Handicap asiatique</strong> ({mp['ah_n']} matchs, push exclu) :
        accuracy <span style="font-weight:bold">{mp['ah_accuracy']*100:.0f}%</span> ·
        couverture home prédite {mp['ah_pred_home']*100:.0f}% vs réelle {mp['ah_actual_home']*100:.0f}%,
        écart <span style="color:{agap_color};font-weight:bold">{agap*100:+.1f}pp</span>
      </div>"""

    return f"""
<h2 style="margin:24px 0 12px 0;font-size:16px;color:#111827">🎯 Performance du modèle (semaine)</h2>
<p style="font-size:12px;color:#6b7280;margin:0 0 8px 0">Qualité prédictive 1X2 sur <strong>tous</strong> les matchs joués cette semaine (pas seulement les value bets) — indépendant du ROI.</p>
<table style="width:100%;border-collapse:collapse;background:#f5f3ff;border-radius:8px;overflow:hidden;margin-bottom:8px">
  <tr>
    <td style="padding:10px;text-align:center;border-right:1px solid #e5e7eb">
      <div style="color:#6b7280;text-transform:uppercase;font-size:10px">Accuracy 1X2</div>
      <div style="font-weight:bold;font-size:18px;margin-top:2px">{acc*100:.1f}%</div>
      <div style="font-size:10px;color:{acc_dc}">vs sem.-1 : {acc_d}</div>
    </td>
    <td style="padding:10px;text-align:center;border-right:1px solid #e5e7eb">
      <div style="color:#6b7280;text-transform:uppercase;font-size:10px">Log-loss</div>
      <div style="font-weight:bold;font-size:18px;margin-top:2px">{ll}</div>
      <div style="font-size:10px;color:{ll_dc}">vs sem.-1 : {ll_d}</div>
    </td>
    <td style="padding:10px;text-align:center;border-right:1px solid #e5e7eb">
      <div style="color:#6b7280;text-transform:uppercase;font-size:10px">Brier</div>
      <div style="font-weight:bold;font-size:18px;margin-top:2px">{brier}</div>
      <div style="font-size:10px;color:#9ca3af">{mp['n']} matchs</div>
    </td>
    <td style="padding:10px;text-align:center">
      <div style="color:#6b7280;text-transform:uppercase;font-size:10px">Acc. par issue</div>
      <div style="font-weight:bold;font-size:13px;margin-top:2px">
        {_pct(mp['home_accuracy'])} / {_pct(mp['draw_accuracy'])} / {_pct(mp['away_accuracy'])}
      </div>
      <div style="font-size:10px;color:#9ca3af">dom / nul / ext</div>
    </td>
  </tr>
</table>{ou_line}{ah_line}
<div style="height:16px"></div>"""


def _pct(v: float | None) -> str:
    return f"{v*100:.0f}%" if v is not None else "—"


def _build_report_html(
    week_kpis: dict,
    prev_kpis: dict,
    global_kpis: dict,
    upcoming_bets: list[dict],
    app_url: str,
    week_label: str,
    ai_analysis: str = "",
    per_market: list[dict] | None = None,
    per_league: list[dict] | None = None,
    advanced: dict | None = None,
    system_status: dict | None = None,
    model_perf: dict | None = None,
    model_perf_prev: dict | None = None,
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

    # Section AI analysis
    ai_html = ""
    if ai_analysis:
        paragraphs = [f'<p style="margin:0 0 12px 0;font-size:14px;line-height:1.6;color:#374151">{p.strip()}</p>'
                      for p in ai_analysis.split("\n\n") if p.strip()]
        ai_html = f"""
        <h2 style="margin:24px 0 12px 0;font-size:16px;color:#111827">🧠 Analyse</h2>
        <div style="background:#f0f9ff;border-left:3px solid #3b82f6;padding:16px;border-radius:0 8px 8px 0;margin-bottom:24px">
          {"".join(paragraphs)}
        </div>
        """

    # Section breakdown par marché + ligue
    def _breakdown_table_html(rows: list[dict], title: str) -> str:
        if not rows:
            return ""
        row_html = []
        for r in rows[:5]:
            color = "#059669" if r["roi"] >= 0 else "#dc2626"
            sign = "+" if r["roi"] >= 0 else ""
            row_html.append(f"""
            <tr style="border-bottom:1px solid #e5e7eb">
              <td style="padding:6px 8px;font-size:13px">{r['label']}</td>
              <td style="padding:6px 8px;font-size:12px;color:#6b7280;text-align:right">{r['n']}</td>
              <td style="padding:6px 8px;font-size:12px;color:#6b7280;text-align:right">{r['hit_rate']*100:.0f}%</td>
              <td style="padding:6px 8px;font-size:13px;text-align:right;font-weight:bold;color:{color}">{sign}{r['roi']:.1f}%</td>
            </tr>
            """)
        return f"""
        <h3 style="margin:0 0 8px 0;font-size:14px;color:#111827">{title}</h3>
        <table style="width:100%;border-collapse:collapse">
          <thead><tr style="background:#f3f4f6"><th style="padding:6px 8px;text-align:left;font-size:10px;text-transform:uppercase;color:#6b7280">Catégorie</th><th style="padding:6px 8px;text-align:right;font-size:10px;text-transform:uppercase;color:#6b7280">N</th><th style="padding:6px 8px;text-align:right;font-size:10px;text-transform:uppercase;color:#6b7280">Hit</th><th style="padding:6px 8px;text-align:right;font-size:10px;text-transform:uppercase;color:#6b7280">ROI</th></tr></thead>
          <tbody>{"".join(row_html)}</tbody>
        </table>
        """

    market_html = _breakdown_table_html(per_market or [], "Par marché")
    league_html = _breakdown_table_html(per_league or [], "Par ligue")
    breakdown_section_html = ""
    if market_html or league_html:
        breakdown_section_html = f"""
        <h2 style="margin:24px 0 12px 0;font-size:16px;color:#111827">📊 Breakdown semaine</h2>
        <table style="width:100%;border-collapse:collapse">
          <tr>
            <td style="vertical-align:top;width:50%;padding-right:8px">{market_html}</td>
            <td style="vertical-align:top;width:50%;padding-left:8px">{league_html}</td>
          </tr>
        </table>
        """

    # Section indicateurs avancés
    advanced_html = ""
    if advanced:
        pf = advanced["profit_factor"]
        sh = advanced["sharpe_like"]
        avg = advanced["avg_odds"]
        mw = advanced["max_win_streak"]
        ml = advanced["max_loss_streak"]
        pf_color = "#059669" if pf is not None and pf > 1 else "#dc2626" if pf is not None else "#9ca3af"
        advanced_html = f"""
        <h3 style="margin:24px 0 8px 0;font-size:14px;color:#111827">📐 Indicateurs avancés (semaine)</h3>
        <table style="width:100%;border-collapse:collapse;background:#f9fafb;border-radius:8px;overflow:hidden">
          <tr>
            <td style="padding:10px;font-size:12px;text-align:center;border-right:1px solid #e5e7eb">
              <div style="color:#6b7280;text-transform:uppercase;font-size:10px">Profit factor</div>
              <div style="font-weight:bold;color:{pf_color};font-size:16px;margin-top:2px">{pf if pf is not None else '—'}</div>
              <div style="color:#9ca3af;font-size:10px">&gt;1 = profitable</div>
            </td>
            <td style="padding:10px;font-size:12px;text-align:center;border-right:1px solid #e5e7eb">
              <div style="color:#6b7280;text-transform:uppercase;font-size:10px">Sharpe-like</div>
              <div style="font-weight:bold;font-size:16px;margin-top:2px">{sh if sh is not None else '—'}</div>
              <div style="color:#9ca3af;font-size:10px">return / variance</div>
            </td>
            <td style="padding:10px;font-size:12px;text-align:center;border-right:1px solid #e5e7eb">
              <div style="color:#6b7280;text-transform:uppercase;font-size:10px">Cote moyenne</div>
              <div style="font-weight:bold;font-size:16px;margin-top:2px">{avg}</div>
              <div style="color:#9ca3af;font-size:10px">pariée</div>
            </td>
            <td style="padding:10px;font-size:12px;text-align:center">
              <div style="color:#6b7280;text-transform:uppercase;font-size:10px">Streaks</div>
              <div style="font-weight:bold;font-size:14px;margin-top:2px"><span style="color:#059669">{mw}W</span> / <span style="color:#dc2626">{ml}L</span></div>
              <div style="color:#9ca3af;font-size:10px">+long V / +long D</div>
            </td>
          </tr>
        </table>
        """

    # Section performance prédictive du modèle (semaine)
    model_perf_html = _model_perf_html(model_perf or {}, model_perf_prev or {})

    # Section statut système
    status_html = ""
    if system_status:
        status_html = _system_status_html(
            system_status.get("odds_remaining"),
            system_status.get("retrain_dates", {}),
        )

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

    {model_perf_html}

    {ai_html}

    {breakdown_section_html}

    {advanced_html}

    {upcoming_html}

    {status_html}

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

    # Nouveau : breakdowns + indicateurs avancés + status système + analyse IA
    per_market = _breakdown_by_dimension(week_kpis["bets"], "market")
    per_league = _breakdown_by_dimension(week_kpis["bets"], "league")
    advanced = _advanced_indicators(week_kpis["bets"])
    system_status = await _fetch_system_status(session, redis)

    # Performance prédictive du modèle (semaine + semaine précédente pour le trend)
    model_perf = await _model_perf_window(
        session, monday_this.astimezone(timezone.utc), now.astimezone(timezone.utc))
    model_perf_prev = await _model_perf_window(
        session, monday_prev.astimezone(timezone.utc), monday_this.astimezone(timezone.utc))

    ai_analysis = await _generate_ai_analysis(
        week_kpis, prev_kpis, global_kpis, per_market, per_league, advanced,
        model_perf=model_perf, model_perf_prev=model_perf_prev,
    )

    week_label = f"du {monday_this.strftime('%d/%m')} au {now.strftime('%d/%m/%Y')}"
    html = _build_report_html(
        week_kpis, prev_kpis, global_kpis, upcoming, app_url, week_label,
        ai_analysis=ai_analysis,
        per_market=per_market,
        per_league=per_league,
        advanced=advanced,
        system_status=system_status,
        model_perf=model_perf,
        model_perf_prev=model_perf_prev,
    )

    roi_sign = "+" if week_kpis["roi_percent"] >= 0 else ""
    subject = f"📊 edgeAI hebdo : ROI {roi_sign}{week_kpis['roi_percent']:.1f}% ({week_kpis['n_settled']} paris)"

    ok = await _send_brevo_email(api_key, from_email, to_email, subject, html)
    if ok:
        await redis.setex(lock_key, WEEKLY_REPORT_LOCK_TTL, "1")
        log.info("weekly_report_sent", to=to_email,
                 n_settled=week_kpis["n_settled"], roi=week_kpis["roi_percent"])
        return True
    return False
