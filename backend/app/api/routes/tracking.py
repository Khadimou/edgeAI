"""
Live tracking : suit chaque prédiction générée en prod, calcule le P&L Kelly
sur les matchs FINISHED, et le compare au backtest historique.

Différence avec /backtest : ici on observe les prédictions RÉELLES qui ont été
faites en production (pas du OOF simulé). C'est la preuve qu'en forward testing,
la stratégie tient ses promesses.
"""
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from app.core.config import settings
from app.core.deps import get_db, get_current_user
from app.db.models import User

router = APIRouter(prefix="/tracking", tags=["tracking"])

# Mêmes hyperparams que la stratégie déployée
INITIAL_BANKROLL = 100.0
KELLY_FRACTION = 0.25
MAX_STAKE_FRACTION = 0.05
EDGE_MIN = 0.08
EDGE_MAX = 0.20


def _actual_1x2(home_score: int, away_score: int) -> str:
    if home_score > away_score:
        return "HOME"
    if home_score < away_score:
        return "AWAY"
    return "DRAW"


def _actual_ou(home_score: int, away_score: int) -> str:
    return "OVER" if (home_score + away_score) > 2.5 else "UNDER"


def _actual_ah_pnl_unit(home_score: int, away_score: int, ah_line: float, side: str) -> float:
    """
    Renvoie P&L unitaire (-1 loss, 0 push, +1 win, ±0.5 half-push) sur un pari AH.
    side : 'AH_HOME' ou 'AH_AWAY'.
    """
    diff = home_score - away_score + (ah_line if side == "AH_HOME" else -ah_line)
    rounded = round(ah_line * 2) / 2
    # Quarter line : split en 2 sous-paris
    if abs(ah_line - rounded) > 1e-9:
        if ah_line > rounded:
            low, high = rounded, rounded + 0.5
        else:
            high, low = rounded, rounded - 0.5
        a = _actual_ah_pnl_unit(home_score, away_score, low, side)
        b = _actual_ah_pnl_unit(home_score, away_score, high, side)
        return (a + b) / 2
    # Half line : pas de push
    if abs(ah_line - round(ah_line)) > 1e-9:
        return 1.0 if diff > 0 else -1.0
    # Whole line : push si diff == 0
    if diff > 0:
        return 1.0
    if diff < 0:
        return -1.0
    return 0.0


def _best_value_bet(candidates, bankroll, edge_min=EDGE_MIN, edge_max=EDGE_MAX):
    """Renvoie le meilleur value bet parmi (outcome, prob, odds) candidates."""
    best = None
    for outcome, prob, odds in candidates:
        if not odds or odds <= 1.0 or prob is None:
            continue
        edge = prob * odds - 1
        if edge < edge_min or edge > edge_max:
            continue
        b = odds - 1
        f_star = (prob * b - (1 - prob)) / b
        if f_star <= 0:
            continue
        stake_frac = min(f_star * KELLY_FRACTION, MAX_STAKE_FRACTION)
        stake = round(bankroll * stake_frac, 2)
        if stake < 1:
            continue
        if best is None or edge > best["edge"]:
            best = {
                "outcome": outcome, "prob": round(prob, 4),
                "odds": round(odds, 2), "edge": round(edge, 4),
                "stake": stake,
            }
    return best


