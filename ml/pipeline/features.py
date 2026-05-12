"""
Feature engineering pour le modèle XGBoost edgeAI.
40 variables couvrant forme récente, contexte du match, effectif et signaux de marché.
"""
import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from typing import Any


@dataclass
class MatchFeatures:
    # Forme récente domicile (5 derniers matchs)
    home_win_rate: float = 0.0
    home_points_per_game: float = 0.0
    home_goals_scored_avg: float = 0.0
    home_goals_conceded_avg: float = 0.0
    home_xg_for_avg: float = 0.0
    home_xg_against_avg: float = 0.0
    home_xg_differential: float = 0.0
    home_shots_on_target_avg: float = 0.0
    home_possession_avg: float = 0.0
    home_press_score: float = 0.0

    # Forme récente extérieur
    away_win_rate: float = 0.0
    away_points_per_game: float = 0.0
    away_goals_scored_avg: float = 0.0
    away_goals_conceded_avg: float = 0.0
    away_xg_for_avg: float = 0.0
    away_xg_against_avg: float = 0.0
    away_xg_differential: float = 0.0
    away_shots_on_target_avg: float = 0.0
    away_possession_avg: float = 0.0
    away_press_score: float = 0.0

    # Contexte du match
    is_home_advantage: float = 1.0
    days_since_last_home: float = 7.0
    days_since_last_away: float = 7.0
    home_matches_in_7_days: float = 0.0
    away_matches_in_7_days: float = 0.0
    home_distance_traveled: float = 0.0
    away_distance_traveled: float = 0.0

    # Head-to-head
    h2h_home_win_rate: float = 0.33
    h2h_goal_differential: float = 0.0

    # Effectif & motivation
    home_key_players_injured: float = 0.0
    away_key_players_injured: float = 0.0
    home_suspended: float = 0.0
    away_suspended: float = 0.0
    is_cup_week: float = 0.0
    home_league_position_delta: float = 0.0
    away_league_position_delta: float = 0.0
    home_squad_depth: float = 0.8
    away_squad_depth: float = 0.8

    # Signaux de marché
    line_movement: float = 0.0
    bookmaker_margin: float = 0.05
    closing_line_value: float = 0.0

    def to_array(self) -> np.ndarray:
        return np.array([
            self.home_win_rate, self.home_points_per_game,
            self.home_goals_scored_avg, self.home_goals_conceded_avg,
            self.home_xg_for_avg, self.home_xg_against_avg, self.home_xg_differential,
            self.home_shots_on_target_avg, self.home_possession_avg, self.home_press_score,
            self.away_win_rate, self.away_points_per_game,
            self.away_goals_scored_avg, self.away_goals_conceded_avg,
            self.away_xg_for_avg, self.away_xg_against_avg, self.away_xg_differential,
            self.away_shots_on_target_avg, self.away_possession_avg, self.away_press_score,
            self.is_home_advantage,
            self.days_since_last_home, self.days_since_last_away,
            self.home_matches_in_7_days, self.away_matches_in_7_days,
            self.home_distance_traveled, self.away_distance_traveled,
            self.h2h_home_win_rate, self.h2h_goal_differential,
            self.home_key_players_injured, self.away_key_players_injured,
            self.home_suspended, self.away_suspended,
            self.is_cup_week,
            self.home_league_position_delta, self.away_league_position_delta,
            self.home_squad_depth, self.away_squad_depth,
            self.line_movement, self.bookmaker_margin, self.closing_line_value,
        ], dtype=np.float32)

    @classmethod
    def feature_names(cls) -> list[str]:
        return [f.name for f in cls.__dataclass_fields__.values()]


