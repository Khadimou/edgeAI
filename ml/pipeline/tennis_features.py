"""
Feature engineering pour le modèle Tennis ATP.

Architecture : on prédit P(player_A bat player_B) en posant la convention
qu'on génère 1 ligne par match avec player_A = winner ET on génère aussi
la ligne symétrique avec swap, pour bien apprendre l'asymétrie.

Au moment de l'inférence, on appelle le modèle avec player_A = home
(arbitraire, ex: ordre alphabétique ou affichage), et la prob retournée
correspond à P(player_A gagne).

Features clés (binaires, pas de draw possible) :
- ELO général + surface-specific (clay/grass/hard)
- Recent form (10 derniers matchs)
- Recent form on surface (5 derniers sur cette surface)
- H2H direct
- Rank et points ATP
- Age + handedness
- Best-of (3 vs 5 = dynamique différente)
- Round (R128/R64/.../F = enjeu)
- Days rest depuis dernier match
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd


SURFACES = ["Hard", "Clay", "Grass", "Carpet"]
ROUND_VALUE = {
    "R128": 1, "R64": 2, "R32": 3, "R16": 4, "QF": 5, "SF": 6, "F": 7,
    "BR": 5, "RR": 3,  # bronze, round robin
}


@dataclass
class TennisFeatures:
    # ELO général (1500 = baseline, K=32)
    a_elo: float = 1500.0
    b_elo: float = 1500.0
    elo_diff: float = 0.0

    # ELO surface (séparé par surface)
    a_elo_surface: float = 1500.0
    b_elo_surface: float = 1500.0
    elo_surface_diff: float = 0.0

    # Recent form globale (10 derniers matchs)
    a_recent_winrate: float = 0.5
    b_recent_winrate: float = 0.5

    # Recent form sur cette surface (5 derniers)
    a_surface_winrate: float = 0.5
    b_surface_winrate: float = 0.5

    # H2H direct
    a_h2h_winrate: float = 0.5
    h2h_matches: float = 0.0

    # Rank ATP
    a_rank: float = 100.0
    b_rank: float = 100.0
    rank_diff: float = 0.0  # a - b (négatif si A mieux classé)

    # Points ATP
    a_rank_points: float = 0.0
    b_rank_points: float = 0.0
    points_log_diff: float = 0.0  # log(a) - log(b)

    # Age (peak tennis ~27 ans)
    a_age: float = 25.0
    b_age: float = 25.0

    # Handedness (1 = lefty, 0 = righty) — lefty advantage faible mais réel
    a_is_lefty: float = 0.0
    b_is_lefty: float = 0.0
    different_hand: float = 0.0

    # Days rest
    a_days_rest: float = 7.0
    b_days_rest: float = 7.0

    # Surface one-hot
    is_hard: float = 0.0
    is_clay: float = 0.0
    is_grass: float = 0.0

    # Best-of (3 ou 5)
    is_best_of_5: float = 0.0

    # Round value (1-7, plus haut = plus tard dans le tournoi)
    round_value: float = 3.0

    # Stats serve/return moyennes (sur 10 derniers matchs)
    a_serve_efficiency: float = 0.6  # 1st serve % won
    b_serve_efficiency: float = 0.6

    def to_array(self) -> np.ndarray:
        return np.array(
            [getattr(self, f) for f in self.__dataclass_fields__],
            dtype=np.float32,
        )

    @classmethod
    def feature_names(cls) -> list[str]:
        return [f.name for f in cls.__dataclass_fields__.values()]


# ──────────────────────────────────────────────────────────
# ELO calculation (général + surface-specific)
# ──────────────────────────────────────────────────────────

def init_elo() -> dict[str, float]:
    return {}


def update_elo(
    elo_general: dict[str, float],
    elo_surface: dict[str, dict[str, float]],  # {surface: {player: elo}}
    winner: str, loser: str, surface: str,
    K: float = 32.0,
) -> None:
    """Update ELO général + ELO surface-specific."""
    # Général
    r_w = elo_general.get(winner, 1500.0)
    r_l = elo_general.get(loser, 1500.0)
    e_w = 1 / (1 + 10 ** ((r_l - r_w) / 400))
    elo_general[winner] = r_w + K * (1 - e_w)
    elo_general[loser] = r_l + K * (0 - (1 - e_w))

    # Surface (skip si surface inconnue)
    if surface in SURFACES:
        if surface not in elo_surface:
            elo_surface[surface] = {}
        es = elo_surface[surface]
        r_w_s = es.get(winner, 1500.0)
        r_l_s = es.get(loser, 1500.0)
        e_w_s = 1 / (1 + 10 ** ((r_l_s - r_w_s) / 400))
        # K plus élevé sur surface car moins d'historique
        K_s = 40.0
        es[winner] = r_w_s + K_s * (1 - e_w_s)
        es[loser] = r_l_s + K_s * (0 - (1 - e_w_s))


# ──────────────────────────────────────────────────────────
# Compute features for one match (player_a vs player_b)
# ──────────────────────────────────────────────────────────

def compute_tennis_features(
    player_a: str, player_b: str,
    match_date: pd.Timestamp,
    surface: str,
    best_of: int,
    round_str: str,
    historical_df: pd.DataFrame,
    elo_general: dict[str, float],
    elo_surface: dict[str, dict[str, float]],
    rank_a: float | None = None,
    rank_b: float | None = None,
    points_a: float | None = None,
    points_b: float | None = None,
    age_a: float | None = None,
    age_b: float | None = None,
    hand_a: str | None = None,
    hand_b: str | None = None,
) -> TennisFeatures:
    feat = TennisFeatures()

    # ELO général
    feat.a_elo = elo_general.get(player_a, 1500.0)
    feat.b_elo = elo_general.get(player_b, 1500.0)
    feat.elo_diff = feat.a_elo - feat.b_elo

    # ELO surface
    surface_elos = elo_surface.get(surface, {})
    feat.a_elo_surface = surface_elos.get(player_a, 1500.0)
    feat.b_elo_surface = surface_elos.get(player_b, 1500.0)
    feat.elo_surface_diff = feat.a_elo_surface - feat.b_elo_surface

    # Surface flags
    feat.is_hard = float(surface == "Hard")
    feat.is_clay = float(surface == "Clay")
    feat.is_grass = float(surface == "Grass")

    # Best of
    feat.is_best_of_5 = float(best_of == 5)

    # Round
    feat.round_value = float(ROUND_VALUE.get(round_str, 3))

    # Rank + points
    if rank_a is not None and not pd.isna(rank_a):
        feat.a_rank = float(rank_a)
    if rank_b is not None and not pd.isna(rank_b):
        feat.b_rank = float(rank_b)
    feat.rank_diff = feat.a_rank - feat.b_rank

    if points_a is not None and not pd.isna(points_a) and points_a > 0:
        feat.a_rank_points = float(points_a)
    if points_b is not None and not pd.isna(points_b) and points_b > 0:
        feat.b_rank_points = float(points_b)
    if feat.a_rank_points > 0 and feat.b_rank_points > 0:
        feat.points_log_diff = float(np.log(feat.a_rank_points) - np.log(feat.b_rank_points))

    # Age
    if age_a is not None and not pd.isna(age_a):
        feat.a_age = float(age_a)
    if age_b is not None and not pd.isna(age_b):
        feat.b_age = float(age_b)

    # Hand
    feat.a_is_lefty = float(hand_a == "L")
    feat.b_is_lefty = float(hand_b == "L")
    feat.different_hand = float((hand_a == "L") != (hand_b == "L"))

    # Recent form depuis historical_df
    past = historical_df[historical_df["match_date"] < match_date]
    feat.a_recent_winrate, feat.a_serve_efficiency = _player_form(past, player_a, n=10)
    feat.b_recent_winrate, feat.b_serve_efficiency = _player_form(past, player_b, n=10)

    # Surface form
    past_surface = past[past["surface"] == surface]
    feat.a_surface_winrate, _ = _player_form(past_surface, player_a, n=5)
    feat.b_surface_winrate, _ = _player_form(past_surface, player_b, n=5)

    # H2H
    h2h = past[
        ((past["winner_name"] == player_a) & (past["loser_name"] == player_b)) |
        ((past["winner_name"] == player_b) & (past["loser_name"] == player_a))
    ].tail(10)
    if len(h2h) > 0:
        a_wins = (h2h["winner_name"] == player_a).sum()
        feat.a_h2h_winrate = float(a_wins / len(h2h))
        feat.h2h_matches = float(len(h2h))

    # Days rest
    feat.a_days_rest = _days_since_last_match(past, player_a, match_date)
    feat.b_days_rest = _days_since_last_match(past, player_b, match_date)

    return feat


def _player_form(past: pd.DataFrame, player: str, n: int = 10) -> tuple[float, float]:
    """Renvoie (winrate, serve_efficiency_avg)."""
    mask = (past["winner_name"] == player) | (past["loser_name"] == player)
    recent = past[mask].sort_values("match_date").tail(n)
    if len(recent) == 0:
        return 0.5, 0.6
    wins = (recent["winner_name"] == player).sum()
    winrate = wins / len(recent)

    # Serve efficiency = 1st serve points won / serve points (when player serves)
    serve_eff = []
    for _, row in recent.iterrows():
        if row["winner_name"] == player:
            sp = row.get("w_svpt")
            won = row.get("w_1stWon", 0) + row.get("w_2ndWon", 0)
        else:
            sp = row.get("l_svpt")
            won = row.get("l_1stWon", 0) + row.get("l_2ndWon", 0)
        if pd.notna(sp) and sp > 0:
            serve_eff.append(won / sp)
    return winrate, float(np.mean(serve_eff)) if serve_eff else 0.6


def _days_since_last_match(past: pd.DataFrame, player: str, match_date: pd.Timestamp) -> float:
    mask = (past["winner_name"] == player) | (past["loser_name"] == player)
    last = past[mask].sort_values("match_date").tail(1)
    if len(last) == 0:
        return 30.0
    delta = match_date - pd.Timestamp(last.iloc[-1]["match_date"])
    return float(max(min(delta.days, 365), 1))
