"""
Backfill rétroactif des prédictions sur les matchs historiques FINISHED.

Objectif : enrichir le live tracking en générant des prédictions "as if" pour
les matchs passés avec les modèles actuels. Permet d'avoir 500-1000+ paris dans
le edge sweep au lieu d'une dizaine.

CAVEAT statistique : les modèles actuels (DC + XGB OU/AH) ont été entraînés sur
l'historique, donc une partie des prédictions backfillées sont in-sample (data
leak partiel). Les ROI absolus seront biaisés à la hausse. MAIS le ranking
relatif des seuils d'edge reste valide (le biais affecte tous les seuils
similairement). C'est suffisant pour identifier le sweet spot.

Les prédictions backfillées sont marquées avec model_version commençant par
"backfill_" pour rester distinguables des prédictions réelles de prod.

Usage (dans un container ml_worker éphémère) :
    docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm \
        ml_worker python backfill_predictions.py --days 730

    # ou pour Foot seulement, ligues whitelistées :
    docker compose ... run --rm ml_worker python backfill_predictions.py \
        --days 730 --leagues "Ligue 1,Premier League,Bundesliga,Serie A,La Liga"
"""
from __future__ import annotations

import argparse
import asyncio
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# Réutilise les fonctions de génération de prédiction de la prod
from pipeline.scheduler import (
    _load_active_model,
    _load_dc_model,
    _load_ou_model,
    _load_ah_model,
    _generate_prediction,
    _generate_ou_prediction,
    _generate_ah_prediction,
)
from pipeline.football_inference import FOOT_STATE


DEFAULT_LEAGUES = ["Ligue 1", "Premier League", "Bundesliga", "Serie A", "La Liga"]


def _build_url(raw: str) -> str:
    """Aligné sur backend/app/db/session.py (DATABASE_URL → asyncpg)."""
    url = raw.split("?")[0]
    url = url.replace("postgresql://", "postgresql+asyncpg://")
    url = url.replace("postgres://", "postgresql+asyncpg://")
    return url


