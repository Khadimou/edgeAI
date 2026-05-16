"""
Backtest historique de la stratégie value-betting.

1. Télécharge les cotes de clôture Bet365 sur football-data.co.uk (gratuit)
2. Merge avec notre dataset de features (matching fuzzy des noms d'équipes)
3. Walk-forward via TimeSeriesSplit : OOF predictions sans data leakage
4. Pour chaque match : si edge ≥ seuil, on simule la mise Kelly et l'outcome
5. Sort un summary (ROI, drawdown, hit rate) + equity curve + bets détaillés

Usage:
    python backtest.py
    python backtest.py --bankroll 1000 --kelly-fraction 0.25 --edge-threshold 0.03
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from pipeline.features import MatchFeatures

DATA_DIR = Path(__file__).parent / "data"
ARTIFACTS_DIR = Path(__file__).parent / "artifacts" / "backtest"
ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

# football-data.co.uk league codes
LEAGUE_FD_CO_UK = {
    "Premier League": "E0",
    "Bundesliga": "D1",
    "Serie A": "I1",
    "La Liga": "SP1",
    "Ligue 1": "F1",
}

# Mapping minimal : noms football-data.co.uk → noms football-data.org
TEAM_NAME_MAP = {
    # Premier League
    "Man City": "Manchester City FC",
    "Man United": "Manchester United FC",
    "Newcastle": "Newcastle United FC",
    "Tottenham": "Tottenham Hotspur FC",
    "Wolves": "Wolverhampton Wanderers FC",
    "Brighton": "Brighton & Hove Albion FC",
    "West Ham": "West Ham United FC",
    "Nott'm Forest": "Nottingham Forest FC",
    "Sheffield United": "Sheffield United FC",
    "Luton": "Luton Town FC",
    "Leicester": "Leicester City FC",
    "Ipswich": "Ipswich Town FC",
    "Bournemouth": "AFC Bournemouth",
    "Crystal Palace": "Crystal Palace FC",
    "Liverpool": "Liverpool FC",
    "Arsenal": "Arsenal FC",
    "Chelsea": "Chelsea FC",
    "Everton": "Everton FC",
    "Aston Villa": "Aston Villa FC",
    "Brentford": "Brentford FC",
    "Fulham": "Fulham FC",
    "Southampton": "Southampton FC",
    "Burnley": "Burnley FC",
    # Bundesliga
    "Bayern Munich": "FC Bayern München",
    "Dortmund": "Borussia Dortmund",
    "Leverkusen": "Bayer 04 Leverkusen",
    "RB Leipzig": "RB Leipzig",
    "Frankfurt": "Eintracht Frankfurt",
    "Stuttgart": "VfB Stuttgart",
    "Hoffenheim": "TSG 1899 Hoffenheim",
    "Mainz": "1. FSV Mainz 05",
    "Wolfsburg": "VfL Wolfsburg",
    "Freiburg": "Sport-Club Freiburg",
    "Augsburg": "FC Augsburg",
    "Union Berlin": "1. FC Union Berlin",
    "M'gladbach": "Borussia Mönchengladbach",
    "Werder Bremen": "SV Werder Bremen",
    "Bochum": "VfL Bochum 1848",
    "Heidenheim": "1. FC Heidenheim 1846",
    "Holstein Kiel": "Holstein Kiel",
    "St Pauli": "FC St. Pauli 1910",
    "Darmstadt": "SV Darmstadt 98",
    "FC Koln": "1. FC Köln",
    # Serie A
    "Inter": "FC Internazionale Milano",
    "Milan": "AC Milan",
    "Juventus": "Juventus FC",
    "Roma": "AS Roma",
    "Napoli": "SSC Napoli",
    "Lazio": "SS Lazio",
    "Atalanta": "Atalanta BC",
    "Fiorentina": "ACF Fiorentina",
    "Bologna": "Bologna FC 1909",
    "Torino": "Torino FC",
    "Udinese": "Udinese Calcio",
    "Monza": "AC Monza",
    "Genoa": "Genoa CFC",
    "Sassuolo": "US Sassuolo Calcio",
    "Empoli": "Empoli FC",
    "Lecce": "US Lecce",
    "Cagliari": "Cagliari Calcio",
    "Hellas Verona": "Hellas Verona FC",
    "Verona": "Hellas Verona FC",
    "Frosinone": "Frosinone Calcio",
    "Salernitana": "US Salernitana 1919",
    "Parma": "Parma Calcio 1913",
    "Como": "Como 1907",
    "Venezia": "Venezia FC",
    # La Liga
    "Real Madrid": "Real Madrid CF",
    "Barcelona": "FC Barcelona",
    "Ath Madrid": "Club Atlético de Madrid",
    "Ath Bilbao": "Athletic Club",
    "Sociedad": "Real Sociedad de Fútbol",
    "Sevilla": "Sevilla FC",
    "Valencia": "Valencia CF",
    "Villarreal": "Villarreal CF",
    "Getafe": "Getafe CF",
    "Betis": "Real Betis Balompié",
    "Girona": "Girona FC",
    "Mallorca": "RCD Mallorca",
    "Celta": "RC Celta de Vigo",
    "Osasuna": "CA Osasuna",
    "Las Palmas": "UD Las Palmas",
    "Almeria": "UD Almería",
    "Cadiz": "Cádiz CF",
    "Granada": "Granada CF",
    "Alaves": "Deportivo Alavés",
    "Vallecano": "Rayo Vallecano de Madrid",
    "Espanol": "RCD Espanyol de Barcelona",
    "Leganes": "CD Leganés",
    "Valladolid": "Real Valladolid CF",
    # Ligue 1
    "Paris SG": "Paris Saint-Germain FC",
    "Marseille": "Olympique de Marseille",
    "Lyon": "Olympique Lyonnais",
    "Monaco": "AS Monaco FC",
    "Nice": "OGC Nice",
    "Lille": "Lille OSC",
    "Rennes": "Stade Rennais FC 1901",
    "Lens": "RC Lens",
    "Strasbourg": "RC Strasbourg Alsace",
    "Reims": "Stade de Reims",
    "Nantes": "FC Nantes",
    "Toulouse": "Toulouse FC",
    "Montpellier": "Montpellier HSC",
    "Brest": "Stade Brestois 29",
    "Le Havre": "Le Havre AC",
    "Metz": "FC Metz",
    "Clermont": "Clermont Foot 63",
    "Lorient": "FC Lorient",
    "Auxerre": "AJ Auxerre",
    "St Etienne": "AS Saint-Étienne",
    "Angers": "Angers SCO",
    "Troyes": "ESTAC Troyes",
    "Ajaccio": "AC Ajaccio",
    "Sochaux": "FC Sochaux-Montbéliard",
}


def fetch_odds_csv(league_code: str, season_code: str) -> pd.DataFrame:
    """Télécharge un CSV football-data.co.uk. season_code = '2324' pour 2023/24."""
    url = f"https://www.football-data.co.uk/mmz4281/{season_code}/{league_code}.csv"
    try:
        r = httpx.get(url, timeout=20, follow_redirects=True)
        r.raise_for_status()
        df = pd.read_csv(StringIO(r.text))
        # Garde colonnes utiles : date, équipes, résultat + cotes Bet365 closing
        keep = ["Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG", "FTR"]
        odds_cols = []
        # Cotes 1X2
        for prefix in ["B365", "PS", "Avg"]:
            if all(f"{prefix}{x}" in df.columns for x in ["H", "D", "A"]):
                odds_cols += [f"{prefix}H", f"{prefix}D", f"{prefix}A"]
        # Cotes O/U 2.5
        for prefix in ["B365", "P", "Avg"]:
            col_o = f"{prefix}>2.5"
            col_u = f"{prefix}<2.5"
            if col_o in df.columns and col_u in df.columns:
                odds_cols += [col_o, col_u]
        df = df[keep + odds_cols].copy()
        df["Date"] = pd.to_datetime(df["Date"], dayfirst=True, errors="coerce")
        return df.dropna(subset=["Date", "HomeTeam", "AwayTeam"])
    except Exception as e:
        print(f"  ! Erreur fetch {league_code}/{season_code}: {e}")
        return pd.DataFrame()


def fetch_all_historical_odds() -> pd.DataFrame:
    """Récupère les cotes pour toutes les ligues × saisons disponibles."""
    seasons = ["2324", "2425"]  # 2023/24 + 2024/25
    all_dfs = []
    for league_name, code in LEAGUE_FD_CO_UK.items():
        for season in seasons:
            print(f"  Téléchargement {league_name} {season}...", end=" ", flush=True)
            df = fetch_odds_csv(code, season)
            if not df.empty:
                df["league"] = league_name
                df["season_code"] = season
                all_dfs.append(df)
                print(f"{len(df)} matchs")
            else:
                print("vide")
    if not all_dfs:
        return pd.DataFrame()
    return pd.concat(all_dfs, ignore_index=True)


def normalize_team(name: str) -> str:
    """Mappe le nom court football-data.co.uk vers le nom long football-data.org."""
    if pd.isna(name):
        return ""
    return TEAM_NAME_MAP.get(str(name).strip(), str(name).strip())


def merge_with_features(features_df: pd.DataFrame, odds_df: pd.DataFrame) -> pd.DataFrame:
    """Joint features_df (notre dataset) avec odds_df (football-data.co.uk) sur (teams, date proche)."""
    odds = odds_df.copy()
    odds["home_team_n"] = odds["HomeTeam"].apply(normalize_team)
    odds["away_team_n"] = odds["AwayTeam"].apply(normalize_team)
    odds["date"] = pd.to_datetime(odds["Date"]).dt.date

    feat = features_df.copy()
    feat["match_date_dt"] = pd.to_datetime(feat["match_date"]).dt.date

    # Choix de la cote de clôture par ordre de préférence : Pinnacle > Bet365 > Moyenne
    def best_odds(row, side):
        for col_prefix in ["PS", "B365", "Avg"]:
            c = f"{col_prefix}{side}"
            if c in row.index and pd.notna(row[c]):
                return float(row[c])
        return None

    odds["home_odds"] = odds.apply(lambda r: best_odds(r, "H"), axis=1)
    odds["draw_odds"] = odds.apply(lambda r: best_odds(r, "D"), axis=1)
    odds["away_odds"] = odds.apply(lambda r: best_odds(r, "A"), axis=1)
    odds = odds.dropna(subset=["home_odds", "draw_odds", "away_odds"])

    merged = feat.merge(
        odds[["date", "home_team_n", "away_team_n", "home_odds", "draw_odds", "away_odds"]],
        left_on=["match_date_dt", "home_team", "away_team"],
        right_on=["date", "home_team_n", "away_team_n"],
        how="inner",
    )
    return merged


def tune_optuna(X: np.ndarray, y: np.ndarray, n_trials: int = 100) -> dict:
    """Optuna tuning sur log-loss multi-class, TimeSeriesSplit 5 folds."""
    import optuna
    from sklearn.model_selection import TimeSeriesSplit
    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.metrics import log_loss
    from xgboost import XGBClassifier
    optuna.logging.set_verbosity(optuna.logging.WARNING)

    def objective(trial):
        p = {
            "n_estimators": trial.suggest_int("n_estimators", 200, 700),
            "max_depth": trial.suggest_int("max_depth", 3, 8),
            "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.15, log=True),
            "subsample": trial.suggest_float("subsample", 0.6, 1.0),
            "colsample_bytree": trial.suggest_float("colsample_bytree", 0.6, 1.0),
            "min_child_weight": trial.suggest_int("min_child_weight", 1, 10),
            "gamma": trial.suggest_float("gamma", 0.0, 0.5),
            "reg_alpha": trial.suggest_float("reg_alpha", 0.0, 1.0),
            "reg_lambda": trial.suggest_float("reg_lambda", 0.5, 2.0),
            "objective": "multi:softprob", "num_class": 3,
            "eval_metric": "mlogloss", "random_state": 42, "n_jobs": -1,
        }
        tscv = TimeSeriesSplit(n_splits=5)
        losses = []
        for ti, vi in tscv.split(X):
            clf = CalibratedClassifierCV(XGBClassifier(**p), method="sigmoid", cv=3)
            clf.fit(X[ti], y[ti])
            losses.append(log_loss(y[vi], clf.predict_proba(X[vi])))
        return float(np.mean(losses))

    study = optuna.create_study(direction="minimize")
    study.optimize(objective, n_trials=n_trials, show_progress_bar=False)
    print(f"  Best CV log-loss : {study.best_value:.4f}")
    return {**study.best_params,
            "objective": "multi:softprob", "num_class": 3,
            "eval_metric": "mlogloss", "random_state": 42, "n_jobs": -1}


def compute_oof_predictions(X: np.ndarray, y: np.ndarray, params: dict | None = None) -> np.ndarray:
    """5-fold TimeSeriesSplit : retourne les probas OOF (les premiers folds restent à 0).
    Calibration sigmoid (Platt) au lieu d'isotonic — plus stable sur les queues de distribution.
    """
    from sklearn.model_selection import TimeSeriesSplit
    from sklearn.calibration import CalibratedClassifierCV
    from xgboost import XGBClassifier

    if params is None:
        params = {
            "n_estimators": 300, "max_depth": 5, "learning_rate": 0.05,
            "subsample": 0.8, "colsample_bytree": 0.8,
            "objective": "multi:softprob", "num_class": 3,
            "eval_metric": "mlogloss", "random_state": 42, "n_jobs": -1,
        }
    oof = np.zeros((len(y), 3))
    tscv = TimeSeriesSplit(n_splits=5)
    for fold, (train_idx, val_idx) in enumerate(tscv.split(X)):
        print(f"  Fold {fold+1}/5 — train={len(train_idx)} val={len(val_idx)}...", flush=True)
        clf = CalibratedClassifierCV(XGBClassifier(**params), method="sigmoid", cv=3)
        clf.fit(X[train_idx], y[train_idx])
        oof[val_idx] = clf.predict_proba(X[val_idx])
    return oof


def simulate(
    df: pd.DataFrame,
    initial_bankroll: float = 100.0,
    edge_threshold: float = 0.03,
    edge_max: float = 0.20,  # filtre les hallucinations (modèle mal calibré)
    min_prob: float = 0.0,  # ne pas miser sur outsider de faible proba
    kelly_fraction: float = 0.25,
    max_stake_fraction: float = 0.05,
    only_best_per_match: bool = True,
) -> tuple[pd.DataFrame, dict]:
    """
    df doit contenir : match_date, league, home_team, away_team, home_odds, draw_odds,
                       away_odds, prob_home, prob_draw, prob_away, label.
    """
    df = df.sort_values("match_date").reset_index(drop=True)
    bankroll = initial_bankroll
    peak = bankroll
    max_dd = 0.0
    bets = []

    for _, row in df.iterrows():
        if bankroll <= 1:
            break

        candidates = []
        for outcome_idx, (label, prob, odds) in enumerate([
            ("HOME", row["prob_home"], row["home_odds"]),
            ("DRAW", row["prob_draw"], row["draw_odds"]),
            ("AWAY", row["prob_away"], row["away_odds"]),
        ]):
            if not odds or odds <= 1.0:
                continue
            edge = prob * odds - 1
            if edge < edge_threshold:
                continue
            # Skip les "value bets" trop énormes (modèle mal calibré)
            if edge > edge_max:
                continue
            # Skip les outsiders à très faible probabilité
            if prob < min_prob:
                continue
            # Kelly fraction
            b = odds - 1
            q = 1 - prob
            f_star = (prob * b - q) / b
            if f_star <= 0:
                continue
            stake_frac = min(f_star * kelly_fraction, max_stake_fraction)
            stake = round(bankroll * stake_frac, 2)
            if stake < 1:
                continue
            candidates.append({
                "outcome_idx": outcome_idx,
                "outcome_label": label,
                "prob": prob,
                "odds": odds,
                "edge": edge,
                "stake": stake,
            })

        if not candidates:
            continue

        if only_best_per_match:
            candidates = sorted(candidates, key=lambda x: -x["edge"])[:1]

        for c in candidates:
            actual = int(row["label"])  # 0=HOME, 1=DRAW, 2=AWAY
            won = c["outcome_idx"] == actual
            profit = round(c["stake"] * (c["odds"] - 1), 2) if won else -c["stake"]
            bankroll = round(bankroll + profit, 2)

            peak = max(peak, bankroll)
            dd = (peak - bankroll) / peak
            if dd > max_dd:
                max_dd = dd

            bets.append({
                "date": str(row["match_date"])[:10],
                "league": row.get("league", ""),
                "home_team": row["home_team"],
                "away_team": row["away_team"],
                "bet_on": c["outcome_label"],
                "odds": round(c["odds"], 2),
                "prob": round(c["prob"], 4),
                "edge": round(c["edge"], 4),
                "stake": c["stake"],
                "actual": ["HOME", "DRAW", "AWAY"][actual],
                "won": won,
                "profit": profit,
                "bankroll": bankroll,
            })

    bets_df = pd.DataFrame(bets)
    if bets_df.empty:
        return bets_df, {"n_bets": 0, "error": "Aucune value bet trouvée"}

    n_bets = len(bets_df)
    n_wins = int(bets_df["won"].sum())
    total_staked = float(bets_df["stake"].sum())
    total_pnl = float(bets_df["profit"].sum())
    roi = total_pnl / total_staked * 100 if total_staked > 0 else 0
    yield_per_bet = total_pnl / n_bets if n_bets > 0 else 0
    avg_odds = float(bets_df["odds"].mean())
    avg_edge = float(bets_df["edge"].mean())

    # Profit factor
    wins_pnl = bets_df.loc[bets_df["profit"] > 0, "profit"].sum()
    losses_pnl = abs(bets_df.loc[bets_df["profit"] < 0, "profit"].sum())
    profit_factor = float(wins_pnl / losses_pnl) if losses_pnl > 0 else 0

    # Par ligue
    per_league = {}
    for league, sub in bets_df.groupby("league"):
        s_stake = sub["stake"].sum()
        s_pnl = sub["profit"].sum()
        per_league[league] = {
            "n_bets": len(sub),
            "hit_rate": round(sub["won"].mean(), 4),
            "roi_percent": round(s_pnl / s_stake * 100, 2) if s_stake > 0 else 0,
            "total_pnl": round(s_pnl, 2),
        }

    summary = {
        "initial_bankroll": initial_bankroll,
        "final_bankroll": round(bankroll, 2),
        "n_bets": n_bets,
        "n_wins": n_wins,
        "hit_rate": round(n_wins / n_bets, 4) if n_bets > 0 else 0,
        "total_staked": round(total_staked, 2),
        "total_pnl": round(total_pnl, 2),
        "roi_percent": round(roi, 2),
        "yield_per_bet": round(yield_per_bet, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "peak_bankroll": round(peak, 2),
        "avg_odds": round(avg_odds, 2),
        "avg_edge_pct": round(avg_edge * 100, 2),
        "profit_factor": round(profit_factor, 2),
        "period_start": str(bets_df["date"].min()),
        "period_end": str(bets_df["date"].max()),
        "per_league": per_league,
        "params": {
            "edge_threshold": edge_threshold,
            "edge_max": edge_max,
            "min_prob": min_prob,
            "kelly_fraction": kelly_fraction,
            "max_stake_fraction": max_stake_fraction,
            "only_best_per_match": only_best_per_match,
            "calibration": "sigmoid",
        },
    }
    return bets_df, summary


def run_backtest(
    initial_bankroll: float = 100.0,
    edge_threshold: float = 0.03,
    edge_max: float = 0.20,
    min_prob: float = 0.0,
    kelly_fraction: float = 0.25,
    tune: bool = False,
    n_trials: int = 100,
):
    print("─" * 60)
    print("Backtest — value betting Kelly")
    print("─" * 60)

    features_path = DATA_DIR / "features" / "dataset.csv"
    if not features_path.exists():
        print(f"ERREUR : dataset features introuvable. Lancez build_features.py d'abord.")
        sys.exit(1)

    features = pd.read_csv(features_path)
    print(f"\n✓ Dataset features : {len(features)} matchs")

    print("\n[1/4] Téléchargement des cotes historiques (football-data.co.uk)...")
    odds = fetch_all_historical_odds()
    print(f"  → {len(odds)} matchs avec cotes")

    if odds.empty:
        print("ERREUR : aucune cote téléchargée.")
        sys.exit(1)

    print("\n[2/4] Mapping noms d'équipes + merge...")
    merged = merge_with_features(features, odds)
    print(f"  → {len(merged)}/{len(features)} matchs avec cotes matchées ({100*len(merged)/len(features):.1f}%)")

    if len(merged) < 200:
        print(f"  ⚠ Trop peu de matchs matchés. Ajustez TEAM_NAME_MAP.")
        # Affichons les noms non-matchés pour debug
        matched_teams = set(merged["home_team"]) | set(merged["away_team"])
        fd_teams = set(odds["HomeTeam"].apply(normalize_team)) | set(odds["AwayTeam"].apply(normalize_team))
        unmatched_fd = sorted(fd_teams - matched_teams)[:20]
        print(f"  Noms football-data.co.uk non mappés (top 20): {unmatched_fd}")

    print("\n[3/4] OOF predictions (TimeSeriesSplit 5 folds)...")
    # 1X2 utilise les features Phase 1 (52) — backtest a montré que les shots
    # Phase 2 dégradent la perf en mode 3-class directionnel
    feature_cols = MatchFeatures.feature_names_phase1()
    merged = merged.sort_values("match_date").reset_index(drop=True)
    X = merged[feature_cols].values.astype(np.float32)
    y = merged["label"].values.astype(int)

    best_params = None
    if tune:
        print(f"  Optuna tuning ({n_trials} trials)...")
        best_params = tune_optuna(X, y, n_trials=n_trials)
        # Persist for downstream reuse (auto-trainer in scheduler)
        out = DATA_DIR.parent / "artifacts" / "models" / "best_params_1x2.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(best_params, indent=2))
        print(f"  Best params saved to {out}")

    oof = compute_oof_predictions(X, y, params=best_params)
    merged["prob_home"] = oof[:, 0]
    merged["prob_draw"] = oof[:, 1]
    merged["prob_away"] = oof[:, 2]
    # Exclure les premières lignes sans OOF prediction (oof.sum=0)
    valid = oof.sum(axis=1) > 0
    merged = merged[valid].reset_index(drop=True)
    print(f"  → {len(merged)} matchs avec prédiction OOF")

    print(f"\n[4/4] Simulation Kelly (bankroll initial: {initial_bankroll}€, edge∈[{edge_threshold:.0%},{edge_max:.0%}], kelly={kelly_fraction})...")
    bets_df, summary = simulate(
        merged,
        initial_bankroll=initial_bankroll,
        edge_threshold=edge_threshold,
        edge_max=edge_max,
        min_prob=min_prob,
        kelly_fraction=kelly_fraction,
    )

    print(f"\n{'─' * 60}")
    print("RÉSULTATS")
    print(f"{'─' * 60}")
    print(f"  Période       : {summary.get('period_start','?')} → {summary.get('period_end','?')}")
    print(f"  Paris placés  : {summary['n_bets']}")
    print(f"  Hit rate      : {summary['hit_rate']*100:.1f}%")
    print(f"  Total misé    : {summary['total_staked']:.0f}€")
    print(f"  P&L total     : {summary['total_pnl']:+.0f}€")
    print(f"  ROI           : {summary['roi_percent']:+.1f}%")
    print(f"  Yield / pari  : {summary['yield_per_bet']:+.2f}€")
    print(f"  Bankroll fin  : {summary['final_bankroll']:.0f}€ (pic: {summary['peak_bankroll']:.0f}€)")
    print(f"  Max drawdown  : {summary['max_drawdown_pct']:.1f}%")
    print(f"  Profit factor : {summary['profit_factor']:.2f}")
    print(f"  Cote moy.     : {summary['avg_odds']}")
    print(f"  Edge moy.     : {summary['avg_edge_pct']:.1f}%")
    print(f"\nPar ligue :")
    for league, stats in summary["per_league"].items():
        print(f"  {league:18} | {stats['n_bets']:4d} paris | hit {stats['hit_rate']*100:5.1f}% | ROI {stats['roi_percent']:+6.1f}% | P&L {stats['total_pnl']:+7.0f}€")

    # Sauvegardes
    bets_path = ARTIFACTS_DIR / "bets.csv"
    summary_path = ARTIFACTS_DIR / "summary.json"
    equity_path = ARTIFACTS_DIR / "equity.csv"
    bets_df.to_csv(bets_path, index=False)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    if not bets_df.empty:
        equity_df = bets_df[["date", "bankroll"]].copy()
        equity_df.to_csv(equity_path, index=False)

    print(f"\n✓ Bets         : {bets_path}")
    print(f"✓ Summary JSON : {summary_path}")
    if not bets_df.empty:
        print(f"✓ Equity curve : {equity_path}")

    # Push vers Redis pour que le backend / frontend puissent lire
    _publish_to_redis(summary, bets_df)


def _publish_to_redis(summary: dict, bets_df: pd.DataFrame):
    try:
        import redis
        url = os.getenv("REDIS_URL", "redis://localhost:6379")
        r = redis.Redis.from_url(url, decode_responses=True)

        # Equity curve : sample max 200 points pour ne pas exploser le payload
        equity = []
        if not bets_df.empty:
            ec = bets_df[["date", "bankroll"]].copy()
            if len(ec) > 200:
                step = max(1, len(ec) // 200)
                ec = ec.iloc[::step]
            equity = ec.to_dict(orient="records")

        # Top 50 paris pour la table frontend
        sample_bets = bets_df.head(50).to_dict(orient="records") if not bets_df.empty else []

        payload = {
            "summary": summary,
            "equity_curve": equity,
            "sample_bets": sample_bets,
            "computed_at": datetime.now(timezone.utc).isoformat(),
        }
        r.set("backtest:latest", json.dumps(payload, default=str))
        print(f"✓ Publié dans Redis : backtest:latest ({len(json.dumps(payload, default=str))} bytes)")
    except Exception as e:
        print(f"⚠ Publication Redis échouée : {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--bankroll", type=float, default=100.0)
    parser.add_argument("--edge-threshold", type=float, default=0.03)
    parser.add_argument("--edge-max", type=float, default=0.20,
                        help="Skip les value bets au-dessus de ce seuil (= hallucinations)")
    parser.add_argument("--min-prob", type=float, default=0.0,
                        help="Skip si la prob du modèle est < ce seuil (filtrer outsiders)")
    parser.add_argument("--kelly-fraction", type=float, default=0.25)
    parser.add_argument("--tune", action="store_true", help="Lance Optuna tuning avant OOF predictions")
    parser.add_argument("--n-trials", type=int, default=100)
    args = parser.parse_args()
    run_backtest(args.bankroll, args.edge_threshold, args.edge_max, args.min_prob,
                 args.kelly_fraction, tune=args.tune, n_trials=args.n_trials)
