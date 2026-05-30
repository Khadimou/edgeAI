"""
Script one-shot : poste un value bet sur la finale UCL (ou n'importe quel match UCL).
Fetch les cotes via the-odds-api (soccer_uefa_champs_league), calcule l'edge à partir
de TA probabilité estimée, et publie via /api/v1/instagram/post/auto.

Usage (dans le conteneur ml_worker) :
    python post_ucl_now.py --outcome HOME --prob 0.55 \
        --home "Paris" --away "Arsenal"
    python post_ucl_now.py --outcome AWAY --prob 0.42 \
        --home "PSG" --away "Arsenal" --dry-run

Args :
    --outcome : HOME / DRAW / AWAY
    --prob    : ta proba estimée pour cette issue (0.0–1.0)
    --home    : substring du nom de l'équipe HOME (case-insensitive)
    --away    : substring du nom de l'équipe AWAY
    --dry-run : affiche le bet calculé sans publier
"""
import argparse
import os
import sys
import httpx

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
SERVICE_TOKEN = os.getenv("INSTAGRAM_SERVICE_TOKEN", "")
BACKEND_URL = os.getenv("BACKEND_URL", "http://backend:8000")
SPORT_KEY = "soccer_uefa_champs_league"


def _median(xs):
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def fetch_match(home_q: str, away_q: str) -> dict:
    """Récupère le match UCL correspondant aux deux team queries, renvoie cotes 1X2."""
    if not ODDS_API_KEY:
        sys.exit("ERREUR : ODDS_API_KEY absent de l'env.")
    r = httpx.get(
        f"https://api.the-odds-api.com/v4/sports/{SPORT_KEY}/odds",
        params={"apiKey": ODDS_API_KEY, "regions": "eu",
                "markets": "h2h", "oddsFormat": "decimal"},
        timeout=30,
    )
    r.raise_for_status()
    games = r.json()
    print(f"[info] {len(games)} matchs UCL renvoyés par the-odds-api")
    matched = []
    for g in games:
        h = g.get("home_team", "")
        a = g.get("away_team", "")
        if home_q.lower() in h.lower() and away_q.lower() in a.lower():
            matched.append(g)
        # essaie aussi en inversé (au cas où home/away sont permutés)
        elif home_q.lower() in a.lower() and away_q.lower() in h.lower():
            print(f"[warn] match trouvé mais home/away inversés : {h} vs {a}")
            matched.append(g)
    if not matched:
        all_teams = [(g.get("home_team"), g.get("away_team")) for g in games]
        sys.exit(f"ERREUR : aucun match trouvé pour '{home_q}' vs '{away_q}'.\n"
                 f"Matchs dispos : {all_teams}")
    if len(matched) > 1:
        print(f"[warn] {len(matched)} matchs match — on prend le premier")
    g = matched[0]

    # Médiane h2h des bookmakers
    h_odds, d_odds, a_odds = [], [], []
    home_name, away_name = g["home_team"], g["away_team"]
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
    if not (h_odds and d_odds and a_odds):
        sys.exit(f"ERREUR : cotes h2h incomplètes pour {home_name} vs {away_name}.")
    return {
        "home_team": home_name,
        "away_team": away_name,
        "commence_time": g["commence_time"],
        "home_odds": round(_median(h_odds), 2),
        "draw_odds": round(_median(d_odds), 2),
        "away_odds": round(_median(a_odds), 2),
        "n_books": len(g.get("bookmakers", [])),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outcome", required=True, choices=["HOME", "DRAW", "AWAY"])
    ap.add_argument("--prob", type=float, required=True, help="Ta proba estimée 0-1")
    ap.add_argument("--home", required=True, help="substring du nom HOME (ex: Paris)")
    ap.add_argument("--away", required=True, help="substring du nom AWAY (ex: Arsenal)")
    ap.add_argument("--league-label", default="UEFA Champions League",
                    help="Nom de la ligue affiché sur l'image")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not (0 < args.prob < 1):
        sys.exit("ERREUR : --prob doit être entre 0 et 1 (ex: 0.55).")

    m = fetch_match(args.home, args.away)
    odds_map = {"HOME": m["home_odds"], "DRAW": m["draw_odds"], "AWAY": m["away_odds"]}
    odds = odds_map[args.outcome]
    edge = args.prob * odds - 1
    # ¼ Kelly fractional
    kelly_stake = max(0.0, (args.prob * (odds - 1) - (1 - args.prob)) / (odds - 1)) * 0.25
    market_implied = round(1 / odds, 4)

    print(f"\n=== {m['home_team']} vs {m['away_team']} — kickoff {m['commence_time']} ===")
    print(f"Cotes médianes ({m['n_books']} bookmakers) : "
          f"HOME {m['home_odds']} / DRAW {m['draw_odds']} / AWAY {m['away_odds']}")
    print(f"Issue choisie : {args.outcome} @ {odds}")
    print(f"  - ta proba    : {args.prob*100:.1f}%")
    print(f"  - implicite   : {market_implied*100:.1f}%")
    print(f"  - edge        : {edge*100:+.2f}%")
    print(f"  - kelly stake : {kelly_stake*100:.2f}% bankroll (¼ Kelly)")

    if edge <= 0:
        print(f"\n⚠ Edge négatif ({edge*100:+.1f}%) — pas de value bet. Abandon.")
        sys.exit(1)

    # Distribution des deux autres issues : on prend les implicites marché
    # normalisées pour qu'elles somment à (1 - args.prob).
    market = {"HOME": 1 / m["home_odds"], "DRAW": 1 / m["draw_odds"], "AWAY": 1 / m["away_odds"]}
    others = {k: v for k, v in market.items() if k != args.outcome}
    s = sum(others.values()) or 1.0
    probs = {args.outcome: args.prob}
    for k, v in others.items():
        probs[k] = round(v / s * (1 - args.prob), 4)
    bet = {
        "home_team": m["home_team"],
        "away_team": m["away_team"],
        "league": args.league_label,
        "match_date": m["commence_time"],
        "outcome": args.outcome,
        "odds": odds,
        "edge": round(edge, 4),
        "kelly_stake": round(kelly_stake, 4),
        "prob_home": round(probs["HOME"], 4),
        "prob_draw": round(probs["DRAW"], 4),
        "prob_away": round(probs["AWAY"], 4),
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