async def _fetch_matches_without_prediction(session, days: int, leagues: list[str]):
    """Retourne les matchs FINISHED des N derniers jours qui n'ont pas encore
    de prédiction stockée (pour ne pas écraser celles déjà calculées en prod)."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    since_naive = since.replace(tzinfo=None)
    result = await session.execute(
        text("""
            SELECT m.id, m.sport, m.league, m.season, m.home_team, m.away_team,
                   m.match_date, m.status,
                   m.home_score, m.away_score,
                   m.ht_home_score, m.ht_away_score,
                   m.home_yellow_cards, m.away_yellow_cards,
                   m.home_red_cards, m.away_red_cards,
                   m.home_shots, m.away_shots,
                   m.home_shots_on_target, m.away_shots_on_target,
                   m.home_corners, m.away_corners
            FROM matches m
            WHERE m.status = 'FINISHED'
              AND m.sport = 'FOOTBALL'
              AND m.match_date >= :since
              AND m.league = ANY(:leagues)
              AND NOT EXISTS (
                  SELECT 1 FROM predictions p WHERE p.match_id = m.id
              )
            ORDER BY m.match_date ASC
        """),
        {"since": since_naive, "leagues": leagues},
    )
    rows = result.fetchall()
    return [
        {
            "id": str(r[0]),
            "sport": r[1],
            "league": r[2],
            "season": r[3],
            "home_team": r[4],
            "away_team": r[5],
            "match_date": r[6].isoformat() if r[6] else "",
            "status": r[7],
            "home_score": r[8],
            "away_score": r[9],
            "ht_home_score": r[10],
            "ht_away_score": r[11],
            "home_yellow_cards": r[12] or 0,
            "away_yellow_cards": r[13] or 0,
            "home_red_cards": r[14] or 0,
            "away_red_cards": r[15] or 0,
            "home_shots": r[16],
            "away_shots": r[17],
            "home_shots_on_target": r[18],
            "away_shots_on_target": r[19],
            "home_corners": r[20],
            "away_corners": r[21],
        }
        for r in rows
    ]


async def _insert_backfill_prediction(
    session, match_id: str, match_date: str, pred: dict
):
    """Insert une prédiction backfillée avec computed_at = veille du match.

    Utilise le préfixe 'backfill_' sur model_version pour rester distinguable.
    """
    # computed_at = match_date - 1 day : simule une prédiction faite la veille
    # (équivalent au cycle de prod qui prédit pour les matchs J+1)
    try:
        match_dt = datetime.fromisoformat(match_date.replace("Z", "+00:00"))
        computed_at = (match_dt - timedelta(days=1)).replace(tzinfo=None)
    except Exception:
        computed_at = datetime.now(timezone.utc).replace(tzinfo=None)

    # Préfixe backfill_ + version originale du modèle
    model_v = pred.get("model_version", "unknown")
    if not model_v.startswith("backfill_"):
        model_v = f"backfill_{model_v}"

    try:
        await session.execute(
            text("""
                INSERT INTO predictions (
                    id, match_id, model_version,
                    prob_home, prob_draw, prob_away,
                    prob_over_25, prob_under_25,
                    prob_ah_home, prob_ah_away,
                    confidence, shap_values, computed_at
                )
                VALUES (
                    gen_random_uuid(), :match_id, :model_version,
                    :prob_home, :prob_draw, :prob_away,
                    :prob_over_25, :prob_under_25,
                    :prob_ah_home, :prob_ah_away,
                    :confidence, NULL, :computed_at
                )
                ON CONFLICT DO NOTHING
            """),
            {
                "match_id": match_id,
                "model_version": model_v,
                "prob_home": pred["prob_home"],
                "prob_draw": pred["prob_draw"],
                "prob_away": pred["prob_away"],
                "prob_over_25": pred.get("prob_over_25"),
                "prob_under_25": pred.get("prob_under_25"),
                "prob_ah_home": pred.get("prob_ah_home"),
                "prob_ah_away": pred.get("prob_ah_away"),
                "confidence": pred.get("confidence", 0.5),
                "computed_at": computed_at,
            },
        )
        return True
    except Exception as e:
        print(f"  ⚠ insert error match={match_id}: {e}")
        return False


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=730,
                        help="Fenêtre de backfill (jours)")
    parser.add_argument(
        "--leagues", type=str, default=",".join(DEFAULT_LEAGUES),
        help="Liste de ligues séparées par virgule",
    )
    parser.add_argument("--batch", type=int, default=100,
                        help="Commit chaque N prédictions")
    parser.add_argument("--limit", type=int, default=None,
                        help="Limite le nombre de matchs (debug)")
    args = parser.parse_args()

    leagues = [l.strip() for l in args.leagues.split(",") if l.strip()]
    print(f"⚙ Backfill : {args.days} jours, ligues={leagues}")

    # Connexion DB
    raw = os.environ["DATABASE_URL"]
    db_url = _build_url(raw)
    connect_args = {"ssl": True} if "sslmode=require" in raw else {}
    engine = create_async_engine(db_url, connect_args=connect_args, pool_pre_ping=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    # Préchauffe le state ELO global utilisé par _build_foot_features
    # (sinon les premières prédictions utilisent les defaults → biais)
    async with Session() as session:
        try:
            ok = await FOOT_STATE.ensure_loaded(session)
            print(f"✓ ELO state chargé ok={ok}")
        except Exception as e:
            print(f"⚠ ELO refresh failed: {e}")

        # Charge les 3 modèles
        model_1x2 = _load_active_model()
        dc_model = _load_dc_model()
        ou_model = _load_ou_model()
        ah_model = _load_ah_model()
        print(f"✓ Modèles chargés : 1x2={bool(model_1x2)} dc={bool(dc_model)} "
              f"ou={bool(ou_model)} ah={bool(ah_model)}")
        if not model_1x2 and not dc_model:
            print("❌ Aucun modèle 1X2 — abort")
            return

        # Fetch matchs à backfiller
        matches = await _fetch_matches_without_prediction(session, args.days, leagues)
        if args.limit:
            matches = matches[: args.limit]
        print(f"✓ {len(matches)} matchs à backfiller")
        if not matches:
            print("Rien à faire.")
            return

        # Boucle de backfill
        ok, fail, skipped = 0, 0, 0
        for i, match in enumerate(matches, 1):
            try:
                pred = await _generate_prediction(
                    model_1x2, match, session, None, 20, dc_model=dc_model
                )
                if ou_model is not None:
                    ou = await _generate_ou_prediction(ou_model, match, session, None, 20)
                    if ou:
                        pred.update(ou)
                if ah_model is not None:
                    ah = await _generate_ah_prediction(ah_model, match, session, None, 20)
                    if ah:
                        pred.update(ah)

                inserted = await _insert_backfill_prediction(
                    session, match["id"], match["match_date"], pred
                )
                if inserted:
                    ok += 1
                else:
                    fail += 1
            except Exception as e:
                fail += 1
                if fail < 5:
                    print(f"  ⚠ predict error match={match['id']} "
                          f"{match['home_team']} vs {match['away_team']}: {e}")

            # Commit par batch pour ne pas tout perdre en cas de crash
            if i % args.batch == 0:
                await session.commit()
                print(f"  [{i}/{len(matches)}] ok={ok} fail={fail}")

        await session.commit()
        print(f"\n✓ Backfill terminé : ok={ok} fail={fail} skipped={skipped} "
              f"sur {len(matches)} matchs")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
