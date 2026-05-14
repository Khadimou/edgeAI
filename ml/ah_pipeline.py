"""
Pipeline Asian Handicap foot : fetch + features + train + backtest + deploy.

Marché AH : on prédit si l'équipe à domicile "couvre" le spread (handicap).
- Ligne -1.0 = home favori de 1 but. Pour gagner, home doit gagner par 2+ buts.
- Ligne 0.0 = pas de handicap. Home gagne si home_score > away_score.
- Ligne +0.5 = home outsider. Home gagne s'il ne perd pas.

Half-lines (.5) : pas de push possible (gain ou perte).
Whole-lines (.0) : push possible (remboursement) si écart = handicap.
Quarter-lines (.25, .75) : bet splité en deux (half-bet à .0 + half-bet à .5).

Source cotes : football-data.co.uk (AHCh = closing line, PCAHH/A = Pinnacle closing).
Target binaire : home covers ? (sur la closing line, en gérant les pushes).

Usage:
    python ah_pipeline.py
    python ah_pipeline.py --tune --n-trials 20 --edge-threshold 0.08 --edge-max 0.20
"""
import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

import httpx
import numpy as np
import pandas as pd
import joblib
from sklearn.calibration import CalibratedClassifierCV
from sklearn.metrics import log_loss, accuracy_score, brier_score_loss
from sklearn.model_selection import TimeSeriesSplit
from xgboost import XGBClassifier

sys.path.insert(0, str(Path(__file__).parent))
from pipeline.features import MatchFeatures, compute_features_from_history
from pipeline.features import compute_standings_from_history
from backtest import LEAGUE_FD_CO_UK, TEAM_NAME_MAP

DATA_DIR = Path(__file__).parent / "data"
ARTIFACTS_DIR_MODELS = Path(__file__).parent / "artifacts" / "models"
ARTIFACTS_DIR_BT = Path(__file__).parent / "artifacts" / "backtest"
ARTIFACTS_DIR_MODELS.mkdir(parents=True, exist_ok=True)
ARTIFACTS_DIR_BT.mkdir(parents=True, exist_ok=True)

SEASONS = ["1920", "2021", "2122", "2223", "2324", "2425"]


# ──────────────── Fetch AH data ────────────────

def fetch_season_ah(league_code: str, league_name: str, season: str) -> pd.DataFrame:
    url = f"https://www.football-data.co.uk/mmz4281/{season}/{league_code}.csv"
    try:
        r = httpx.get(url, timeout=20, follow_redirects=True)
        r.raise_for_status()
        df = pd.read_csv(StringIO(r.text))
    except Exception as e:
        print(f"  ! {league_name} {season}: {e}")
        return pd.DataFrame()

    if df.empty or "AHh" not in df.columns:
        return pd.DataFrame()

    df["Date"] = pd.to_datetime(df["Date"], dayfirst=True, errors="coerce")
    df = df.dropna(subset=["Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG", "AHh"])

    def pick_ah_odds(row, side):
        for prefix in ["PCAH", "PAH", "B365CAH", "B365AH", "AvgCAH", "AvgAH"]:
            col = f"{prefix}{side}"
            if col in row.index and pd.notna(row[col]):
                try:
                    v = float(row[col])
                    if v > 1:
                        return v
                except (ValueError, TypeError):
                    pass
        return None

    def pick_line(row):
        for col in ["AHCh", "AHh"]:
            if col in row.index and pd.notna(row[col]):
                try:
                    return float(row[col])
                except (ValueError, TypeError):
                    pass
        return None

    rows = []
    for _, r_ in df.iterrows():
        home_long = TEAM_NAME_MAP.get(str(r_["HomeTeam"]).strip(), str(r_["HomeTeam"]).strip())
        away_long = TEAM_NAME_MAP.get(str(r_["AwayTeam"]).strip(), str(r_["AwayTeam"]).strip())
        line = pick_line(r_)
        if line is None:
            continue
        ah_h = pick_ah_odds(r_, "H")
        ah_a = pick_ah_odds(r_, "A")
        if ah_h is None or ah_a is None:
            continue
        rows.append({
            "match_date": r_["Date"].isoformat(),
            "league": league_name,
            "home_team": home_long,
            "away_team": away_long,
            "home_score": int(r_["FTHG"]),
            "away_score": int(r_["FTAG"]),
            "ah_line": line,
            "ah_home_odds": ah_h,
            "ah_away_odds": ah_a,
        })
    return pd.DataFrame(rows)


