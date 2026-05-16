"""
Backfill des stats offensives (shots, SOT, corners, fouls) depuis football-data.co.uk.

Source : http://www.football-data.co.uk/mmz4281/{season}/{league}.csv
Colonnes utiles :
  HS  / AS  = home/away shots
  HST / AST = home/away shots on target
  HC  / AC  = home/away corners
  HF  / AF  = home/away fouls

Pour chaque match du CSV, on cherche le match correspondant en DB par
(match_date, home_team, away_team) après mapping des noms (TEAM_NAME_MAP)
et on UPDATE les colonnes home_shots, away_shots, etc.

Usage :
    python collect_shots.py                    # backfill toutes saisons 2015-2026
    python collect_shots.py --start 2122       # seulement à partir d'une saison
    python collect_shots.py --recent           # seulement la saison en cours (rapide, daily)
"""
import argparse
import asyncio
import os
import sys
from datetime import datetime
from io import StringIO
from pathlib import Path

import httpx
import pandas as pd
import structlog
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).parent))
from backtest import LEAGUE_FD_CO_UK, TEAM_NAME_MAP

log = structlog.get_logger()

# Toutes les saisons disponibles 2015-2025 (au format fdco: "1516" = saison 2015/16)
ALL_SEASONS = [f"{y%100:02d}{(y+1)%100:02d}" for y in range(2015, 2026)]


def fetch_season(league_code: str, season: str) -> pd.DataFrame:
    """Fetch un CSV football-data.co.uk pour une (ligue, saison) donnée."""
    url = f"https://www.football-data.co.uk/mmz4281/{season}/{league_code}.csv"
    try:
        r = httpx.get(url, timeout=30, follow_redirects=True)
        r.raise_for_status()
        df = pd.read_csv(StringIO(r.text))
        # Colonnes requises
        cols_needed = ["Date", "HomeTeam", "AwayTeam"]
        if not all(c in df.columns for c in cols_needed):
            return pd.DataFrame()
        # Garde uniquement les colonnes de stats
        keep = cols_needed + [c for c in ["HS", "AS", "HST", "AST", "HC", "AC", "HF", "AF"]
                              if c in df.columns]
        df = df[keep].copy()
        df["Date"] = pd.to_datetime(df["Date"], dayfirst=True, errors="coerce")
        df = df.dropna(subset=["Date", "HomeTeam", "AwayTeam"])
        return df
    except Exception as e:
        log.warning("fetch_season_failed", league=league_code, season=season, error=str(e))
        return pd.DataFrame()


def normalize_team(name: str) -> str:
    if pd.isna(name):
        return ""
    return TEAM_NAME_MAP.get(str(name).strip(), str(name).strip())


def fetch_all(seasons: list[str]) -> pd.DataFrame:
    """Fetch toutes les (ligue × saison) demandées + concat + map team names."""
    dfs = []
    for league_name, code in LEAGUE_FD_CO_UK.items():
        for season in seasons:
            log.info("fetching", league=league_name, season=season)
            df = fetch_season(code, season)
            if df.empty:
                continue
            df["league"] = league_name
            dfs.append(df)
    if not dfs:
        return pd.DataFrame()
    out = pd.concat(dfs, ignore_index=True)
    out["home_team_mapped"] = out["HomeTeam"].apply(normalize_team)
    out["away_team_mapped"] = out["AwayTeam"].apply(normalize_team)
    return out


def build_db_url(raw: str) -> str:
    url = raw.split("?")[0]
    url = url.replace("postgresql://", "postgresql+asyncpg://")
    url = url.replace("postgres://", "postgresql+asyncpg://")
    return url


