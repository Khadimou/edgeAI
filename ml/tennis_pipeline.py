"""
Pipeline Tennis ATP : build features + train + backtest + deploy.

- Train binary : P(winner gagne) = 1
- Augmente le dataset en générant 2 lignes/match : (A=winner, label=1) + (A=loser, label=0)
- Train sur tous les matchs, backtest sur les matchs avec cotes (Pinnacle/Bet365)

Usage:
    python tennis_pipeline.py
    python tennis_pipeline.py --tune --n-trials 30
"""
import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.metrics import log_loss, accuracy_score, brier_score_loss
from sklearn.model_selection import TimeSeriesSplit
from xgboost import XGBClassifier

sys.path.insert(0, str(Path(__file__).parent))
from pipeline.tennis_features import (
    TennisFeatures, compute_tennis_features, init_elo, update_elo
)

DATA_DIR = Path(__file__).parent / "data"
INPUT = DATA_DIR / "raw" / "atp_matches.csv"
OUTPUT_FEAT = DATA_DIR / "features" / "tennis_dataset.csv"
ARTIFACTS_DIR_MODELS = Path(__file__).parent / "artifacts" / "models"
ARTIFACTS_DIR_BT = Path(__file__).parent / "artifacts" / "backtest"
ARTIFACTS_DIR_MODELS.mkdir(parents=True, exist_ok=True)
ARTIFACTS_DIR_BT.mkdir(parents=True, exist_ok=True)


# ──────────────────────────────────────────────────────────
# Build features (chronologique avec ELO update)
# ──────────────────────────────────────────────────────────

def build_features(df: pd.DataFrame) -> pd.DataFrame:
    print("\n[1/3] Build features avec ELO chronologique...")
    df = df.sort_values("match_date").reset_index(drop=True)
    elo_general = init_elo()
    elo_surface = {}

    feature_cols = TennisFeatures.feature_names()
    rows = []

    for i, row in df.iterrows():
        # Compute features BEFORE updating ELO
        match_date = pd.Timestamp(row["match_date"])
        surface = str(row.get("surface", "Hard")) if pd.notna(row.get("surface")) else "Hard"

        winner = str(row["winner_name"])
        loser = str(row["loser_name"])

        # Génère 2 lignes : (A=winner, label=1) + (A=loser, label=0)
        for player_a, player_b, label, rank_a, rank_b, pts_a, pts_b, age_a, age_b, hand_a, hand_b in [
            (winner, loser, 1,
             row.get("winner_rank"), row.get("loser_rank"),
             row.get("winner_rank_points"), row.get("loser_rank_points"),
             row.get("winner_age"), row.get("loser_age"),
             row.get("winner_hand"), row.get("loser_hand")),
            (loser, winner, 0,
             row.get("loser_rank"), row.get("winner_rank"),
             row.get("loser_rank_points"), row.get("winner_rank_points"),
             row.get("loser_age"), row.get("winner_age"),
             row.get("loser_hand"), row.get("winner_hand")),
        ]:
            feat = compute_tennis_features(
                player_a, player_b, match_date, surface,
                int(row.get("best_of", 3)) if pd.notna(row.get("best_of")) else 3,
                str(row.get("round", "R32")),
                df.iloc[:i],  # passé strict
                elo_general, elo_surface,
                rank_a=rank_a, rank_b=rank_b,
                points_a=pts_a, points_b=pts_b,
                age_a=age_a, age_b=age_b,
                hand_a=hand_a, hand_b=hand_b,
            )
            d = dict(zip(feature_cols, feat.to_array()))
            d["label"] = label
            d["match_date"] = match_date
            d["player_a"] = player_a
            d["player_b"] = player_b
            d["surface"] = surface
            d["match_idx"] = i  # pour grouper backtest par match unique

            # Stocke aussi les odds pour A (si winner: odds_winner, si loser: odds_loser)
            if label == 1:
                d["odds_a"] = row.get("odds_winner")
                d["odds_b"] = row.get("odds_loser")
            else:
                d["odds_a"] = row.get("odds_loser")
                d["odds_b"] = row.get("odds_winner")

            rows.append(d)

        # Update ELO APRÈS calcul features
        update_elo(elo_general, elo_surface, winner, loser, surface)

        if (i + 1) % 5000 == 0:
            print(f"  {i+1}/{len(df)} matchs traités... (ELO general size: {len(elo_general)})")

    result = pd.DataFrame(rows)
    print(f"\n  → {len(result)} lignes feature ({len(result)//2} matchs)")
    OUTPUT_FEAT.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(OUTPUT_FEAT, index=False)
    print(f"  Saved : {OUTPUT_FEAT}")
    return result


# ──────────────────────────────────────────────────────────
# Train + eval
# ──────────────────────────────────────────────────────────

