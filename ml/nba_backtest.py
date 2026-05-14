"""
Backtest historique NBA — value betting binaire.

Source des cotes : sportsbookreviewsonline.com (archives gratuites 2007-2023).
Couvre nos saisons 2020-21, 2021-22, 2022-23 sur 3300+ matchs.

Pipeline :
1. Télécharge les odds ML (moneyline) de 3 saisons
2. Convertit American → Decimal
3. Merge avec dataset NBA features + labels
4. 5-fold TimeSeriesSplit → OOF predictions
5. Simule Kelly binaire (HOME/AWAY)
6. Publish dans Redis pour /backtest

Usage:
    python nba_backtest.py
    python nba_backtest.py --edge-threshold 0.05 --kelly-fraction 0.25
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
from pipeline.nba_features import NBAFeatures

DATA_DIR = Path(__file__).parent / "data"
ARTIFACTS_DIR = Path(__file__).parent / "artifacts" / "backtest"
ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

# Mapping sportsbookreviewsonline → nba_api full names
TEAM_MAP = {
    "Atlanta": "Atlanta Hawks", "Boston": "Boston Celtics",
    "Brooklyn": "Brooklyn Nets", "Charlotte": "Charlotte Hornets",
    "Chicago": "Chicago Bulls", "Cleveland": "Cleveland Cavaliers",
    "Dallas": "Dallas Mavericks", "Denver": "Denver Nuggets",
    "Detroit": "Detroit Pistons", "GoldenState": "Golden State Warriors",
    "Houston": "Houston Rockets", "Indiana": "Indiana Pacers",
    "LAClippers": "LA Clippers", "LALakers": "Los Angeles Lakers",
    "Memphis": "Memphis Grizzlies", "Miami": "Miami Heat",
    "Milwaukee": "Milwaukee Bucks", "Minnesota": "Minnesota Timberwolves",
    "NewOrleans": "New Orleans Pelicans", "NewYork": "New York Knicks",
    "OklahomaCity": "Oklahoma City Thunder", "Orlando": "Orlando Magic",
    "Philadelphia": "Philadelphia 76ers", "Phoenix": "Phoenix Suns",
    "Portland": "Portland Trail Blazers", "Sacramento": "Sacramento Kings",
    "SanAntonio": "San Antonio Spurs", "Toronto": "Toronto Raptors",
    "Utah": "Utah Jazz", "Washington": "Washington Wizards",
}

SEASONS = ["2020-21", "2021-22", "2022-23"]


def american_to_decimal(ml: float | int) -> float | None:
    """Convertit cote moneyline US → décimale."""
    if ml is None or pd.isna(ml):
        return None
    ml = float(ml)
    if ml > 0:
        return round(ml / 100 + 1, 3)
    if ml < 0:
        return round(100 / abs(ml) + 1, 3)
    return None


def parse_season_date(date_str: str, season: str) -> pd.Timestamp | None:
    """
    Le format date est 'MMDD' (ex: '1018' = 18 octobre).
    L'année dépend de la saison : MMDD octobre-décembre → année1, sinon année2.
    """
    if not date_str or len(str(date_str)) not in (3, 4):
        return None
    s = str(date_str).zfill(4)
    month = int(s[:2])
    day = int(s[2:])
    year1, year2 = season.split("-")
    year1 = int(year1)
    year2 = 2000 + int(year2)
    year = year1 if month >= 10 else year2
    try:
        return pd.Timestamp(year=year, month=month, day=day)
    except Exception:
        return None


def fetch_season_odds(season: str) -> pd.DataFrame:
    """Télécharge + parse les cotes d'une saison NBA."""
    url = f"https://www.sportsbookreviewsonline.com/scoresoddsarchives/nba-odds-{season}"
    print(f"  {season}...", end=" ", flush=True)
    try:
        r = httpx.get(url, timeout=20, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        t = pd.read_html(StringIO(r.text))[0]
        t.columns = t.iloc[0]
        t = t[1:].reset_index(drop=True)
        # Format : 1 ligne par équipe, alterné V/H. Group par paires.
        rows = []
        for i in range(0, len(t) - 1, 2):
            r1, r2 = t.iloc[i], t.iloc[i + 1]
            if r1.get("VH") not in ("V", "H") or r2.get("VH") not in ("V", "H"):
                continue
            visitor = r1 if r1["VH"] == "V" else r2
            home = r1 if r1["VH"] == "H" else r2
            date = parse_season_date(r1["Date"], season)
            if date is None:
                continue
            try:
                home_ml = float(home["ML"])
                away_ml = float(visitor["ML"])
            except (ValueError, TypeError):
                continue
            rows.append({
                "match_date": date,
                "home_team_raw": home["Team"],
                "away_team_raw": visitor["Team"],
                "home_team": TEAM_MAP.get(home["Team"], home["Team"]),
                "away_team": TEAM_MAP.get(visitor["Team"], visitor["Team"]),
                "home_odds": american_to_decimal(home_ml),
                "away_odds": american_to_decimal(away_ml),
                "season": season,
            })
        df = pd.DataFrame(rows)
        df = df.dropna(subset=["home_odds", "away_odds"])
        print(f"{len(df)} matchs")
        return df
    except Exception as e:
        print(f"ERROR: {e}")
        return pd.DataFrame()


def fetch_all_seasons() -> pd.DataFrame:
    all_dfs = [fetch_season_odds(s) for s in SEASONS]
    all_dfs = [d for d in all_dfs if not d.empty]
    if not all_dfs:
        return pd.DataFrame()
    return pd.concat(all_dfs, ignore_index=True)


def compute_oof_binary(X, y):
    """5-fold TimeSeriesSplit pour modèle binaire. Calibration sigmoid (Platt)."""
    from sklearn.model_selection import TimeSeriesSplit
    from sklearn.calibration import CalibratedClassifierCV
    from xgboost import XGBClassifier

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


def simulate_binary(
    df: pd.DataFrame,
    initial_bankroll: float = 100.0,
    edge_threshold: float = 0.03,
    edge_max: float = 0.20,
    min_prob: float = 0.0,
    kelly_fraction: float = 0.25,
    max_stake_fraction: float = 0.05,
):
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
            ("AWAY", row["prob_away"], row["away_odds"]),
        ]):
            if not odds or odds <= 1.0:
                continue
            edge = prob * odds - 1
            if edge < edge_threshold or edge > edge_max:
                continue
            if prob < min_prob:
                continue
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
                "outcome_idx": outcome_idx, "outcome_label": label,
                "prob": prob, "odds": odds, "edge": edge, "stake": stake,
            })
        if not candidates:
            continue
        # Prendre uniquement le meilleur pari par match
        best = max(candidates, key=lambda x: x["edge"])
        actual = int(row["label_binary"])  # 0 = HOME win, 1 = AWAY win
        won = best["outcome_idx"] == actual
        profit = round(best["stake"] * (best["odds"] - 1), 2) if won else -best["stake"]
        bankroll = round(bankroll + profit, 2)
        peak = max(peak, bankroll)
        dd = (peak - bankroll) / peak
        if dd > max_dd:
            max_dd = dd
        bets.append({
            "date": str(row["match_date"])[:10],
            "league": "NBA",
            "home_team": row["home_team"],
            "away_team": row["away_team"],
            "bet_on": best["outcome_label"],
            "odds": round(best["odds"], 2),
            "prob": round(best["prob"], 4),
            "edge": round(best["edge"], 4),
            "stake": best["stake"],
            "actual": "HOME" if actual == 0 else "AWAY",
            "won": won,
            "profit": profit,
            "bankroll": bankroll,
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

    summary = {
        "sport": "NBA",
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
        "per_league": {"NBA": {
            "n_bets": n_bets, "hit_rate": round(n_wins / n_bets, 4),
            "roi_percent": round(roi, 2), "total_pnl": round(total_pnl, 2),
        }},
        "params": {
            "edge_threshold": edge_threshold,
            "edge_max": edge_max,
            "min_prob": min_prob,
            "kelly_fraction": kelly_fraction,
            "max_stake_fraction": max_stake_fraction,
            "only_best_per_match": True,
            "calibration": "sigmoid",
        },
    }
    return bets_df, summary


def publish_to_redis(summary: dict, bets_df: pd.DataFrame):
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
        sample_bets = bets_df.head(50).to_dict(orient="records") if not bets_df.empty else []
        payload = {
            "summary": summary,
            "equity_curve": equity,
            "sample_bets": sample_bets,
            "computed_at": datetime.now(timezone.utc).isoformat(),
        }
        r.set("backtest:nba:latest", json.dumps(payload, default=str))
        print(f"✓ Redis : backtest:nba:latest ({len(json.dumps(payload, default=str))} bytes)")
    except Exception as e:
        print(f"⚠ Redis échoué : {e}")


def run_backtest(initial_bankroll=100.0, edge_threshold=0.03, edge_max=0.20, min_prob=0.0, kelly_fraction=0.25):
    print("─" * 60)
    print("Backtest NBA — value betting Kelly")
    print("─" * 60)

    features_path = DATA_DIR / "features" / "nba_dataset.csv"
    if not features_path.exists():
        print(f"ERREUR : {features_path} introuvable. Lancez nba_build_features.py d'abord.")
        sys.exit(1)

    features = pd.read_csv(features_path, parse_dates=["match_date"])
    print(f"\n✓ Dataset NBA : {len(features)} matchs")

    print(f"\n[1/4] Téléchargement cotes sportsbookreviewsonline.com ({len(SEASONS)} saisons)...")
    odds = fetch_all_seasons()
    print(f"  → {len(odds)} matchs avec cotes")

    if odds.empty:
        print("ERREUR : aucune cote téléchargée")
        sys.exit(1)

    print("\n[2/4] Merge features ↔ cotes...")
    features["date"] = pd.to_datetime(features["match_date"]).dt.date
    odds["date"] = pd.to_datetime(odds["match_date"]).dt.date
    merged = features.merge(
        odds[["date", "home_team", "away_team", "home_odds", "away_odds"]],
        on=["date", "home_team", "away_team"],
        how="inner",
    )
    print(f"  → {len(merged)}/{len(features)} matchs matchés ({100*len(merged)/len(features):.1f}%)")

    if len(merged) < 200:
        print("ERREUR : pas assez de matchs matchés")
        sys.exit(1)

    print("\n[3/4] OOF predictions (TimeSeriesSplit binaire)...")
    feature_cols = NBAFeatures.feature_names()
    merged = merged.sort_values("match_date").reset_index(drop=True)
    X = merged[feature_cols].values.astype(np.float32)
    # label CSV : 0 = home, 2 = away → binariser
    merged["label_binary"] = (merged["label"] == 2).astype(int)
    y = merged["label_binary"].values
    oof = compute_oof_binary(X, y)
    merged["prob_home"] = oof[:, 0]
    merged["prob_away"] = oof[:, 1]
    valid = oof.sum(axis=1) > 0
    merged = merged[valid].reset_index(drop=True)
    print(f"  → {len(merged)} matchs avec OOF prediction")

    print(f"\n[4/4] Simulation Kelly (bankroll {initial_bankroll}€, edge∈[{edge_threshold:.0%},{edge_max:.0%}], kelly={kelly_fraction})...")
    bets_df, summary = simulate_binary(
        merged, initial_bankroll, edge_threshold, edge_max, min_prob, kelly_fraction,
    )

    print(f"\n{'─' * 60}")
    print("RÉSULTATS NBA")
    print(f"{'─' * 60}")
    if summary.get("n_bets", 0) == 0:
        print("Aucune value bet trouvée — modèle trop calibré (les cotes battent toujours le modèle).")
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

    bets_df.to_csv(ARTIFACTS_DIR / "nba_bets.csv", index=False)
    (ARTIFACTS_DIR / "nba_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    publish_to_redis(summary, bets_df)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--bankroll", type=float, default=100.0)
    parser.add_argument("--edge-threshold", type=float, default=0.03)
    parser.add_argument("--edge-max", type=float, default=0.20)
    parser.add_argument("--min-prob", type=float, default=0.0)
    parser.add_argument("--kelly-fraction", type=float, default=0.25)
    args = parser.parse_args()
    run_backtest(args.bankroll, args.edge_threshold, args.edge_max, args.min_prob, args.kelly_fraction)
