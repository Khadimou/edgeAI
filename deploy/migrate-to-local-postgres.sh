#!/usr/bin/env bash
# Migration depuis Prisma Cloud DB (planLimitReached) vers Postgres self-hosted
# sur le même VPS Hetzner. Total control + zero coût supplémentaire.
#
# Étapes :
# 1. Génère un password aléatoire pour postgres
# 2. Update .env avec nouveau DATABASE_URL (postgres local)
# 3. Stop tous les services
# 4. Pull nouveau docker-compose (avec service postgres)
# 5. Up postgres seul + wait healthy
# 6. npx prisma db push (applique le schema)
# 7. Up backend + ml_worker
# 8. Import matches.csv (re-peuple matches table)
# 9. Train DC + autres modèles
# 10. Up frontend + nginx
#
# Note : on PERD les données Prisma Cloud (predictions, bets historiques,
# users, etc.) car Prisma est plan-limited. Le user pourra recréer son
# compte. Les matchs sont réimportés depuis matches.csv local.

set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/edgeai}"
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.prod.yml"

cd "$REPO_DIR"

# Sauvegarde l'ancien .env
echo "==> Backup .env actuel..."
cp .env ".env.backup-prisma-$(date +%s)"

# Generate strong password si pas déjà set
if grep -q "^POSTGRES_PASSWORD=" .env; then
    echo "==> POSTGRES_PASSWORD déjà dans .env, on le garde"
else
    PG_PASSWORD=$(openssl rand -hex 24)
    echo "==> Génère POSTGRES_PASSWORD aléatoire..."
    cat >> .env <<EOF

# Postgres self-hosted (migration depuis Prisma Cloud)
POSTGRES_DB=edgeai
POSTGRES_USER=edgeai
POSTGRES_PASSWORD=${PG_PASSWORD}
EOF
fi

# Read password back
PG_PASSWORD=$(grep "^POSTGRES_PASSWORD=" .env | cut -d'=' -f2)

# Update DATABASE_URL
echo "==> Update DATABASE_URL → postgres local..."
NEW_URL="postgresql://edgeai:${PG_PASSWORD}@127.0.0.1:5432/edgeai"
# Replace existing DATABASE_URL or add it
if grep -q "^DATABASE_URL=" .env; then
    # Comment l'ancienne ligne pour rollback possible
    sed -i 's|^DATABASE_URL=|# DATABASE_URL_OLD_PRISMA=|' .env
fi
echo "DATABASE_URL=${NEW_URL}" >> .env

# Aussi besoin de la URL Docker-internal pour les containers (postgres:5432 au lieu de 127.0.0.1)
DOCKER_INTERNAL_URL="postgresql://edgeai:${PG_PASSWORD}@postgres:5432/edgeai"

echo "==> Stop tous les services..."
$COMPOSE down

echo "==> Rebuild images (récupère le nouveau docker-compose avec postgres service)..."
$COMPOSE pull postgres
$COMPOSE build backend ml_worker frontend

echo "==> Up postgres seul, wait healthy..."
$COMPOSE up -d postgres
# Wait healthy (max 60s)
for i in {1..30}; do
    if $COMPOSE ps postgres 2>&1 | grep -q "(healthy)"; then
        echo "    postgres healthy"
        break
    fi
    sleep 2
done

echo "==> Apply Prisma schema (npx prisma db push)..."
# Override DATABASE_URL pour utiliser 127.0.0.1:5432 depuis l'host
DATABASE_URL="${NEW_URL}" npx prisma db push --accept-data-loss --skip-generate

echo "==> Up backend + ml_worker (mais les containers utilisent postgres:5432 via Docker network)..."
# IMPORTANT : passer DATABASE_URL avec hostname interne 'postgres'
# Le compose utilise ${DATABASE_URL} depuis .env mais on l'override pour les containers
# via un .env.docker temporaire OU on update .env avec hostname interne avant up
# Solution la plus propre : modifier .env pour utiliser hostname interne et override pour prisma uniquement
sed -i "s|^DATABASE_URL=postgresql://edgeai:${PG_PASSWORD}@127.0.0.1:5432/edgeai|DATABASE_URL=${DOCKER_INTERNAL_URL}|" .env

$COMPOSE up -d backend ml_worker

echo "==> Import matches.csv (re-peuple matches FINISHED)..."
sleep 5
$COMPOSE exec ml_worker python import_matches_to_prod.py --batch 500

echo "==> Backfill shots/SOT (Phase 2 features)..."
$COMPOSE exec ml_worker python collect_shots.py --target db

echo "==> Pré-entraîne les 3 modèles XGBoost (1X2, OU, AH)..."
$COMPOSE run --rm ml_worker python -c "
import asyncio, os
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from pipeline.trainer import maybe_auto_retrain_all
def build(raw):
    url = raw.split('?')[0]
    return url.replace('postgresql://','postgresql+asyncpg://').replace('postgres://','postgresql+asyncpg://')
async def main():
    raw = os.environ['DATABASE_URL']
    e = create_async_engine(build(raw), pool_pre_ping=True)
    S = async_sessionmaker(e, expire_on_commit=False)
    async with S() as s:
        results = await maybe_auto_retrain_all(s, force=True)
        await s.commit()
        print(f'Auto-retrain results: {results}')
    await e.dispose()
asyncio.run(main())
"

echo "==> Train Dixon-Coles (5 modèles per-league)..."
$COMPOSE run --rm ml_worker python train_dc.py

echo "==> Up frontend + restart ml_worker pour charger les nouveaux modèles..."
$COMPOSE up -d frontend
$COMPOSE restart ml_worker backend

echo ""
echo "==> ✓ Migration terminée"
echo ""
echo "Vérifications :"
echo "  - Postgres : docker compose -f docker-compose.yml -f docker-compose.prod.yml ps postgres"
echo "  - Logs : docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f ml_worker --tail=50"
echo "  - DB password sauvegardé dans .env (POSTGRES_PASSWORD)"
echo ""
echo "IMPORTANT : tu dois recréer ton compte utilisateur (perte des données Prisma)."
echo "Va sur https://edgeai-betting.duckdns.org/register"
