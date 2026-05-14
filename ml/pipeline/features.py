"""
Feature engineering pour le modèle XGBoost edgeAI.
36 features réelles : forme récente, venue-specific, contexte, H2H, standings, cartons, mi-temps.
Toutes calculables depuis football-data.org — aucune feature morte.
"""
import pandas as pd
import numpy as np
from dataclasses import dataclass


@dataclass
class MatchFeatures:
    # Forme récente globale — 5 derniers matchs (domicile + extérieur)
    home_win_rate: float = 0.0
    home_points_per_game: float = 0.0
    home_goals_scored_avg: float = 0.0
    home_goals_conceded_avg: float = 0.0
    home_goal_diff_avg: float = 0.0

    away_win_rate: float = 0.0
    away_points_per_game: float = 0.0
    away_goals_scored_avg: float = 0.0
    away_goals_conceded_avg: float = 0.0
    away_goal_diff_avg: float = 0.0

    # Forme venue-specific — 5 derniers matchs à domicile / à l'extérieur
    home_home_win_rate: float = 0.0
    home_home_goals_scored_avg: float = 0.0
    home_home_goals_conceded_avg: float = 0.0

    away_away_win_rate: float = 0.0
    away_away_goals_scored_avg: float = 0.0
    away_away_goals_conceded_avg: float = 0.0

    # Contexte du match
    days_since_last_home: float = 7.0
    days_since_last_away: float = 7.0
    home_matches_in_7_days: float = 0.0
    away_matches_in_7_days: float = 0.0

    # Head-to-head
    h2h_home_win_rate: float = 0.33
    h2h_goal_differential: float = 0.0
    h2h_matches_played: float = 0.0

    # Signaux de marché
    market_implied_home_prob: float = 0.33
    market_implied_draw_prob: float = 0.33
    market_implied_away_prob: float = 0.33
    bookmaker_margin: float = 0.05

    # Classement (0 = 1er, 1 = dernier ; défaut 0.5 = inconnu)
    home_position_norm: float = 0.5
    away_position_norm: float = 0.5
    position_diff: float = 0.0  # (away_pos - home_pos) / n_teams, positif = domicile mieux classé

    # Mi-temps (performance 1ère mi-temps)
    home_ht_goals_avg: float = 0.0
    away_ht_goals_avg: float = 0.0

    # Discipline (cartons moyens sur 5 derniers matchs)
    home_yellow_cards_avg: float = 1.5
    away_yellow_cards_avg: float = 1.5

    # Solidité défensive
    home_clean_sheet_rate: float = 0.0
    away_clean_sheet_rate: float = 0.0

    def to_array(self) -> np.ndarray:
        return np.array(
            [getattr(self, f) for f in self.__dataclass_fields__],
            dtype=np.float32,
        )

    @classmethod
    def feature_names(cls) -> list[str]:
        return [f.name for f in cls.__dataclass_fields__.values()]


