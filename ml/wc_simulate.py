"""
Simulation Monte-Carlo de la Coupe du Monde 2026 (48 équipes, 12 groupes de 4).

Utilise le modèle de buts Dixon-Coles (model_wcgoals_latest.joblib) pour simuler
chaque match → on rejoue le tournoi N fois → probabilités :
  - P(qualification) pour la phase à élimination directe (top 2 + 8 meilleurs 3es)
  - P(vainqueur du tournoi)
  - P(atteindre chaque tour)

Format 2026 : 12 groupes (A→L), top 2 + 8 meilleurs 3es = 32 → R32 → R16 → QF → SF → F.

⚠️ Le tirage officiel et le mapping exact des 3es en R32 ne sont pas connus à l'avance.
   La phase à élimination directe est donc approchée par un BRACKET PAR SEEDING :
   les 32 qualifiés sont classés par force estimée (attack - defense) et placés pour
   que les meilleurs se rencontrent le plus tard → estimation neutre et défendable.
   Une fois le tirage connu, on pourra encoder le bracket réel dans la config.

Usage:
    python wc_simulate.py --config wc2026_groups.json --sims 20000
"""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import joblib
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from pipeline.wc_goals import WCGoalsModel

ARTIFACTS = Path(__file__).parent / "artifacts" / "models"


def load_model(path: Path | None = None) -> WCGoalsModel:
    p = path or (ARTIFACTS / "model_wcgoals_latest.joblib")
    bundle = joblib.load(p)
    return WCGoalsModel.from_dict(bundle["goals_model"])


def sim_match_goals(model: WCGoalsModel, home: str, away: str, rng: np.random.Generator,
                    neutral: bool = True) -> tuple[int, int]:
    """Tire un score depuis la matrice de probabilités Dixon-Coles."""
    mat = model.score_matrix(home, away, neutral)
    flat = mat.ravel()
    flat = flat / flat.sum()
    k = rng.choice(len(flat), p=flat)
    n = mat.shape[1]
    return divmod(k, n)  # (home_goals, away_goals)


def sim_group(model, teams, rng) -> list[tuple[str, int, int, int]]:
    """Round-robin. Renvoie classement [(team, pts, gd, gf)] trié."""
    pts = dict.fromkeys(teams, 0)
    gd = dict.fromkeys(teams, 0)
    gf = dict.fromkeys(teams, 0)
    for i in range(len(teams)):
        for j in range(i + 1, len(teams)):
            h, a = teams[i], teams[j]
            hg, ag = sim_match_goals(model, h, a, rng)
            gf[h] += hg; gf[a] += ag
            gd[h] += hg - ag; gd[a] += ag - hg
            if hg > ag: pts[h] += 3
            elif hg < ag: pts[a] += 3
            else: pts[h] += 1; pts[a] += 1
    # Tri : points, puis diff de buts, puis buts marqués, puis aléatoire
    ranked = sorted(teams, key=lambda t: (pts[t], gd[t], gf[t], rng.random()), reverse=True)
    return [(t, pts[t], gd[t], gf[t]) for t in ranked]


def strength(model: WCGoalsModel, team: str) -> float:
    return model._atk(team) - model._def(team)


def knockout(model, qualifiers, rng) -> tuple[str, dict]:
    """
    Bracket par seeding : qualifiés classés par force, placés en bracket standard
    (1 vs 32, 2 vs 31, ... dans des moitiés opposées) → simulation à élimination directe.
    Renvoie (champion, {round_name: set(teams encore en lice à ce tour)}).
    """
    seeds = sorted(qualifiers, key=lambda t: strength(model, t), reverse=True)
    # Bracket standard par seeding pour une puissance de 2
    size = len(seeds)
    order = _seed_order(size)
    bracket = [seeds[i] for i in order]

    reached = {}
    round_names = _round_names(size)
    current = bracket
    for rname in round_names:
        reached[rname] = set(current)
        nxt = []
        for i in range(0, len(current), 2):
            h, a = current[i], current[i + 1]
            hg, ag = sim_match_goals(model, h, a, rng)
            if hg == ag:  # tirs au but ≈ 50/50 pondéré par la force
                ph = 1 / (1 + 10 ** ((strength(model, a) - strength(model, h))))
                winner = h if rng.random() < ph else a
            else:
                winner = h if hg > ag else a
            nxt.append(winner)
        current = nxt
    champion = current[0]
    reached["CHAMPION"] = {champion}
    return champion, reached


