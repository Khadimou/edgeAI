"""
Collecte des matchs ATP historiques + cotes de clôture.

Sources :
1. Jeff Sackmann tennis_atp (github) : matchs + stats détaillées 1968-présent
2. tennis-data.co.uk : cotes Pinnacle + Bet365 closing 2000-présent

Output : data/raw/atp_matches.csv (avec cotes mergées)

Usage:
    python tennis_collect_data.py
    python tennis_collect_data.py --start-year 2010 --end-year 2024
"""
import argparse
import re
import sys
from io import BytesIO, StringIO
from pathlib import Path

import httpx
import pandas as pd

DATA_DIR = Path(__file__).parent / "data" / "raw"
DATA_DIR.mkdir(parents=True, exist_ok=True)

JEFF_URL = "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master/atp_matches_{year}.csv"
TD_URL = "http://www.tennis-data.co.uk/{year}/{year}.xlsx"


# ──────────────────────────────────────────────────────────
# Jeff Sackmann ATP matches (stats détaillées, no odds)
# ──────────────────────────────────────────────────────────

def fetch_jeff_year(year: int) -> pd.DataFrame:
    url = JEFF_URL.format(year=year)
    try:
        r = httpx.get(url, timeout=30, follow_redirects=True)
        r.raise_for_status()
        df = pd.read_csv(StringIO(r.text))
        return df
    except Exception as e:
        print(f"  ! Jeff {year}: {e}")
        return pd.DataFrame()


def fetch_all_jeff(start: int, end: int) -> pd.DataFrame:
    print(f"\n[1/3] Fetch Jeff Sackmann ATP {start}-{end}...")
    dfs = []
    for y in range(start, end + 1):
        print(f"  {y}...", end=" ", flush=True)
        df = fetch_jeff_year(y)
        if not df.empty:
            df["match_year"] = y
            print(f"{len(df)} matchs")
            dfs.append(df)
        else:
            print("vide")
    if not dfs:
        return pd.DataFrame()
    out = pd.concat(dfs, ignore_index=True)
    # Date format YYYYMMDD → datetime
    out["match_date"] = pd.to_datetime(out["tourney_date"].astype(str), format="%Y%m%d", errors="coerce")
    out = out.dropna(subset=["match_date", "winner_name", "loser_name"])
    return out


# ──────────────────────────────────────────────────────────
# tennis-data.co.uk (Pinnacle/Bet365 closing odds)
# ──────────────────────────────────────────────────────────

def fetch_td_year(year: int) -> pd.DataFrame:
    url = TD_URL.format(year=year)
    try:
        r = httpx.get(url, timeout=30, follow_redirects=True)
        r.raise_for_status()
        df = pd.read_excel(BytesIO(r.content))
        return df
    except Exception as e:
        print(f"  ! TD {year}: {e}")
        return pd.DataFrame()


def fetch_all_td(start: int, end: int) -> pd.DataFrame:
    print(f"\n[2/3] Fetch tennis-data.co.uk {start}-{end}...")
    dfs = []
    for y in range(start, end + 1):
        print(f"  {y}...", end=" ", flush=True)
        df = fetch_td_year(y)
        if not df.empty:
            print(f"{len(df)} matchs")
            dfs.append(df)
        else:
            print("vide")
    if not dfs:
        return pd.DataFrame()
    return pd.concat(dfs, ignore_index=True)


# ──────────────────────────────────────────────────────────
# Merge : Jeff (stats) + tennis-data (odds)
# Stratégie : match par (date, surname_winner, surname_loser)
# ──────────────────────────────────────────────────────────

def surname(name: str) -> str:
    """Roger Federer → 'Federer'. Federer R. → 'Federer'."""
    if not name or pd.isna(name):
        return ""
    # Strip dots, normalize
    s = str(name).strip().replace(".", "")
    # Tennis-data format: "Federer R" or "Federer R."
    # Jeff format: "Roger Federer"
    parts = s.split()
    if not parts:
        return ""
    # If multiple parts, take the longest (usually surname)
    # But for "Federer R", first is surname
    if len(parts) == 1:
        return parts[0].lower()
    # Detect tennis-data format (last part is single letter = initial)
    if len(parts[-1]) <= 2:
        return parts[0].lower()  # surname first
    return parts[-1].lower()  # surname last (Jeff format)


