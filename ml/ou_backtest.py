"""
Backtest Over/Under 2.5 buts avec cotes de clôture football-data.co.uk.

Pipeline :
1. Télécharge les cotes O/U 2.5 (colonnes B365>2.5 / B365<2.5)
2. Merge avec dataset features + ou_label
3. 5-fold TimeSeriesSplit binaire → OOF predictions
4. Simule Kelly binaire (Over / Under)
5. Publish dans Redis

Usage:
    python ou_backtest.py
    python ou_backtest.py --edge-threshold 0.05 --edge-max 0.20
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
# Réutilise les mappings et fetch du backtest 1X2
from backtest import (
    LEAGUE_FD_CO_UK,
    normalize_team,
    fetch_odds_csv,
)

DATA_DIR = Path(__file__).parent / "data"
ARTIFACTS_DIR = Path(__file__).parent / "artifacts" / "backtest"
ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)


def fetch_ou_odds() -> pd.DataFrame:
    """Récupère les cotes O/U 2.5 pour les saisons + ligues disponibles."""
    seasons = ["2324", "2425"]
    all_dfs = []
    for league_name, code in LEAGUE_FD_CO_UK.items():
        for season in seasons:
            print(f"  {league_name} {season}...", end=" ", flush=True)
            df = fetch_odds_csv(code, season)
            if df.empty:
                print("vide")
                continue
            # Cherche les cotes O/U 2.5 (préférence Pinnacle > Bet365 > Avg)
            over_col = under_col = None
            for prefix in ["P", "B365", "Avg"]:
                p_over = f"{prefix}>2.5"
                p_under = f"{prefix}<2.5"
                if p_over in df.columns and p_under in df.columns:
                    over_col = p_over
                    under_col = p_under
                    break
            if over_col is None:
                print("pas d'O/U")
                continue
            df["over_25_odds"] = pd.to_numeric(df[over_col], errors="coerce")
            df["under_25_odds"] = pd.to_numeric(df[under_col], errors="coerce")
            df = df.dropna(subset=["over_25_odds", "under_25_odds"])
            df["league"] = league_name
            print(f"{len(df)} matchs")
            all_dfs.append(df)
    if not all_dfs:
        return pd.DataFrame()
    return pd.concat(all_dfs, ignore_index=True)


def merge_with_features(features_df: pd.DataFrame, odds_df: pd.DataFrame) -> pd.DataFrame:
    odds = odds_df.copy()
    odds["home_team_n"] = odds["HomeTeam"].apply(normalize_team)
    odds["away_team_n"] = odds["AwayTeam"].apply(normalize_team)
    odds["date"] = pd.to_datetime(odds["Date"]).dt.date

    feat = features_df.copy()
    feat["match_date_dt"] = pd.to_datetime(feat["match_date"]).dt.date

    merged = feat.merge(
        odds[["date", "home_team_n", "away_team_n", "over_25_odds", "under_25_odds"]],
        left_on=["match_date_dt", "home_team", "away_team"],
        right_on=["date", "home_team_n", "away_team_n"],
        how="inner",
    )
    return merged


def tune_optuna_ou(X, y, n_trials=100):
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
            "objective": "binary:logistic", "eval_metric": "logloss",
            "random_state": 42, "n_jobs": -1,
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
            "objective": "binary:logistic", "eval_metric": "logloss",
            "random_state": 42, "n_jobs": -1}


def compute_oof_ou(X, y, params=None):
    from sklearn.model_selection import TimeSeriesSplit
    from sklearn.calibration import CalibratedClassifierCV
    from xgboost import XGBClassifier

    if params is None:
        params = {
            "n_estimators": 300, "max_depth": 5, "learning_rate": 0.05,
            "subsample": 0.8, "colsample_bytree": 0.8,
            "objective": "binary:logistic", "eval_metric": "logloss",
            "random_state": 42, "n_jobs": -1,
        }
    oof = np.zeros((len(y), 2))
    tscv = TimeSeriesSplit(n_splits=5)
    for fold, (train_idx, val_idx) in enumerate(tscv.split(X)):
        print(f"  Fold {fold+1}/5 — train={len(train_idx)} val={len(val_idx)}...", flush=True)
        clf = CalibratedClassifierCV(XGBClassifier(**params), method="sigmoid", cv=3)
        clf.fit(X[train_idx], y[train_idx])
        oof[val_idx] = clf.predict_proba(X[val_idx])
    return oof


def simulate(df, initial_bankroll, edge_threshold, edge_max, kelly_fraction):
    df = df.sort_values("match_date").reset_index(drop=True)
    bankroll = initial_bankroll
    peak = bankroll
    max_dd = 0.0
    bets = []
    MAX_STAKE = 0.05

    for _, row in df.iterrows():
        if bankroll <= 1:
            break
        candidates = []
        # 0 = Under, 1 = Over (matches our model output)
        for outcome_idx, (label, prob, odds) in enumerate([
            ("UNDER", row["prob_under"], row["under_25_odds"]),
            ("OVER", row["prob_over"], row["over_25_odds"]),
        ]):
            if not odds or odds <= 1.0:
                continue
            edge = prob * odds - 1
            if edge < edge_threshold or edge > edge_max:
                continue
            b = odds - 1
            q = 1 - prob
            f_star = (prob * b - q) / b
            if f_star <= 0:
                continue
            stake_frac = min(f_star * kelly_fraction, MAX_STAKE)
            stake = round(bankroll * stake_frac, 2)
            if stake < 1:
                continue
            candidates.append({
                "outcome_idx": outcome_idx, "outcome_label": label,
                "prob": prob, "odds": odds, "edge": edge, "stake": stake,
            })
        if not candidates:
            continue
        best = max(candidates, key=lambda x: x["edge"])
        actual = int(row["label"])  # 1 = Over, 0 = Under
        won = best["outcome_idx"] == actual
        profit = round(best["stake"] * (best["odds"] - 1), 2) if won else -best["stake"]
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
            "bet_on": best["outcome_label"],
            "odds": round(best["odds"], 2),
            "prob": round(best["prob"], 4),
            "edge": round(best["edge"], 4),
            "stake": best["stake"],
            "actual": "OVER" if actual == 1 else "UNDER",
            "won": won, "profit": profit, "bankroll": bankroll,
        })

    bets_df = pd.DataFrame(bets)
    if bets_df.empty:
        return bets_df, {"n_bets": 0}

    n_bets = len(bets_df)
    n_wins = int(bets_df["won"].sum())
    total_staked = float(bets_df["stake"].sum())
    total_pnl = float(bets_df["profit"].sum())
    roi = total_pnl / total_staked * 100 if total_staked > 0 else 0
    wins_pnl = bets_df.loc[bets_df["profit"] > 0, "profit"].sum()
    losses_pnl = abs(bets_df.loc[bets_df["profit"] < 0, "profit"].sum())
    profit_factor = float(wins_pnl / losses_pnl) if losses_pnl > 0 else 0

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
        "market": "OVER_UNDER_2_5",
        "initial_bankroll": initial_bankroll,
        "final_bankroll": round(bankroll, 2),
        "n_bets": n_bets, "n_wins": n_wins,
        "hit_rate": round(n_wins / n_bets, 4),
        "total_staked": round(total_staked, 2),
        "total_pnl": round(total_pnl, 2),
        "roi_percent": round(roi, 2),
        "yield_per_bet": round(total_pnl / n_bets, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "peak_bankroll": round(peak, 2),
        "avg_odds": round(float(bets_df["odds"].mean()), 2),
        "avg_edge_pct": round(float(bets_df["edge"].mean()) * 100, 2),
        "profit_factor": round(profit_factor, 2),
        "period_start": str(bets_df["date"].min()),
        "period_end": str(bets_df["date"].max()),
        "per_league": per_league,
        "params": {
            "edge_threshold": edge_threshold,
            "edge_max": edge_max,
            "kelly_fraction": kelly_fraction,
            "max_stake_fraction": MAX_STAKE,
            "only_best_per_match": True,
            "calibration": "sigmoid",
        },
    }
    return bets_df, summary


def publish_to_redis(summary, bets_df):
    try:
        import redis
        url = os.getenv("REDIS_URL", "redis://localhost:6379")
        r = redis.Redis.from_url(url, decode_responses=True)
        equity = []
        if not bets_df.empty:
            ec = bets_df[["date", "bankroll"]].copy()
            if len(ec) > 200:
                step = max(1, len(ec) // 200)
                ec = ec.iloc[::step]
            equity = ec.to_dict(orient="records")
        payload = {
            "summary": summary,
            "equity_curve": equity,
            "sample_bets": bets_df.head(50).to_dict(orient="records") if not bets_df.empty else [],
            "computed_at": datetime.now(timezone.utc).isoformat(),
        }
        r.set("backtest:ou:latest", json.dumps(payload, default=str))
        print(f"✓ Redis : backtest:ou:latest ({len(json.dumps(payload, default=str))} bytes)")
    except Exception as e:
        print(f"⚠ Redis échoué : {e}")


def run_backtest(initial_bankroll=100.0, edge_threshold=0.03, edge_max=0.20, kelly_fraction=0.25,
                 tune=False, n_trials=100):
    print("─" * 60)
    print("Backtest O/U 2.5 buts — value betting Kelly")
    print("─" * 60)

    features_path = DATA_DIR / "features" / "ou_dataset.csv"
    if not features_path.exists():
        print(f"ERREUR : {features_path}. Lancez ou_build_features.py d'abord.")
        sys.exit(1)

    features = pd.read_csv(features_path, parse_dates=["match_date"])
    print(f"\n✓ Dataset O/U : {len(features)} matchs")

    print(f"\n[1/4] Téléchargement cotes O/U football-data.co.uk...")
    odds = fetch_ou_odds()
    print(f"  → {len(odds)} matchs avec cotes O/U")
    if odds.empty:
        sys.exit(1)

    print("\n[2/4] Merge features ↔ cotes...")
    merged = merge_with_features(features, odds)
    print(f"  → {len(merged)}/{len(features)} matchs matchés ({100*len(merged)/len(features):.1f}%)")
    if len(merged) < 200:
        print("ERREUR : pas assez de matchs matchés")
        sys.exit(1)

    print("\n[3/4] OOF predictions (TimeSeriesSplit binaire)...")
    # OU utilise les features Phase 1 (52) — shots Phase 2 dégradent l'OU
    # (mean-reversion des goals, overfitting sur petit edge)
    feature_cols = MatchFeatures.feature_names_phase1()
    merged = merged.sort_values("match_date").reset_index(drop=True)
    X = merged[feature_cols].values.astype(np.float32)
    y = merged["label"].values.astype(int)  # 1 = Over

    best_params = None
    if tune:
        print(f"  Optuna tuning ({n_trials} trials)...")
        best_params = tune_optuna_ou(X, y, n_trials=n_trials)
        out = DATA_DIR.parent / "artifacts" / "models" / "best_params_ou.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(best_params, indent=2))
        print(f"  Best params saved to {out}")

    oof = compute_oof_ou(X, y, params=best_params)
    merged["prob_under"] = oof[:, 0]
    merged["prob_over"] = oof[:, 1]
    valid = oof.sum(axis=1) > 0
    merged = merged[valid].reset_index(drop=True)
    print(f"  → {len(merged)} matchs avec OOF prediction")

    print(f"\n[4/4] Simulation Kelly (bankroll {initial_bankroll}€, edge∈[{edge_threshold:.0%},{edge_max:.0%}], kelly={kelly_fraction})...")
    bets_df, summary = simulate(merged, initial_bankroll, edge_threshold, edge_max, kelly_fraction)

    print(f"\n{'─' * 60}")
    print("RÉSULTATS O/U 2.5")
    print(f"{'─' * 60}")
    if summary.get("n_bets", 0) == 0:
        print("Aucune value bet trouvée.")
        return
    print(f"  Période       : {summary['period_start']} → {summary['period_end']}")
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

    bets_df.to_csv(ARTIFACTS_DIR / "ou_bets.csv", index=False)
    (ARTIFACTS_DIR / "ou_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    publish_to_redis(summary, bets_df)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--bankroll", type=float, default=100.0)
    parser.add_argument("--edge-threshold", type=float, default=0.03)
    parser.add_argument("--edge-max", type=float, default=0.20)
    parser.add_argument("--kelly-fraction", type=float, default=0.25)
    parser.add_argument("--tune", action="store_true")
    parser.add_argument("--n-trials", type=int, default=100)
    args = parser.parse_args()
    run_backtest(args.bankroll, args.edge_threshold, args.edge_max, args.kelly_fraction,
                 tune=args.tune, n_trials=args.n_trials)
