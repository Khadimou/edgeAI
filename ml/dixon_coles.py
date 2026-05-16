"""
Dixon-Coles bivariate Poisson model pour la prédiction des scores de foot.

Référence : Dixon & Coles (1997) "Modelling Association Football Scores and
Inefficiencies in the Football Betting Market".

Modèle :
    Home goals X ~ Poisson(λ_home)
    Away goals Y ~ Poisson(λ_away)
    λ_home = exp(α_home + β_away + γ)     # γ = home advantage
    λ_away = exp(α_away + β_home)

    Joint probability avec correction τ pour les scores faibles :
    P(X=x, Y=y) = τ(x,y; λ_home, λ_away, ρ) × Poisson(x; λ_home) × Poisson(y; λ_away)

    où τ corrige les 4 scores faibles (0-0, 1-0, 0-1, 1-1) pour mieux capturer
    la correlation négative observée dans les matchs serrés.

Fit : Maximum Likelihood avec poids temporels exponentiels (matchs récents
comptent plus). Optimization scipy.optimize.minimize.

Predict : sample la matrice de scores P(x,y) pour x,y in 0..MAX_GOALS, puis
agrège pour avoir P(H/D/A), P(Over/Under), P(AH).

Usage :
    dc = DixonColes()
    dc.fit(matches_df, decay_half_life=180)
    proba = dc.predict("Bayern Munich", "Köln")  # → {"home": 0.75, "draw": 0.18, "away": 0.07}
    proba_score = dc.predict_score("Bayern Munich", "Köln")  # → matrix 7×7
    proba_ou = dc.predict_ou("Bayern Munich", "Köln", line=2.5)  # → {"over": 0.62, "under": 0.38}
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.special import gammaln
from scipy.stats import poisson

MAX_GOALS = 8  # tronque la matrice de scores à 8×8 (>99.9% de la masse)


def _tau(x: int, y: int, lambda_h: float, lambda_a: float, rho: float) -> float:
    """Correction τ de Dixon-Coles pour les scores 0-0, 1-0, 0-1, 1-1.

    Toutes les autres combinaisons → τ = 1 (pas de correction).
    """
    if x == 0 and y == 0:
        return 1.0 - lambda_h * lambda_a * rho
    if x == 0 and y == 1:
        return 1.0 + lambda_h * rho
    if x == 1 and y == 0:
        return 1.0 + lambda_a * rho
    if x == 1 and y == 1:
        return 1.0 - rho
    return 1.0


def _log_likelihood_match(x: int, y: int, lambda_h: float, lambda_a: float,
                          rho: float) -> float:
    """Log-likelihood d'un match (x, y) sous le modèle DC."""
    # Poisson log-likelihoods
    ll = x * np.log(lambda_h) - lambda_h - sum(np.log(np.arange(1, x + 1) or [1]))
    ll += y * np.log(lambda_a) - lambda_a - sum(np.log(np.arange(1, y + 1) or [1]))
    # Correction τ
    tau_val = _tau(x, y, lambda_h, lambda_a, rho)
    if tau_val <= 0:
        return -1e10  # invalid
    ll += np.log(tau_val)
    return ll


