"""
Ingestion des données sportives depuis football-data.org et Odds API.
"""
import asyncio
import os
from datetime import datetime, timezone
import httpx
import structlog

log = structlog.get_logger()

FOOTBALL_DATA_BASE = "https://api.football-data.org/v4"
ODDS_API_BASE = "https://api.the-odds-api.com/v4"

SUPPORTED_LEAGUES = {
    "PL": "Premier League",
    "PD": "La Liga",
    "BL1": "Bundesliga",
    "SA": "Serie A",
    "FL1": "Ligue 1",
    "WC": "World Cup",  # FIFA World Cup — actif en juin-juillet pendant la compétition
}


class FootballDataClient:
    def __init__(self, api_key: str):
        self._key = api_key
        self._client = httpx.AsyncClient(
            base_url=FOOTBALL_DATA_BASE,
            headers={"X-Auth-Token": api_key},
            timeout=30,
        )

    async def get_upcoming_matches(self, league_code: str, days: int = 7) -> list[dict]:
        try:
            resp = await self._client.get(
                f"/competitions/{league_code}/matches",
                params={"status": "SCHEDULED", "dateFrom": _today(), "dateTo": _in_days(days)},
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("matches", [])
        except Exception as e:
            log.error("football_data_error", league=league_code, error=str(e))
            return []

    async def get_recently_finished(self, league_code: str, days: int = 2) -> list[dict]:
        from datetime import timedelta
        date_from = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        date_to = _today()
        try:
            resp = await self._client.get(
                f"/competitions/{league_code}/matches",
                params={"status": "FINISHED", "dateFrom": date_from, "dateTo": date_to},
            )
            resp.raise_for_status()
            return resp.json().get("matches", [])
        except Exception as e:
            log.error("football_data_finished_error", league=league_code, error=str(e))
            return []

    async def get_recent_matches(self, league_code: str, limit: int = 100) -> list[dict]:
        try:
            resp = await self._client.get(
                f"/competitions/{league_code}/matches",
                params={"status": "FINISHED", "limit": limit},
            )
            resp.raise_for_status()
            return resp.json().get("matches", [])
        except Exception as e:
            log.error("football_data_recent_error", league=league_code, error=str(e))
            return []

    async def get_season_matches(self, league_code: str, season: int) -> list[dict]:
        try:
            resp = await self._client.get(
                f"/competitions/{league_code}/matches",
                params={"season": season, "status": "FINISHED"},
            )
            resp.raise_for_status()
            return resp.json().get("matches", [])
        except Exception as e:
            log.error("football_data_season_error", league=league_code, season=season, error=str(e))
            return []

    async def get_standings(self, league_code: str) -> dict[str, int]:
        """Returns {team_name: position} for current league standings."""
        try:
            resp = await self._client.get(f"/competitions/{league_code}/standings")
            resp.raise_for_status()
            data = resp.json()
            for group in data.get("standings", []):
                if group.get("type") == "TOTAL":
                    return {
                        entry["team"]["name"]: entry["position"]
                        for entry in group.get("table", [])
                    }
        except Exception as e:
            log.error("standings_error", league=league_code, error=str(e))
        return {}

    async def close(self):
        await self._client.aclose()


class OddsAPIClient:
    def __init__(self, api_key: str):
        self._key = api_key
        self._client = httpx.AsyncClient(base_url=ODDS_API_BASE, timeout=30)
        self.last_remaining: int | None = None  # tracked across calls

    async def get_odds(self, sport: str = "soccer_epl", markets: str = "h2h,totals") -> list[dict]:
        """Récupère h2h + totals 2.5 (over/under) pour une ligue de foot."""
        if not self._key:
            return []
        try:
            resp = await self._client.get(
                f"/sports/{sport}/odds",
                params={
                    "apiKey": self._key,
                    "regions": "eu",
                    "markets": markets,
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
            log.info("odds_api_fetched", sport=sport, count=len(resp.json()), remaining=remaining)
            return resp.json()
        except Exception as e:
            log.error("odds_api_error", sport=sport, error=str(e))
            return []

    async def close(self):
        await self._client.aclose()


# Mapping de nos noms de ligues vers les sport_keys the-odds-api
ODDS_API_SOCCER_KEYS = {
    "Premier League": "soccer_epl",
    "La Liga": "soccer_spain_la_liga",
    "Bundesliga": "soccer_germany_bundesliga",
    "Serie A": "soccer_italy_serie_a",
    "Ligue 1": "soccer_france_ligue_one",
    "World Cup": "soccer_fifa_world_cup",  # actif uniquement pendant la WC
}


def extract_totals_25(bookmakers: list[dict]) -> tuple[float | None, float | None]:
    """Extrait les cotes O/U 2.5 médianes des bookmakers."""
    overs, unders = [], []
    for bk in bookmakers:
        for market in bk.get("markets", []):
            if market.get("key") != "totals":
                continue
            outcomes = market.get("outcomes", [])
            # Chercher la ligne 2.5
            over_o = next((o for o in outcomes if o.get("name") == "Over" and float(o.get("point", 0)) == 2.5), None)
            under_o = next((o for o in outcomes if o.get("name") == "Under" and float(o.get("point", 0)) == 2.5), None)
            if over_o and under_o:
                overs.append(float(over_o["price"]))
                unders.append(float(under_o["price"]))
    if not overs or not unders:
        return None, None
    return _median(overs), _median(unders)


def extract_spreads(bookmakers: list[dict], home: str, away: str) -> tuple[float | None, float | None, float | None]:
    """
    Extrait la ligne AH (Asian Handicap) et les cotes home/away médianes.
    Retourne (line, home_odds, away_odds). Line = handicap sur HOME (négatif si favori).
    On prend la ligne médiane parmi les bookmakers.
    """
    lines = []
    by_line = {}  # line → [(home_odd, away_odd), ...]
    for bk in bookmakers:
        for market in bk.get("markets", []):
            if market.get("key") != "spreads":
                continue
            home_o = next((o for o in market.get("outcomes", []) if o.get("name") == home), None)
            away_o = next((o for o in market.get("outcomes", []) if o.get("name") == away), None)
            if not (home_o and away_o):
                continue
            try:
                home_line = float(home_o.get("point", 0))
                away_line = float(away_o.get("point", 0))
                # AH symétrique : home_line + away_line ≈ 0
                if abs(home_line + away_line) > 0.5:
                    continue
                price_h = float(home_o["price"])
                price_a = float(away_o["price"])
                if price_h <= 1 or price_a <= 1:
                    continue
                # Snap à la ligne home
                lines.append(home_line)
                by_line.setdefault(home_line, []).append((price_h, price_a))
            except (ValueError, TypeError, KeyError):
                continue

    if not lines:
        return None, None, None

    # Ligne médiane
    median_line = _median(lines)
    # Snap à 0.25 increment
    snapped = round(median_line * 4) / 4
    # Prendre les odds à la ligne la plus proche
    closest_line = min(by_line.keys(), key=lambda l: abs(l - snapped))
    prices = by_line[closest_line]
    home_odds = _median([p[0] for p in prices])
    away_odds = _median([p[1] for p in prices])
    return closest_line, home_odds, away_odds


def extract_h2h(bookmakers: list[dict], home: str, away: str) -> tuple[float | None, float | None, float | None]:
    """Extrait cotes médianes home/draw/away."""
    h, d, a = [], [], []
    for bk in bookmakers:
        for market in bk.get("markets", []):
            if market.get("key") != "h2h":
                continue
            for o in market.get("outcomes", []):
                price = float(o.get("price", 0))
                if price <= 1:
                    continue
                name = o.get("name")
                if name == home:
                    h.append(price)
                elif name == away:
                    a.append(price)
                elif name == "Draw":
                    d.append(price)
    return (
        _median(h) if h else None,
        _median(d) if d else None,
        _median(a) if a else None,
    )


def _median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    if n % 2:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2


def normalize_match(raw: dict, league: str) -> dict:
    """Normalise un match football-data.org vers le format edgeAI."""
    home = raw["homeTeam"]["name"]
    away = raw["awayTeam"]["name"]
    score = raw.get("score", {})
    full = score.get("fullTime", {})
    half = score.get("halfTime", {})

    # Cartons jaunes et rouges
    bookings = raw.get("bookings", [])
    home_yellow = sum(
        1 for b in bookings
        if b.get("team", {}).get("name") == home and b.get("card") == "YELLOW_CARD"
    )
    away_yellow = sum(
        1 for b in bookings
        if b.get("team", {}).get("name") == away and b.get("card") == "YELLOW_CARD"
    )
    home_red = sum(
        1 for b in bookings
        if b.get("team", {}).get("name") == home and b.get("card") in ("RED_CARD", "YELLOW_RED_CARD")
    )
    away_red = sum(
        1 for b in bookings
        if b.get("team", {}).get("name") == away and b.get("card") in ("RED_CARD", "YELLOW_RED_CARD")
    )

    return {
        "external_id": str(raw["id"]),
        "league": league,
        "season": raw.get("season", {}).get("startDate", "")[:4],
        "home_team": home,
        "away_team": away,
        "match_date": raw["utcDate"],
        "status": _map_status(raw["status"]),
        "home_score": full.get("home"),
        "away_score": full.get("away"),
        "ht_home_score": half.get("home"),
        "ht_away_score": half.get("away"),
        "home_yellow_cards": home_yellow,
        "away_yellow_cards": away_yellow,
        "home_red_cards": home_red,
        "away_red_cards": away_red,
    }


def _map_status(status: str) -> str:
    mapping = {
        "SCHEDULED": "SCHEDULED",
        "TIMED": "SCHEDULED",
        "IN_PLAY": "LIVE",
        "PAUSED": "LIVE",
        "FINISHED": "FINISHED",
        "POSTPONED": "POSTPONED",
        "SUSPENDED": "POSTPONED",
        "CANCELLED": "CANCELLED",
    }
    return mapping.get(status, "SCHEDULED")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _in_days(n: int) -> str:
    from datetime import timedelta
    return (datetime.now(timezone.utc) + timedelta(days=n)).strftime("%Y-%m-%d")
