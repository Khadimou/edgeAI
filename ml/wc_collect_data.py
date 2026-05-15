"""
Collecte des matchs internationaux historiques (foot équipes nationales).

Source : github.com/martj42/international_results — ~50 000 matchs depuis 1872.
On garde tout (incl. amicaux + qualifications) car ça donne du contexte pour les
features (forme récente, ELO, H2H), mais on label les matchs de WC séparément.

Usage:
    python wc_collect_data.py
"""
import sys
from io import StringIO
from pathlib import Path

import httpx
import pandas as pd

DATA_DIR = Path(__file__).parent / "data" / "raw"
DATA_DIR.mkdir(parents=True, exist_ok=True)

URL = "https://raw.githubusercontent.com/martj42/international_results/master/results.csv"


def main():
    print(f"Fetching {URL}...")
    r = httpx.get(URL, timeout=60, follow_redirects=True)
    r.raise_for_status()
    df = pd.read_csv(StringIO(r.text))
    print(f"  → {len(df)} matchs bruts")

    # Normalisation colonnes
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date", "home_team", "away_team", "home_score", "away_score"])
    df["home_score"] = df["home_score"].astype(int)
    df["away_score"] = df["away_score"].astype(int)

    # On garde tout, mais on isole les matchs WC pour le tag
    df["is_wc"] = df["tournament"] == "FIFA World Cup"
    df["is_wc_qualifier"] = df["tournament"].str.contains("World Cup qualification", na=False, regex=False)
    df["is_friendly"] = df["tournament"] == "Friendly"

    # Stats récapitulatives
    n_total = len(df)
    n_wc = df["is_wc"].sum()
    n_wcq = df["is_wc_qualifier"].sum()
    n_friendly = df["is_friendly"].sum()

    print(f"\n  Total matchs : {n_total}")
    print(f"  - WC matchs : {n_wc}")
    print(f"  - WC qualifs : {n_wcq}")
    print(f"  - Amicaux : {n_friendly}")
    print(f"  - Autres compétitions : {n_total - n_wc - n_wcq - n_friendly}")

    # Distribution des résultats WC
    wc_only = df[df["is_wc"]].copy()
    home_wins = (wc_only["home_score"] > wc_only["away_score"]).sum()
    draws = (wc_only["home_score"] == wc_only["away_score"]).sum()
    away_wins = (wc_only["home_score"] < wc_only["away_score"]).sum()
    print(f"\n  Distribution WC (home perspective) :")
    print(f"    Home wins : {home_wins} ({100*home_wins/n_wc:.1f}%)")
    print(f"    Draws     : {draws} ({100*draws/n_wc:.1f}%)")
    print(f"    Away wins : {away_wins} ({100*away_wins/n_wc:.1f}%)")
    print(f"\n  Note : 'home' au WC est l'équipe la plus haute dans la table, pas vraiment domicile.")

    # WC par édition
    wc_only["year"] = wc_only["date"].dt.year
    print(f"\n  WC par édition (5 derniers) :")
    by_year = wc_only["year"].value_counts().sort_index().tail(8)
    for year, count in by_year.items():
        print(f"    {year} : {count} matchs")

    output = DATA_DIR / "international_matches.csv"
    df.to_csv(output, index=False)
    print(f"\n✓ Sauvegardé : {output}")
    print(f"  Période : {df['date'].min().date()} → {df['date'].max().date()}")


if __name__ == "__main__":
    main()
