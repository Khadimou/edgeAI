"""
Script one-shot pour publier MANUELLEMENT un value bet foot sur Instagram.

Generique : marche pour n'importe quelle compétition supportée par the-odds-api
(WC, UCL, Top-5 ligues, etc.). Fetch les cotes via the-odds-api, calcule l'edge
depuis TA proba estimée, et publie via /api/v1/instagram/post/auto.

Sport keys courants the-odds-api :
    soccer_fifa_world_cup           Coupe du Monde 2026
    soccer_uefa_champs_league       Ligue des Champions
    soccer_uefa_europa_league       Europa League
    soccer_epl                      Premier League
    soccer_france_ligue_one         Ligue 1
    soccer_italy_serie_a            Serie A
    soccer_germany_bundesliga       Bundesliga
    soccer_spain_la_liga            La Liga
    soccer_uefa_european_championship   Euro

Usage (depuis le conteneur ml_worker) :
    # Match Coupe du Monde
    python post_match_now.py --sport-key soccer_fifa_world_cup \\
        --home "France" --away "Brazil" --outcome HOME --prob 0.42

    # Match Champions League
    python post_match_now.py --sport-key soccer_uefa_champs_league \\
        --home "Real Madrid" --away "Barcelona" --outcome HOME --prob 0.48

    # Dry-run pour vérifier les cotes avant de publier
    python post_match_now.py --sport-key soccer_fifa_world_cup \\
        --home "Argentina" --away "Germany" --outcome HOME --prob 0.45 --dry-run
"""
import argparse
import os
import sys
import httpx

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
SERVICE_TOKEN = os.getenv("INSTAGRAM_SERVICE_TOKEN", "")
BACKEND_URL = os.getenv("BACKEND_URL", "http://backend:8000")

# Labels lisibles pour les sport keys courants
LEAGUE_LABELS = {
    "soccer_fifa_world_cup": "World Cup",
    "soccer_uefa_champs_league": "UEFA Champions League",
    "soccer_uefa_europa_league": "UEFA Europa League",
    "soccer_uefa_european_championship": "UEFA Euro",
    "soccer_epl": "Premier League",
    "soccer_france_ligue_one": "Ligue 1",
    "soccer_italy_serie_a": "Serie A",
    "soccer_germany_bundesliga": "Bundesliga",
    "soccer_spain_la_liga": "La Liga",
}


