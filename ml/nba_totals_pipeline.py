"""
Pipeline NBA Totals : fetch cotes historiques + train binary model + backtest + deploy.

Source des lines : sportsbookreviewsonline.com (saisons 2020-21 à 2022-23).
Convention SBR : V row → total line, H row → spread.
Odds Over/Under : standard -110 / -110 = decimal 1.91 / 1.91 (4.5% margin).

Target : total_points > closing_line (binaire).

Usage:
    python nba_totals_pipeline.py            # full pipeline
    python nba_totals_pipeline.py --train-only
    python nba_totals_pipeline.py --backtest-only
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
from pipeline.nba_features import NBAFeatures, compute_nba_features
from nba_backtest import SEASONS, TEAM_MAP, parse_season_date

DATA_DIR = Path(__file__).parent / "data"
ARTIFACTS_DIR_MODELS = Path(__file__).parent / "artifacts" / "models"
ARTIFACTS_DIR_BT = Path(__file__).parent / "artifacts" / "backtest"
ARTIFACTS_DIR_MODELS.mkdir(parents=True, exist_ok=True)
ARTIFACTS_DIR_BT.mkdir(parents=True, exist_ok=True)

# Over/Under odds standard : -110 each side = decimal 1.91
DEFAULT_OVER_ODDS = 1.91
DEFAULT_UNDER_ODDS = 1.91


# ──────────────── Fetch totals data ────────────────

def fetch_totals_for_season(season: str) -> pd.DataFrame:
    """Récupère les totals lines depuis SBR pour une saison."""
    url = f"https://www.sportsbookreviewsonline.com/scoresoddsarchives/nba-odds-{season}"
    print(f"  {season}...", end=" ", flush=True)
    try:
        r = httpx.get(url, timeout=20, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        t = pd.read_html(StringIO(r.text))[0]
        t.columns = t.iloc[0]
        t = t[1:].reset_index(drop=True)

        rows = []
        for i in range(0, len(t) - 1, 2):
            r1, r2 = t.iloc[i], t.iloc[i + 1]
            if r1.get("VH") != "V" or r2.get("VH") != "H":
                continue
            visitor, home = r1, r2

            # V row : Open/Close = total line
            try:
                opening_total = float(visitor["Open"])
                closing_total = float(visitor["Close"])
            except (ValueError, TypeError):
                continue

            # Filter out lines < 100 (likely spread misread) or > 300 (invalid)
            if closing_total < 100 or closing_total > 300:
                continue

            date = parse_season_date(r1["Date"], season)
            if date is None:
                continue

            try:
                home_final = int(home["Final"])
                away_final = int(visitor["Final"])
            except (ValueError, TypeError):
                continue

            total_actual = home_final + away_final

            rows.append({
                "match_date": date,
                "home_team_raw": home["Team"],
                "away_team_raw": visitor["Team"],
                "home_team": TEAM_MAP.get(home["Team"], home["Team"]),
                "away_team": TEAM_MAP.get(visitor["Team"], visitor["Team"]),
                "home_score": home_final,
                "away_score": away_final,
                "total_actual": total_actual,
                "opening_total": opening_total,
                "closing_total": closing_total,
                "total_over": int(total_actual > closing_total),
                "season": season,
            })
        df = pd.DataFrame(rows)
        print(f"{len(df)} matchs")
        return df
    except Exception as e:
        print(f"ERROR: {e}")
        return pd.DataFrame()


def fetch_all_totals() -> pd.DataFrame:
    print("\n[1/4] Téléchargement totals lines (sportsbookreviewsonline.com)...")
    dfs = [fetch_totals_for_season(s) for s in SEASONS]
    dfs = [d for d in dfs if not d.empty]
    if not dfs:
        return pd.DataFrame()
    return pd.concat(dfs, ignore_index=True)


# ──────────────── Build features ────────────────

def build_features_dataset(totals_df: pd.DataFrame) -> pd.DataFrame:
    """Calcule les NBAFeatures pour chaque match avec ses target."""
    print("\n[2/4] Build features...")
    # Charge le dataset NBA pour reconstituer l'historique
    raw = pd.read_csv(DATA_DIR / "raw" / "nba_matches.csv", parse_dates=["match_date"])
    raw["home_score"] = pd.to_numeric(raw["home_score"], errors="coerce")
    raw["away_score"] = pd.to_numeric(raw["away_score"], errors="coerce")
    raw = raw.dropna(subset=["home_score", "away_score"]).sort_values("match_date").reset_index(drop=True)

    totals_df["match_date"] = pd.to_datetime(totals_df["match_date"])
    totals_df = totals_df.sort_values("match_date").reset_index(drop=True)

    # Merge totals with our match data (by date + teams)
    feat_rows = []
    skipped = 0
    for i, row in totals_df.iterrows():
        # Past matches for feature computation
        past = raw[raw["match_date"] < row["match_date"]]
        home_n = len(past[(past["home_team"] == row["home_team"]) | (past["away_team"] == row["home_team"])])
        away_n = len(past[(past["home_team"] == row["away_team"]) | (past["away_team"] == row["away_team"])])
        if home_n < 10 or away_n < 10:
            skipped += 1
            continue

        feat = compute_nba_features(row["home_team"], row["away_team"], row["match_date"], past)
        d = dict(zip(NBAFeatures.feature_names(), feat.to_array()))
        d["label"] = int(row["total_over"])
        d["match_date"] = row["match_date"].isoformat()
        d["home_team"] = row["home_team"]
        d["away_team"] = row["away_team"]
        d["closing_total"] = row["closing_total"]
        d["opening_total"] = row["opening_total"]
        d["total_actual"] = row["total_actual"]
        feat_rows.append(d)
        if (i + 1) % 500 == 0:
            print(f"  {i+1}/{len(totals_df)} traités...")

    df = pd.DataFrame(feat_rows)
    print(f"  → {len(df)} exemples ({skipped} ignorés, historique <10 matchs)")
    over_pct = df["label"].mean() * 100
    print(f"  Distribution : Over {over_pct:.1f}% / Under {100-over_pct:.1f}%")

    output = DATA_DIR / "features" / "nba_totals_dataset.csv"
    df.to_csv(output, index=False)
    print(f"  Saved : {output}")
    return df


# ──────────────── Train ────────────────

def train_model(df: pd.DataFrame, tune: bool = False, n_trials: int = 20):
    """Train binary classifier with sigmoid calibration."""
    print("\n[3/4] Training...")
    feature_cols = NBAFeatures.feature_names()
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
        params = {**params, **study.best_params}
        params.update({"objective": "binary:logistic", "eval_metric": "logloss",
                       "random_state": 42, "n_jobs": -1})
        print(f"  Best log-loss : {study.best_value:.4f}")

    # OOF metrics
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

    # Modèle final sur tout
    version = "nba_totals_" + datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    final = CalibratedClassifierCV(XGBClassifier(**params), method="sigmoid", cv=3)
    final.fit(X, y)

    metrics = {
        "version": version,
        "market": "NBA_TOTALS",
        "log_loss": round(ll, 4),
        "accuracy": round(acc, 4),
        "brier_score": round(brier, 4),
        "n_samples": len(X),
    }
    print(f"  log_loss={metrics['log_loss']:.4f}, acc={metrics['accuracy']:.4f}, brier={metrics['brier_score']:.4f}")

    path = ARTIFACTS_DIR_MODELS / f"model_{version}.joblib"
    joblib.dump({"model": final, "version": version, "market": "NBA_TOTALS"}, path)
    (ARTIFACTS_DIR_MODELS / f"metrics_{version}.json").write_text(json.dumps(metrics, indent=2))
    shutil.copy2(path, ARTIFACTS_DIR_MODELS / "model_nba_totals_latest.joblib")
    print(f"  Saved : {path}")
    return final, metrics, df_sorted, oof, valid


# ──────────────── Backtest ────────────────

def backtest(df: pd.DataFrame, oof: np.ndarray, valid: np.ndarray,
             edge_threshold=0.03, edge_max=0.20, kelly_fraction=0.25,
             initial_bankroll=100.0, max_stake_fraction=0.05):
    print("\n[4/4] Backtest Kelly...")
    sub = df[valid].reset_index(drop=True).copy()
    sub["prob_under"] = oof[valid, 0]
    sub["prob_over"] = oof[valid, 1]

    bankroll = initial_bankroll
    peak = bankroll
    max_dd = 0.0
    bets = []

    for _, row in sub.iterrows():
        if bankroll <= 1:
            break

        candidates = []
        for outcome, prob in [("OVER", row["prob_over"]), ("UNDER", row["prob_under"])]:
            odds = DEFAULT_OVER_ODDS if outcome == "OVER" else DEFAULT_UNDER_ODDS
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
            candidates.append({"outcome": outcome, "prob": prob, "odds": odds, "edge": edge, "stake": stake})

        if not candidates:
            continue
        best = max(candidates, key=lambda x: x["edge"])
        actual_over = int(row["label"]) == 1
        won = (best["outcome"] == "OVER" and actual_over) or (best["outcome"] == "UNDER" and not actual_over)
        profit = round(best["stake"] * (best["odds"] - 1), 2) if won else -best["stake"]
        bankroll = round(bankroll + profit, 2)
        peak = max(peak, bankroll)
        dd = (peak - bankroll) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

        bets.append({
            "date": str(row["match_date"])[:10],
            "home_team": row["home_team"],
            "away_team": row["away_team"],
            "closing_total": row["closing_total"],
            "total_actual": row["total_actual"],
            "bet_on": best["outcome"],
            "prob": round(best["prob"], 4),
            "odds": best["odds"],
            "edge": round(best["edge"], 4),
            "stake": best["stake"],
            "won": won,
            "profit": profit,
            "bankroll": bankroll,
        })

    bets_df = pd.DataFrame(bets)
    if bets_df.empty:
        print("  Aucune value bet trouvée")
        return None, None

    n = len(bets_df)
    n_wins = int(bets_df["won"].sum())
    total_staked = float(bets_df["stake"].sum())
    total_pnl = float(bets_df["profit"].sum())
    roi = total_pnl / total_staked * 100 if total_staked > 0 else 0
    wins_pnl = bets_df.loc[bets_df["profit"] > 0, "profit"].sum()
    losses_pnl = abs(bets_df.loc[bets_df["profit"] < 0, "profit"].sum())
    pf = float(wins_pnl / losses_pnl) if losses_pnl > 0 else 0

    summary = {
        "market": "NBA_TOTALS",
        "initial_bankroll": initial_bankroll,
        "final_bankroll": round(bankroll, 2),
        "n_bets": n, "n_wins": n_wins,
        "hit_rate": round(n_wins / n, 4),
        "total_staked": round(total_staked, 2),
        "total_pnl": round(total_pnl, 2),
        "roi_percent": round(roi, 2),
        "yield_per_bet": round(total_pnl / n, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "peak_bankroll": round(peak, 2),
        "avg_edge_pct": round(float(bets_df["edge"].mean()) * 100, 2),
        "profit_factor": round(pf, 2),
        "period_start": str(bets_df["date"].min()),
        "period_end": str(bets_df["date"].max()),
        "params": {
            "edge_threshold": edge_threshold, "edge_max": edge_max,
            "kelly_fraction": kelly_fraction,
            "over_odds": DEFAULT_OVER_ODDS, "under_odds": DEFAULT_UNDER_ODDS,
            "calibration": "sigmoid",
        },
        "per_league": {"NBA": {
            "n_bets": n, "hit_rate": round(n_wins / n, 4),
            "roi_percent": round(roi, 2), "total_pnl": round(total_pnl, 2),
        }},
    }
    print(f"  ROI {roi:+.2f}%, {n} paris, hit {n_wins/n*100:.1f}%, DD {max_dd*100:.1f}%, PF {pf:.2f}")

    bets_df.to_csv(ARTIFACTS_DIR_BT / "nba_totals_bets.csv", index=False)
    (ARTIFACTS_DIR_BT / "nba_totals_summary.json").write_text(json.dumps(summary, indent=2, default=str))
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
        r.set("backtest:nba_totals:latest", json.dumps(payload, default=str))
        print(f"  ✓ Redis : backtest:nba_totals:latest")
    except Exception as e:
        print(f"  ⚠ Redis échoué : {e}")


# ──────────────── Main ────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--edge-threshold", type=float, default=0.03)
    parser.add_argument("--edge-max", type=float, default=0.20)
    parser.add_argument("--kelly-fraction", type=float, default=0.25)
    parser.add_argument("--tune", action="store_true")
    parser.add_argument("--n-trials", type=int, default=20)
    args = parser.parse_args()

    print("─" * 60)
    print("NBA Totals — pipeline complet")
    print("─" * 60)

    totals = fetch_all_totals()
    if totals.empty:
        print("\nERREUR : aucune donnée téléchargée"); sys.exit(1)
    print(f"\n✓ {len(totals)} matchs avec totals lines")

    df = build_features_dataset(totals)
    if len(df) < 200:
        print("ERREUR : pas assez de samples"); sys.exit(1)

    model, metrics, df_sorted, oof, valid = train_model(df, tune=args.tune, n_trials=args.n_trials)
    bets_df, summary = backtest(
        df_sorted, oof, valid,
        edge_threshold=args.edge_threshold,
        edge_max=args.edge_max,
        kelly_fraction=args.kelly_fraction,
    )

    if summary:
        publish_to_redis(summary, bets_df)
        print("\n" + "=" * 60)
        print("RÉSULTATS NBA TOTALS")
        print("=" * 60)
        print(f"  Période       : {summary['period_start']} → {summary['period_end']}")
        print(f"  Paris placés  : {summary['n_bets']}")
        print(f"  Hit rate      : {summary['hit_rate']*100:.1f}%")
        print(f"  Total misé    : {summary['total_staked']:.0f}EUR")
        print(f"  P&L           : {summary['total_pnl']:+.0f}EUR")
        print(f"  ROI           : {summary['roi_percent']:+.2f}%")
        print(f"  Bankroll fin  : {summary['final_bankroll']:.0f}EUR (pic {summary['peak_bankroll']:.0f})")
        print(f"  Drawdown      : {summary['max_drawdown_pct']:.1f}%")
        print(f"  Profit factor : {summary['profit_factor']:.2f}")
        print(f"  Edge moyen    : {summary['avg_edge_pct']:.1f}%")