def train_and_eval(df: pd.DataFrame, tune: bool = False, n_trials: int = 30):
    feature_cols = TennisFeatures.feature_names()
    df["match_date"] = pd.to_datetime(df["match_date"])
    df = df.sort_values("match_date").reset_index(drop=True)

    # Garde les matchs après 2012 (les premiers ont ELO non convergé)
    df = df[df["match_date"] >= "2012-01-01"].reset_index(drop=True)

    X = df[feature_cols].values.astype(np.float32)
    y = df["label"].values.astype(int)
    print(f"\n[2/3] Training : {len(X)} samples, {X.shape[1]} features")
    print(f"  Distribution : winrate label=1 : {y.mean()*100:.1f}%")

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
                "max_depth": trial.suggest_int("max_depth", 3, 7),
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
        print(f"  Best CV log-loss : {study.best_value:.4f}")

    # OOF + final model
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
    print(f"\n  OOF metrics : log_loss={ll:.4f}, accuracy={acc:.4f}, brier={brier:.4f}")

    # Final model on all data
    version = "tennis_" + datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    final = CalibratedClassifierCV(XGBClassifier(**params), method="sigmoid", cv=3)
    final.fit(X, y)

    metrics = {
        "version": version, "market": "TENNIS_ATP",
        "log_loss": round(ll, 4), "accuracy": round(acc, 4),
        "brier_score": round(brier, 4),
        "n_samples": len(X),
    }
    path = ARTIFACTS_DIR_MODELS / f"model_{version}.joblib"
    joblib.dump({"model": final, "version": version, "market": "TENNIS_ATP"}, path)
    (ARTIFACTS_DIR_MODELS / f"metrics_{version}.json").write_text(json.dumps(metrics, indent=2))
    shutil.copy2(path, ARTIFACTS_DIR_MODELS / "model_tennis_latest.joblib")
    print(f"  Saved : {path.name}")

    return final, df, oof, valid


# ──────────────────────────────────────────────────────────
# Backtest sur matchs avec cotes
# ──────────────────────────────────────────────────────────