class DixonColes:
    """
    Modèle Dixon-Coles fitté sur un DataFrame de matchs.

    Attributs après fit :
    - attack : {team: α}
    - defense : {team: β}
    - home_adv : γ (scalar)
    - rho : ρ (scalar, correction draws)
    """

    def __init__(self):
        self.attack: dict[str, float] = {}
        self.defense: dict[str, float] = {}
        self.home_adv: float = 0.25  # ~0.4 goals
        self.rho: float = -0.05
        self.teams: list[str] = []
        self._fitted: bool = False

    # ──────────────────────────────────────────────────────────
    # Fit
    # ──────────────────────────────────────────────────────────

    def fit(self, df: pd.DataFrame, decay_half_life: float | None = 180.0,
            verbose: bool = False) -> "DixonColes":
        """
        Fit DC par MLE sur les matchs du DataFrame.

        Args :
            df : columns required : home_team, away_team, home_score, away_score,
                                    match_date (pd.Timestamp)
            decay_half_life : demi-vie en jours pour les poids temporels.
                              None = pas de pondération.
            verbose : print loss à chaque itération

        Retourne self (méthode chaînable).
        """
        df = df.dropna(subset=["home_team", "away_team", "home_score", "away_score",
                               "match_date"]).copy()
        df["home_score"] = df["home_score"].astype(int)
        df["away_score"] = df["away_score"].astype(int)

        # Liste des équipes uniques (ordre alphabétique, conservé pour init params)
        teams = sorted(set(df["home_team"]) | set(df["away_team"]))
        self.teams = teams
        n_teams = len(teams)
        team_idx = {t: i for i, t in enumerate(teams)}

        # Poids temporels : exp(-ln(2) * Δt / half_life)
        if decay_half_life:
            max_date = df["match_date"].max()
            df["days_ago"] = (max_date - df["match_date"]).dt.days
            df["weight"] = np.exp(-np.log(2) * df["days_ago"] / decay_half_life)
        else:
            df["weight"] = 1.0

        # Cast en arrays NumPy pour speed
        home_ids = df["home_team"].map(team_idx).values.astype(int)
        away_ids = df["away_team"].map(team_idx).values.astype(int)
        x = df["home_score"].values.astype(int)
        y = df["away_score"].values.astype(int)
        weights = df["weight"].values.astype(float)

        # Précompute log(x!) et log(y!) une fois pour tous (constante de la log-pmf Poisson)
        log_x_fact = gammaln(x + 1)
        log_y_fact = gammaln(y + 1)

        # Smart init : démarre près de l'optimum pour aider la convergence.
        # α_i = log(avg goals scored by team i / overall avg)  → attack rating
        # β_i = -log(avg goals conceded / overall avg)        → defense rating (inversé)
        # γ = log(avg home goals / avg away goals)             → home advantage
        # ρ = -0.05 (valeur littérature DC pour football)
        # On parameterize : params = [α_1..α_{N-1}, β_1..β_{N-1}, γ, ρ]
        # Contrainte sum(α)=0 ⇒ α_N = -Σα_{1..N-1}, idem β.
        avg_goals_overall = (df["home_score"].sum() + df["away_score"].sum()) / (2 * len(df))
        avg_home_goals = df["home_score"].mean()
        avg_away_goals = df["away_score"].mean()
        gamma_init = float(np.log(max(avg_home_goals, 0.1) / max(avg_away_goals, 0.1)))

        # Compute per-team attack/defense from empirical goals
        # Aggregation : pour chaque team, somme des goals scored (home + away) et conceded
        scored = np.zeros(n_teams)
        conceded = np.zeros(n_teams)
        games = np.zeros(n_teams)
        for i in range(len(df)):
            h, a = home_ids[i], away_ids[i]
            scored[h] += x[i]
            scored[a] += y[i]
            conceded[h] += y[i]
            conceded[a] += x[i]
            games[h] += 1
            games[a] += 1
        games = np.maximum(games, 1)
        avg_scored = scored / games
        avg_conceded = conceded / games
        # Center on overall mean to respect sum=0 constraint approximatively
        alpha_init = np.log(np.maximum(avg_scored, 0.1) / max(avg_goals_overall, 0.1))
        alpha_init -= alpha_init.mean()  # center
        beta_init = -np.log(np.maximum(avg_conceded, 0.1) / max(avg_goals_overall, 0.1))
        beta_init -= beta_init.mean()

        x0 = np.concatenate([
            alpha_init[:-1],     # α_1..α_{N-1} (α_N déduit par contrainte)
            beta_init[:-1],      # β_1..β_{N-1} (β_N déduit)
            [gamma_init, -0.05],  # γ, ρ
        ])

        if verbose:
            print(f"  Smart init: γ={gamma_init:.3f}, "
                  f"α range [{alpha_init.min():.2f}, {alpha_init.max():.2f}]")

        def unpack(params):
            alpha = np.zeros(n_teams)
            beta = np.zeros(n_teams)
            alpha[:n_teams - 1] = params[:n_teams - 1]
            alpha[n_teams - 1] = -alpha[:n_teams - 1].sum()  # sum = 0 constraint
            beta[:n_teams - 1] = params[n_teams - 1:2 * (n_teams - 1)]
            beta[n_teams - 1] = -beta[:n_teams - 1].sum()
            gamma = params[-2]
            rho = params[-1]
            return alpha, beta, gamma, rho

        def neg_log_lik(params):
            alpha, beta, gamma, rho = unpack(params)
            # Constrain rho dans [-0.5, 0.5] pour stabilité
            if rho > 0.5 or rho < -0.5:
                return 1e10
            lambda_h = np.exp(alpha[home_ids] + beta[away_ids] + gamma)
            lambda_a = np.exp(alpha[away_ids] + beta[home_ids])
            # Per-match log-likelihood (FULLY vectorized)
            # log P(X=x; λ) = x*log(λ) - λ - log(x!)
            log_p_h = x * np.log(lambda_h) - lambda_h - log_x_fact
            log_p_a = y * np.log(lambda_a) - lambda_a - log_y_fact
            # τ correction (vectorized via masks)
            tau = np.ones(len(x), dtype=float)
            mask_00 = (x == 0) & (y == 0)
            mask_01 = (x == 0) & (y == 1)
            mask_10 = (x == 1) & (y == 0)
            mask_11 = (x == 1) & (y == 1)
            tau[mask_00] = 1.0 - lambda_h[mask_00] * lambda_a[mask_00] * rho
            tau[mask_01] = 1.0 + lambda_h[mask_01] * rho
            tau[mask_10] = 1.0 + lambda_a[mask_10] * rho
            tau[mask_11] = 1.0 - rho
            # τ doit être > 0 (sinon log explose)
            if (tau <= 0).any():
                return 1e10
            log_tau = np.log(tau)
            total = (log_p_h + log_p_a + log_tau) * weights
            return -total.sum()

        if verbose:
            print(f"Fitting DC on {len(df)} matches, {n_teams} teams...")

        # maxiter 2000 (au lieu de 200) pour assurer la convergence sur de gros
        # datasets multi-ligues (167 teams × 21k matchs = 334 params à fitter)
        result = minimize(neg_log_lik, x0, method="L-BFGS-B",
                          options={"maxiter": 2000, "maxfun": 20000})

        if not result.success:
            # Continue quand même si pas converged (souvent presque OK)
            if verbose:
                print(f"  Warning: optimization not converged. status={result.message}")

        alpha, beta, gamma, rho = unpack(result.x)
        self.attack = dict(zip(teams, alpha))
        self.defense = dict(zip(teams, beta))
        self.home_adv = float(gamma)
        self.rho = float(rho)
        self._fitted = True

        if verbose:
            print(f"  γ (home_adv) = {gamma:.3f}  (≈ {np.exp(gamma):.2f} goals factor)")
            print(f"  ρ (draws correction) = {rho:.3f}")
            top5_attack = sorted(self.attack.items(), key=lambda x: -x[1])[:5]
            print(f"  Top 5 attack ratings: {top5_attack}")

        return self

    # ──────────────────────────────────────────────────────────
    # Predict
    # ──────────────────────────────────────────────────────────

    def _lambdas(self, home_team: str, away_team: str) -> tuple[float, float]:
        """Renvoie (λ_home, λ_away) pour un match. Default 0 si équipe inconnue."""
        alpha_h = self.attack.get(home_team, 0.0)
        alpha_a = self.attack.get(away_team, 0.0)
        beta_h = self.defense.get(home_team, 0.0)
        beta_a = self.defense.get(away_team, 0.0)
        lambda_h = np.exp(alpha_h + beta_a + self.home_adv)
        lambda_a = np.exp(alpha_a + beta_h)
        return float(lambda_h), float(lambda_a)

    def predict_score_matrix(self, home_team: str, away_team: str,
                             max_goals: int = MAX_GOALS) -> np.ndarray:
        """
        Renvoie P(home_goals=x, away_goals=y) pour x, y in 0..max_goals.
        Matrice (max_goals+1) × (max_goals+1).
        """
        if not self._fitted:
            raise RuntimeError("DixonColes pas encore fit (appeler .fit() d'abord)")

        lambda_h, lambda_a = self._lambdas(home_team, away_team)
        pmf_h = poisson.pmf(np.arange(max_goals + 1), lambda_h)
        pmf_a = poisson.pmf(np.arange(max_goals + 1), lambda_a)
        # Matrice base : outer product
        score_mat = np.outer(pmf_h, pmf_a)
        # Apply τ corrections aux 4 cases faibles
        rho = self.rho
        score_mat[0, 0] *= 1.0 - lambda_h * lambda_a * rho
        score_mat[0, 1] *= 1.0 + lambda_h * rho
        score_mat[1, 0] *= 1.0 + lambda_a * rho
        score_mat[1, 1] *= 1.0 - rho
        # Renormalize (la troncature à max_goals + correction τ casse la somme = 1)
        total = score_mat.sum()
        if total > 0:
            score_mat /= total
        return score_mat

    def predict(self, home_team: str, away_team: str) -> dict:
        """Renvoie probas 1X2 + métriques pratiques."""
        m = self.predict_score_matrix(home_team, away_team)
        # 1X2
        p_home = np.tril(m, -1).sum()  # diagonale inférieure : home_goals > away_goals
        p_draw = np.diag(m).sum()
        p_away = np.triu(m, 1).sum()
        # Lambdas
        lambda_h, lambda_a = self._lambdas(home_team, away_team)
        return {
            "prob_home": float(p_home),
            "prob_draw": float(p_draw),
            "prob_away": float(p_away),
            "expected_home_goals": lambda_h,
            "expected_away_goals": lambda_a,
            "expected_total_goals": lambda_h + lambda_a,
        }

    def predict_ou(self, home_team: str, away_team: str, line: float = 2.5) -> dict:
        """Renvoie P(over line) / P(under line)."""
        m = self.predict_score_matrix(home_team, away_team)
        max_goals = m.shape[0] - 1
        p_over = 0.0
        for x in range(max_goals + 1):
            for y in range(max_goals + 1):
                if x + y > line:
                    p_over += m[x, y]
        return {"prob_over": float(p_over), "prob_under": float(1.0 - p_over)}

    def predict_ah(self, home_team: str, away_team: str, line: float = 0.0) -> dict:
        """
        Renvoie P(home covers AH line) en mode SIMPLE (whole/half-line).
        line négatif si home favori (e.g. -0.5 = home gagne par 1+).
        Note : ne gère pas les quarter-lines (à splitter par caller).
        """
        m = self.predict_score_matrix(home_team, away_team)
        max_goals = m.shape[0] - 1
        p_home_covers = 0.0
        p_push = 0.0
        for x in range(max_goals + 1):
            for y in range(max_goals + 1):
                diff = (x - y) + line
                if diff > 0:
                    p_home_covers += m[x, y]
                elif abs(diff) < 1e-9:
                    p_push += m[x, y]
        # Pour les half-lines pas de push possible
        return {
            "prob_ah_home": float(p_home_covers),
            "prob_ah_away": float(1.0 - p_home_covers - p_push),
            "prob_push": float(p_push),
        }

    # ──────────────────────────────────────────────────────────
    # Save / Load
    # ──────────────────────────────────────────────────────────

    def save(self, path: Path) -> None:
        import joblib
        joblib.dump({
            "attack": self.attack, "defense": self.defense,
            "home_adv": self.home_adv, "rho": self.rho,
            "teams": self.teams, "_fitted": self._fitted,
        }, path)

    @classmethod
    def load(cls, path: Path) -> "DixonColes":
        import joblib
        data = joblib.load(path)
        dc = cls()
        dc.attack = data["attack"]
        dc.defense = data["defense"]
        dc.home_adv = data["home_adv"]
        dc.rho = data["rho"]
        dc.teams = data["teams"]
        dc._fitted = data["_fitted"]
        return dc