async def _fetch_tracking_rows(db: AsyncSession, days: int):
    """Fetch les lignes brutes pour les calculs de tracking (1 query, réutilisable)."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    since_naive = since.replace(tzinfo=None)
    result = await db.execute(text("""
        SELECT m.id, m.sport, m.league, m.home_team, m.away_team,
               m.match_date, m.status, m.home_score, m.away_score,
               m.home_odds, m.draw_odds, m.away_odds,
               m.over_25_odds, m.under_25_odds,
               m.ah_line, m.ah_home_odds, m.ah_away_odds,
               m.opening_home_odds, m.opening_draw_odds, m.opening_away_odds,
               m.opening_over_25_odds, m.opening_under_25_odds,
               m.opening_ah_home_odds, m.opening_ah_away_odds,
               p.prob_home, p.prob_draw, p.prob_away,
               p.prob_over_25, p.prob_under_25,
               p.prob_ah_home, p.prob_ah_away,
               p.model_version, p.computed_at
        FROM matches m
        JOIN LATERAL (
            SELECT * FROM predictions
            WHERE match_id = m.id
            ORDER BY computed_at DESC
            LIMIT 1
        ) p ON TRUE
        WHERE m.match_date >= :since
          AND m.match_date <= NOW() + interval '7 days'
        ORDER BY m.match_date ASC
    """), {"since": since_naive})
    return result.fetchall()


def _compute_bets(rows, market_filter: str, edge_min: float, edge_max: float):
    """Calcule les value bets pour un seuil d'edge donné. Retourne (bets, bankroll, peak, max_dd)."""
    league_wl_1x2 = set(settings.value_bet_leagues)
    league_wl_ou = set(settings.value_bet_ou_leagues)
    league_wl_ah = set(settings.value_bet_ah_leagues)

    bets = []
    bankroll = INITIAL_BANKROLL
    peak = bankroll
    max_dd = 0.0

    for r in rows:
        (match_id, sport, league, home, away, match_date, status,
         h_score, a_score, h_odds, d_odds, a_odds, o25_odds, u25_odds,
         ah_line, ah_h_odds, ah_a_odds,
         o_h_odds, o_d_odds, o_a_odds, o_o25_odds, o_u25_odds,
         o_ah_h_odds, o_ah_a_odds,
         ph, pd_, pa, p_over, p_under, p_ah_h, p_ah_a, model_v, computed) = r

        # Map outcome → closing/opening odds pour CLV
        odds_by_outcome = {
            "HOME": (h_odds, o_h_odds), "DRAW": (d_odds, o_d_odds),
            "AWAY": (a_odds, o_a_odds), "OVER": (o25_odds, o_o25_odds),
            "UNDER": (u25_odds, o_u25_odds),
            "AH_HOME": (ah_h_odds, o_ah_h_odds),
            "AH_AWAY": (ah_a_odds, o_ah_a_odds),
        }

        candidates_by_market = {}

        # 1X2 (foot whitelisté ou NBA)
        if sport == "NBA":
            cands = [("HOME", ph, h_odds), ("AWAY", pa, a_odds)]
            market_label = "NBA"
        else:
            if league in league_wl_1x2:
                cands = [("HOME", ph, h_odds), ("DRAW", pd_, d_odds), ("AWAY", pa, a_odds)]
                market_label = "FOOTBALL_1X2"
            else:
                cands = []
                market_label = None
        if cands:
            vb = _best_value_bet(cands, bankroll, edge_min, edge_max)
            if vb:
                candidates_by_market[market_label] = vb

        # O/U 2.5 (foot whitelisté seulement)
        if sport != "NBA" and league in league_wl_ou and p_over and p_under:
            ou_vb = _best_value_bet(
                [("OVER", p_over, o25_odds), ("UNDER", p_under, u25_odds)],
                bankroll, edge_min, edge_max,
            )
            if ou_vb:
                candidates_by_market["FOOTBALL_OU"] = ou_vb

        # Asian Handicap (foot whitelisté seulement)
        if (sport != "NBA" and league in league_wl_ah
                and p_ah_h and p_ah_a and ah_line is not None):
            ah_vb = _best_value_bet(
                [("AH_HOME", p_ah_h, ah_h_odds), ("AH_AWAY", p_ah_a, ah_a_odds)],
                bankroll, edge_min, edge_max,
            )
            if ah_vb:
                candidates_by_market["FOOTBALL_AH"] = ah_vb

        # Filter by user-requested market
        if market_filter != "ALL":
            candidates_by_market = {k: v for k, v in candidates_by_market.items() if k == market_filter}

        if not candidates_by_market:
            continue

        for mkt, vb in candidates_by_market.items():
            # Outcome réel + P&L
            outcome_actual = None
            won = None
            profit = None
            settled = status == "FINISHED" and h_score is not None and a_score is not None

            if settled:
                if mkt == "FOOTBALL_AH" and ah_line is not None:
                    # AH : P&L unitaire en tenant compte pushes / quarter-lines
                    pnl_unit = _actual_ah_pnl_unit(h_score, a_score, ah_line, vb["outcome"])
                    if pnl_unit > 0:
                        profit = round(vb["stake"] * (vb["odds"] - 1) * pnl_unit, 2)
                        won = True
                    elif pnl_unit < 0:
                        profit = round(vb["stake"] * pnl_unit, 2)
                        won = False
                    else:
                        profit = 0.0
                        won = None  # push
                    outcome_actual = "covered" if pnl_unit > 0 else ("not_covered" if pnl_unit < 0 else "push")
                else:
                    if mkt == "FOOTBALL_OU":
                        outcome_actual = _actual_ou(h_score, a_score)
                    else:
                        outcome_actual = _actual_1x2(h_score, a_score)
                    won = vb["outcome"] == outcome_actual
                    profit = round(vb["stake"] * (vb["odds"] - 1), 2) if won else -vb["stake"]
                bankroll = round(bankroll + profit, 2)
                peak = max(peak, bankroll)
                if peak > 0:
                    dd = (peak - bankroll) / peak
                    if dd > max_dd:
                        max_dd = dd

            # CLV : (opening_odds / closing_odds) - 1 sur l'outcome parié.
            # Positif = la cote a baissé après notre détection → on a bien anticipé le marché.
            closing_odds, opening_odds = odds_by_outcome.get(vb["outcome"], (None, None))
            clv_percent = None
            if opening_odds and closing_odds and opening_odds > 0 and closing_odds > 0 and opening_odds != closing_odds:
                clv_percent = round((opening_odds / closing_odds - 1) * 100, 2)

            bets.append({
                "match_id": match_id,
                "match_date": match_date.isoformat() if match_date else None,
                "status": status,
                "sport": sport,
                "league": league,
                "home_team": home,
                "away_team": away,
                "home_score": h_score,
                "away_score": a_score,
                "market": mkt,
                "outcome": vb["outcome"],
                "outcome_label": _label_for(vb["outcome"], mkt, home, away),
                "prob": vb["prob"],
                "odds": vb["odds"],
                "opening_odds": opening_odds,
                "clv_percent": clv_percent,
                "edge": vb["edge"],
                "edge_percent": round(vb["edge"] * 100, 1),
                "stake": vb["stake"],
                "model_version": model_v,
                "computed_at": computed.isoformat() if computed else None,
                "outcome_actual": outcome_actual,
                "settled": settled,
                "won": won,
                "profit": profit,
                "bankroll_after": bankroll if settled else None,
            })

    # Stats agrégées (sur les paris settled uniquement pour le ROI)
    settled_bets = [b for b in bets if b["settled"]]
    n_settled = len(settled_bets)
    n_pending = sum(1 for b in bets if not b["settled"])
    n_wins = sum(1 for b in settled_bets if b["won"])
    total_staked = sum(b["stake"] for b in settled_bets)
    total_pnl = sum(b["profit"] for b in settled_bets)
    roi = total_pnl / total_staked * 100 if total_staked > 0 else 0
    hit_rate = n_wins / n_settled if n_settled > 0 else 0

    # CLV moyen (sur tous les paris où on a opening != closing)
    clv_vals = [b["clv_percent"] for b in bets if b["clv_percent"] is not None]
    clv_avg = round(sum(clv_vals) / len(clv_vals), 2) if clv_vals else None
    n_clv_positive = sum(1 for v in clv_vals if v > 0)
    clv_positive_rate = round(n_clv_positive / len(clv_vals), 4) if clv_vals else None

    # Per market
    per_market = {}
    for b in settled_bets:
        mkt = b["market"]
        if mkt not in per_market:
            per_market[mkt] = {"n_bets": 0, "n_wins": 0, "stake": 0, "pnl": 0}
        per_market[mkt]["n_bets"] += 1
        per_market[mkt]["n_wins"] += 1 if b["won"] else 0
        per_market[mkt]["stake"] += b["stake"]
        per_market[mkt]["pnl"] += b["profit"]
    for mkt, st in per_market.items():
        st["hit_rate"] = round(st["n_wins"] / st["n_bets"], 4) if st["n_bets"] else 0
        st["roi_percent"] = round(st["pnl"] / st["stake"] * 100, 2) if st["stake"] > 0 else 0
        st["pnl"] = round(st["pnl"], 2)
        st["stake"] = round(st["stake"], 2)

    # Per league (settled)
    per_league = {}
    for b in settled_bets:
        lg = b["league"]
        if lg not in per_league:
            per_league[lg] = {"n_bets": 0, "n_wins": 0, "stake": 0, "pnl": 0}
        per_league[lg]["n_bets"] += 1
        per_league[lg]["n_wins"] += 1 if b["won"] else 0
        per_league[lg]["stake"] += b["stake"]
        per_league[lg]["pnl"] += b["profit"]
    for lg, st in per_league.items():
        st["hit_rate"] = round(st["n_wins"] / st["n_bets"], 4) if st["n_bets"] else 0
        st["roi_percent"] = round(st["pnl"] / st["stake"] * 100, 2) if st["stake"] > 0 else 0
        st["pnl"] = round(st["pnl"], 2)
        st["stake"] = round(st["stake"], 2)

    # Equity curve (cumulatif sur settled only)
    equity = []
    if settled_bets:
        cum = INITIAL_BANKROLL
        for b in sorted(settled_bets, key=lambda x: x["match_date"]):
            cum = round(cum + b["profit"], 2)
            equity.append({"date": (b["match_date"] or "")[:10], "bankroll": cum})

    return {
        "summary": {
            "initial_bankroll": INITIAL_BANKROLL,
            "current_bankroll": round(bankroll, 2),
            "n_pending": n_pending,
            "n_settled": n_settled,
            "n_wins": n_wins,
            "hit_rate": round(hit_rate, 4),
            "total_staked": round(total_staked, 2),
            "total_pnl": round(total_pnl, 2),
            "roi_percent": round(roi, 2),
            "max_drawdown_pct": round(max_dd * 100, 2),
            "peak_bankroll": round(peak, 2),
            "clv_avg_percent": clv_avg,
            "clv_positive_rate": clv_positive_rate,
            "clv_sample_size": len(clv_vals),
        },
        "per_market": per_market,
        "per_league": per_league,
        "equity_curve": equity,
        "bets": list(reversed(bets)),  # plus récents d'abord
    }


