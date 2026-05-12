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

    async def close(self):
        await self._client.aclose()


class OddsAPIClient:
    def __init__(self, api_key: str):
        self._key = api_key
        self._client = httpx.AsyncClient(base_url=ODDS_API_BASE, timeout=30)

    async def get_odds(self, sport: str = "soccer_epl") -> list[dict]:
        try:
            resp = await self._client.get(
                f"/sports/{sport}/odds",
                params={
                    "apiKey": self._key,
                    "regions": "eu",
                    "markets": "h2h",
                    "oddsFormat": "decimal",
                },
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.error("odds_api_error", sport=sport, error=str(e))
            return []

    async def close(self):
        await self._client.aclose()


def normalize_match(raw: dict, league: str) -> dict:
    """Normalise un match football-data.org vers le format edgeAI."""
    home = raw["homeTeam"]["name"]
    away = raw["awayTeam"]["name"]
    score = raw.get("score", {})
    full = score.get("fullTime", {})

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
