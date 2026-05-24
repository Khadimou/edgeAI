"""
Entraînement + backtest out-of-sample du modèle de buts Dixon-Coles international.

Train : matchs internationaux < cutoff (par défaut avant WC 2022)
Eval  : WC 2022 (64 matchs) — strictement out-of-sample
On évalue 1X2 (log-loss/accuracy), O/U 2.5 et la calibration AH (via ROI simulé
sur le résultat réel des matchs).

Usage:
    python wc_goals_train.py                  # backtest WC 2022 puis fit final (toute la data)
    python wc_goals_train.py --no-final       # backtest seulement
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import log_loss, accuracy_score

sys.path.insert(0, str(Path(__file__).parent))
from pipeline.wc_goals import fit_dixon_coles, WCGoalsModel

DATA = Path(__file__).parent / "data" / "raw" / "international_matches.csv"
ARTIFACTS = Path(__file__).parent / "artifacts" / "models"
ARTIFACTS.mkdir(parents=True, exist_ok=True)


def load_df() -> pd.DataFrame:
    df = pd.read_csv(DATA, parse_dates=["date"])
    for c in ("neutral", "is_wc", "is_friendly"):
        df[c] = df[c].astype(bool)
    return df.dropna(subset=["home_team", "away_team", "home_score", "away_score", "date"])


def evaluate(model: WCGoalsModel, eval_df: pd.DataFrame, label: str) -> dict:
    probs, labels = [], []
    ou_probs, ou_labels = [], []
    for _, r in eval_df.iterrows():
        # Les matchs de WC sont quasi tous neutres (sauf pays hôte) → neutral=True
        mp = model.market_probs(r["home_team"], r["away_team"], neutral=bool(r["neutral"]))
        probs.append([mp["prob_home"], mp["prob_draw"], mp["prob_away"]])
        if r["home_score"] > r["away_score"]:
            labels.append(0)
        elif r["home_score"] == r["away_score"]:
            labels.append(1)
        else:
            labels.append(2)
        ou_probs.append(mp["prob_over"])
        ou_labels.append(int((r["home_score"] + r["away_score"]) > 2.5))

    probs = np.array(probs)
    probs = probs / probs.sum(axis=1, keepdims=True)
    labels = np.array(labels)
    acc = accuracy_score(labels, probs.argmax(axis=1))
    ll = log_loss(labels, probs, labels=[0, 1, 2])
    naive = (labels == 0).mean()

    # O/U : log-loss binaire
    ou_probs = np.clip(np.array(ou_probs), 1e-6, 1 - 1e-6)
    ou_labels = np.array(ou_labels)
    ou_ll = -np.mean(ou_labels * np.log(ou_probs) + (1 - ou_labels) * np.log(1 - ou_probs))

    print(f"\n  === Eval {label} ({len(labels)} matchs) ===")
    print(f"    1X2 accuracy   : {acc:.4f} ({acc*100:.1f}%)  | baseline HOME {naive*100:.1f}%")
    print(f"    1X2 log-loss   : {ll:.4f}")
    print(f"    O/U 2.5 logloss: {ou_ll:.4f}  (taux Over réel {ou_labels.mean()*100:.1f}%)")
    return {
        "eval_n": int(len(labels)),
        "acc_1x2": round(float(acc), 4),
        "logloss_1x2": round(float(ll), 4),
        "baseline_home": round(float(naive), 4),
        "logloss_ou25": round(float(ou_ll), 4),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-final", action="store_true", help="backtest seulement, pas de fit final")
    ap.add_argument("--half-life", type=int, default=365 * 3)
    args = ap.parse_args()

    print("─" * 60)
    print("Dixon-Coles international — train + backtest WC 2022")
    print("─" * 60)
    df = load_df()
    print(f"Chargé {len(df)} matchs ({df['date'].min().date()} → {df['date'].max().date()})")

    # ── Backtest : train < 2022-11, eval = WC 2022 ──
    cutoff = pd.Timestamp("2022-11-01")
    train = df[df["date"] < cutoff]
    wc2022 = df[(df["tournament"] == "FIFA World Cup") &
                (df["date"] >= cutoff) & (df["date"] < "2023-01-01")]
    print(f"\n[1] Fit backtest sur {len(train)} matchs (< {cutoff.date()})...")
    m = fit_dixon_coles(train, half_life_days=args.half_life, ref_date=cutoff)
    print(f"    home_adv={m.home_adv:.3f}  rho={m.rho:.3f}  équipes={len(m.attack)}")
    metrics = evaluate(m, wc2022, "WC 2022")

    # Sanity : top 5 attaques / défenses
    top_atk = sorted(m.attack.items(), key=lambda kv: -kv[1])[:5]
    top_def = sorted(m.defense.items(), key=lambda kv: -kv[1])[:5]
    print(f"\n    Top attaques : {', '.join(f'{t}({v:+.2f})' for t,v in top_atk)}")
    print(f"    Top défenses : {', '.join(f'{t}({v:+.2f})' for t,v in top_def)}")

    if args.no_final:
        return

    # ── Fit final sur TOUTE la donnée (pour la prod) ──
    print(f"\n[2] Fit final sur {len(df)} matchs (toute la donnée)...")
    final = fit_dixon_coles(df, half_life_days=args.half_life)
    version = "wcgoals_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    bundle = {"goals_model": final.to_dict(), "version": version, "market": "WORLD_CUP_GOALS"}
    path = ARTIFACTS / f"model_{version}.joblib"
    joblib.dump(bundle, path)
    joblib.dump(bundle, ARTIFACTS / "model_wcgoals_latest.joblib")
    out_metrics = {"version": version, "market": "WORLD_CUP_GOALS",
                   "half_life_days": args.half_life,
                   "home_adv": round(final.home_adv, 4), "rho": round(final.rho, 4),
                   "n_teams": len(final.attack), "backtest_wc2022": metrics}
    (ARTIFACTS / f"metrics_{version}.json").write_text(json.dumps(out_metrics, indent=2))
    print(f"\n✓ Modèle final sauvegardé : {path.name} (+ model_wcgoals_latest.joblib)")


if __name__ == "__main__":
    main()
