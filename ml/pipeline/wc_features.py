"""
Feature engineering pour le modèle Coupe du Monde (foot international).

Différences vs modèle club :
- ELO international dynamique (proxy du FIFA ranking, plus prédictif)
- Forme = 10 derniers internationaux (pas club)
- WC experience : participations + winrate historique en WC
- Continent : facteur de continent advantage (EU vs SA vs autres)
- H2H international

Pas de standings (knockout), pas de cartons, pas de mi-temps (data non dispo).
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd


# Mapping basique pays → continent (simplifié, ~200 pays)
CONTINENT_MAP = {
    # Europe
    "France": "EU", "Germany": "EU", "Italy": "EU", "Spain": "EU", "England": "EU",
    "Netherlands": "EU", "Portugal": "EU", "Belgium": "EU", "Croatia": "EU", "Switzerland": "EU",
    "Denmark": "EU", "Poland": "EU", "Sweden": "EU", "Austria": "EU", "Czech Republic": "EU",
    "Serbia": "EU", "Ukraine": "EU", "Wales": "EU", "Scotland": "EU", "Ireland": "EU",
    "Norway": "EU", "Russia": "EU", "Turkey": "EU", "Romania": "EU", "Greece": "EU",
    "Hungary": "EU", "Slovakia": "EU", "Slovenia": "EU", "Iceland": "EU", "Finland": "EU",
    "Bosnia and Herzegovina": "EU", "Bulgaria": "EU", "Albania": "EU", "Northern Ireland": "EU",
    "Republic of Ireland": "EU", "North Macedonia": "EU", "Montenegro": "EU", "Kosovo": "EU",
    # South America
    "Brazil": "SA", "Argentina": "SA", "Uruguay": "SA", "Colombia": "SA", "Chile": "SA",
    "Peru": "SA", "Paraguay": "SA", "Ecuador": "SA", "Venezuela": "SA", "Bolivia": "SA",
    # North/Central America
    "United States": "NA", "Mexico": "NA", "Canada": "NA", "Costa Rica": "NA",
    "Honduras": "NA", "Panama": "NA", "Jamaica": "NA", "El Salvador": "NA",
    "Trinidad and Tobago": "NA", "Haiti": "NA", "Guatemala": "NA",
    # Africa
    "Senegal": "AF", "Morocco": "AF", "Tunisia": "AF", "Algeria": "AF", "Egypt": "AF",
    "Nigeria": "AF", "Ghana": "AF", "Cameroon": "AF", "Ivory Coast": "AF", "South Africa": "AF",
    "Mali": "AF", "Burkina Faso": "AF", "DR Congo": "AF", "Zambia": "AF", "Kenya": "AF",
    "Angola": "AF", "Cape Verde": "AF", "Gabon": "AF", "Guinea": "AF",
    # Asia
    "Japan": "AS", "South Korea": "AS", "Saudi Arabia": "AS", "Iran": "AS", "Australia": "AS",
    "Qatar": "AS", "Iraq": "AS", "United Arab Emirates": "AS", "China PR": "AS", "Uzbekistan": "AS",
    "Thailand": "AS", "Vietnam": "AS", "Lebanon": "AS", "Syria": "AS", "Jordan": "AS",
    "Oman": "AS", "Bahrain": "AS", "Kuwait": "AS",
    # Oceania
    "New Zealand": "OC", "Fiji": "OC", "Tahiti": "OC", "Solomon Islands": "OC",
}


def get_continent(team: str) -> str:
    return CONTINENT_MAP.get(team, "OTHER")


@dataclass
class WCFeatures:
    # ELO international (1500 = baseline, K=20)
    home_elo: float = 1500.0
    away_elo: float = 1500.0
    elo_diff: float = 0.0  # home_elo - away_elo

    # Forme récente (10 derniers internationaux)
    home_intl_win_rate: float = 0.5
    home_intl_goals_scored_avg: float = 1.0
    home_intl_goals_conceded_avg: float = 1.0
    home_intl_point_diff_avg: float = 0.0

    away_intl_win_rate: float = 0.5
    away_intl_goals_scored_avg: float = 1.0
    away_intl_goals_conceded_avg: float = 1.0
    away_intl_point_diff_avg: float = 0.0

    # WC experience (historique)
    home_wc_appearances: float = 0.0  # nb matchs WC joués historiquement
    away_wc_appearances: float = 0.0
    home_wc_win_rate: float = 0.33  # winrate historique en WC
    away_wc_win_rate: float = 0.33

    # Head-to-head international (last 5)
    h2h_home_win_rate: float = 0.5
    h2h_matches_played: float = 0.0
    h2h_goal_diff_avg: float = 0.0

    # Continent (proxy stylistique)
    # one-hot encoding simplifié
    home_is_eu: float = 0.0
    home_is_sa: float = 0.0
    away_is_eu: float = 0.0
    away_is_sa: float = 0.0
    # Same continent = same style ≈ équilibré
    same_continent: float = 0.0

    # Repos (jours depuis dernier match international)
    home_days_rest: float = 30.0
    away_days_rest: float = 30.0

    # Avantage terrain (1 si vraiment chez soi = pays hôte, 0 = neutral)
    is_home_country: float = 0.0

    def to_array(self) -> np.ndarray:
        return np.array(
            [getattr(self, f) for f in self.__dataclass_fields__],
            dtype=np.float32,
        )

    @classmethod
    def feature_names(cls) -> list[str]:
        return [f.name for f in cls.__dataclass_fields__.values()]


# ──────────────────────────────────────────────────────────
# ELO international calculation
# ──────────────────────────────────────────────────────────

def init_elo_dict() -> dict[str, float]:
    return {}


def update_elo(elo: dict[str, float], home: str, away: str,
               home_score: int, away_score: int,
               is_wc: bool = False, K: float = 20.0) -> None:
    """
    Met à jour ELO après un match (modifie elo in-place).
    K plus élevé pour matchs WC (signal plus important).
    """
    if is_wc:
        K = 30.0
    r_h = elo.get(home, 1500.0)
    r_a = elo.get(away, 1500.0)
    # Score attendu (formule ELO classique)
    e_h = 1 / (1 + 10 ** ((r_a - r_h) / 400))
    e_a = 1 - e_h
    # Score réel
    if home_score > away_score:
        s_h, s_a = 1.0, 0.0
    elif home_score < away_score:
        s_h, s_a = 0.0, 1.0
    else:
        s_h, s_a = 0.5, 0.5
    # Margin of victory multiplier (matchs serrés = K standard, blowout = K boosté)
    diff = abs(home_score - away_score)
    if diff == 0:
        mov = 1.0
    elif diff == 1:
        mov = 1.0
    elif diff == 2:
        mov = 1.5
    else:
        mov = (11 + diff) / 8.0
    elo[home] = r_h + K * mov * (s_h - e_h)
    elo[away] = r_a + K * mov * (s_a - e_a)


# ──────────────────────────────────────────────────────────
# Compute features for one match
# ──────────────────────────────────────────────────────────

def compute_wc_features(
    home_team: str,
    away_team: str,
    match_date: pd.Timestamp,
    historical_df: pd.DataFrame,
    elo: dict[str, float],
    is_home_country: bool = False,
    window: int = 10,
) -> WCFeatures:
    """
    Calcule les features WC pour un match.

    historical_df : tous les matchs internationaux avec date < match_date
                    colonnes : date, home_team, away_team, home_score, away_score,
                    is_wc, tournament
    elo : dict {team_name: elo_rating} (déjà à jour pour cette date)
    """
    feat = WCFeatures()
    past = historical_df[historical_df["date"] < match_date]

    # --- ELO ---
    feat.home_elo = elo.get(home_team, 1500.0)
    feat.away_elo = elo.get(away_team, 1500.0)
    feat.elo_diff = feat.home_elo - feat.away_elo

    # --- Forme récente (10 derniers internationaux) ---
    feat.home_intl_win_rate, feat.home_intl_goals_scored_avg, feat.home_intl_goals_conceded_avg, feat.home_intl_point_diff_avg = \
        _team_form(past, home_team, window)
    feat.away_intl_win_rate, feat.away_intl_goals_scored_avg, feat.away_intl_goals_conceded_avg, feat.away_intl_point_diff_avg = \
        _team_form(past, away_team, window)

    # --- WC experience ---
    wc_only = past[past["is_wc"]]
    home_wc = wc_only[(wc_only["home_team"] == home_team) | (wc_only["away_team"] == home_team)]
    away_wc = wc_only[(wc_only["home_team"] == away_team) | (wc_only["away_team"] == away_team)]
    feat.home_wc_appearances = float(len(home_wc))
    feat.away_wc_appearances = float(len(away_wc))
    feat.home_wc_win_rate = _winrate(home_wc, home_team)
    feat.away_wc_win_rate = _winrate(away_wc, away_team)

    # --- H2H international (last 5) ---
    h2h = past[
        ((past["home_team"] == home_team) & (past["away_team"] == away_team)) |
        ((past["home_team"] == away_team) & (past["away_team"] == home_team))
    ].tail(5)
    if len(h2h) > 0:
        home_wins = 0
        diffs = []
        for _, row in h2h.iterrows():
            if row["home_team"] == home_team:
                if row["home_score"] > row["away_score"]:
                    home_wins += 1
                diffs.append(row["home_score"] - row["away_score"])
            else:
                if row["away_score"] > row["home_score"]:
                    home_wins += 1
                diffs.append(row["away_score"] - row["home_score"])
        feat.h2h_home_win_rate = home_wins / len(h2h)
        feat.h2h_matches_played = float(len(h2h))
        feat.h2h_goal_diff_avg = float(np.mean(diffs))

    # --- Continent ---
    home_cont = get_continent(home_team)
    away_cont = get_continent(away_team)
    feat.home_is_eu = float(home_cont == "EU")
    feat.home_is_sa = float(home_cont == "SA")
    feat.away_is_eu = float(away_cont == "EU")
    feat.away_is_sa = float(away_cont == "SA")
    feat.same_continent = float(home_cont == away_cont)

    # --- Repos ---
    home_last = past[
        (past["home_team"] == home_team) | (past["away_team"] == home_team)
    ].sort_values("date").tail(1)
    if len(home_last) > 0:
        delta = match_date - pd.Timestamp(home_last.iloc[-1]["date"])
        feat.home_days_rest = float(max(min(delta.days, 365), 1))  # clamp 1-365

    away_last = past[
        (past["home_team"] == away_team) | (past["away_team"] == away_team)
    ].sort_values("date").tail(1)
    if len(away_last) > 0:
        delta = match_date - pd.Timestamp(away_last.iloc[-1]["date"])
        feat.away_days_rest = float(max(min(delta.days, 365), 1))

    # --- Avantage terrain ---
    feat.is_home_country = float(is_home_country)

    return feat


def _team_form(past: pd.DataFrame, team: str, window: int) -> tuple[float, float, float, float]:
    """Renvoie (winrate, goals_scored_avg, goals_conceded_avg, goal_diff_avg)."""
    mask = (past["home_team"] == team) | (past["away_team"] == team)
    recent = past[mask].sort_values("date").tail(window)
    if len(recent) == 0:
        return 0.5, 1.0, 1.0, 0.0
    wins = 0
    scored, conceded = [], []
    for _, row in recent.iterrows():
        if row["home_team"] == team:
            scored.append(row["home_score"])
            conceded.append(row["away_score"])
            if row["home_score"] > row["away_score"]:
                wins += 1
        else:
            scored.append(row["away_score"])
            conceded.append(row["home_score"])
            if row["away_score"] > row["home_score"]:
                wins += 1
    return (
        wins / len(recent),
        float(np.mean(scored)),
        float(np.mean(conceded)),
        float(np.mean(scored)) - float(np.mean(conceded)),
    )


def _winrate(matches: pd.DataFrame, team: str) -> float:
    if len(matches) == 0:
        return 0.33
    wins = 0
    for _, row in matches.iterrows():
        if row["home_team"] == team and row["home_score"] > row["away_score"]:
            wins += 1
        elif row["away_team"] == team and row["away_score"] > row["home_score"]:
            wins += 1
    return wins / len(matches)
