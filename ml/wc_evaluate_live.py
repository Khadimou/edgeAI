"""
Évaluation LIVE du modèle WC sur les matchs réellement joués (WC 2026).

Compare les prédictions stockées en base (model_version 'wc_intl') aux résultats
réels. Mesure :
  1. Échantillon + accuracy 1X2 + log-loss + Brier
  2. Calibration (reliability) : par tranche de proba, le taux réel correspond-il ?
  3. Modèle vs marché : le modèle est-il "compressé" (sous-estime les favoris) ?
  4. Performance des value bets (edge ≥ min, cote ≤ max) : ROI réel.

Usage (dans le conteneur ml_worker) :
    python wc_evaluate_live.py
    python wc_evaluate_live.py --edge-min 0.05 --odds-max 5.0
"""
import argparse
import asyncio
import os
from math import log

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


def _db_url() -> str:
    raw = os.getenv("DATABASE_URL", "postgresql+asyncpg://edgeai:edgeai_secret@localhost:5432/edgeai")
    url = raw.replace("postgres://", "postgresql+asyncpg://").replace("postgresql://", "postgresql+asyncpg://")
    return url


EPS = 1e-15


async def fetch_rows():
    engine = create_async_engine(_db_url())
    async with engine.connect() as conn:
        rows = (await conn.execute(text("""
            SELECT m.home_team, m.away_team, m.home_score, m.away_score,
                   m.home_odds, m.draw_odds, m.away_odds,
                   p.prob_home, p.prob_draw, p.prob_away
            FROM matches m
            JOIN LATERAL (
                SELECT * FROM predictions
                WHERE match_id = m.id AND model_version LIKE 'wc%'
                ORDER BY computed_at DESC LIMIT 1
            ) p ON TRUE
            WHERE m.league = 'World Cup' AND m.status = 'FINISHED'
              AND m.home_score IS NOT NULL AND m.away_score IS NOT NULL
            ORDER BY m.match_date
        """))).mappings().all()
    await engine.dispose()
    return rows


def _outcome(hs, as_):
    return 0 if hs > as_ else (1 if hs == as_ else 2)