def normalize_td(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["match_date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["match_date", "Winner", "Loser"])
    df["winner_surname"] = df["Winner"].apply(surname)
    df["loser_surname"] = df["Loser"].apply(surname)
    # Choisir les meilleures cotes dispo
    def pick_odds(row, side):
        for col in [f"PS{side}", f"B365{side}", f"Avg{side}"]:
            if col in row.index and pd.notna(row[col]):
                try:
                    v = float(row[col])
                    if v > 1:
                        return v
                except (ValueError, TypeError):
                    pass
        return None
    df["odds_winner"] = df.apply(lambda r: pick_odds(r, "W"), axis=1)
    df["odds_loser"] = df.apply(lambda r: pick_odds(r, "L"), axis=1)
    return df[["match_date", "winner_surname", "loser_surname",
               "odds_winner", "odds_loser", "Surface", "Best of", "Round"]]


def merge_data(jeff: pd.DataFrame, td: pd.DataFrame) -> pd.DataFrame:
    print(f"\n[3/3] Merge Jeff ({len(jeff)}) + tennis-data ({len(td)})...")
    j = jeff.copy()
    j["winner_surname"] = j["winner_name"].apply(surname)
    j["loser_surname"] = j["loser_name"].apply(surname)

    td_norm = normalize_td(td)
    # Merge sur (match_date, winner_surname, loser_surname)
    merged = j.merge(
        td_norm,
        on=["match_date", "winner_surname", "loser_surname"],
        how="left",
    )
    n_with_odds = merged["odds_winner"].notna().sum()
    print(f"  → {len(merged)} matchs, {n_with_odds} avec cotes ({100*n_with_odds/len(merged):.1f}%)")
    return merged


# ──────────────────────────────────────────────────────────

def main(start: int, end: int):
    jeff = fetch_all_jeff(start, end)
    if jeff.empty:
        print("ERREUR : aucune data Jeff Sackmann"); sys.exit(1)
    print(f"\n✓ Jeff Sackmann : {len(jeff)} matchs")

    td = fetch_all_td(start, end)
    if td.empty:
        print("⚠ Pas de tennis-data odds")
        merged = jeff.copy()
        merged["odds_winner"] = None
        merged["odds_loser"] = None
    else:
        merged = merge_data(jeff, td)

    # Garde colonnes utiles + outcome target
    keep = [
        "match_date", "match_year",
        "tourney_name", "surface", "tourney_level", "best_of", "round",
        "winner_name", "loser_name",
        "winner_id", "loser_id",
        "winner_rank", "loser_rank",
        "winner_rank_points", "loser_rank_points",
        "winner_age", "loser_age",
        "winner_hand", "loser_hand",
        "winner_ht", "loser_ht",
        "odds_winner", "odds_loser",
        "minutes",
        # Stats sets
        "w_ace", "w_df", "w_svpt", "w_1stWon", "w_2ndWon", "w_bpSaved", "w_bpFaced",
        "l_ace", "l_df", "l_svpt", "l_1stWon", "l_2ndWon", "l_bpSaved", "l_bpFaced",
    ]
    keep = [c for c in keep if c in merged.columns]
    out = merged[keep].copy()
    out = out.sort_values("match_date").reset_index(drop=True)

    output = DATA_DIR / "atp_matches.csv"
    out.to_csv(output, index=False)
    print(f"\n✓ Total : {len(out)} matchs ATP")
    print(f"  Période : {out['match_date'].min().date()} → {out['match_date'].max().date()}")
    print(f"  Avec cotes : {out['odds_winner'].notna().sum()} ({100*out['odds_winner'].notna().mean():.1f}%)")
    print(f"  Surfaces : {out['surface'].value_counts().to_dict()}")
    print(f"\n✓ Sauvegardé : {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-year", type=int, default=2010)
    parser.add_argument("--end-year", type=int, default=2024)
    args = parser.parse_args()
    main(args.start_year, args.end_year)