def _seed_order(n: int) -> list[int]:
    """Ordre de placement bracket standard (seeding) pour n = puissance de 2."""
    order = [0]
    while len(order) < n:
        m = len(order) * 2
        order = [x for i in order for x in (i, m - 1 - i)]
    return order


def _round_names(size: int) -> list[str]:
    names = {32: "R32", 16: "R16", 8: "QF", 4: "SF", 2: "F"}
    out, s = [], size
    while s >= 2:
        out.append(names.get(s, f"R{s}"))
        s //= 2
    return out


def select_qualifiers(group_results: dict, rng) -> list[str]:
    """Top 2 de chaque groupe + 8 meilleurs 3es (format 2026 → 32 qualifiés)."""
    qualified, thirds = [], []
    for g, ranking in group_results.items():
        qualified.append(ranking[0][0])
        qualified.append(ranking[1][0])
        if len(ranking) >= 3:
            thirds.append(ranking[2])  # (team, pts, gd, gf)
    thirds.sort(key=lambda r: (r[1], r[2], r[3], rng.random()), reverse=True)
    qualified += [t[0] for t in thirds[:8]]
    return qualified


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="JSON {groups: {A: [t1,t2,t3,t4], ...}}")
    ap.add_argument("--sims", type=int, default=20000)
    ap.add_argument("--model", default=None)
    args = ap.parse_args()

    model = load_model(Path(args.model) if args.model else None)
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    groups = cfg["groups"]
    all_teams = [t for g in groups.values() for t in g]

    # Avertit si des équipes sont inconnues du modèle (params fallback = moyenne)
    unknown = [t for t in all_teams if t not in model.attack]
    if unknown:
        print(f"⚠️  {len(unknown)} équipe(s) inconnue(s) du modèle (force moyenne utilisée): {unknown}")

    n_sims = args.sims
    rng = np.random.default_rng(42)
    win_count = defaultdict(int)
    qualif_count = defaultdict(int)
    reach_count = defaultdict(lambda: defaultdict(int))

    for _ in range(n_sims):
        group_results = {g: sim_group(model, teams, rng) for g, teams in groups.items()}
        qualifiers = select_qualifiers(group_results, rng)
        for t in qualifiers:
            qualif_count[t] += 1
        # bracket nécessite une puissance de 2 (32 → ok)
        champion, reached = knockout(model, qualifiers, rng)
        win_count[champion] += 1
        for rname, teams_in in reached.items():
            for t in teams_in:
                reach_count[t][rname] += 1

    print(f"\n=== Coupe du Monde 2026 — {n_sims} simulations ===\n")
    print(f"{'Équipe':<22}{'P(qualif)':>10}{'P(SF)':>9}{'P(finale)':>11}{'P(titre)':>10}")
    print("-" * 62)
    ranked = sorted(all_teams, key=lambda t: win_count[t], reverse=True)
    for t in ranked[:24]:
        pq = qualif_count[t] / n_sims
        psf = reach_count[t]["SF"] / n_sims
        pf = reach_count[t]["F"] / n_sims
        pw = win_count[t] / n_sims
        print(f"{t:<22}{pq*100:>9.1f}%{psf*100:>8.1f}%{pf*100:>10.1f}%{pw*100:>9.1f}%")


if __name__ == "__main__":
    main()
