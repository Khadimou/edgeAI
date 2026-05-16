"""
Feature engineering pour le modèle XGBoost edgeAI.
50 features réelles : forme récente, venue-specific, contexte, H2H, standings, cartons, mi-temps,
ELO ratings (général + venue), Pythagorean expectation, streaks, BTTS, forme ELO-pondérée.
Toutes calculables depuis football-data.org — aucune feature morte.

ELO :
- ELO général : 1 dict {team: rating}, K=20, initial 1500, home-advantage 65pts en update
- ELO venue : 2 dicts séparés home/away pour modéliser dynamique différente
- Forme ELO-pondérée : surperformance vs expected_points calculé depuis adversaires
"""
import pandas as pd
import numpy as np
from dataclasses import dataclass

ELO_K = 20.0
ELO_HOME_ADV = 65.0  # ~0.4 goal advantage en moyenne sur big-5
ELO_INIT = 1500.0

# Noms des features Phase 2 (shots/SOT/corners depuis football-data.co.uk).
# Backtest a montré que ces features améliorent AH mais détériorent 1X2/OU.
# → on les EXCLUT du training et inference des modèles 1X2/OU via feature_names_phase1()
PHASE2_FEATURE_NAMES = frozenset([
    "home_shots_avg", "away_shots_avg",
    "home_sot_avg", "away_sot_avg",
    "home_shots_against_avg", "away_shots_against_avg",
    "home_corners_avg", "away_corners_avg",
    "home_shot_accuracy", "away_shot_accuracy",
    "home_shot_conversion", "away_shot_conversion",
    "home_xg_proxy", "away_xg_proxy", "xg_diff",
])


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

    # ─── Phase 1 features ──────────────────────────────────────
    # ELO ratings (général) — calculés chronologiquement
    home_elo: float = 1500.0
    away_elo: float = 1500.0
    elo_diff: float = 0.0  # home - away (positif = home favori)

    # ELO venue-specific (séparé domicile vs extérieur)
    home_elo_home: float = 1500.0  # rating de home_team quand elle joue à domicile
    away_elo_away: float = 1500.0  # rating de away_team quand elle joue à l'extérieur
    elo_venue_diff: float = 0.0

    # Pythagorean win expectation (sur 10 derniers matchs)
    # Pyth = GF^1.83 / (GF^1.83 + GA^1.83), formule Morey/Hollinger adaptée
    home_pythag: float = 0.5
    away_pythag: float = 0.5

    # Streaks (W/L/D consécutifs sur les 10 derniers matchs)
    home_unbeaten_streak: float = 0.0
    home_losing_streak: float = 0.0
    away_unbeaten_streak: float = 0.0
    away_losing_streak: float = 0.0

    # Forme ELO-pondérée (strength of schedule)
    # = points réels - points attendus selon ELO des adversaires
    # Positif = surperforme, négatif = sousperforme
    home_form_vs_expected: float = 0.0
    away_form_vs_expected: float = 0.0

    # Both Teams To Score rate (sur 10 derniers matchs)
    home_btts_rate: float = 0.5
    away_btts_rate: float = 0.5

    # ─── Phase 2 features (shots/SOT/corners depuis fdco) ──────
    # Avg sur 10 derniers matchs
    home_shots_avg: float = 12.0  # baseline ~12 shots / match en big-5
    away_shots_avg: float = 12.0
    home_sot_avg: float = 4.0     # baseline ~4 SOT / match
    away_sot_avg: float = 4.0
    home_shots_against_avg: float = 12.0
    away_shots_against_avg: float = 12.0
    home_corners_avg: float = 5.0
    away_corners_avg: float = 5.0

    # Efficacité : SOT / shots (précision)
    home_shot_accuracy: float = 0.33
    away_shot_accuracy: float = 0.33

    # Conversion : goals / shots sur 10 derniers (clinique vs gâcheur)
    home_shot_conversion: float = 0.10
    away_shot_conversion: float = 0.10

    # xG proxy (Caley/Eastwood weighting approximé):
    # xG ≈ 0.05 × non_sot_shots + 0.30 × sot
    # Sert d'estimateur de qualité offensive intrinsèque
    home_xg_proxy: float = 1.4
    away_xg_proxy: float = 1.4
    xg_diff: float = 0.0          # home - away

    def to_array(self) -> np.ndarray:
        return np.array(
            [getattr(self, f) for f in self.__dataclass_fields__],
            dtype=np.float32,
        )

    def to_array_phase1(self) -> np.ndarray:
        """Retourne uniquement les 52 features Phase 1 (sans shots/SOT/corners).

        Backtests montrent que les features Phase 2 (shots) :
        - améliorent AH (+2.1pts ROI) → utiliser to_array() pour AH
        - dégradent 1X2 (-2.5pts) et OU (-12pts) → utiliser to_array_phase1()
        """
        return np.array(
            [getattr(self, f) for f in self.__dataclass_fields__
             if f not in PHASE2_FEATURE_NAMES],
            dtype=np.float32,
        )

    @classmethod
    def feature_names(cls) -> list[str]:
        return [f.name for f in cls.__dataclass_fields__.values()]

    @classmethod
    def feature_names_phase1(cls) -> list[str]:
        """Sous-ensemble Phase 1 (52 fields, sans les shots/SOT/corners Phase 2)."""
        return [f.name for f in cls.__dataclass_fields__.values()
                if f.name not in PHASE2_FEATURE_NAMES]