def fetch_all_ah() -> pd.DataFrame:
    print("\n[1/4] Téléchargement des AH lines (football-data.co.uk)...")
    dfs = []
    for league_name, code in LEAGUE_FD_CO_UK.items():
        for season in SEASONS:
            print(f"  {league_name} {season}...", end=" ", flush=True)
            df = fetch_season_ah(code, league_name, season)
            if not df.empty:
                print(f"{len(df)} matchs")
                dfs.append(df)
            else:
                print("vide")
    if not dfs:
        return pd.DataFrame()
    return pd.concat(dfs, ignore_index=True)


# ──────────────── AH math ────────────────

def compute_ah_outcome(home_score: int, away_score: int, ah_line: float) -> tuple[float, float]:
    """
    Renvoie (home_pnl_unit, away_pnl_unit) pour 1€ misé.
    Gère les half/quarter/whole lines.
    home_pnl_unit : -1 (loss), 0 (push), +0.5 (half-push half-win), +1 (full win) etc.
    """
    # Score différentiel ajusté du handicap (positif si home l'emporte sur le spread)
    diff = home_score - away_score + ah_line

    # Quarter line : split bet en deux (.25 = .0 + .5, .75 = .5 + 1.0)
    rounded = round(ah_line * 2) / 2  # snap to half-line
    if abs(ah_line - rounded) > 1e-9:
        # Split en deux sub-bets : lower_line et upper_line, chaque 0.5€ misé
        if ah_line > rounded:
            low = rounded
            high = rounded + 0.5
        else:
            high = rounded
            low = rounded - 0.5
        h_low, _ = compute_ah_outcome(home_score, away_score, low)
        h_high, _ = compute_ah_outcome(home_score, away_score, high)
        return (h_low + h_high) / 2, -(h_low + h_high) / 2

    # Half line (.5) : pas de push
    if abs(ah_line - round(ah_line)) > 1e-9:
        if diff > 0:
            return 1.0, -1.0
        else:
            return -1.0, 1.0

    # Whole line (.0) : push possible si diff == 0
    if diff > 0:
        return 1.0, -1.0
    if diff < 0:
        return -1.0, 1.0
    return 0.0, 0.0  # push


# ──────────────── Build features ────────────────

def build_features(ah_df: pd.DataFrame) -> pd.DataFrame:
    """Merge AH data avec features 1X2 existantes (déjà calculées)."""
    print("\n[2/4] Build features...")
    feat_path = DATA_DIR / "features" / "dataset.csv"
    if not feat_path.exists():
        print("ERREUR : dataset.csv 1X2 absent (lancez build_features.py d'abord)")
        sys.exit(1)

    features = pd.read_csv(feat_path, parse_dates=["match_date"])
    features["date"] = pd.to_datetime(features["match_date"]).dt.date

    ah_df["match_date"] = pd.to_datetime(ah_df["match_date"])
    ah_df["date"] = ah_df["match_date"].dt.date

    merged = ah_df.merge(
        features.drop(columns=["label", "league"]),
        on=["date", "home_team", "away_team"],
        how="inner",
        suffixes=("", "_feat"),
    )
    print(f"  → {len(merged)}/{len(ah_df)} matchs matchés ({100*len(merged)/len(ah_df):.1f}%)")

    # Target : home covers ? (>0 win, =0 push, <0 loss)
    merged["home_pnl"] = merged.apply(
        lambda r: compute_ah_outcome(int(r["home_score"]), int(r["away_score"]), float(r["ah_line"]))[0],
        axis=1,
    )

    # Pour le training binary : on prédit P(home covers complètement)
    # Si half-cover (push partiel), label = 1 si pnl > 0
    merged["label"] = (merged["home_pnl"] > 0).astype(int)

    print(f"  Distribution : Home covers {merged['label'].mean()*100:.1f}% / no-cover {(1-merged['label'].mean())*100:.1f}%")
    print(f"  Lines distribution :")
    print(merged["ah_line"].value_counts().head(8))

    output = DATA_DIR / "features" / "ah_dataset.csv"
    merged.to_csv(output, index=False)
    print(f"  Saved : {output}")
    return merged