def _median(xs):
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def fetch_match(sport_key: str, home_q: str, away_q: str) -> dict:
    if not ODDS_API_KEY:
        sys.exit("ERREUR : ODDS_API_KEY absent de l'env.")
    r = httpx.get(
        f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds",
        params={"apiKey": ODDS_API_KEY, "regions": "eu",
                "markets": "h2h", "oddsFormat": "decimal"},
        timeout=30,
    )
    r.raise_for_status()
    games = r.json()
    print(f"[info] {len(games)} matchs renvoyés par the-odds-api pour {sport_key}")

    matched = []
    for g in games:
        h, a = g.get("home_team", ""), g.get("away_team", "")
        if home_q.lower() in h.lower() and away_q.lower() in a.lower():
            matched.append(g)
        elif home_q.lower() in a.lower() and away_q.lower() in h.lower():
            print(f"[warn] match trouvé mais home/away inversés : {h} vs {a}")
            matched.append(g)
    if not matched:
        all_teams = [(g.get("home_team"), g.get("away_team")) for g in games]
        sys.exit(f"ERREUR : aucun match trouvé pour '{home_q}' vs '{away_q}'.\n"
                 f"Matchs dispos ({len(all_teams)}) : {all_teams[:10]}...")
    if len(matched) > 1:
        print(f"[warn] {len(matched)} matchs match — on prend le premier")
    g = matched[0]

    home_name, away_name = g["home_team"], g["away_team"]
    h_odds, d_odds, a_odds = [], [], []
    for bk in g.get("bookmakers", []):
        for mkt in bk.get("markets", []):
            if mkt.get("key") != "h2h":
                continue
            for o in mkt.get("outcomes", []):
                price = float(o.get("price", 0))
                if price <= 1:
                    continue
                if o.get("name") == home_name:
                    h_odds.append(price)
                elif o.get("name") == away_name:
                    a_odds.append(price)
                elif o.get("name") == "Draw":
                    d_odds.append(price)
    if not (h_odds and a_odds):
        sys.exit(f"ERREUR : cotes h2h incomplètes pour {home_name} vs {away_name}.")
    # Le draw peut manquer sur certains marchés (US sports), mais c'est OK pour le foot
    return {
        "home_team": home_name,
        "away_team": away_name,
        "commence_time": g["commence_time"],
        "home_odds": round(_median(h_odds), 2),
        "draw_odds": round(_median(d_odds), 2) if d_odds else None,
        "away_odds": round(_median(a_odds), 2),
        "n_books": len(g.get("bookmakers", [])),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport-key", default="soccer_uefa_champs_league",
                    help=f"Clé sport the-odds-api. Connus : {', '.join(LEAGUE_LABELS)}")
    ap.add_argument("--home", required=True, help="Substring nom HOME (ex: France)")
    ap.add_argument("--away", required=True, help="Substring nom AWAY (ex: Brazil)")
    ap.add_argument("--outcome", required=True, choices=["HOME", "DRAW", "AWAY"])
    ap.add_argument("--prob", type=float, required=True, help="Ta proba estimée 0-1")
    ap.add_argument("--league-label", default=None,
                    help="Nom de la ligue affiché sur l'image (auto si sport-key connue)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not (0 < args.prob < 1):
        sys.exit("ERREUR : --prob doit être entre 0 et 1 (ex: 0.55).")

    league_label = args.league_label or LEAGUE_LABELS.get(args.sport_key, args.sport_key)

    m = fetch_match(args.sport_key, args.home, args.away)
    if args.outcome == "DRAW" and m["draw_odds"] is None:
        sys.exit("ERREUR : pas de cote DRAW dispo pour ce match.")
    odds_map = {"HOME": m["home_odds"], "DRAW": m["draw_odds"], "AWAY": m["away_odds"]}
    odds = odds_map[args.outcome]
    edge = args.prob * odds - 1
    kelly_stake = max(0.0, (args.prob * (odds - 1) - (1 - args.prob)) / (odds - 1)) * 0.25
    market_implied = round(1 / odds, 4)

    print(f"\n=== {m['home_team']} vs {m['away_team']} — {m['commence_time']} ===")
    cotes = f"HOME {m['home_odds']} / AWAY {m['away_odds']}"
    if m["draw_odds"]:
        cotes = f"HOME {m['home_odds']} / DRAW {m['draw_odds']} / AWAY {m['away_odds']}"
    print(f"Cotes médianes ({m['n_books']} bookmakers) : {cotes}")
    print(f"Ligue affichée : {league_label}")
    print(f"Issue choisie : {args.outcome} @ {odds}")
    print(f"  - ta proba    : {args.prob*100:.1f}%")
    print(f"  - implicite   : {market_implied*100:.1f}%")
    print(f"  - edge        : {edge*100:+.2f}%")
    print(f"  - kelly stake : {kelly_stake*100:.2f}% bankroll (¼ Kelly)")

    if edge <= 0:
        print(f"\n⚠ Edge négatif ({edge*100:+.1f}%) — pas de value bet. Abandon.")
        sys.exit(1)

    # Distribution probas autres issues : implicites marché normalisées
    market = {"HOME": 1 / m["home_odds"], "AWAY": 1 / m["away_odds"]}
    if m["draw_odds"]:
        market["DRAW"] = 1 / m["draw_odds"]
    others = {k: v for k, v in market.items() if k != args.outcome}
    s = sum(others.values()) or 1.0
    probs = {args.outcome: args.prob}
    for k, v in others.items():
        probs[k] = round(v / s * (1 - args.prob), 4)

    bet = {
        "home_team": m["home_team"],
        "away_team": m["away_team"],
        "league": league_label,
        "match_date": m["commence_time"],
        "outcome": args.outcome,
        "odds": odds,
        "edge": round(edge, 4),
        "kelly_stake": round(kelly_stake, 4),
        "prob_home": round(probs.get("HOME", 0), 4),
        "prob_draw": round(probs.get("DRAW", 0), 4),
        "prob_away": round(probs.get("AWAY", 0), 4),
    }

    if args.dry_run:
        print(f"\n[dry-run] payload :\n{bet}")
        return

    if not SERVICE_TOKEN:
        sys.exit("ERREUR : INSTAGRAM_SERVICE_TOKEN absent. Ne peut pas publier.")

    print(f"\n→ POST {BACKEND_URL}/api/v1/instagram/post/auto ...")
    r = httpx.post(
        f"{BACKEND_URL}/api/v1/instagram/post/auto",
        json={"bet": bet},
        headers={"X-Service-Token": SERVICE_TOKEN},
        timeout=60,
    )
    print(f"← HTTP {r.status_code}")
    print(r.text[:800])
    if r.status_code != 200:
        sys.exit(1)


if __name__ == "__main__":
    main()