@router.get("/live")
async def get_live_tracking(
    days: int = Query(60, ge=1, le=1095),
    market: str = Query("ALL", pattern="^(ALL|FOOTBALL_1X2|FOOTBALL_OU|FOOTBALL_AH|NBA)$"),
    edge_min: float = Query(EDGE_MIN, ge=0.0, le=0.5),
    edge_max: float = Query(EDGE_MAX, ge=0.0, le=1.0),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Renvoie toutes les value bets identifiées en prod sur les N derniers jours."""
    rows = await _fetch_tracking_rows(db, days)
    result = _compute_bets(rows, market, edge_min, edge_max)
    return {
        "window_days": days,
        "market_filter": market,
        "edge_min": edge_min,
        "edge_max": edge_max,
        **result,
    }


# Seuils testés dans le sweep. Couvre toute la gamme :
# - 2% : très agressif, volume max (utile contre closing odds Pinnacle efficients)
# - 8% : config actuelle prod
# - 20% : conservateur, sweet spot historique du backtest
DEFAULT_EDGE_SWEEP = [0.02, 0.03, 0.05, 0.08, 0.10, 0.12, 0.15, 0.20]


@router.get("/edge-sweep")
async def get_edge_sweep(
    days: int = Query(180, ge=1, le=1095),
    market: str = Query("ALL", pattern="^(ALL|FOOTBALL_1X2|FOOTBALL_OU|FOOTBALL_AH|NBA)$"),
    edge_max: float = Query(EDGE_MAX, ge=0.0, le=1.0),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """
    Compare plusieurs seuils d'edge minimum sur la même période pour identifier
    le sweet spot. Renvoie une ligne synthétique par seuil (ROI, CLV, hit, n_bets, DD).
    """
    rows = await _fetch_tracking_rows(db, days)
    sweep = []
    for emin in DEFAULT_EDGE_SWEEP:
        if emin >= edge_max:
            continue
        res = _compute_bets(rows, market, emin, edge_max)
        s = res["summary"]
        sweep.append({
            "edge_min": emin,
            "edge_max": edge_max,
            "edge_min_percent": round(emin * 100, 1),
            "n_bets": s["n_settled"],
            "n_pending": s["n_pending"],
            "hit_rate": s["hit_rate"],
            "roi_percent": s["roi_percent"],
            "total_pnl": s["total_pnl"],
            "total_staked": s["total_staked"],
            "current_bankroll": s["current_bankroll"],
            "max_drawdown_pct": s["max_drawdown_pct"],
            "clv_avg_percent": s["clv_avg_percent"],
            "clv_positive_rate": s["clv_positive_rate"],
            "clv_sample_size": s["clv_sample_size"],
        })

    # Ranking : meilleur ROI parmi ceux avec ≥30 paris settled (sample suffisant).
    # Sinon, fallback sur le plus gros sample.
    significant = [s for s in sweep if s["n_bets"] >= 30]
    best = (max(significant, key=lambda s: s["roi_percent"])
            if significant
            else (max(sweep, key=lambda s: s["n_bets"]) if sweep else None))

    return {
        "window_days": days,
        "market_filter": market,
        "initial_bankroll": INITIAL_BANKROLL,
        "sweep": sweep,
        "best_edge_min": best["edge_min"] if best else None,
        "best_edge_min_percent": best["edge_min_percent"] if best else None,
        "min_sample_size": 30,
    }


def _label_for(outcome: str, market: str, home: str, away: str) -> str:
    """Renvoie un label lisible pour un outcome (ex: nom équipe)."""
    if market == "FOOTBALL_OU":
        return "Plus de 2.5 buts" if outcome == "OVER" else "Moins de 2.5 buts"
    if market == "FOOTBALL_AH":
        return f"{home} (handicap)" if outcome == "AH_HOME" else f"{away} (handicap)"
    if outcome == "HOME":
        return home
    if outcome == "AWAY":
        return away
    if outcome == "DRAW":
        return "Match nul"
    return outcome
