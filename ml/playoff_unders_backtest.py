"""
Backtest de la thèse "parier Under sur les totals de playoffs NBA est rentable".

Hypothèse à tester : les matchs de playoffs scorent moins (défenses serrées,
rythme lent) donc parier systématiquement Under bat le marché.

Contre-hypothèse (la mienne) : le marché le sait et baisse déjà les lignes en
playoffs, donc l'edge est déjà pricé → ROI proche de 0.

Méthode :
- Charge les totals NBA (closing lines) de toutes les saisons dispo (SBR)
- Identifie les matchs de playoffs par heuristique de date (après ~15 avril)
- Backteste : parier Under sur CHAQUE match de playoffs à la closing line
- Compare playoffs vs saison régulière pour isoler l'effet
- Cotes standard Over/Under : -110 = 1.909 (4.5% de marge bookmaker)

Usage (dans un container ml_worker éphémère, pas besoin de DB) :
    docker compose -f docker-compose.yml -f docker-compose.prod.yml \\
        run --rm ml_worker python playoff_unders_backtest.py

    # Pour limiter aux saisons récentes :
    docker compose ... run --rm ml_worker python playoff_unders_backtest.py \\
        --seasons 2022-23,2023-24,2024-25
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from nba_totals_pipeline import fetch_totals_for_season

# Cote standard Over/Under aux US : -110 chaque côté = decimal 1.909
ODDS = 1.909

# Heuristique playoffs : la saison régulière NBA finit ~mi-avril.
# Tout match à partir du 14 avril (inclut play-in tournament) = playoffs.
PLAYOFF_MONTH = 4
PLAYOFF_DAY = 14


def _is_playoff(match_date: pd.Timestamp) -> bool:
    """True si le match tombe en période playoffs (après ~14 avril)."""
    if match_date.month > PLAYOFF_MONTH:  # mai, juin
        return True
    if match_date.month == PLAYOFF_MONTH and match_date.day >= PLAYOFF_DAY:
        return True
    return False


def _backtest_side(df: pd.DataFrame, side: str) -> dict:
    """Backteste un pari systématique (side='UNDER' ou 'OVER') à 1 unité/pari.

    total_over = 1 si total_actual > closing_line (Over gagne), 0 sinon.
    Push (total == ligne) : on rembourse (P&L 0).
    """
    n = len(df)
    if n == 0:
        return {"n": 0, "wins": 0, "pushes": 0, "roi": 0.0, "pnl": 0.0, "hit": 0.0}

    wins = pushes = 0
    pnl = 0.0
    for _, r in df.iterrows():
        total = r["total_actual"]
        line = r["closing_total"]
        if total == line:
            pushes += 1
            continue  # remboursé, P&L 0
        over_won = total > line
        bet_won = (side == "OVER" and over_won) or (side == "UNDER" and not over_won)
        if bet_won:
            wins += 1
            pnl += (ODDS - 1)  # gain net
        else:
            pnl -= 1  # mise perdue
    decided = n - pushes
    staked = decided  # 1 unité par pari décidé
    roi = (pnl / staked * 100) if staked > 0 else 0.0
    hit = (wins / decided) if decided > 0 else 0.0
    return {
        "n": n, "decided": decided, "wins": wins, "pushes": pushes,
        "roi": round(roi, 2), "pnl": round(pnl, 2), "hit": round(hit * 100, 1),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--seasons", type=str,
        default="2020-21,2021-22,2022-23,2023-24,2024-25",
        help="Saisons à tester (séparées par virgule). Les 404 sont ignorées.",
    )
    args = parser.parse_args()
    seasons = [s.strip() for s in args.seasons.split(",") if s.strip()]

    print(f"⚙ Backtest unders playoffs — saisons demandées : {seasons}\n")

    # 1. Fetch toutes les saisons (skip celles qui 404 ou sont vides)
    dfs = []
    for s in seasons:
        df = fetch_totals_for_season(s)
        if df.empty:
            print(f"  ⚠ {s} : aucune donnée (404 ou archive indisponible)")
            continue
        df["season"] = s
        dfs.append(df)
    if not dfs:
        print("\n❌ Aucune donnée récupérée. SBR ne couvre peut-être que jusqu'à 2022-23.")
        return
    data = pd.concat(dfs, ignore_index=True)
    data["match_date"] = pd.to_datetime(data["match_date"])
    data["is_playoff"] = data["match_date"].apply(_is_playoff)

    playoffs = data[data["is_playoff"]]
    regular = data[~data["is_playoff"]]

    print("\n" + "=" * 70)
    print("RÉSULTATS GLOBAUX (toutes saisons confondues)")
    print("=" * 70)
    print(f"Total matchs : {len(data)}  |  Playoffs : {len(playoffs)}  |  Saison rég. : {len(regular)}")
    print(f"Ligne moyenne playoffs : {playoffs['closing_total'].mean():.1f}  "
          f"vs saison rég. : {regular['closing_total'].mean():.1f}")
    print(f"Total points moyen playoffs : {playoffs['total_actual'].mean():.1f}  "
          f"vs saison rég. : {regular['total_actual'].mean():.1f}")

    print("\n── Stratégie : UNDER systématique ──")
    print(f"{'Segment':<20} | {'N':>5} | {'Hit%':>6} | {'ROI%':>7} | {'P&L (u)':>8}")
    print("-" * 60)
    for label, subset in [("Playoffs", playoffs), ("Saison régulière", regular), ("Tout", data)]:
        r = _backtest_side(subset, "UNDER")
        print(f"{label:<20} | {r['decided']:>5} | {r['hit']:>5.1f}% | {r['roi']:>+6.2f}% | {r['pnl']:>+7.1f}")

    print("\n── Pour comparaison : OVER systématique (playoffs) ──")
    r_over = _backtest_side(playoffs, "OVER")
    print(f"{'Playoffs OVER':<20} | {r_over['decided']:>5} | {r_over['hit']:>5.1f}% | {r_over['roi']:>+6.2f}% | {r_over['pnl']:>+7.1f}")

    # Détail par saison (playoffs unders)
    print("\n" + "=" * 70)
    print("UNDERS PLAYOFFS — détail par saison")
    print("=" * 70)
    print(f"{'Saison':<10} | {'N':>4} | {'Hit%':>6} | {'ROI%':>7} | {'Ligne moy':>9}")
    print("-" * 50)
    for s in seasons:
        sub = playoffs[playoffs["season"] == s]
        if sub.empty:
            continue
        r = _backtest_side(sub, "UNDER")
        print(f"{s:<10} | {r['decided']:>4} | {r['hit']:>5.1f}% | {r['roi']:>+6.2f}% | {sub['closing_total'].mean():>9.1f}")

    # Verdict automatique
    print("\n" + "=" * 70)
    print("VERDICT")
    print("=" * 70)
    po = _backtest_side(playoffs, "UNDER")
    breakeven = (1 / ODDS) * 100  # hit rate nécessaire pour break-even (~52.4%)
    print(f"Hit rate break-even (vig inclus) : {breakeven:.1f}%")
    print(f"Hit rate unders playoffs observé : {po['hit']:.1f}%")
    if po["decided"] < 50:
        print(f"\n⚠ Échantillon trop petit ({po['decided']} paris) pour conclure.")
    elif po["roi"] > 3:
        print(f"\n✅ Les unders playoffs SEMBLENT rentables (ROI {po['roi']:+.2f}% sur {po['decided']} paris).")
        print("   → Ta thèse tient. À confirmer sur plus de données + CLV.")
    elif po["roi"] > -2:
        print(f"\n➖ Les unders playoffs sont à l'équilibre (ROI {po['roi']:+.2f}%).")
        print("   → Le marché a déjà pricé l'effet playoff. Pas d'edge sur la simple")
        print("     stratégie 'under systématique' — l'edge éventuel est dans la SÉLECTION")
        print("     des matchs, pas dans le pattern global.")
    else:
        print(f"\n❌ Les unders playoffs PERDENT (ROI {po['roi']:+.2f}%).")
        print("   → Le marché sur-corrige même. Parier under aveuglément coûte de l'argent.")


if __name__ == "__main__":
    main()
