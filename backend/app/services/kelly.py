from dataclasses import dataclass
from enum import Enum


class RiskProfile(str, Enum):
    CONSERVATIVE = "CONSERVATIVE"
    MODERATE = "MODERATE"
    AGGRESSIVE = "AGGRESSIVE"


KELLY_MULTIPLIERS = {
    RiskProfile.CONSERVATIVE: 0.25,
    RiskProfile.MODERATE: 0.50,
    RiskProfile.AGGRESSIVE: 0.75,
}

MAX_BET_FRACTION = 0.10
# Seuils calibrés par le backtest historique (edge ∈ [8%, 20%] = sweet spot)
MIN_EDGE_THRESHOLD = 0.08
MAX_EDGE_THRESHOLD = 0.20  # au-delà = hallucination (modèle mal calibré)


@dataclass
class KellyResult:
    kelly_fraction: float
    adjusted_fraction: float
    recommended_amount: float
    edge: float
    is_value_bet: bool
    reason: str


def calculate_kelly(
    prob: float,
    odds: float,
    bankroll: float,
    risk_profile: RiskProfile = RiskProfile.MODERATE,
    kelly_user_fraction: float = 0.50,
) -> KellyResult:
    """
    Calcule la mise optimale selon le critère de Kelly fractionnel.

    f* = (p * b - q) / b
    où b = odds - 1, q = 1 - p
    """
    b = odds - 1.0
    q = 1.0 - prob

    if b <= 0:
        return KellyResult(0, 0, 0, 0, False, "Cote invalide")

    # Kelly complet
    kelly_full = (prob * b - q) / b
    edge = prob * odds - 1.0

    if kelly_full <= 0 or edge < MIN_EDGE_THRESHOLD:
        return KellyResult(
            kelly_fraction=max(0, kelly_full),
            adjusted_fraction=0,
            recommended_amount=0,
            edge=edge,
            is_value_bet=False,
            reason=f"Pas de value bet - edge {edge:.1%} < seuil {MIN_EDGE_THRESHOLD:.1%}",
        )

    # Edge "trop élevé" = signe de mauvaise calibration → on filtre
    if edge > MAX_EDGE_THRESHOLD:
        return KellyResult(
            kelly_fraction=kelly_full,
            adjusted_fraction=0,
            recommended_amount=0,
            edge=edge,
            is_value_bet=False,
            reason=f"Edge {edge:.1%} > {MAX_EDGE_THRESHOLD:.0%} (probable hallucination du modèle)",
        )

    # Kelly fractionnel selon profil de risque
    profile_multiplier = KELLY_MULTIPLIERS[risk_profile]
    adjusted = kelly_full * profile_multiplier * kelly_user_fraction

    # Cap à 10% de la bankroll par pari
    adjusted = min(adjusted, MAX_BET_FRACTION)

    recommended_amount = round(bankroll * adjusted, 2)
    recommended_amount = max(1.0, recommended_amount)

    return KellyResult(
        kelly_fraction=kelly_full,
        adjusted_fraction=adjusted,
        recommended_amount=recommended_amount,
        edge=edge,
        is_value_bet=True,
        reason=f"Value bet - edge {edge:.1%}, Kelly {adjusted:.1%} de la bankroll",
    )


def check_portfolio_risk(
    bets_in_progress: int,
    monthly_loss_pct: float,
    max_concurrent: int = 3,
    stop_loss_pct: float = 0.30,
) -> tuple[bool, str]:
    """Vérifie si de nouveaux paris peuvent être placés selon les règles de risque."""
    if bets_in_progress >= max_concurrent:
        return False, f"Maximum {max_concurrent} paris simultanés atteint"

    if monthly_loss_pct >= stop_loss_pct:
        return False, f"Stop-loss mensuel atteint ({monthly_loss_pct:.0%} de perte)"

    return True, "OK"
