"""
Inférence runtime pour le modèle Coupe du Monde.
Charge l'historique CSV + reconstruit ELO dict + génère prédictions.
"""
from pathlib import Path

import pandas as pd
import structlog

from .wc_features import (
    WCFeatures, compute_wc_features,
    init_elo_dict, update_elo,
)

log = structlog.get_logger()


class WCInference:
    """Holds historical state for WC predictions (lazy-loaded).

    1X2 vient du classifieur XGBoost (model_bundle) ; les marchés de buts
    (Over/Under, Asian Handicap) viennent du modèle Dixon-Coles `goals_model`
    (optionnel) qui produit une distribution de scores.
    """

    def __init__(self, model_bundle: dict, csv_path: Path, goals_model=None):
        self.model = model_bundle.get("model")
        self.goals_model = goals_model  # WCGoalsModel | None
        self.csv_path = csv_path
        self.df: pd.DataFrame | None = None
        self.elo: dict[str, float] = {}
        self._loaded = False

    def load(self) -> bool:
        """Lazy load CSV + rebuild ELO dict. Cache after first call."""
        if self._loaded:
            return True
        if not self.csv_path.exists():
            log.warning("wc_inference_no_csv", path=str(self.csv_path))
            return False
        try:
            df = pd.read_csv(self.csv_path, parse_dates=["date"])
            df["is_wc"] = df["is_wc"].astype(bool)
            df = df.dropna(subset=["date", "home_team", "away_team", "home_score", "away_score"])
            df = df.sort_values("date").reset_index(drop=True)

            # Build ELO dict incrementally (O(n))
            elo = init_elo_dict()
            for _, row in df.iterrows():
                update_elo(
                    elo, row["home_team"], row["away_team"],
                    int(row["home_score"]), int(row["away_score"]),
                    is_wc=bool(row["is_wc"]),
                )
            self.df = df
            self.elo = elo
            self._loaded = True
            log.info("wc_inference_loaded", n_matches=len(df), n_teams_elo=len(elo))
            return True
        except Exception as e:
            log.error("wc_inference_load_error", error=str(e))
            return False

    def predict(self, home: str, away: str, match_date) -> dict | None:
        """Génère prédiction H/D/A pour un match WC à venir."""
        if not self.load():
            return None
        if self.model is None:
            return None
        try:
            md = pd.Timestamp(match_date)
            if md.tzinfo is not None:
                md = md.tz_localize(None)
            feat = compute_wc_features(home, away, md, self.df, self.elo)
            X = feat.to_array().reshape(1, -1)
            proba = self.model.predict_proba(X)[0]
            return {
                "prob_home": round(float(proba[0]), 4),
                "prob_draw": round(float(proba[1]), 4),
                "prob_away": round(float(proba[2]), 4),
                "confidence": round(float(max(proba)), 4),
            }
        except Exception as e:
            log.error("wc_predict_error", home=home, away=away, error=str(e))
            return None

    def goals_markets(self, home: str, away: str, neutral: bool = True,
                      ah_line: float | None = None) -> dict:
        """
        Marchés dérivés du modèle de buts Dixon-Coles : O/U 2.5 + AH (au ah_line donné).
        Renvoie {} si pas de goals_model. N'inclut prob_ah_* que si ah_line fourni.
        """
        if self.goals_model is None:
            return {}
        try:
            mp = self.goals_model.market_probs(home, away, neutral=neutral, ah_line=ah_line)
            out = {
                "prob_over_25": mp["prob_over"],
                "prob_under_25": mp["prob_under"],
            }
            if ah_line is not None:
                out["prob_ah_home"] = mp["prob_ah_home"]
                out["prob_ah_away"] = mp["prob_ah_away"]
            return out
        except Exception as e:
            log.error("wc_goals_markets_error", home=home, away=away, error=str(e))
            return {}
