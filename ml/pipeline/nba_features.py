"""
Feature engineering NBA — modèle binaire HOME/AWAY win.
24 features réelles : forme, venue-specific, repos/B2B, shooting, H2H, marché.
"""
import pandas as pd
import numpy as np
from dataclasses import dataclass


@dataclass
class NBAFeatures:
    # Forme récente globale (10 derniers matchs — NBA en a beaucoup plus que le foot)
    home_win_rate: float = 0.5
    home_points_scored_avg: float = 110.0
    home_points_allowed_avg: float = 110.0
    home_point_diff_avg: float = 0.0
    home_fg_pct_avg: float = 0.46
    home_fg3_pct_avg: float = 0.36

    away_win_rate: float = 0.5
    away_points_scored_avg: float = 110.0
    away_points_allowed_avg: float = 110.0
    away_point_diff_avg: float = 0.0
    away_fg_pct_avg: float = 0.46
    away_fg3_pct_avg: float = 0.36

    # Forme venue-specific
    home_home_win_rate: float = 0.55
    home_home_point_diff_avg: float = 1.5
    away_away_win_rate: float = 0.45
    away_away_point_diff_avg: float = -1.5

    # Repos & calendrier
    home_rest_days: float = 2.0
    away_rest_days: float = 2.0
    home_is_b2b: float = 0.0  # back-to-back
    away_is_b2b: float = 0.0
    home_games_last_7d: float = 3.0
    away_games_last_7d: float = 3.0

    # Head-to-head (saison + saison précédente)
    h2h_home_win_rate: float = 0.5
    h2h_matches_played: float = 0.0

    def to_array(self) -> np.ndarray:
        return np.array(
            [getattr(self, f) for f in self.__dataclass_fields__],
            dtype=np.float32,
        )

    @classmethod
    def feature_names(cls) -> list[str]:
        return [f.name for f in cls.__dataclass_fields__.values()]


