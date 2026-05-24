"""
Modèle de buts Dixon-Coles pour le foot international (Coupe du Monde).

Pourquoi un modèle de buts (et pas juste le classifieur 1X2 existant) ?
- L'AH (handicap asiatique) et la simulation Monte-Carlo du tournoi exigent une
  DISTRIBUTION DE SCORES, pas seulement P(H/D/A). Dixon-Coles fournit λ_home / λ_away
  → matrice de scores → on en dérive 1X2, O/U, AH, et on simule le bracket.

Modèle (Dixon-Coles 1997, adapté international) :
    λ_home = exp(attack[home] - defense[away] + γ · is_home_country)
    λ_away = exp(attack[away] - defense[home])
    P(x,y) = τ(x,y) · Poisson(x; λ_home) · Poisson(y; λ_away)
où τ est la correction des petits scores (corrélation 0-0 / 1-1) via ρ.

Spécificités internationales :
- Pondération temporelle (demi-vie configurable) : un match de 2010 compte moins.
- Pondération par compétition : amicaux sous-pondérés (rotations, enjeu faible).
- Avantage terrain appliqué UNIQUEMENT si non neutre (la quasi-totalité des matchs
  de WC sont sur terrain neutre → γ ≈ 0 pour eux).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.stats import poisson

# Compétitions sous-pondérées (enjeu / sérieux moindre)
FRIENDLY_WEIGHT = 0.5
DEFAULT_HALFLIFE_DAYS = 365 * 3  # demi-vie 3 ans
MAX_GOALS = 10  # taille de la matrice de scores (0..10)


# ──────────────────────────────────────────────────────────
# Correction Dixon-Coles des petits scores
# ──────────────────────────────────────────────────────────

def _tau(x: np.ndarray, y: np.ndarray, lh: np.ndarray, la: np.ndarray, rho: float) -> np.ndarray:
    """Facteur de correction τ pour (x,y) ∈ {0,1}², 1.0 sinon."""
    t = np.ones_like(lh, dtype=float)
    m00 = (x == 0) & (y == 0)
    m01 = (x == 0) & (y == 1)
    m10 = (x == 1) & (y == 0)
    m11 = (x == 1) & (y == 1)
    t[m00] = 1.0 - lh[m00] * la[m00] * rho
    t[m01] = 1.0 + lh[m01] * rho
    t[m10] = 1.0 + la[m10] * rho
    t[m11] = 1.0 - rho
    return t


@dataclass
class WCGoalsModel:
    attack: dict[str, float] = field(default_factory=dict)
    defense: dict[str, float] = field(default_factory=dict)
    intercept: float = 0.0     # μ = log du taux de buts de base
    home_adv: float = 0.25
    rho: float = -0.05
    mean_attack: float = 0.0   # fallback pour équipe inconnue (centré → 0)
    mean_defense: float = 0.0
    trained_through: str = ""  # date max du train set

    # ---- inférence ----
    def _atk(self, team: str) -> float:
        return self.attack.get(team, self.mean_attack)

    def _def(self, team: str) -> float:
        return self.defense.get(team, self.mean_defense)

    def expected_goals(self, home: str, away: str, neutral: bool = True) -> tuple[float, float]:
        ha = 0.0 if neutral else self.home_adv
        lh = np.exp(self.intercept + self._atk(home) - self._def(away) + ha)
        la = np.exp(self.intercept + self._atk(away) - self._def(home))
        return float(lh), float(la)

    def score_matrix(self, home: str, away: str, neutral: bool = True) -> np.ndarray:
        """Matrice (MAX_GOALS+1)² des P(score_home=i, score_away=j)."""
        lh, la = self.expected_goals(home, away, neutral)
        gh = poisson.pmf(np.arange(MAX_GOALS + 1), lh)
        ga = poisson.pmf(np.arange(MAX_GOALS + 1), la)
        mat = np.outer(gh, ga)
        # Correction Dixon-Coles sur le coin bas (0,0),(0,1),(1,0),(1,1)
        mat[0, 0] *= 1.0 - lh * la * self.rho
        mat[0, 1] *= 1.0 + lh * self.rho
        mat[1, 0] *= 1.0 + la * self.rho
        mat[1, 1] *= 1.0 - self.rho
        mat /= mat.sum()  # renormalise
        return mat

    def market_probs(self, home: str, away: str, neutral: bool = True,
                     ou_line: float = 2.5, ah_line: float | None = None) -> dict:
        """
        Renvoie les probabilités de tous les marchés dérivées de la matrice de scores.
        ah_line : ligne de handicap côté HOME (ex -1.5 = home doit gagner par 2+).
                  away prend le handicap opposé.
        """
        lh, la = self.expected_goals(home, away, neutral)
        mat = self.score_matrix(home, away, neutral)
        idx = np.arange(MAX_GOALS + 1)
        diff = idx[:, None] - idx[None, :]  # home_goals - away_goals
        total = idx[:, None] + idx[None, :]

        p_home = float(mat[diff > 0].sum())
        p_draw = float(mat[diff == 0].sum())
        p_away = float(mat[diff < 0].sum())

        p_over = float(mat[total > ou_line].sum())
        p_under = float(mat[total < ou_line].sum())

        out = {
            "lambda_home": round(lh, 3),
            "lambda_away": round(la, 3),
            "prob_home": round(p_home, 4),
            "prob_draw": round(p_draw, 4),
            "prob_away": round(p_away, 4),
            "prob_over": round(p_over, 4),
            "prob_under": round(p_under, 4),
        }

        if ah_line is not None:
            out.update(self._ah_probs(mat, diff, ah_line))
        return out

    @staticmethod
    def _ah_probs(mat: np.ndarray, diff: np.ndarray, ah_line: float) -> dict:
        """
        Probabilités de gain net pour un handicap asiatique côté HOME = ah_line.
        Gère les lignes entières (push possible), demi (-0.5/-1.5...) et
        quart (-0.25/-0.75 = split en deux demi-lignes).
        Renvoie prob de gain "moyenne" (win=1, push=0.5, half-win=0.75...).
        """
        def settle(line: float) -> tuple[float, float]:
            """(prob_gain_net_home, prob_gain_net_away) pour une demi/entière ligne."""
            adj = diff + line  # marge home après handicap
            win = float(mat[adj > 0].sum())
            push = float(mat[adj == 0].sum())
            loss = float(mat[adj < 0].sum())
            # valeur espérée d'1u misée (push rembourse) → on renvoie "equity"
            home_equity = win + 0.5 * push
            away_equity = loss + 0.5 * push
            return home_equity, away_equity

        # Ligne quart : moyenne de deux demi-lignes adjacentes
        if abs((ah_line * 4) % 2) == 1:  # .25 ou .75
            lo, hi = ah_line - 0.25, ah_line + 0.25
            h1, a1 = settle(lo)
            h2, a2 = settle(hi)
            home_eq, away_eq = (h1 + h2) / 2, (a1 + a2) / 2
        else:
            home_eq, away_eq = settle(ah_line)

        return {
            "ah_line": ah_line,
            "prob_ah_home": round(home_eq, 4),
            "prob_ah_away": round(away_eq, 4),
        }

    # ---- persistence ----
    def to_dict(self) -> dict:
        return {
            "attack": self.attack, "defense": self.defense,
            "intercept": self.intercept,
            "home_adv": self.home_adv, "rho": self.rho,
            "mean_attack": self.mean_attack, "mean_defense": self.mean_defense,
            "trained_through": self.trained_through,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "WCGoalsModel":
        return cls(**d)


# ──────────────────────────────────────────────────────────
# Entraînement (maximum de vraisemblance pondéré)
# ──────────────────────────────────────────────────────────

def fit_dixon_coles(
    df: pd.DataFrame,
    half_life_days: int = DEFAULT_HALFLIFE_DAYS,
    min_matches: int = 15,
    ref_date: pd.Timestamp | None = None,
) -> WCGoalsModel:
    """
    Ajuste attack/defense/home_adv/rho par MLE pondéré.

    df : matchs avec colonnes date, home_team, away_team, home_score, away_score,
         neutral, is_friendly.
    min_matches : équipes avec moins de matchs sont regroupées (params partagés via
                  fallback moyenne) pour éviter l'overfit sur micro-nations.
    """
    df = df.dropna(subset=["home_team", "away_team", "home_score", "away_score", "date"]).copy()
    df["home_score"] = df["home_score"].astype(int).clip(0, MAX_GOALS)
    df["away_score"] = df["away_score"].astype(int).clip(0, MAX_GOALS)
    ref = ref_date or pd.Timestamp(df["date"].max())

    # Équipes éligibles (assez de matchs)
    counts = pd.concat([df["home_team"], df["away_team"]]).value_counts()
    teams = sorted(counts[counts >= min_matches].index.tolist())
    tset = set(teams)
    df = df[df["home_team"].isin(tset) & df["away_team"].isin(tset)].reset_index(drop=True)
    tidx = {t: i for i, t in enumerate(teams)}
    n = len(teams)

    h = df["home_team"].map(tidx).to_numpy()
    a = df["away_team"].map(tidx).to_numpy()
    x = df["home_score"].to_numpy()
    y = df["away_score"].to_numpy()
    is_home = (~df["neutral"].astype(bool)).to_numpy().astype(float)

    # Poids = décroissance temporelle × poids compétition
    age_days = (ref - pd.to_datetime(df["date"])).dt.days.clip(lower=0).to_numpy()
    w_time = 0.5 ** (age_days / half_life_days)
    w_comp = np.where(df["is_friendly"].astype(bool).to_numpy(), FRIENDLY_WEIGHT, 1.0)
    w = w_time * w_comp

    # Paramètres : [attack(n), defense(n), intercept μ, home_adv, rho]
    # μ = taux de buts de base (log). Identifiabilité : on centre attack ET defense
    # à moyenne 0 après optim, en repliant les moyennes dans μ (λ préservés).
    def unpack(p):
        atk = p[:n]
        dfn = p[n:2 * n]
        mu = p[2 * n]
        ha = p[2 * n + 1]
        rho = p[2 * n + 2]
        return atk, dfn, mu, ha, rho

    def neg_ll(p):
        atk, dfn, mu, ha, rho = unpack(p)
        log_lh = mu + atk[h] - dfn[a] + ha * is_home
        log_la = mu + atk[a] - dfn[h]
        lh = np.exp(np.clip(log_lh, -3, 3))
        la = np.exp(np.clip(log_la, -3, 3))
        # log Poisson sans le terme factoriel (constant en p)
        ll = x * log_lh - lh + y * log_la - la
        # correction Dixon-Coles
        tau = _tau(x, y, lh, la, rho)
        tau = np.clip(tau, 1e-6, None)
        ll = ll + np.log(tau)
        return -np.sum(w * ll)

    # Init : attack/defense à 0, μ = log(1.3) ≈ 0.26, home_adv 0.25, rho -0.05
    p0 = np.concatenate([np.zeros(n), np.zeros(n), [np.log(1.3)], [0.25], [-0.05]])
    bounds = [(-2.0, 2.0)] * (2 * n) + [(-1.0, 1.5), (-0.5, 1.0), (-0.2, 0.2)]

    res = minimize(neg_ll, p0, method="L-BFGS-B", bounds=bounds,
                   options={"maxiter": 200, "maxfun": 100000})

    atk, dfn, mu, ha, rho = unpack(res.x)
    # Centrage : moyenne(attack)=moyenne(defense)=0, μ absorbe le niveau (λ inchangés).
    a_bar, d_bar = atk.mean(), dfn.mean()
    mu = float(mu + a_bar - d_bar)
    atk = atk - a_bar
    dfn = dfn - d_bar

    model = WCGoalsModel(
        attack={t: float(atk[i]) for t, i in tidx.items()},
        defense={t: float(dfn[i]) for t, i in tidx.items()},
        intercept=mu,
        home_adv=float(ha),
        rho=float(rho),
        mean_attack=0.0,
        mean_defense=0.0,
        trained_through=str(ref.date()),
    )
    return model
