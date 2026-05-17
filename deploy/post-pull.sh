#!/usr/bin/env bash
# Script de déploiement post-pull pour edgeAI sur Hetzner.
# Gère les mises à jour qui changent le schema des features (e.g. Phase 1 ELO),
# en pré-entraînant les modèles AVANT de redémarrer le worker pour éviter
# une fenêtre de prédictions cassées (~6h entre 2 cycles auto-retrain).
#
# Usage (en root sur le VPS) :
#   bash /opt/edgeai/deploy/post-pull.sh

set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/edgeai}"
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.prod.yml"

cd "$REPO_DIR"

echo "==> git pull..."
git pull origin main

echo "==> Rebuild des images (full reset : rm image + prune + --no-cache --pull)..."
# Le pattern simple --no-cache ne suffit pas avec BuildKit : il garde des
# layers persistants par contenu hash. Pour forcer un VRAI rebuild from scratch :
# 1. rm les images
# 2. prune le builder cache
# 3. build avec --pull --no-cache
docker image rm edgeai-frontend:latest edgeai-backend:latest edgeai-ml_worker:latest 2>/dev/null || true
docker builder prune -af
$COMPOSE build --no-cache --pull ml_worker backend frontend

echo "==> Pré-entraînement des 3 modèles sur la DB prod (one-shot)..."
# Le ml_worker rebuild a la nouvelle version du code. On lance un container
# éphémère qui appelle maybe_auto_retrain_all() de force, ce qui :
#  - charge l'historique depuis la DB
#  - calcule les features Phase 1
#  - entraîne 1X2 + OU + AH
#  - bypasse le gate "régression" si features_hash a changé
#  - sauvegarde model_*_latest.joblib dans le volume ./ml/artifacts/models/
$COMPOSE run --rm ml_worker python -c "
import asyncio
import os
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from pipeline.trainer import maybe_auto_retrain_all

# Aligné sur backend/app/db/session.py
def build_url(raw: str) -> str:
    url = raw.split('?')[0]
    url = url.replace('postgresql://', 'postgresql+asyncpg://')
    url = url.replace('postgres://', 'postgresql+asyncpg://')
    return url

async def main():
    raw = os.environ['DATABASE_URL']
    db_url = build_url(raw)
    connect_args = {'ssl': True} if 'sslmode=require' in raw else {}
    print(f'DB URL prefix: {db_url[:40]}... (ssl={bool(connect_args)})')
    engine = create_async_engine(db_url, connect_args=connect_args, pool_pre_ping=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        # force=True : bypass cooldown + RETRAIN_MIN_SAMPLES pour le 1er retrain
        # post-migration (changement de schema features)
        results = await maybe_auto_retrain_all(session, force=True)
        # Commit explicite pour persister _register_in_db (sinon rollback silencieux
        # en sortie du async with → model_versions reste désynchro avec le disque)
        await session.commit()
        print(f'Auto-retrain results: {results}')
    await engine.dispose()

asyncio.run(main())
"

echo "==> Restart des services..."
$COMPOSE up -d

echo "==> Tail logs ml_worker (Ctrl+C pour quitter)..."
$COMPOSE logs -f ml_worker --tail=50