def compute_nba_features(
    home_team: str,
    away_team: str,
    match_date: pd.Timestamp,
    history: pd.DataFrame,
    window: int = 10,
) -> NBAFeatures:
    """
    Calcule les features NBA depuis l'historique.
    history doit contenir : match_date, home_team, away_team, home_score, away_score,
                            home_fg_pct, home_fg3_pct, away_fg_pct, away_fg3_pct
    """
    feat = NBAFeatures()
    past = history[history["match_date"] < match_date]

    # --- Forme globale (10 derniers matchs) ---
    home_hist = _team_history(past, home_team, window)
    away_hist = _team_history(past, away_team, window)

    if len(home_hist) > 0:
        feat.home_win_rate = _win_rate(home_hist, home_team)
        feat.home_points_scored_avg = _scored_avg(home_hist, home_team)
        feat.home_points_allowed_avg = _allowed_avg(home_hist, home_team)
        feat.home_point_diff_avg = feat.home_points_scored_avg - feat.home_points_allowed_avg
        feat.home_fg_pct_avg = _shooting_pct(home_hist, home_team, "fg_pct")
        feat.home_fg3_pct_avg = _shooting_pct(home_hist, home_team, "fg3_pct")

    if len(away_hist) > 0:
        feat.away_win_rate = _win_rate(away_hist, away_team)
        feat.away_points_scored_avg = _scored_avg(away_hist, away_team)
        feat.away_points_allowed_avg = _allowed_avg(away_hist, away_team)
        feat.away_point_diff_avg = feat.away_points_scored_avg - feat.away_points_allowed_avg
        feat.away_fg_pct_avg = _shooting_pct(away_hist, away_team, "fg_pct")
        feat.away_fg3_pct_avg = _shooting_pct(away_hist, away_team, "fg3_pct")

    # --- Venue-specific ---
    home_home = past[(past["home_team"] == home_team)].tail(window)
    away_away = past[(past["away_team"] == away_team)].tail(window)

    if len(home_home) > 0:
        feat.home_home_win_rate = float((home_home["home_score"] > home_home["away_score"]).mean())
        feat.home_home_point_diff_avg = float((home_home["home_score"] - home_home["away_score"]).mean())

    if len(away_away) > 0:
        feat.away_away_win_rate = float((away_away["away_score"] > away_away["home_score"]).mean())
        feat.away_away_point_diff_avg = float((away_away["away_score"] - away_away["home_score"]).mean())

    # --- Repos & B2B ---
    last_home_game = past[
        (past["home_team"] == home_team) | (past["away_team"] == home_team)
    ].tail(1)
    if len(last_home_game) > 0:
        delta = match_date - pd.Timestamp(last_home_game.iloc[-1]["match_date"])
        days = max(delta.days, 0)
        feat.home_rest_days = float(days)
        feat.home_is_b2b = float(days <= 1)

    last_away_game = past[
        (past["home_team"] == away_team) | (past["away_team"] == away_team)
    ].tail(1)
    if len(last_away_game) > 0:
        delta = match_date - pd.Timestamp(last_away_game.iloc[-1]["match_date"])
        days = max(delta.days, 0)
        feat.away_rest_days = float(days)
        feat.away_is_b2b = float(days <= 1)

    seven_days_ago = match_date - pd.Timedelta(days=7)
    feat.home_games_last_7d = float(len(past[
        ((past["home_team"] == home_team) | (past["away_team"] == home_team)) &
        (past["match_date"] >= seven_days_ago)
    ]))
    feat.away_games_last_7d = float(len(past[
        ((past["home_team"] == away_team) | (past["away_team"] == away_team)) &
        (past["match_date"] >= seven_days_ago)
    ]))

    # --- H2H ---
    h2h = past[
        ((past["home_team"] == home_team) & (past["away_team"] == away_team)) |
        ((past["home_team"] == away_team) & (past["away_team"] == home_team))
    ].tail(5)

    if len(h2h) > 0:
        home_wins = 0
        for _, row in h2h.iterrows():
            if row["home_team"] == home_team and row["home_score"] > row["away_score"]:
                home_wins += 1
            elif row["away_team"] == home_team and row["away_score"] > row["home_score"]:
                home_wins += 1
        feat.h2h_home_win_rate = home_wins / len(h2h)
        feat.h2h_matches_played = float(len(h2h))

    return feat


def _team_history(past: pd.DataFrame, team: str, window: int) -> pd.DataFrame:
    mask = (past["home_team"] == team) | (past["away_team"] == team)
    return past[mask].tail(window)


def _win_rate(hist: pd.DataFrame, team: str) -> float:
    wins = sum(
        1 for _, row in hist.iterrows()
        if (row["home_team"] == team and row["home_score"] > row["away_score"]) or
           (row["away_team"] == team and row["away_score"] > row["home_score"])
    )
    return wins / len(hist) if len(hist) > 0 else 0.5


def _scored_avg(hist: pd.DataFrame, team: str) -> float:
    pts = [
        row["home_score"] if row["home_team"] == team else row["away_score"]
        for _, row in hist.iterrows()
    ]
    return float(np.mean(pts)) if pts else 110.0


def _allowed_avg(hist: pd.DataFrame, team: str) -> float:
    pts = [
        row["away_score"] if row["home_team"] == team else row["home_score"]
        for _, row in hist.iterrows()
    ]
    return float(np.mean(pts)) if pts else 110.0


def _shooting_pct(hist: pd.DataFrame, team: str, kind: str) -> float:
    """kind = 'fg_pct' ou 'fg3_pct'"""
    home_col = f"home_{kind}"
    away_col = f"away_{kind}"
    if home_col not in hist.columns:
        return 0.46 if kind == "fg_pct" else 0.36
    vals = []
    for _, row in hist.iterrows():
        col = home_col if row["home_team"] == team else away_col
        val = row.get(col)
        if pd.notna(val):
            vals.append(float(val))
    return float(np.mean(vals)) if vals else (0.46 if kind == "fg_pct" else 0.36)