def compute_features_from_history(
    home_team: str,
    away_team: str,
    match_date: pd.Timestamp,
    historical_df: pd.DataFrame,
    odds_df: pd.DataFrame | None = None,
    standings: dict[str, int] | None = None,
    total_teams: int = 20,
    window: int = 5,
) -> MatchFeatures:
    """
    Calcule les features à partir de l'historique des matchs.

    historical_df colonnes requises : date, home_team, away_team, home_score, away_score
    Colonnes optionnelles : ht_home_score, ht_away_score, home_yellow_cards, away_yellow_cards
    standings : {team_name: position (1-based)} — si None, utilise les valeurs par défaut
    """
    feat = MatchFeatures()

    # --- Forme globale ---
    home_hist = _get_team_history(historical_df, home_team, match_date, window)
    away_hist = _get_team_history(historical_df, away_team, match_date, window)

    if len(home_hist) > 0:
        feat.home_win_rate = _win_rate(home_hist, home_team)
        feat.home_points_per_game = _points_per_game(home_hist, home_team)
        feat.home_goals_scored_avg = _goals_scored_avg(home_hist, home_team)
        feat.home_goals_conceded_avg = _goals_conceded_avg(home_hist, home_team)
        feat.home_goal_diff_avg = feat.home_goals_scored_avg - feat.home_goals_conceded_avg
        feat.home_clean_sheet_rate = _clean_sheet_rate(home_hist, home_team)

    if len(away_hist) > 0:
        feat.away_win_rate = _win_rate(away_hist, away_team)
        feat.away_points_per_game = _points_per_game(away_hist, away_team)
        feat.away_goals_scored_avg = _goals_scored_avg(away_hist, away_team)
        feat.away_goals_conceded_avg = _goals_conceded_avg(away_hist, away_team)
        feat.away_goal_diff_avg = feat.away_goals_scored_avg - feat.away_goals_conceded_avg
        feat.away_clean_sheet_rate = _clean_sheet_rate(away_hist, away_team)

    # --- Forme venue-specific ---
    home_home_hist = _get_home_games(historical_df, home_team, match_date, window)
    away_away_hist = _get_away_games(historical_df, away_team, match_date, window)

    if len(home_home_hist) > 0:
        feat.home_home_win_rate = float((home_home_hist["home_score"] > home_home_hist["away_score"]).sum() / len(home_home_hist))
        feat.home_home_goals_scored_avg = float(home_home_hist["home_score"].mean())
        feat.home_home_goals_conceded_avg = float(home_home_hist["away_score"].mean())

    if len(away_away_hist) > 0:
        feat.away_away_win_rate = float((away_away_hist["away_score"] > away_away_hist["home_score"]).sum() / len(away_away_hist))
        feat.away_away_goals_scored_avg = float(away_away_hist["away_score"].mean())
        feat.away_away_goals_conceded_avg = float(away_away_hist["home_score"].mean())

    # --- Contexte ---
    last_home = _get_team_history(historical_df, home_team, match_date, window=1)
    if len(last_home) > 0:
        delta = match_date - pd.Timestamp(last_home.iloc[-1]["date"])
        feat.days_since_last_home = float(max(delta.days, 1))

    last_away = _get_team_history(historical_df, away_team, match_date, window=1)
    if len(last_away) > 0:
        delta = match_date - pd.Timestamp(last_away.iloc[-1]["date"])
        feat.days_since_last_away = float(max(delta.days, 1))

    seven_days_ago = match_date - pd.Timedelta(days=7)
    feat.home_matches_in_7_days = float(len(historical_df[
        ((historical_df["home_team"] == home_team) | (historical_df["away_team"] == home_team)) &
        (historical_df["date"] >= seven_days_ago) &
        (historical_df["date"] < match_date)
    ]))
    feat.away_matches_in_7_days = float(len(historical_df[
        ((historical_df["home_team"] == away_team) | (historical_df["away_team"] == away_team)) &
        (historical_df["date"] >= seven_days_ago) &
        (historical_df["date"] < match_date)
    ]))

    # --- H2H ---
    h2h = historical_df[
        (historical_df["home_team"] == home_team) &
        (historical_df["away_team"] == away_team) &
        (historical_df["date"] < match_date)
    ].tail(5)

    if len(h2h) > 0:
        feat.h2h_home_win_rate = float((h2h["home_score"] > h2h["away_score"]).sum() / len(h2h))
        feat.h2h_goal_differential = float((h2h["home_score"] - h2h["away_score"]).mean())
        feat.h2h_matches_played = float(len(h2h))

    # --- Marché ---
    if odds_df is not None and len(odds_df) > 0:
        match_odds = odds_df[
            (odds_df["home_team"] == home_team) &
            (odds_df["away_team"] == away_team)
        ]
        if len(match_odds) > 0:
            closing = match_odds.iloc[-1]
            h_odds = float(closing.get("home_odds") or 3.0)
            d_odds = float(closing.get("draw_odds") or 3.0)
            a_odds = float(closing.get("away_odds") or 3.0)
            if h_odds > 1 and d_odds > 1 and a_odds > 1:
                total_prob = 1 / h_odds + 1 / d_odds + 1 / a_odds
                feat.market_implied_home_prob = (1 / h_odds) / total_prob
                feat.market_implied_draw_prob = (1 / d_odds) / total_prob
                feat.market_implied_away_prob = (1 / a_odds) / total_prob
                feat.bookmaker_margin = float(total_prob - 1)

    # --- Classement ---
    if standings:
        home_pos = standings.get(home_team)
        away_pos = standings.get(away_team)
        if home_pos is not None:
            feat.home_position_norm = (home_pos - 1) / max(total_teams - 1, 1)
        if away_pos is not None:
            feat.away_position_norm = (away_pos - 1) / max(total_teams - 1, 1)
        if home_pos is not None and away_pos is not None:
            feat.position_diff = (away_pos - home_pos) / max(total_teams - 1, 1)

    # --- Mi-temps ---
    if "ht_home_score" in historical_df.columns:
        home_ht = home_hist[home_hist["ht_home_score"].notna()]
        away_ht = away_hist[away_hist["ht_away_score"].notna()]
        if len(home_ht) > 0:
            home_ht_scored = home_ht.apply(
                lambda r: r["ht_home_score"] if r["home_team"] == home_team else r["ht_away_score"], axis=1
            )
            feat.home_ht_goals_avg = float(home_ht_scored.mean())
        if len(away_ht) > 0:
            away_ht_scored = away_ht.apply(
                lambda r: r["ht_away_score"] if r["away_team"] == away_team else r["ht_home_score"], axis=1
            )
            feat.away_ht_goals_avg = float(away_ht_scored.mean())

    # --- Cartons ---
    if "home_yellow_cards" in historical_df.columns:
        if len(home_hist) > 0:
            home_yellows = home_hist.apply(
                lambda r: r["home_yellow_cards"] if r["home_team"] == home_team else r["away_yellow_cards"], axis=1
            )
            feat.home_yellow_cards_avg = float(home_yellows.mean())
        if len(away_hist) > 0:
            away_yellows = away_hist.apply(
                lambda r: r["away_yellow_cards"] if r["away_team"] == away_team else r["home_yellow_cards"], axis=1
            )
            feat.away_yellow_cards_avg = float(away_yellows.mean())

    return feat


