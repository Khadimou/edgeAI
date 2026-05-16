"""
État ELO global pour l'inférence en temps réel du modèle foot (1X2/OU/AH).

Phase 1 features (ELO + Pythagorean + streaks + form-vs-expected + BTTS)
exigent un état chronologique cohérent. Pour éviter de re-calculer à chaque
match, on maintient des dicts ELO globaux refresh à intervalle régulier.

Architecture :
- `load(session)` : query tous les matchs FINISHED, rebuild ELO chronologiquement
- Cache TTL : 6h (les nouveaux matchs ajoutés en cours de journée ne changent
  que marginalement les ELO ; refresh quotidien suffit)
- `compute_features(home, away, match_date, league, session)` : helper qui
  combine standings + ELO state + history pour calculer MatchFeatures complète

Utilisation côté scheduler :
    from pipeline.football_inference import FOOT_STATE

    await FOOT_STATE.ensure_loaded(session)
    feat = await FOOT_STATE.compute_features(home, away, match_date, league, session)
    proba = model.predict_proba(feat.to_array().reshape(1, -1))[0]
"""
from datetime import datetime, timezone, timedelta

import pandas as pd
import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from .features import (
    MatchFeatures,
    compute_features_from_history,
    compute_standings_from_history,
    init_elo,
    update_elo,
    update_elo_venue,
)

log = structlog.get_logger()

CACHE_TTL_HOURS = 6


class FootballInferenceState:
    """Singleton-ish : maintien des dicts ELO + cache du DataFrame historique."""

    def __init__(self):
        self.elo_general: dict[str, float] = {}
        self.elo_home_venue: dict[str, float] = {}
        self.elo_away_venue: dict[str, float] = {}
        self.historical_df: pd.DataFrame | None = None
        self.last_loaded: datetime | None = None

    def _is_fresh(self) -> bool:
        if self.last_loaded is None or self.historical_df is None:
            return False
        age = datetime.now(timezone.utc) - self.last_loaded
        return age < timedelta(hours=CACHE_TTL_HOURS)

    async def ensure_loaded(self, session: AsyncSession, force: bool = False) -> bool:
        """Charge l'état si périmé ou inexistant. Retourne True si state utilisable."""
        if not force and self._is_fresh():
            return True
        return await self.load(session)

    async def load(self, session: AsyncSession) -> bool:
        """Query tous les matchs FINISHED et rebuild ELO chronologiquement."""
        try:
            result = await session.execute(text("""
                SELECT home_team, away_team, home_score, away_score, match_date, league,
                       ht_home_score, ht_away_score,
                       COALESCE(home_yellow_cards, 0), COALESCE(away_yellow_cards, 0)
                FROM matches
                WHERE status = 'FINISHED'
                  AND home_score IS NOT NULL
                  AND away_score IS NOT NULL
                  AND UPPER(sport) = 'FOOTBALL'
                ORDER BY match_date
            """))
            rows = result.fetchall()
            if not rows:
                log.warning("foot_inference_no_finished_matches")
                return False

            df = pd.DataFrame(rows, columns=[
                "home_team", "away_team", "home_score", "away_score", "date", "league",
                "ht_home_score", "ht_away_score", "home_yellow_cards", "away_yellow_cards",
            ])
            df["date"] = pd.to_datetime(df["date"])
            df["home_score"] = pd.to_numeric(df["home_score"], errors="coerce")
            df["away_score"] = pd.to_numeric(df["away_score"], errors="coerce")
            df = df.dropna(subset=["home_score", "away_score"])
            df["home_score"] = df["home_score"].astype(int)
            df["away_score"] = df["away_score"].astype(int)
            df = df.sort_values("date").reset_index(drop=True)

            elo_general = init_elo()
            elo_home_venue = init_elo()
            elo_away_venue = init_elo()
            for _, row in df.iterrows():
                update_elo(elo_general, row["home_team"], row["away_team"],
                           int(row["home_score"]), int(row["away_score"]))
                update_elo_venue(elo_home_venue, elo_away_venue,
                                 row["home_team"], row["away_team"],
                                 int(row["home_score"]), int(row["away_score"]))

            self.elo_general = elo_general
            self.elo_home_venue = elo_home_venue
            self.elo_away_venue = elo_away_venue
            self.historical_df = df
            self.last_loaded = datetime.now(timezone.utc)
            log.info("foot_inference_loaded",
                     n_matches=len(df), n_teams=len(elo_general),
                     top5_elo=sorted(elo_general.items(), key=lambda x: -x[1])[:5])
            return True
        except Exception as e:
            log.error("foot_inference_load_error", error=str(e))
            return False

    def compute_features_sync(
        self,
        home_team: str,
        away_team: str,
        match_date: pd.Timestamp,
        league: str,
    ) -> MatchFeatures:
        """Version synchrone : nécessite que load() ait été appelé en amont."""
        if self.historical_df is None:
            return MatchFeatures()

        # Sub-history < match_date
        past = self.historical_df[self.historical_df["date"] < match_date]

        # Standings live à match_date
        standings, total_teams = compute_standings_from_history(past, match_date, league)

        return compute_features_from_history(
            home_team=home_team,
            away_team=away_team,
            match_date=match_date,
            historical_df=past,
            standings=standings,
            total_teams=total_teams,
            elo_general=self.elo_general,
            elo_home_venue=self.elo_home_venue,
            elo_away_venue=self.elo_away_venue,
        )


# Singleton global réutilisé par scheduler.py
FOOT_STATE = FootballInferenceState()