def compute_features_from_history(
    home_team: str,
    away_team: str,
    match_date: pd.Timestamp,
    historical_df: pd.DataFrame,
    odds_df: pd.DataFrame | None = None,
    window: int = 5,
) -> MatchFeatures:
    """
    Calcule les features à partir de l'historique des matchs.
    historical_df doit contenir : date, home_team, away_team, home_score, away_score,
                                   home_xg, away_xg, home_shots_ot, away_shots_ot,
                                   home_possession, away_possession
    """
    feat = MatchFeatures()

    home_hist = _get_team_history(historical_df, home_team, match_date, window)
    away_hist = _get_team_history(historical_df, away_team, match_date, window)

    if len(home_hist) > 0:
        feat.home_win_rate = _win_rate(home_hist, home_team)
        feat.home_points_per_game = _points_per_game(home_hist, home_team)
        feat.home_goals_scored_avg = _goals_scored_avg(home_hist, home_team)
        feat.home_goals_conceded_avg = _goals_conceded_avg(home_hist, home_team)
        feat.home_xg_for_avg = home_hist.get("home_xg", pd.Series([0])).mean()
        feat.home_xg_against_avg = home_hist.get("away_xg", pd.Series([0])).mean()
        feat.home_xg_differential = feat.home_xg_for_avg - feat.home_xg_against_avg
        feat.home_shots_on_target_avg = home_hist.get("home_shots_ot", pd.Series([0])).mean()
        feat.home_possession_avg = home_hist.get("home_possession", pd.Series([50])).mean()

    if len(away_hist) > 0:
        feat.away_win_rate = _win_rate(away_hist, away_team)
        feat.away_points_per_game = _points_per_game(away_hist, away_team)
        feat.away_goals_scored_avg = _goals_scored_avg(away_hist, away_team)
        feat.away_goals_conceded_avg = _goals_conceded_avg(away_hist, away_team)
        feat.away_xg_for_avg = away_hist.get("away_xg", pd.Series([0])).mean()
        feat.away_xg_against_avg = away_hist.get("home_xg", pd.Series([0])).mean()
        feat.away_xg_differential = feat.away_xg_for_avg - feat.away_xg_against_avg
        feat.away_shots_on_target_avg = away_hist.get("away_shots_ot", pd.Series([0])).mean()
        feat.away_possession_avg = away_hist.get("away_possession", pd.Series([50])).mean()

    # H2H
    h2h = historical_df[
        (historical_df["home_team"] == home_team) &
        (historical_df["away_team"] == away_team) &
        (historical_df["date"] < match_date)
    ].tail(5)

    if len(h2h) > 0:
        home_wins = (h2h["home_score"] > h2h["away_score"]).sum()
        feat.h2h_home_win_rate = home_wins / len(h2h)
        feat.h2h_goal_differential = (h2h["home_score"] - h2h["away_score"]).mean()

    # Odds / signaux de marché
    if odds_df is not None and len(odds_df) > 0:
        match_odds = odds_df[
            (odds_df["home_team"] == home_team) &
            (odds_df["away_team"] == away_team)
        ]
        if len(match_odds) > 0:
            opening = match_odds.iloc[0]
            closing = match_odds.iloc[-1]
            feat.line_movement = float(closing.get("home_odds", 0) - opening.get("home_odds", 0))
            total_prob = (1 / closing.get("home_odds", 3) +
                         1 / closing.get("draw_odds", 3) +
                         1 / closing.get("away_odds", 3))
            feat.bookmaker_margin = float(total_prob - 1)

    return feat


def _get_team_history(df: pd.DataFrame, team: str, before: pd.Timestamp, window: int) -> pd.DataFrame:
    mask = (
        ((df["home_team"] == team) | (df["away_team"] == team)) &
        (df["date"] < before)
    )
    return df[mask].sort_values("date").tail(window)


def _win_rate(hist: pd.DataFrame, team: str) -> float:
    wins = 0
    for _, row in hist.iterrows():
        if row["home_team"] == team:
            if row["home_score"] > row["away_score"]:
                wins += 1
        else:
            if row["away_score"] > row["home_score"]:
                wins += 1
    return wins / len(hist) if len(hist) > 0 else 0.0


def _points_per_game(hist: pd.DataFrame, team: str) -> float:
    points = 0
    for _, row in hist.iterrows():
        if row["home_team"] == team:
            if row["home_score"] > row["away_score"]:
                points += 3
            elif row["home_score"] == row["away_score"]:
                points += 1
        else:
            if row["away_score"] > row["home_score"]:
                points += 3
            elif row["home_score"] == row["away_score"]:
                points += 1
    return points / len(hist) if len(hist) > 0 else 0.0


def _goals_scored_avg(hist: pd.DataFrame, team: str) -> float:
    goals = []
    for _, row in hist.iterrows():
        if row["home_team"] == team:
            goals.append(row["home_score"])
        else:
            goals.append(row["away_score"])
    return float(np.mean(goals)) if goals else 0.0


def _goals_conceded_avg(hist: pd.DataFrame, team: str) -> float:
    goals = []
    for _, row in hist.iterrows():
        if row["home_team"] == team:
            goals.append(row["away_score"])
        else:
            goals.append(row["home_score"])
    return float(np.mean(goals)) if goals else 0.0
