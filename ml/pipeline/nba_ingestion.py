"""
Ingestion NBA via the-odds-api.com (500 req/mois gratuites).

Récupère :
- Les matchs upcoming avec cotes H2H (moneyline) + Totals (over/under)
- Les scores des matchs récemment terminés

L'API ne fournit que les matchs futurs + résultats récents (~72h).
Pour l'historique long, voir docs/ROADMAP.md → "Backfill historique NBA".

⚠ QUOTA : le plan gratuit (500 req/mois) est partagé entre foot et NBA.
Reset le 1er du mois civil. Si épuisement, NBA est totalement à l'arrêt
(odds + scores tous via cette API). Voir docs/OPERATIONS.md → section quotas.
"""
import os
from datetime import datetime, timezone

import httpx
import structlog

log = structlog.get_logger()

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
SPORT_KEY = "basketball_nba"


class NBAOddsClient:
    """Client minimal pour the-odds-api — NBA uniquement."""

    def __init__(self, api_key: str):
        self._key = api_key
        self._client = httpx.AsyncClient(base_url=ODDS_API_BASE, timeout=30)
        self.last_remaining: int | None = None

    async def get_upcoming(self) -> list[dict]:
        """Récupère les matchs NBA upcoming avec cotes consensus (US bookmakers).

        Demande à la fois h2h (moneyline) et totals (over/under sur la ligne du
        bookmaker, ex 224.5 points) pour activer les value bets sur 2 marchés.
        """
        if not self._key:
            log.warning("nba_odds_no_key")
            return []
        try:
            resp = await self._client.get(
                f"/sports/{SPORT_KEY}/odds",
                params={
                    "apiKey": self._key,
                    "regions": "us",
                    "markets": "h2h,totals",
                    "oddsFormat": "decimal",
                },
            )
            resp.raise_for_status()
            remaining = resp.headers.get("x-requests-remaining")
            if remaining:
                try:
                    self.last_remaining = int(remaining)
                except ValueError:
                    pass
            log.info("nba_odds_fetched", count=len(resp.json()), remaining=remaining)
            return resp.json()
        except Exception as e:
            log.error("nba_odds_error", error=str(e))
            return []

    async def get_scores(self, days_from: int = 2) -> list[dict]:
        """Récupère les scores des matchs récemment terminés (last 2-3 days)."""
        if not self._key:
            return []
        try:
            resp = await self._client.get(
                f"/sports/{SPORT_KEY}/scores",
                params={"apiKey": self._key, "daysFrom": days_from},
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.error("nba_scores_error", error=str(e))
            return []

    async def close(self):
        await self._client.aclose()


def _consensus_odds(bookmakers: list[dict], home: str, away: str) -> tuple[float | None, float | None]:
    """Calcule la cote consensus (médiane) sur tous les bookmakers."""
    home_odds = []
    away_odds = []
    for bk in bookmakers:
        for market in bk.get("markets", []):
            if market.get("key") != "h2h":
                continue
            for outcome in market.get("outcomes", []):
                price = outcome.get("price")
                if price and price > 1:
                    if outcome.get("name") == home:
                        home_odds.append(float(price))
                    elif outcome.get("name") == away:
                        away_odds.append(float(price))
    if not home_odds or not away_odds:
        return None, None
    # Médiane pour gommer les outliers
    return _median(home_odds), _median(away_odds)


def _median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    if n % 2:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2


def _consensus_totals(bookmakers: list[dict]) -> tuple[float | None, float | None, float | None]:
    """Calcule la ligne totals consensus + cotes Over/Under (médianes sur tous bookies).

    Renvoie (line, over_odds, under_odds). La ligne est arrondie au quart de point
    le plus proche (les bookies utilisent des lignes type X.0, X.5, parfois X.25).
    Si les bookies ont des lignes différentes, on prend la médiane des lignes et on
    ne garde que les cotes des bookies à cette ligne consensus.
    """
    # Étape 1 : trouver la ligne consensus
    all_lines: list[float] = []
    for bk in bookmakers:
        for market in bk.get("markets", []):
            if market.get("key") != "totals":
                continue
            for outcome in market.get("outcomes", []):
                pt = outcome.get("point")
                if pt is not None:
                    try:
                        all_lines.append(float(pt))
                    except (TypeError, ValueError):
                        pass
    if not all_lines:
        return None, None, None
    consensus_line = _median(all_lines)

    # Étape 2 : récupérer les cotes Over/Under à cette ligne (tolérance ±0.5)
    overs: list[float] = []
    unders: list[float] = []
    for bk in bookmakers:
        for market in bk.get("markets", []):
            if market.get("key") != "totals":
                continue
            outcomes = market.get("outcomes", [])
            over_o = next(
                (o for o in outcomes
                 if o.get("name") == "Over" and o.get("point") is not None
                 and abs(float(o["point"]) - consensus_line) <= 0.5),
                None,
            )
            under_o = next(
                (o for o in outcomes
                 if o.get("name") == "Under" and o.get("point") is not None
                 and abs(float(o["point"]) - consensus_line) <= 0.5),
                None,
            )
            if over_o and under_o:
                try:
                    op = float(over_o["price"])
                    up = float(under_o["price"])
                    if op > 1 and up > 1:
                        overs.append(op)
                        unders.append(up)
                except (TypeError, ValueError):
                    pass
    if not overs or not unders:
        return consensus_line, None, None
    return consensus_line, _median(overs), _median(unders)


def normalize_nba_upcoming(raw: dict) -> dict | None:
    """Convertit un match brut the-odds-api → format edgeAI."""
    try:
        external_id = f"nba:{raw['id']}"
        home = raw["home_team"]
        away = raw["away_team"]
        commence = raw["commence_time"]  # ISO 8601 UTC
        home_o, away_o = _consensus_odds(raw.get("bookmakers", []), home, away)
        if home_o is None or away_o is None:
            return None
        # Totals (over/under) : optionnel, on n'échoue pas si absent
        total_line, over_o, under_o = _consensus_totals(raw.get("bookmakers", []))
        # Saison NBA : commence en octobre, finit en juin.
        dt = datetime.fromisoformat(commence.replace("Z", "+00:00"))
        season = str(dt.year if dt.month >= 10 else dt.year - 1)
        return {
            "external_id": external_id,
            "sport": "NBA",
            "league": "NBA",
            "season": season,
            "home_team": home,
            "away_team": away,
            "match_date": commence,
            "status": "SCHEDULED",
            "home_odds": home_o,
            "away_odds": away_o,
            "draw_odds": None,  # pas de nul en NBA
            "nba_total_line": total_line,
            "over_25_odds": over_o,   # réutilisé pour NBA totals (line = nba_total_line)
            "under_25_odds": under_o,
        }
    except (KeyError, ValueError) as e:
        log.error("nba_normalize_error", error=str(e))
        return None


def normalize_nba_score(raw: dict) -> dict | None:
    """Convertit un score the-odds-api → champs match update."""
    try:
        if not raw.get("completed"):
            return None
        scores = {s["name"]: int(s["score"]) for s in (raw.get("scores") or []) if s.get("score") is not None}
        home, away = raw["home_team"], raw["away_team"]
        if home not in scores or away not in scores:
            return None
        return {
            "external_id": f"nba:{raw['id']}",
            "home_score": scores[home],
            "away_score": scores[away],
            "status": "FINISHED",
        }
    except Exception as e:
        log.error("nba_score_normalize_error", error=str(e))
        return None