# --- Helpers ---

def _get_team_history(df: pd.DataFrame, team: str, before: pd.Timestamp, window: int) -> pd.DataFrame:
    mask = (
        ((df["home_team"] == team) | (df["away_team"] == team)) &
        (df["date"] < before)
    )
    return df[mask].sort_values("date").tail(window)


def _get_home_games(df: pd.DataFrame, team: str, before: pd.Timestamp, window: int) -> pd.DataFrame:
    mask = (df["home_team"] == team) & (df["date"] < before)
    return df[mask].sort_values("date").tail(window)


def _get_away_games(df: pd.DataFrame, team: str, before: pd.Timestamp, window: int) -> pd.DataFrame:
    mask = (df["away_team"] == team) & (df["date"] < before)
    return df[mask].sort_values("date").tail(window)


def _win_rate(hist: pd.DataFrame, team: str) -> float:
    wins = sum(
        1 for _, row in hist.iterrows()
        if (row["home_team"] == team and row["home_score"] > row["away_score"]) or
           (row["away_team"] == team and row["away_score"] > row["home_score"])
    )
    return wins / len(hist)


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
    return points / len(hist)


def _goals_scored_avg(hist: pd.DataFrame, team: str) -> float:
    goals = [
        row["home_score"] if row["home_team"] == team else row["away_score"]
        for _, row in hist.iterrows()
    ]
    return float(np.mean(goals)) if goals else 0.0


def _goals_conceded_avg(hist: pd.DataFrame, team: str) -> float:
    goals = [
        row["away_score"] if row["home_team"] == team else row["home_score"]
        for _, row in hist.iterrows()
    ]
    return float(np.mean(goals)) if goals else 0.0


def _clean_sheet_rate(hist: pd.DataFrame, team: str) -> float:
    clean_sheets = sum(
        1 for _, row in hist.iterrows()
        if (row["home_team"] == team and row["away_score"] == 0) or
           (row["away_team"] == team and row["home_score"] == 0)
    )
    return clean_sheets / len(hist)


def compute_standings_from_history(
    df: pd.DataFrame,
    match_date: pd.Timestamp,
    league: str,
) -> tuple[dict[str, int], int]:
    """
    Calcule le classement d'une ligue à une date donnée depuis les résultats historiques.
    Retourne ({team: position}, total_teams). Évite tout data leakage temporel.
    """
    past = df[(df["date"] < match_date) & (df["league"] == league)]
    if len(past) == 0:
        return {}, 20

    points: dict[str, int] = {}
    gd: dict[str, int] = {}

    for _, row in past.iterrows():
        h, a = row["home_team"], row["away_team"]
        hs, as_ = int(row["home_score"]), int(row["away_score"])
        for team in (h, a):
            points.setdefault(team, 0)
            gd.setdefault(team, 0)
        if hs > as_:
            points[h] += 3
        elif hs == as_:
            points[h] += 1
            points[a] += 1
        else:
            points[a] += 3
        gd[h] += hs - as_
        gd[a] += as_ - hs

    sorted_teams = sorted(points, key=lambda t: (points[t], gd[t]), reverse=True)
    standings = {team: idx + 1 for idx, team in enumerate(sorted_teams)}
    return standings, len(sorted_teams)