async def update_db(df: pd.DataFrame) -> dict:
    """UPDATE chaque row de la DB par (date, home, away) avec les stats fdco."""
    if df.empty:
        return {"updated": 0, "no_match": 0}

    raw = os.environ["DATABASE_URL"]
    db_url = build_db_url(raw)
    connect_args = {"ssl": True} if "sslmode=require" in raw else {}
    engine = create_async_engine(db_url, connect_args=connect_args, pool_pre_ping=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    n_updated = 0
    n_no_match = 0

    async with Session() as session:
        for _, row in df.iterrows():
            date_only = row["Date"].date()
            params = {
                "date": date_only,
                "home": row["home_team_mapped"],
                "away": row["away_team_mapped"],
                "hs": int(row["HS"]) if pd.notna(row.get("HS")) else None,
                "as_": int(row["AS"]) if pd.notna(row.get("AS")) else None,
                "hst": int(row["HST"]) if pd.notna(row.get("HST")) else None,
                "ast": int(row["AST"]) if pd.notna(row.get("AST")) else None,
                "hc": int(row["HC"]) if pd.notna(row.get("HC")) else None,
                "ac": int(row["AC"]) if pd.notna(row.get("AC")) else None,
                "hf": int(row["HF"]) if pd.notna(row.get("HF")) else None,
                "af": int(row["AF"]) if pd.notna(row.get("AF")) else None,
            }
            # Skip rows sans aucune stat (CSV incomplet pour cette ligue/saison)
            if not any(params[k] is not None for k in ["hs", "as_", "hst", "ast"]):
                continue
            result = await session.execute(text("""
                UPDATE matches
                SET home_shots = COALESCE(:hs, home_shots),
                    away_shots = COALESCE(:as_, away_shots),
                    home_shots_on_target = COALESCE(:hst, home_shots_on_target),
                    away_shots_on_target = COALESCE(:ast, away_shots_on_target),
                    home_corners = COALESCE(:hc, home_corners),
                    away_corners = COALESCE(:ac, away_corners),
                    home_fouls = COALESCE(:hf, home_fouls),
                    away_fouls = COALESCE(:af, away_fouls)
                WHERE DATE(match_date) = :date
                  AND home_team = :home
                  AND away_team = :away
            """), params)
            if result.rowcount > 0:
                n_updated += result.rowcount
            else:
                n_no_match += 1
        await session.commit()

    await engine.dispose()
    return {"updated": n_updated, "no_match": n_no_match}


def update_local_csv(df: pd.DataFrame, csv_path: Path) -> dict:
    """Merge shots data dans un CSV local matches.csv (utilisé par build_features.py)."""
    if not csv_path.exists():
        log.warning("local_csv_not_found", path=str(csv_path))
        return {"updated": 0, "no_match": 0}
    local = pd.read_csv(csv_path, parse_dates=["match_date"])
    local["_date"] = pd.to_datetime(local["match_date"]).dt.date

    fdco = df.copy()
    fdco["_date"] = fdco["Date"].dt.date

    # Index fdco par (date, home, away) pour lookup
    fdco_idx = fdco.set_index(["_date", "home_team_mapped", "away_team_mapped"])

    # Init colonnes si absentes
    for col in ["home_shots", "away_shots", "home_shots_on_target", "away_shots_on_target",
                "home_corners", "away_corners", "home_fouls", "away_fouls"]:
        if col not in local.columns:
            local[col] = pd.NA

    n_updated = 0
    for i, row in local.iterrows():
        key = (row["_date"], row["home_team"], row["away_team"])
        if key not in fdco_idx.index:
            continue
        match = fdco_idx.loc[key]
        if isinstance(match, pd.DataFrame):
            match = match.iloc[0]
        col_map = {
            "HS": "home_shots", "AS": "away_shots",
            "HST": "home_shots_on_target", "AST": "away_shots_on_target",
            "HC": "home_corners", "AC": "away_corners",
            "HF": "home_fouls", "AF": "away_fouls",
        }
        any_set = False
        for src, dst in col_map.items():
            if src in match.index and pd.notna(match[src]):
                local.at[i, dst] = int(match[src])
                any_set = True
        if any_set:
            n_updated += 1

    local = local.drop(columns=["_date"])
    local.to_csv(csv_path, index=False)
    return {"updated": n_updated, "total_rows": len(local)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=str, default=None,
                        help="Code saison de départ (ex: '2122' pour 2021/22+). Défaut: toutes.")
    parser.add_argument("--recent", action="store_true",
                        help="Ne fetch que les 2 dernières saisons (rapide, daily backfill).")
    parser.add_argument("--target", choices=["db", "csv", "both"], default="both",
                        help="Cible du backfill : DB seule, CSV local seul, ou les deux.")
    parser.add_argument("--csv-path", type=str, default="data/raw/matches.csv",
                        help="Chemin du CSV local à mettre à jour (relatif à ml/).")
    args = parser.parse_args()

    if args.recent:
        seasons = ALL_SEASONS[-2:]
    elif args.start:
        idx = ALL_SEASONS.index(args.start) if args.start in ALL_SEASONS else 0
        seasons = ALL_SEASONS[idx:]
    else:
        seasons = ALL_SEASONS

    log.info("collect_shots_start", seasons=seasons, target=args.target)
    df = fetch_all(seasons)
    log.info("collect_shots_fetched", n_matches=len(df))
    if df.empty:
        log.warning("no_matches_fetched")
        return

    if args.target in ("db", "both"):
        stats_db = asyncio.run(update_db(df))
        log.info("db_backfill_done", **stats_db)

    if args.target in ("csv", "both"):
        csv_path = Path(__file__).parent / args.csv_path
        stats_csv = update_local_csv(df, csv_path)
        log.info("csv_backfill_done", path=str(csv_path), **stats_csv)


if __name__ == "__main__":
    main()
