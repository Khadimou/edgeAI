"""
Pipeline Coupe du Monde : build features + train + eval out-of-sample sur WC 2022.

Train set : tous les matchs internationaux pré-2022 (~48k matchs)
Eval set  : WC 2022 (64 matchs, out-of-sample strict)

Usage:
    python wc_pipeline.py
    python wc_pipeline.py --tune --n-trials 30
"""
import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import joblib
from sklearn.calibration import CalibratedClassifierCV
from sklearn.metrics import log_loss, accuracy_score, brier_score_loss
from xgboost import XGBClassifier

sys.path.insert(0, str(Path(__file__).parent))
from pipeline.wc_features import (
    WCFeatures, compute_wc_features, init_elo_dict, update_elo
)

DATA_DIR = Path(__file__).parent / "data"
INPUT = DATA_DIR / "raw" / "international_matches.csv"
OUTPUT_FEAT = DATA_DIR / "features" / "wc_dataset.csv"
ARTIFACTS_DIR = Path(__file__).parent / "artifacts" / "models"
ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)


# ──────────────────────────────────────────────────────────
# Build features dataset
# ──────────────────────────────────────────────────────────

def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Pour chaque match (chronologique), calcule features + ELO,
    update ELO après chaque match.
    """
    print("\n[1/3] Build features (chronologique, avec ELO update)...")
    df = df.sort_values("date").reset_index(drop=True)
    elo = init_elo_dict()

    feature_rows = []
    feature_cols = WCFeatures.feature_names()

    for i, row in df.iterrows():
        # Calcule features SUR HISTORIQUE (= avant ce match)
        # past = df.iloc[:i]  # plus simple : on a pas besoin du dataframe entier ici
        # Pour la perf, on passe le slice — au lieu de filter par date
        past = df.iloc[:i]
        match_date = pd.Timestamp(row["date"])

        # is_home_country = True si home_team = country == match country (data dispo)
        is_home_country = row.get("country", "") == row["home_team"] or row.get("neutral", False) == False

        feat = compute_wc_features(
            row["home_team"], row["away_team"],
            match_date, past, elo,
            is_home_country=is_home_country,
        )

        d = dict(zip(feature_cols, feat.to_array()))

        # Label : 0=HOME win, 1=DRAW, 2=AWAY win
        if row["home_score"] > row["away_score"]:
            label = 0
        elif row["home_score"] == row["away_score"]:
            label = 1
        else:
            label = 2

        d["label"] = label
        d["date"] = row["date"]
        d["home_team"] = row["home_team"]
        d["away_team"] = row["away_team"]
        d["home_score"] = row["home_score"]
        d["away_score"] = row["away_score"]
        d["tournament"] = row["tournament"]
        d["is_wc"] = row["is_wc"]
        feature_rows.append(d)

        # Update ELO APRÈS calcul features
        update_elo(elo, row["home_team"], row["away_team"],
                   int(row["home_score"]), int(row["away_score"]),
                   is_wc=bool(row["is_wc"]))

        if (i + 1) % 5000 == 0:
            print(f"  {i+1}/{len(df)} matchs traités... (ELO uniques: {len(elo)})")

    result = pd.DataFrame(feature_rows)
    print(f"\n  → {len(result)} matchs avec features")
    OUTPUT_FEAT.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(OUTPUT_FEAT, index=False)
    print(f"  Sauvegardé : {OUTPUT_FEAT}")
    return result, elo


# ──────────────────────────────────────────────────────────
# Train + eval
# ──────────────────────────────────────────────────────────

def train_and_eval(df: pd.DataFrame, tune: bool = False, n_trials: int = 30):
    """
    Train sur tous les matchs PRÉ-2022, eval sur WC 2022.
    Échantillon training : on garde uniquement matchs "compétitifs"
    (pas les amicaux pour éviter le bruit) — environ 30k matchs.
    """
    feature_cols = WCFeatures.feature_names()
    df["date"] = pd.to_datetime(df["date"])

    # Filter pour le training : matchs "sérieux" (pas amicaux) avec assez de données ELO
    # On évite les premiers matchs (~1900) où ELO converge encore
    train_mask = (
        (df["date"] >= "1990-01-01") &
        (df["date"] < "2022-11-01") &  # avant WC 2022
        (df["tournament"] != "Friendly")
    )
    train_df = df[train_mask].sort_values("date").reset_index(drop=True)

    # Eval set : WC 2022 strict
    eval_mask = (df["tournament"] == "FIFA World Cup") & (df["date"] >= "2022-11-01") & (df["date"] < "2023-01-01")
    eval_df = df[eval_mask].sort_values("date").reset_index(drop=True)

    print(f"\n[2/3] Train/Eval split :")
    print(f"  Train : {len(train_df)} matchs (1990-2022, hors amicaux)")
    print(f"  Eval  : {len(eval_df)} matchs (WC 2022)")
    print(f"  Train label distribution : {dict(zip(['HOME','DRAW','AWAY'], np.bincount(train_df['label'])))}")
    print(f"  Eval label distribution  : {dict(zip(['HOME','DRAW','AWAY'], np.bincount(eval_df['label'])))}")

    X_train = train_df[feature_cols].values.astype(np.float32)
    y_train = train_df["label"].values.astype(int)
    X_eval = eval_df[feature_cols].values.astype(np.float32)
    y_eval = eval_df["label"].values.astype(int)

    params = {
        "n_estimators": 300, "max_depth": 4, "learning_rate": 0.05,
        "subsample": 0.8, "colsample_bytree": 0.8,
        "objective": "multi:softprob", "num_class": 3,
        "eval_metric": "mlogloss",
        "random_state": 42, "n_jobs": -1,
    }

    if tune:
        print(f"\n  Optuna tuning ({n_trials} trials)...")
        import optuna
        from sklearn.model_selection import TimeSeriesSplit
        optuna.logging.set_verbosity(optuna.logging.WARNING)
        tscv = TimeSeriesSplit(n_splits=5)

        def objective(trial):
            p = {
                "n_estimators": trial.suggest_int("n_estimators", 150, 500),
                "max_depth": trial.suggest_int("max_depth", 3, 6),
                "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.15, log=True),
                "subsample": trial.suggest_float("subsample", 0.6, 1.0),
                "colsample_bytree": trial.suggest_float("colsample_bytree", 0.6, 1.0),
                "min_child_weight": trial.suggest_int("min_child_weight", 1, 10),
                "reg_alpha": trial.suggest_float("reg_alpha", 0.0, 1.0),
                "reg_lambda": trial.suggest_float("reg_lambda", 0.5, 2.0),
                "objective": "multi:softprob", "num_class": 3,
                "eval_metric": "mlogloss", "random_state": 42, "n_jobs": -1,
            }
            losses = []
            for ti, vi in tscv.split(X_train):
                clf = CalibratedClassifierCV(XGBClassifier(**p), method="sigmoid", cv=3)
                clf.fit(X_train[ti], y_train[ti])
                losses.append(log_loss(y_train[vi], clf.predict_proba(X_train[vi])))
            return float(np.mean(losses))

        study = optuna.create_study(direction="minimize")
        study.optimize(objective, n_trials=n_trials, show_progress_bar=False)
        params = {**params, **study.best_params,
                  "objective": "multi:softprob", "num_class": 3,
                  "eval_metric": "mlogloss", "random_state": 42, "n_jobs": -1}
        print(f"  Best CV log-loss : {study.best_value:.4f}")

    # Train final
    print(f"\n[3/3] Entraînement final + eval...")
    model = CalibratedClassifierCV(XGBClassifier(**params), method="sigmoid", cv=3)
    model.fit(X_train, y_train)

    # Eval out-of-sample
    proba_eval = model.predict_proba(X_eval)
    preds_eval = proba_eval.argmax(axis=1)
    eval_acc = accuracy_score(y_eval, preds_eval)
    eval_ll = log_loss(y_eval, proba_eval)
    eval_brier = brier_score_loss((y_eval == 0).astype(int), proba_eval[:, 0])

    # Baseline naïve = toujours prédire HOME
    naive_acc = (y_eval == 0).mean()

    print(f"\n  === Eval WC 2022 ({len(y_eval)} matchs) ===")
    print(f"    Accuracy        : {eval_acc:.4f} ({eval_acc*100:.1f}%)")
    print(f"    Baseline naïve  : {naive_acc:.4f} ({naive_acc*100:.1f}%) (toujours HOME)")
    print(f"    Log-loss        : {eval_ll:.4f}")
    print(f"    Brier score     : {eval_brier:.4f}")

    # Verdict
    if eval_acc > naive_acc + 0.05:
        verdict = "✅ Bat la baseline (>5 points)"
    elif eval_acc > naive_acc:
        verdict = "🟡 Légèrement meilleur que baseline"
    else:
        verdict = "❌ Ne bat pas la baseline"
    print(f"    Verdict         : {verdict}")

    # Save
    version = "wc_" + datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    metrics = {
        "version": version,
        "market": "WORLD_CUP_1X2",
        "train_n": len(train_df),
        "eval_n": len(eval_df),
        "eval_accuracy": round(float(eval_acc), 4),
        "eval_log_loss": round(float(eval_ll), 4),
        "eval_brier_score": round(float(eval_brier), 4),
        "baseline_naive_acc": round(float(naive_acc), 4),
        "improvement_pts": round(float(eval_acc - naive_acc) * 100, 2),
        "verdict": verdict,
    }
    if tune:
        metrics["best_params"] = params

    path = ARTIFACTS_DIR / f"model_{version}.joblib"
    joblib.dump({"model": model, "version": version, "market": "WORLD_CUP_1X2"}, path)
    (ARTIFACTS_DIR / f"metrics_{version}.json").write_text(json.dumps(metrics, indent=2))
    shutil.copy2(path, ARTIFACTS_DIR / "model_wc_latest.joblib")
    print(f"\n  Saved : {path.name}")

    return model, metrics


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tune", action="store_true")
    parser.add_argument("--n-trials", type=int, default=30)
    parser.add_argument("--skip-build", action="store_true",
                        help="Skip feature build (use existing wc_dataset.csv)")
    args = parser.parse_args()

    print("─" * 60)
    print("Coupe du Monde — pipeline ML")
    print("─" * 60)

    if not args.skip_build:
        if not INPUT.exists():
            print(f"ERREUR : {INPUT} introuvable. Lance wc_collect_data.py d'abord.")
            sys.exit(1)
        df = pd.read_csv(INPUT, parse_dates=["date"])
        df["is_wc"] = df["is_wc"].astype(bool)
        result, elo = build_features(df)
    else:
        if not OUTPUT_FEAT.exists():
            print(f"ERREUR : {OUTPUT_FEAT} introuvable. Lance sans --skip-build.")
            sys.exit(1)
        result = pd.read_csv(OUTPUT_FEAT, parse_dates=["date"])
        result["is_wc"] = result["is_wc"].astype(bool)
        print(f"\n[1/3] Skipped feature build, loaded {len(result)} from {OUTPUT_FEAT}")

    model, metrics = train_and_eval(result, tune=args.tune, n_trials=args.n_trials)