def main(edge_min: float, odds_max: float):
    rows = asyncio.run(fetch_rows())
    n = len(rows)
    print("=" * 64)
    print(f"ÉVALUATION LIVE — Modèle WC 2026  ({n} matchs joués)")
    print("=" * 64)
    if n == 0:
        print("Aucun match WC terminé avec prédiction. Reviens après quelques matchs.")
        return

    # ── 1. Accuracy / log-loss / Brier ──
    correct = 0
    ll_sum = brier_sum = 0.0
    naive_home = 0
    # buckets calibration : [(low, high)] sur la proba de l'issue prédite (max)
    buckets = {(0.0, 0.2): [0, 0], (0.2, 0.35): [0, 0], (0.35, 0.5): [0, 0],
               (0.5, 0.7): [0, 0], (0.7, 1.01): [0, 0]}  # [n, n_realised]
    # compression : proba du favori (max des 3) modèle vs marché
    model_fav_sum = market_fav_sum = 0.0
    fav_count = 0

    for r in rows:
        hs, as_ = r["home_score"], r["away_score"]
        actual = _outcome(hs, as_)
        if actual == 0:
            naive_home += 1
        probs = [r["prob_home"] or 0, r["prob_draw"] or 0, r["prob_away"] or 0]
        s = sum(probs) or 1.0
        probs = [p / s for p in probs]
        pred = max(range(3), key=lambda i: probs[i])
        if pred == actual:
            correct += 1
        ll_sum += -log(max(min(probs[actual], 1 - EPS), EPS))
        for i in range(3):
            brier_sum += (probs[i] - (1.0 if i == actual else 0.0)) ** 2

        # Calibration sur l'issue PRÉDITE (proba max)
        pmax = probs[pred]
        for (lo, hi), cell in buckets.items():
            if lo <= pmax < hi:
                cell[0] += 1
                if pred == actual:
                    cell[1] += 1
                break

        # Compression : compare la proba du favori marché vs modèle
        odds = [r["home_odds"], r["draw_odds"], r["away_odds"]]
        if all(o and o > 1 for o in odds):
            implied = [1 / o for o in odds]
            si = sum(implied)
            implied = [x / si for x in implied]  # dévigorisé
            fav = max(range(3), key=lambda i: implied[i])
            market_fav_sum += implied[fav]
            model_fav_sum += probs[fav]
            fav_count += 1

    acc = correct / n
    print(f"\n[1] Performance 1X2")
    print(f"    Accuracy      : {acc*100:.1f}%   (baseline 'toujours domicile' : {naive_home/n*100:.1f}%)")
    print(f"    Log-loss      : {ll_sum/n:.4f}   (plus bas = mieux ; ~1.05 attendu)")
    print(f"    Brier score   : {brier_sum/n:.4f}")

    # ── 2. Calibration ──
    print(f"\n[2] Calibration (sur l'issue prédite par le modèle)")
    print(f"    {'Tranche proba':<16}{'N':>5}{'Prédit':>9}{'Réel':>8}")
    for (lo, hi), (cnt, realised) in buckets.items():
        if cnt == 0:
            continue
        mid = (lo + hi) / 2
        real_rate = realised / cnt
        flag = "  ⚠️ écart" if abs(real_rate - mid) > 0.15 else ""
        print(f"    {lo*100:.0f}-{hi*100:.0f}%{'':<10}{cnt:>5}{mid*100:>8.0f}%{real_rate*100:>7.0f}%{flag}")

    # ── 3. Compression modèle vs marché ──
    if fav_count:
        mf = model_fav_sum / fav_count
        mkf = market_fav_sum / fav_count
        print(f"\n[3] Modèle vs marché (proba moyenne du favori, {fav_count} matchs)")
        print(f"    Marché  : {mkf*100:.1f}%")
        print(f"    Modèle  : {mf*100:.1f}%")
        diff = (mf - mkf) * 100
        if diff < -4:
            print(f"    → Modèle SOUS-estime les favoris de {abs(diff):.1f}pts (compression "
                  f"→ faux value sur outsiders). Blend modèle+marché recommandé.")
        elif diff > 4:
            print(f"    → Modèle SUR-estime les favoris de {diff:.1f}pts.")
        else:
            print(f"    → Écart {diff:+.1f}pts : calibration vs marché correcte.")

    # ── 4. Performance value bets ──
    print(f"\n[4] Value bets (edge ≥ {edge_min*100:.0f}%, cote ≤ {odds_max})")
    stake = 1.0
    n_vb = wins = 0
    pnl = 0.0
    for r in rows:
        hs, as_ = r["home_score"], r["away_score"]
        actual = _outcome(hs, as_)
        probs = [r["prob_home"] or 0, r["prob_draw"] or 0, r["prob_away"] or 0]
        odds = [r["home_odds"], r["draw_odds"], r["away_odds"]]
        for i, (p, o) in enumerate(zip(probs, odds)):
            if not o or o <= 1:
                continue
            edge = p * o - 1
            if edge >= edge_min and o <= odds_max:
                n_vb += 1
                if actual == i:
                    wins += 1
                    pnl += stake * (o - 1)
                else:
                    pnl -= stake
    if n_vb:
        roi = pnl / (n_vb * stake) * 100
        print(f"    {n_vb} value bets · {wins} gagnés ({wins/n_vb*100:.0f}%) · "
              f"P&L {pnl:+.2f}u · ROI {roi:+.1f}%")
    else:
        print(f"    Aucun value bet dans ces critères sur les matchs joués.")

    print("\n" + "=" * 64)
    print("Note : échantillon WC encore petit — lire les tendances, pas les chiffres exacts.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--edge-min", type=float, default=0.05)
    ap.add_argument("--odds-max", type=float, default=5.0)
    args = ap.parse_args()
    main(args.edge_min, args.odds_max)