# ──────────────────────────────────────────────────────────
# Quick CLI : fit + backtest sur matches.csv local
# ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", type=str, default="data/raw/matches.csv")
    parser.add_argument("--half-life", type=float, default=180.0)
    parser.add_argument("--league", type=str, default=None,
                        help="Filtre par ligue (ex: 'Premier League'). Default: toutes.")
    parser.add_argument("--test-since", type=str, default="2024-01-01",
                        help="Date à partir de laquelle évaluer en out-of-sample.")
    args = parser.parse_args()

    df = pd.read_csv(args.csv, parse_dates=["match_date"])
    if args.league:
        df = df[df["league"] == args.league]
    df = df.dropna(subset=["home_score", "away_score"])
    df["home_score"] = df["home_score"].astype(int)
    df["away_score"] = df["away_score"].astype(int)

    train = df[df["match_date"] < pd.Timestamp(args.test_since)]
    test = df[df["match_date"] >= pd.Timestamp(args.test_since)]
    print(f"Train: {len(train)} matches, Test: {len(test)}")

    dc = DixonColes()
    dc.fit(train, decay_half_life=args.half_life, verbose=True)

    # Évaluation OOS sur test set
    from sklearn.metrics import log_loss, accuracy_score
    y_true = []
    y_pred_proba = []
    for _, row in test.iterrows():
        try:
            pred = dc.predict(row["home_team"], row["away_team"])
            y_pred_proba.append([pred["prob_home"], pred["prob_draw"], pred["prob_away"]])
            if row["home_score"] > row["away_score"]:
                y_true.append(0)
            elif row["home_score"] == row["away_score"]:
                y_true.append(1)
            else:
                y_true.append(2)
        except Exception as e:
            continue

    y_true = np.array(y_true)
    y_pred_proba = np.array(y_pred_proba)
    y_pred = y_pred_proba.argmax(axis=1)
    print(f"\nOOS Test ({len(y_true)} matches):")
    print(f"  log_loss = {log_loss(y_true, y_pred_proba):.4f}")
    print(f"  accuracy = {accuracy_score(y_true, y_pred):.4f}")
    print(f"  baseline (always home) = {(y_true == 0).mean():.4f}")