def compute_features_from_history(
    home_team: str,
    away_team: str,
    match_date: pd.Timestamp,
    historical_df: pd.DataFrame,
    odds_df: pd.DataFrame | None = None,
    standings: dict[str, int] | None = None,
    total_teams: int = 20,
    window: int = 5,
    elo_general: dict[str, float] | None = None,
    elo_home_venue: dict[str, float] | None = None,
    elo_away_venue: dict[str, float] | None = None,
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

    # --- Phase 1 : ELO + advanced features ---
    if elo_general is not None:
        compute_elo_features(
            feat, home_team, away_team,
            elo_general,
            elo_home_venue if elo_home_venue is not None else {},
            elo_away_venue if elo_away_venue is not None else {},
        )
        compute_advanced_features(
            feat, home_team, away_team, match_date,
            historical_df, elo_general, window=10,
        )

    # --- Phase 2 : shots/SOT/corners features (depuis fdco backfill) ---
    # Skip silencieusement si les colonnes ne sont pas dispos (legacy DB)
    if "home_shots" in historical_df.columns:
        compute_shots_features(
            feat, home_team, away_team, match_date,
            historical_df, window=10,
        )

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


# ──────────────────────────────────────────────────────────
# Phase 1 : ELO ratings + advanced features
# ──────────────────────────────────────────────────────────

def init_elo() -> dict[str, float]:
    """État ELO initial (vide). Sera peuplé chronologiquement par le builder."""
    return {}


def elo_expected(r_a: float, r_b: float) -> float:
    """Probabilité que A batte B selon ELO."""
    return 1.0 / (1.0 + 10 ** ((r_b - r_a) / 400.0))


def update_elo(
    elo: dict[str, float],
    home_team: str,
    away_team: str,
    home_score: int,
    away_score: int,
    K: float = ELO_K,
    home_adv: float = ELO_HOME_ADV,
) -> None:
    """
    Update ELO général après un match. Home advantage intégré dans expected.

    Score réel : 1 = home_win, 0.5 = draw, 0 = away_win
    Bonus victoire large (>= 2 buts d'écart) : multiplicateur K (1.5x si écart 2, 1.75x si 3+)
    """
    r_h = elo.get(home_team, ELO_INIT)
    r_a = elo.get(away_team, ELO_INIT)
    # Home advantage : on traite r_h comme si c'était r_h + home_adv pour le calcul de l'expected
    exp_h = elo_expected(r_h + home_adv, r_a)
    if home_score > away_score:
        score_h = 1.0
    elif home_score < away_score:
        score_h = 0.0
    else:
        score_h = 0.5

    # Goal margin multiplier (résultat plus convaincant si large)
    margin = abs(home_score - away_score)
    if margin >= 3:
        k_mult = 1.75
    elif margin == 2:
        k_mult = 1.5
    else:
        k_mult = 1.0
    k = K * k_mult

    elo[home_team] = r_h + k * (score_h - exp_h)
    elo[away_team] = r_a + k * ((1 - score_h) - (1 - exp_h))


def update_elo_venue(
    elo_home: dict[str, float],
    elo_away: dict[str, float],
    home_team: str,
    away_team: str,
    home_score: int,
    away_score: int,
    K: float = ELO_K,
) -> None:
    """
    Update ELO venue-specific (2 dicts séparés). Pas de home advantage car déjà venue-segmenté.
    """
    r_h = elo_home.get(home_team, ELO_INIT)
    r_a = elo_away.get(away_team, ELO_INIT)
    exp_h = elo_expected(r_h, r_a)
    if home_score > away_score:
        score_h = 1.0
    elif home_score < away_score:
        score_h = 0.0
    else:
        score_h = 0.5

    margin = abs(home_score - away_score)
    if margin >= 3:
        k_mult = 1.75
    elif margin == 2:
        k_mult = 1.5
    else:
        k_mult = 1.0
    k = K * k_mult

    elo_home[home_team] = r_h + k * (score_h - exp_h)
    elo_away[away_team] = r_a + k * ((1 - score_h) - (1 - exp_h))


def _pythagorean(goals_for: float, goals_against: float, exponent: float = 1.83) -> float:
    """Pythagorean win expectation (Morey/Hollinger adapted for football, exponent 1.83)."""
    if goals_for <= 0 and goals_against <= 0:
        return 0.5
    a = goals_for ** exponent
    b = goals_against ** exponent
    return float(a / (a + b)) if (a + b) > 0 else 0.5


def _streaks(hist: pd.DataFrame, team: str) -> tuple[float, float]:
    """
    Renvoie (unbeaten_streak, losing_streak) en partant du match le plus récent.
    Unbeaten = W ou D consécutifs ; losing = L consécutifs.
    """
    if len(hist) == 0:
        return 0.0, 0.0
    unb = 0
    lose = 0
    # iter chronologiquement inversé (plus récent en premier)
    for _, row in hist.iloc[::-1].iterrows():
        if row["home_team"] == team:
            won = row["home_score"] > row["away_score"]
            drew = row["home_score"] == row["away_score"]
        else:
            won = row["away_score"] > row["home_score"]
            drew = row["home_score"] == row["away_score"]

        if unb == 0 and lose == 0:
            # premier match qu'on regarde : initialise la streak active
            if won or drew:
                unb = 1
            else:
                lose = 1
            continue
        # continue la streak active si même type
        if unb > 0:
            if won or drew:
                unb += 1
            else:
                break
        elif lose > 0:
            if not won and not drew:
                lose += 1
            else:
                break
    return float(unb), float(lose)


def _btts_rate(hist: pd.DataFrame) -> float:
    """% matchs où les 2 équipes ont marqué."""
    if len(hist) == 0:
        return 0.5
    btts = ((hist["home_score"] >= 1) & (hist["away_score"] >= 1)).sum()
    return float(btts / len(hist))


def _form_vs_expected(
    hist: pd.DataFrame, team: str, elo_general: dict[str, float],
) -> float:
    """
    Calcule (points réels - points attendus) sur les `hist` matchs en fonction
    de l'ELO de l'adversaire AVANT ce match (snapshot à match_date).

    Comme on n'a pas l'ELO historique exact, on approxime avec l'ELO actuel
    (best-effort). Acceptable car la corrélation reste valide.
    """
    if len(hist) == 0:
        return 0.0
    team_elo = elo_general.get(team, ELO_INIT)
    total = 0.0
    for _, row in hist.iterrows():
        if row["home_team"] == team:
            opp = row["away_team"]
            won = row["home_score"] > row["away_score"]
            drew = row["home_score"] == row["away_score"]
            home_for_team = True
        else:
            opp = row["home_team"]
            won = row["away_score"] > row["home_score"]
            drew = row["home_score"] == row["away_score"]
            home_for_team = False

        opp_elo = elo_general.get(opp, ELO_INIT)
        # Expected points selon ELO (1 pour W, 0.5 pour D, 0 pour L → multiplié par 3 pour points)
        # On approx prob_win seul (drawn rare in pure 2-outcome ELO, simplification)
        if home_for_team:
            exp_p = elo_expected(team_elo + ELO_HOME_ADV, opp_elo) * 3
        else:
            exp_p = elo_expected(team_elo, opp_elo + ELO_HOME_ADV) * 3

        actual_p = 3 if won else (1 if drew else 0)
        total += actual_p - exp_p
    return float(total / len(hist))


def compute_elo_features(
    feat: MatchFeatures,
    home_team: str, away_team: str,
    elo_general: dict[str, float],
    elo_home_venue: dict[str, float],
    elo_away_venue: dict[str, float],
) -> None:
    """Remplit les champs ELO du feat in-place."""
    feat.home_elo = elo_general.get(home_team, ELO_INIT)
    feat.away_elo = elo_general.get(away_team, ELO_INIT)
    feat.elo_diff = feat.home_elo - feat.away_elo

    feat.home_elo_home = elo_home_venue.get(home_team, ELO_INIT)
    feat.away_elo_away = elo_away_venue.get(away_team, ELO_INIT)
    feat.elo_venue_diff = feat.home_elo_home - feat.away_elo_away


def compute_advanced_features(
    feat: MatchFeatures,
    home_team: str, away_team: str,
    match_date: pd.Timestamp,
    historical_df: pd.DataFrame,
    elo_general: dict[str, float],
    window: int = 10,
) -> None:
    """Remplit Pythagorean + streaks + ELO-weighted form + BTTS in-place."""
    home_hist = _get_team_history(historical_df, home_team, match_date, window)
    away_hist = _get_team_history(historical_df, away_team, match_date, window)

    if len(home_hist) > 0:
        gf = sum(r["home_score"] if r["home_team"] == home_team else r["away_score"]
                 for _, r in home_hist.iterrows())
        ga = sum(r["away_score"] if r["home_team"] == home_team else r["home_score"]
                 for _, r in home_hist.iterrows())
        feat.home_pythag = _pythagorean(gf, ga)
        feat.home_unbeaten_streak, feat.home_losing_streak = _streaks(home_hist, home_team)
        feat.home_form_vs_expected = _form_vs_expected(home_hist, home_team, elo_general)
        feat.home_btts_rate = _btts_rate(home_hist)

    if len(away_hist) > 0:
        gf = sum(r["home_score"] if r["home_team"] == away_team else r["away_score"]
                 for _, r in away_hist.iterrows())
        ga = sum(r["away_score"] if r["home_team"] == away_team else r["home_score"]
                 for _, r in away_hist.iterrows())
        feat.away_pythag = _pythagorean(gf, ga)
        feat.away_unbeaten_streak, feat.away_losing_streak = _streaks(away_hist, away_team)
        feat.away_form_vs_expected = _form_vs_expected(away_hist, away_team, elo_general)
        feat.away_btts_rate = _btts_rate(away_hist)


# ──────────────────────────────────────────────────────────
# Phase 2 : shots/SOT/corners features
# ──────────────────────────────────────────────────────────

def _shots_stats(hist: pd.DataFrame, team: str) -> dict:
    """
    Pour un team sur les `hist` matchs : calcule shots/SOT/corners moyens
    + shots conceded + accuracy (sot/shots) + conversion (goals/shots).

    Retourne defaults si les colonnes shots ne sont pas dispo (None/NaN).
    """
    defaults = {
        "shots_avg": 12.0, "sot_avg": 4.0, "shots_against_avg": 12.0,
        "corners_avg": 5.0, "accuracy": 0.33, "conversion": 0.10,
        "xg_proxy": 1.4,
    }
    if len(hist) == 0:
        return defaults
    if "home_shots" not in hist.columns:
        return defaults

    shots_for = []
    shots_against = []
    sot_for = []
    corners_for = []
    goals_for = []
    for _, r in hist.iterrows():
        is_home = (r["home_team"] == team)
        hs, as_ = r.get("home_shots"), r.get("away_shots")
        hst, ast = r.get("home_shots_on_target"), r.get("away_shots_on_target")
        hc, ac = r.get("home_corners"), r.get("away_corners")
        ghome, gaway = r["home_score"], r["away_score"]
        if is_home:
            if pd.notna(hs): shots_for.append(float(hs))
            if pd.notna(as_): shots_against.append(float(as_))
            if pd.notna(hst): sot_for.append(float(hst))
            if pd.notna(hc): corners_for.append(float(hc))
            goals_for.append(float(ghome))
        else:
            if pd.notna(as_): shots_for.append(float(as_))
            if pd.notna(hs): shots_against.append(float(hs))
            if pd.notna(ast): sot_for.append(float(ast))
            if pd.notna(ac): corners_for.append(float(ac))
            goals_for.append(float(gaway))

    if not shots_for:
        return defaults

    shots_avg = float(np.mean(shots_for))
    sot_avg = float(np.mean(sot_for)) if sot_for else 4.0
    shots_against_avg = float(np.mean(shots_against)) if shots_against else 12.0
    corners_avg = float(np.mean(corners_for)) if corners_for else 5.0
    goals_avg = float(np.mean(goals_for))

    accuracy = (sot_avg / shots_avg) if shots_avg > 0 else 0.33
    conversion = (goals_avg / shots_avg) if shots_avg > 0 else 0.10
    # xG proxy : Caley-like weighting approx :
    #   xG ≈ 0.05 × (shots - sot) + 0.30 × sot
    non_sot = max(shots_avg - sot_avg, 0.0)
    xg_proxy = 0.05 * non_sot + 0.30 * sot_avg

    return {
        "shots_avg": shots_avg, "sot_avg": sot_avg,
        "shots_against_avg": shots_against_avg,
        "corners_avg": corners_avg,
        "accuracy": float(min(max(accuracy, 0.0), 1.0)),
        "conversion": float(min(max(conversion, 0.0), 1.0)),
        "xg_proxy": float(xg_proxy),
    }


def compute_shots_features(
    feat: MatchFeatures,
    home_team: str, away_team: str,
    match_date: pd.Timestamp,
    historical_df: pd.DataFrame,
    window: int = 10,
) -> None:
    """Remplit les shots/SOT/corners features in-place."""
    home_hist = _get_team_history(historical_df, home_team, match_date, window)
    away_hist = _get_team_history(historical_df, away_team, match_date, window)

    if len(home_hist) > 0:
        s = _shots_stats(home_hist, home_team)
        feat.home_shots_avg = s["shots_avg"]
        feat.home_sot_avg = s["sot_avg"]
        feat.home_shots_against_avg = s["shots_against_avg"]
        feat.home_corners_avg = s["corners_avg"]
        feat.home_shot_accuracy = s["accuracy"]
        feat.home_shot_conversion = s["conversion"]
        feat.home_xg_proxy = s["xg_proxy"]

    if len(away_hist) > 0:
        s = _shots_stats(away_hist, away_team)
        feat.away_shots_avg = s["shots_avg"]
        feat.away_sot_avg = s["sot_avg"]
        feat.away_shots_against_avg = s["shots_against_avg"]
        feat.away_corners_avg = s["corners_avg"]
        feat.away_shot_accuracy = s["accuracy"]
        feat.away_shot_conversion = s["conversion"]
        feat.away_xg_proxy = s["xg_proxy"]

    feat.xg_diff = feat.home_xg_proxy - feat.away_xg_proxy


# ──────────────────────────────────────────────────────────

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