# ──────────────── Train ────────────────

def train(df: pd.DataFrame, tune: bool = False, n_trials: int = 20):
    print("\n[3/4] Training...")
    feature_cols = MatchFeatures.feature_names()
    df_sorted = df.sort_values("match_date").reset_index(drop=True)
    X = df_sorted[feature_cols].values.astype(np.float32)
    y = df_sorted["label"].values.astype(int)
    print(f"  {len(X)} samples, {X.shape[1]} features")

    params = {
        "n_estimators": 300, "max_depth": 4, "learning_rate": 0.05,
        "subsample": 0.8, "colsample_bytree": 0.8,
        "objective": "binary:logistic", "eval_metric": "logloss",
        "random_state": 42, "n_jobs": -1,
    }

    if tune:
        print(f"  Optuna tuning ({n_trials} trials)...")
        import optuna
        optuna.logging.set_verbosity(optuna.logging.WARNING)
        tscv = TimeSeriesSplit(n_splits=5)

        def objective(trial):
            p = {
                "n_estimators": trial.suggest_int("n_estimators", 200, 600),
                "max_depth": trial.suggest_int("max_depth", 3, 6),
                "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.15, log=True),
                "subsample": trial.suggest_float("subsample", 0.6, 1.0),
                "colsample_bytree": trial.suggest_float("colsample_bytree", 0.6, 1.0),
                "min_child_weight": trial.suggest_int("min_child_weight", 1, 10),
                "reg_alpha": trial.suggest_float("reg_alpha", 0.0, 1.0),
                "reg_lambda": trial.suggest_float("reg_lambda", 0.5, 2.0),
                "objective": "binary:logistic", "eval_metric": "logloss",
                "random_state": 42, "n_jobs": -1,
            }
            losses = []
            for ti, vi in tscv.split(X):
                clf = CalibratedClassifierCV(XGBClassifier(**p), method="sigmoid", cv=3)
                clf.fit(X[ti], y[ti])
                losses.append(log_loss(y[vi], clf.predict_proba(X[vi])))
            return float(np.mean(losses))

        study = optuna.create_study(direction="minimize")
        study.optimize(objective, n_trials=n_trials, show_progress_bar=False)
        params = {**params, **study.best_params,
                  "objective": "binary:logistic", "eval_metric": "logloss",
                  "random_state": 42, "n_jobs": -1}
        print(f"  Best log-loss : {study.best_value:.4f}")

    # OOF
    tscv = TimeSeriesSplit(n_splits=5)
    oof = np.zeros((len(y), 2))
    for ti, vi in tscv.split(X):
        clf = CalibratedClassifierCV(XGBClassifier(**params), method="sigmoid", cv=3)
        clf.fit(X[ti], y[ti])
        oof[vi] = clf.predict_proba(X[vi])

    valid = oof.sum(axis=1) > 0
    ll = float(log_loss(y[valid], oof[valid]))
    acc = float(accuracy_score(y[valid], oof[valid].argmax(axis=1)))
    brier = float(brier_score_loss(y[valid], oof[valid][:, 1]))

    version = "ah_" + datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    final = CalibratedClassifierCV(XGBClassifier(**params), method="sigmoid", cv=3)
    final.fit(X, y)

    metrics = {
        "version": version, "market": "AH",
        "log_loss": round(ll, 4), "accuracy": round(acc, 4), "brier_score": round(brier, 4),
        "n_samples": len(X),
    }
    print(f"  log_loss={metrics['log_loss']:.4f}, acc={metrics['accuracy']:.4f}, brier={metrics['brier_score']:.4f}")

    path = ARTIFACTS_DIR_MODELS / f"model_{version}.joblib"
    joblib.dump({"model": final, "version": version, "market": "AH"}, path)
    (ARTIFACTS_DIR_MODELS / f"metrics_{version}.json").write_text(json.dumps(metrics, indent=2))
    shutil.copy2(path, ARTIFACTS_DIR_MODELS / "model_ah_latest.joblib")
    print(f"  Saved : {path.name}")

    return final, metrics, df_sorted, oof, valid


