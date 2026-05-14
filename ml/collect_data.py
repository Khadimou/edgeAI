"""
Collecte des données historiques depuis football-data.org.

Usage:
    python collect_data.py --seasons 2024 --leagues PL
    python collect_data.py --seasons 2021 2022 2023 2024 --export-csv
"""
import argparse
import asyncio
import csv
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).parent))
from pipeline.ingestion import FootballDataClient, normalize_match, SUPPORTED_LEAGUES

API_KEY = os.getenv("FOOTBALL_DATA_API_KEY", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
DATA_DIR = Path(__file__).parent / "data" / "raw"

# Rate limit free tier : 10 req/min → 7s entre chaque requête
REQUEST_DELAY = 7.0


async def collect(seasons: list[int], leagues: list[str], export_csv: bool):
    if not API_KEY:
        print("ERREUR : FOOTBALL_DATA_API_KEY manquant dans .env")
        sys.exit(1)

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Connexion DB optionnelle
    db_conn = None
    if DATABASE_URL:
        try:
            url = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://").split("?")[0]
            db_conn = await asyncpg.connect(url, ssl="require" if "sslmode=require" in DATABASE_URL else None)
            print("✓ Connecté à la base de données")
        except Exception as e:
            print(f"⚠ Base de données inaccessible ({e}) — export CSV uniquement")

    client = FootballDataClient(API_KEY)
    all_matches = []

    total = len(seasons) * len(leagues)
    done = 0

    try:
        for season in seasons:
            for league_code in leagues:
                league_name = SUPPORTED_LEAGUES.get(league_code)
                if not league_name:
                    print(f"⚠ Ligue inconnue : {league_code}. Valides : {list(SUPPORTED_LEAGUES)}")
                    continue

                print(f"[{done+1}/{total}] {league_name} saison {season}...", end=" ", flush=True)
                raw_matches = await client.get_season_matches(league_code, season)

                if not raw_matches:
                    print("0 matchs")
                    done += 1
                    continue

                normalized = []
                for raw in raw_matches:
                    score = raw.get("score", {}).get("fullTime", {})
                    if score.get("home") is None:
                        continue  # match sans score (annulé, etc.)
                    m = normalize_match(raw, league_name)
                    m["home_odds"] = _extract_odds(raw, "homeWin")
                    m["draw_odds"] = _extract_odds(raw, "draw")
                    m["away_odds"] = _extract_odds(raw, "awayWin")
                    normalized.append(m)
                    # normalize_match extrait déjà ht_home_score, ht_away_score,
                    # home_yellow_cards, away_yellow_cards, home_red_cards, away_red_cards

                print(f"{len(normalized)} matchs")
                all_matches.extend(normalized)

                if db_conn:
                    await _upsert_to_db(db_conn, normalized)

                done += 1
                if done < total:
                    time.sleep(REQUEST_DELAY)

    finally:
        await client.close()
        if db_conn:
            await db_conn.close()

    print(f"\n✓ Total collecté : {len(all_matches)} matchs")

    if export_csv or not DATABASE_URL:
        csv_path = DATA_DIR / "matches.csv"
        _export_csv(all_matches, csv_path)
        print(f"✓ CSV exporté : {csv_path}")

    return all_matches


async def _upsert_to_db(conn: asyncpg.Connection, matches: list[dict]):
    for m in matches:
        try:
            date_val = m["match_date"]
            if isinstance(date_val, str):
                from datetime import datetime as _dt
                date_val = _dt.fromisoformat(date_val.replace("Z", "+00:00")).replace(tzinfo=None)
            await conn.execute("""
                INSERT INTO matches (
                    id, external_id, league, season, home_team, away_team,
                    match_date, status, home_score, away_score,
                    home_odds, draw_odds, away_odds, created_at, updated_at
                )
                VALUES (
                    gen_random_uuid(), $1, $2, $3, $4, $5,
                    $6, $7, $8, $9,
                    $10, $11, $12, NOW(), NOW()
                )
                ON CONFLICT (external_id) DO UPDATE
                    SET status = EXCLUDED.status,
                        home_score = EXCLUDED.home_score,
                        away_score = EXCLUDED.away_score,
                        home_odds = EXCLUDED.home_odds,
                        draw_odds = EXCLUDED.draw_odds,
                        away_odds = EXCLUDED.away_odds,
                        updated_at = NOW()
            """,
                m["external_id"], m["league"], m["season"],
                m["home_team"], m["away_team"], date_val,
                m["status"], m.get("home_score"), m.get("away_score"),
                m.get("home_odds"), m.get("draw_odds"), m.get("away_odds"),
            )
        except Exception as e:
            print(f"  ⚠ Upsert échoué ({m['home_team']} vs {m['away_team']}) : {e}")


def _export_csv(matches: list[dict], path: Path):
    if not matches:
        return
    fieldnames = list(matches[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(matches)


def _extract_odds(raw: dict, key: str) -> float | None:
    odds = raw.get("odds", {})
    val = odds.get(key)
    return float(val) if val else None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Collecte les données historiques football-data.org")
    parser.add_argument("--seasons", nargs="+", type=int, default=[2024],
                        help="Années de saisons (ex: 2021 2022 2023 2024)")
    parser.add_argument("--leagues", nargs="+", default=list(SUPPORTED_LEAGUES.keys()),
                        help=f"Codes ligues : {list(SUPPORTED_LEAGUES.keys())}")
    parser.add_argument("--export-csv", action="store_true",
                        help="Exporte aussi en CSV même si DB disponible")
    args = parser.parse_args()

    print(f"edgeAI — Collecte données")
    print(f"  Saisons : {args.seasons}")
    print(f"  Ligues  : {args.leagues}")
    print(f"  Délai   : {REQUEST_DELAY}s entre requêtes\n")

    asyncio.run(collect(args.seasons, args.leagues, args.export_csv))