def backtest(df: pd.DataFrame, oof: np.ndarray, valid: np.ndarray,
             edge_threshold=0.05, edge_max=0.25, kelly_fraction=0.25,
             initial_bankroll=100.0, max_stake_fraction=0.05,
             surface_filter=None):
    print(f"\n[3/3] Backtest (edge ∈ [{edge_threshold:.0%}, {edge_max:.0%}])...")

    # On veut UNE ligne par match unique (les 2 lignes A/B sont des features symétriques)
    # Pour le backtest, on prend label=1 (player_a = winner) puis on filtre par cotes dispo
    sub = df[valid].copy()
    sub["prob_a"] = oof[valid, 1]  # P(player_a gagne)

    # Dedup : pour chaque match_idx, on garde la ligne où player_a était winner (label=1)
    # car les odds_a sont alors les odds du vrai winner. Sinon on prend label=0 et inverse.
    # Plus simple : on garde TOUT et on simule pour chaque ligne séparément.
    # Mais ça double-compte → faut grouper.
    # Stratégie : on garde label==1 lignes (true winner = A) → prob_a = P(winner gagne selon modèle)
    sub_w = sub[sub["label"] == 1].copy()
    sub_w = sub_w.dropna(subset=["odds_a", "odds_b"])

    # Surface filter (Hard / Clay / Grass / Carpet)
    if surface_filter:
        if isinstance(surface_filter, str):
            surface_filter = [surface_filter]
        before = len(sub_w)
        sub_w = sub_w[sub_w["surface"].isin(surface_filter)].copy()
        print(f"  Surface filter {surface_filter} : {before} → {len(sub_w)} matchs")

    print(f"  → {len(sub_w)} matchs avec cotes pour backtest")
    if len(sub_w) < 50:
        print("  ! Pas assez de matchs avec cotes")
        return None, None

    bankroll = initial_bankroll
    peak = bankroll
    max_dd = 0.0
    bets = []

    for _, row in sub_w.sort_values("match_date").iterrows():
        if bankroll <= 1:
            break
        prob_a = row["prob_a"]  # P(le vrai winner gagne selon modèle)
        prob_b = 1 - prob_a
        odds_a = row["odds_a"]  # odds du winner
        odds_b = row["odds_b"]  # odds du loser

        candidates = []
        for side, prob, odds in [
            ("A_wins", prob_a, odds_a),    # parier sur le vrai winner
            ("B_wins", prob_b, odds_b),     # parier sur le vrai loser
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
        # Outcome : A (winner) gagne TOUJOURS car label==1
        won = best["side"] == "A_wins"
        profit = round(best["stake"] * (best["odds"] - 1), 2) if won else -best["stake"]
        bankroll = round(bankroll + profit, 2)
        peak = max(peak, bankroll)
        dd = (peak - bankroll) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

        bets.append({
            "date": str(row["match_date"])[:10],
            "surface": row["surface"],
            "winner": row["player_a"],
            "loser": row["player_b"],
            "bet_on": "winner" if best["side"] == "A_wins" else "loser",
            "prob": round(best["prob"], 4),
            "odds": round(best["odds"], 2),
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

    # Per surface
    per_surface = {}
    for surf, g in bets_df.groupby("surface"):
        ps = float(g["stake"].sum())
        pp = float(g["profit"].sum())
        per_surface[surf] = {
            "n_bets": len(g),
            "hit_rate": round(float((g["profit"] > 0).sum() / len(g)), 4),
            "roi_percent": round(pp / ps * 100, 2) if ps > 0 else 0,
            "total_pnl": round(pp, 2),
        }

    summary = {
        "market": "TENNIS_ATP",
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
        "avg_odds": round(float(bets_df["odds"].mean()), 2),
        "avg_edge_pct": round(float(bets_df["edge"].mean()) * 100, 2),
        "profit_factor": round(pf, 2),
        "period_start": str(bets_df["date"].min()),
        "period_end": str(bets_df["date"].max()),
        "per_surface": per_surface,
        "per_league": {"ATP": {
            "n_bets": n, "hit_rate": round(n_wins / n, 4),
            "roi_percent": round(roi, 2), "total_pnl": round(total_pnl, 2),
        }},
        "params": {
            "edge_threshold": edge_threshold, "edge_max": edge_max,
            "kelly_fraction": kelly_fraction, "calibration": "sigmoid",
            "surface_filter": surface_filter,
        },
    }

    bets_df.to_csv(ARTIFACTS_DIR_BT / "tennis_bets.csv", index=False)
    (ARTIFACTS_DIR_BT / "tennis_summary.json").write_text(json.dumps(summary, indent=2, default=str))
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
        r.set("backtest:tennis:latest", json.dumps(payload, default=str))
        print(f"  ✓ Redis : backtest:tennis:latest")
    except Exception as e:
        print(f"  ⚠ Redis : {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--edge-threshold", type=float, default=0.05)
    parser.add_argument("--edge-max", type=float, default=0.25)
    parser.add_argument("--kelly-fraction", type=float, default=0.25)
    parser.add_argument("--tune", action="store_true")
    parser.add_argument("--n-trials", type=int, default=30)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--surface", type=str, default=None,
                        help="Filter backtest by surface (Hard/Clay/Grass/Carpet). "
                             "Comma-separated for multiple (e.g. 'Hard,Grass').")
    args = parser.parse_args()

    surface_filter = None
    if args.surface:
        surface_filter = [s.strip() for s in args.surface.split(",") if s.strip()]

    print("─" * 60)
    print("Tennis ATP — pipeline ML complet")
    print("─" * 60)

    if not args.skip_build:
        if not INPUT.exists():
            print(f"ERREUR : {INPUT}. Lance tennis_collect_data.py d'abord.")
            sys.exit(1)
        df_raw = pd.read_csv(INPUT, parse_dates=["match_date"])
        result = build_features(df_raw)
    else:
        result = pd.read_csv(OUTPUT_FEAT, parse_dates=["match_date"])

    model, df_sorted, oof, valid = train_and_eval(result, tune=args.tune, n_trials=args.n_trials)

    bets_df, summary = backtest(
        df_sorted, oof, valid,
        edge_threshold=args.edge_threshold,
        edge_max=args.edge_max,
        kelly_fraction=args.kelly_fraction,
        surface_filter=surface_filter,
    )
    if summary:
        publish_to_redis(summary, bets_df)
        print("\n" + "=" * 60)
        print("RÉSULTATS TENNIS ATP")
        print("=" * 60)
        print(f"  Période       : {summary['period_start']} → {summary['period_end']}")
        print(f"  Paris placés  : {summary['n_bets']}")
        print(f"  Hit rate      : {summary['hit_rate']*100:.1f}%")
        print(f"  ROI           : {summary['roi_percent']:+.2f}%")
        print(f"  Bankroll fin  : {summary['final_bankroll']:.0f}EUR (pic {summary['peak_bankroll']:.0f})")
        print(f"  Drawdown      : {summary['max_drawdown_pct']:.1f}%")
        print(f"  Profit factor : {summary['profit_factor']:.2f}")
        print(f"  Edge moyen    : {summary['avg_edge_pct']:.1f}%")
        print(f"\nPar surface :")
        for surf, st in summary["per_surface"].items():
            print(f"  {surf:10} | {st['n_bets']:4d} paris | hit {st['hit_rate']*100:5.1f}% | ROI {st['roi_percent']:+6.1f}%")