# ──────────────── Backtest ────────────────

def backtest(df: pd.DataFrame, oof: np.ndarray, valid: np.ndarray,
             edge_threshold=0.08, edge_max=0.20, kelly_fraction=0.25,
             initial_bankroll=100.0, max_stake_fraction=0.05):
    print(f"\n[4/4] Backtest (edge ∈ [{edge_threshold:.0%}, {edge_max:.0%}])...")
    sub = df[valid].sort_values("match_date").reset_index(drop=True).copy()
    sub["prob_home_cover"] = oof[valid, 1]
    sub["prob_away_cover"] = oof[valid, 0]

    bankroll = initial_bankroll
    peak = bankroll
    max_dd = 0.0
    bets = []

    for _, row in sub.iterrows():
        if bankroll <= 1:
            break

        candidates = []
        for side, prob, odds in [
            ("HOME", row["prob_home_cover"], row["ah_home_odds"]),
            ("AWAY", row["prob_away_cover"], row["ah_away_odds"]),
        ]:
            if not odds or odds <= 1.0:
                continue
            edge = prob * odds - 1
            if edge < edge_threshold or edge > edge_max:
                continue
            b = odds - 1
            f_star = (prob * b - (1 - prob)) / b
            if f_star <= 0:
                continue
            stake_frac = min(f_star * kelly_fraction, max_stake_fraction)
            stake = round(bankroll * stake_frac, 2)
            if stake < 1:
                continue
            candidates.append({"side": side, "prob": prob, "odds": odds, "edge": edge, "stake": stake})

        if not candidates:
            continue
        best = max(candidates, key=lambda x: x["edge"])

        # PnL : utiliser compute_ah_outcome pour le côté parié
        home_pnl_unit, away_pnl_unit = compute_ah_outcome(
            int(row["home_score"]), int(row["away_score"]), float(row["ah_line"])
        )
        if best["side"] == "HOME":
            outcome_pnl = home_pnl_unit
        else:
            outcome_pnl = away_pnl_unit

        # Convertir l'outcome unit en € : full win = stake * (odds - 1), push = 0, loss = -stake
        if outcome_pnl > 0:
            profit = round(best["stake"] * (best["odds"] - 1) * outcome_pnl, 2)
        elif outcome_pnl < 0:
            profit = round(best["stake"] * outcome_pnl, 2)
        else:
            profit = 0.0

        bankroll = round(bankroll + profit, 2)
        peak = max(peak, bankroll)
        dd = (peak - bankroll) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

        bets.append({
            "date": str(row["match_date"])[:10],
            "league": row["league"],
            "home_team": row["home_team"], "away_team": row["away_team"],
            "score": f"{int(row['home_score'])}-{int(row['away_score'])}",
            "ah_line": row["ah_line"],
            "bet_on": best["side"],
            "prob": round(best["prob"], 4),
            "odds": round(best["odds"], 2),
            "edge": round(best["edge"], 4),
            "stake": best["stake"],
            "outcome_pnl_unit": round(outcome_pnl, 2),
            "profit": profit,
            "bankroll": bankroll,
        })

    bets_df = pd.DataFrame(bets)
    if bets_df.empty:
        print("  Aucune value bet")
        return None, None

    n = len(bets_df)
    n_wins = int((bets_df["profit"] > 0).sum())
    total_staked = float(bets_df["stake"].sum())
    total_pnl = float(bets_df["profit"].sum())
    roi = total_pnl / total_staked * 100 if total_staked > 0 else 0
    wins_pnl = bets_df.loc[bets_df["profit"] > 0, "profit"].sum()
    losses_pnl = abs(bets_df.loc[bets_df["profit"] < 0, "profit"].sum())
    pf = float(wins_pnl / losses_pnl) if losses_pnl > 0 else 0

    # Per league
    per_league = {}
    for league, g in bets_df.groupby("league"):
        ps = float(g["stake"].sum())
        pp = float(g["profit"].sum())
        per_league[league] = {
            "n_bets": len(g),
            "hit_rate": round(float((g["profit"] > 0).sum() / len(g)), 4),
            "roi_percent": round(pp / ps * 100, 2) if ps > 0 else 0,
            "total_pnl": round(pp, 2),
        }

    summary = {
        "market": "AH", "initial_bankroll": initial_bankroll,
        "final_bankroll": round(bankroll, 2),
        "n_bets": n, "n_wins": n_wins,
        "hit_rate": round(n_wins / n, 4),
        "total_staked": round(total_staked, 2),
        "total_pnl": round(total_pnl, 2),
        "roi_percent": round(roi, 2),
        "yield_per_bet": round(total_pnl / n, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "peak_bankroll": round(peak, 2),
        "avg_odds": round(float(bets_df["odds"].mean()), 2),
        "avg_edge_pct": round(float(bets_df["edge"].mean()) * 100, 2),
        "profit_factor": round(pf, 2),
        "period_start": str(bets_df["date"].min()),
        "period_end": str(bets_df["date"].max()),
        "per_league": per_league,
        "params": {
            "edge_threshold": edge_threshold, "edge_max": edge_max,
            "kelly_fraction": kelly_fraction, "calibration": "sigmoid",
        },
    }

    bets_df.to_csv(ARTIFACTS_DIR_BT / "ah_bets.csv", index=False)
    (ARTIFACTS_DIR_BT / "ah_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(f"  ROI {roi:+.2f}%, {n} paris, hit {n_wins/n*100:.1f}%, DD {max_dd*100:.1f}%, PF {pf:.2f}")
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
            "summary": summary, "equity_curve": equity,
            "sample_bets": bets_df.head(50).to_dict(orient="records") if not bets_df.empty else [],
            "computed_at": datetime.now(timezone.utc).isoformat(),
        }
        r.set("backtest:ah:latest", json.dumps(payload, default=str))
        print(f"  ✓ Redis : backtest:ah:latest")
    except Exception as e:
        print(f"  ⚠ Redis : {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--edge-threshold", type=float, default=0.08)
    parser.add_argument("--edge-max", type=float, default=0.20)
    parser.add_argument("--kelly-fraction", type=float, default=0.25)
    parser.add_argument("--tune", action="store_true")
    parser.add_argument("--n-trials", type=int, default=20)
    args = parser.parse_args()

    print("─" * 60)
    print("Asian Handicap — pipeline complet")
    print("─" * 60)

    ah = fetch_all_ah()
    if ah.empty:
        print("ERREUR : aucune donnée"); sys.exit(1)
    print(f"\n✓ {len(ah)} matchs avec AH lines")

    df = build_features(ah)
    if len(df) < 200:
        print("ERREUR : pas assez de samples"); sys.exit(1)

    model, metrics, df_sorted, oof, valid = train(df, tune=args.tune, n_trials=args.n_trials)
    bets_df, summary = backtest(
        df_sorted, oof, valid,
        edge_threshold=args.edge_threshold,
        edge_max=args.edge_max,
        kelly_fraction=args.kelly_fraction,
    )

    if summary:
        publish_to_redis(summary, bets_df)
        print("\n" + "=" * 60)
        print("RÉSULTATS ASIAN HANDICAP")
        print("=" * 60)
        print(f"  Période       : {summary['period_start']} → {summary['period_end']}")
        print(f"  Paris placés  : {summary['n_bets']}")
        print(f"  Hit rate      : {summary['hit_rate']*100:.1f}%")
        print(f"  ROI           : {summary['roi_percent']:+.2f}%")
        print(f"  Bankroll fin  : {summary['final_bankroll']:.0f}EUR (pic {summary['peak_bankroll']:.0f})")
        print(f"  Drawdown      : {summary['max_drawdown_pct']:.1f}%")
        print(f"  Profit factor : {summary['profit_factor']:.2f}")
        print(f"  Cote moyenne  : {summary['avg_odds']}")
        print(f"  Edge moyen    : {summary['avg_edge_pct']:.1f}%")
        print(f"\nPar ligue :")
        for league, st in summary["per_league"].items():
            print(f"  {league:18} | {st['n_bets']:4d} | hit {st['hit_rate']*100:5.1f}% | ROI {st['roi_percent']:+6.1f}% | P&L {st['total_pnl']:+5.0f}")
