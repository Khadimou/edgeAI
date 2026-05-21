# Operations edgeAI

Déploiement, maintenance, troubleshooting récurrent. Tout ce qu'il faut savoir pour garder l'instance live en vie.

## Infrastructure prod

- **Serveur** : Hetzner CCX13 (2 vCPU, 8 GB RAM, 80 GB SSD), Ubuntu 22.04
- **Hostname** : edgeai-betting.duckdns.org
- **Backup** : ❌ pas encore automatisé (cf. ROADMAP)
- **Monitoring** : Sentry (front + back), logs structlog en stdout des containers

## Déploiement standard (post-pull)

Toute modification poussée sur `main` se déploie via :

```bash
ssh root@<vps>
cd /opt/edgeai
bash deploy/post-pull.sh
```

Ce script fait :
1. `git pull origin main`
2. **Full reset Docker BuildKit cache** (`docker image rm` + `builder prune -af`) — sinon Docker reuse des layers et le code n'est pas mis à jour
3. `docker compose build --no-cache --pull ml_worker backend frontend`
4. Lance un container ml_worker éphémère qui force `maybe_auto_retrain_all(force=True)` (5-15 min selon les modèles à rebuild)
5. `docker compose up -d`
6. Tail les logs

## Migrations DB

Prisma db push via container Node éphémère :

```bash
DB_URL=$(grep '^DATABASE_URL=' /opt/edgeai/.env | cut -d= -f2-)
docker run --rm --network edgeai_default \
  -v /opt/edgeai:/app -w /app \
  -e DATABASE_URL="$DB_URL" \
  node:20-alpine sh -c "npx -y prisma@6 db push --skip-generate"
```

⚠ **Prisma db push ment parfois** ("already in sync" alors que les colonnes manquent). Vérifier toujours :

```bash
docker exec edgeai-postgres-1 psql -U edgeai -d edgeai -c "\d matches"
```

Si Prisma ment, force en SQL direct (idempotent grâce à `IF NOT EXISTS`) :

```bash
docker exec edgeai-postgres-1 psql -U edgeai -d edgeai -c "
ALTER TABLE matches ADD COLUMN IF NOT EXISTS my_new_col DOUBLE PRECISION;
"
```

## Sécurité

### Redis ne doit JAMAIS être exposé sur l'host

**Incident 19/05/2026** : le BSI/CERT-Bund a signalé via Hetzner que le Redis était
accessible depuis Internet sans auth (port 6379 ouvert). Cause : `docker-compose.yml`
publiait `"6379:6379"` (bind 0.0.0.0) et le `ports: []` du prod override **ne
l'effaçait pas** — Docker Compose concatène les listes `ports` au lieu de les
remplacer. **Docker bypasse aussi ufw** (écrit directement dans iptables), donc un
firewall classique ne protège pas les ports publiés.

**Fix appliqué** :
1. Suppression totale du mapping de port Redis (les containers le joignent via le
   réseau interne `redis:6379`, jamais besoin d'exposer sur l'host)
2. Mot de passe Redis obligatoire (`--requirepass`), via `REDIS_PASSWORD` du `.env`
3. REDIS_URL devient `redis://:${REDIS_PASSWORD}@redis:6379`

**Règle générale** : aucun service de données (Redis, Postgres) ne doit avoir de
`ports:` mappé sur `0.0.0.0`. Si besoin d'accès host (genre prisma db push), bind
sur loopback uniquement : `127.0.0.1:5432:5432`. Pour vérifier ce qui est exposé :

```bash
# Liste les ports publiés sur toutes interfaces (DANGER si 0.0.0.0)
docker ps --format '{{.Names}}: {{.Ports}}'
# Scan externe pour confirmer (depuis une autre machine)
nmap -p 6379,5432,8000,3000 <IP_VPS>
```

### Firewall host (défense en profondeur)

Comme Docker bypasse ufw pour les ports publiés, la vraie protection passe par la
chain `DOCKER-USER` :

```bash
# Bloque tout accès externe aux ports de données (garde le réseau Docker interne)
sudo iptables -I DOCKER-USER -p tcp --dport 6379 ! -s 172.16.0.0/12 -j DROP
sudo iptables -I DOCKER-USER -p tcp --dport 5432 ! -s 172.16.0.0/12 -j DROP
# Persister les règles au reboot
sudo apt install -y iptables-persistent && sudo netfilter-persistent save
```

## Quotas externes

### the-odds-api (CRITIQUE — quota mensuel)

- **Plan gratuit** : 500 req/mois
- **Reset** : 1er du mois civil
- **Usage** : foot (5 ligues × 1 fetch/h avec lock 22h) + NBA (1 fetch/22h)
- **Statut au 18 mai 2026** : ❌ épuisé. Reset prévu le **1er juin 2026**.

Check du quota :

```bash
RP=$(grep '^REDIS_PASSWORD=' /opt/edgeai/.env | cut -d= -f2-)
docker exec edgeai-redis-1 redis-cli -a "$RP" --no-auth-warning get odds_api:remaining
```

Pendant l'épuisement :
- Foot 1X2/AH continuent de fonctionner via football-data.org (clé indépendante)
- NBA totalement à l'arrêt (odds + scores tous via the-odds-api)

**Upgrade conseillé** : $30/mois pour 20k req (≈ 4× usage actuel).

### football-data.org (quota par minute)

- 10 req/minute en plan gratuit
- Locks dans `scheduler.py` : cooldown 1h par ligue
- Pas de problème de quota observé

### Anthropic Claude (chatbot)

- Pay per usage (~$0.80 / 1M input tokens en Haiku 4.5)
- Rate limit côté edgeAI : 20 questions/heure/user via Redis
- Coût actuel : ~$0.10/jour estimé

## Modèles ML

### Liste des modèles déployés

```
/opt/edgeai/ml/artifacts/models/
├── model_latest.joblib              # XGB foot 1X2 global fallback
├── model_dc_latest.joblib           # ⭐ Dixon-Coles per-league (priorité 1X2)
├── model_ou_latest.joblib           # XGB foot O/U 2.5
├── model_ah_latest.joblib           # XGB foot Asian Handicap
├── model_perleague_serie_a_latest.joblib  # XGB Serie A dédié (actif)
├── model_perleague_<autres>_latest.joblib # entraînés mais inutilisés (global meilleur)
├── model_nba_latest.joblib          # XGB NBA 1X2
└── model_nba_totals_latest.joblib   # ⭐ XGB NBA totals (binaire)
```

Le scheduler les charge tous au démarrage avec fallback gracieux (warning si fichier absent).

### Retraining

Auto-retrain via cycle ml_worker (cf. `ml/pipeline/trainer.py:maybe_auto_retrain_*`). Conditions :
- ≥ 500 nouveaux samples depuis le dernier retrain
- cooldown 24h respecté
- nouveau log_loss ne régresse pas de plus de 5%

Force un retrain manuel :

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  run --rm ml_worker python -c "
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from pipeline.trainer import maybe_auto_retrain_all
import os

raw = os.environ['DATABASE_URL']
url = raw.replace('postgresql://', 'postgresql+asyncpg://').split('?')[0]
engine = create_async_engine(url, pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False)

async def main():
    async with Session() as s:
        r = await maybe_auto_retrain_all(s, force=True)
        await s.commit()
        print(r)
asyncio.run(main())
"
```

### Train spécifique

- **Dixon-Coles per-league** : `python train_dc.py` (15 min)
- **XGB per-league** : `python train_per_league.py` (10 min, retrain les 5 ligues)
- **NBA Totals** : `python nba_totals_pipeline.py` (5-10 min)

## Backfills (one-shot)

À ne lancer que dans des cas spécifiques (migration DB, recovery, calibration initiale) :

```bash
# Backfill cotes historiques foot (football-data.co.uk)
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  run --rm ml_worker python backfill_odds.py

# Backfill predictions (génère prédictions rétroactives avec modèles actuels)
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  run --rm ml_worker python backfill_predictions.py --days 730
```

**Caveat data leak** : voir `docs/ARCHITECTURE.md` → section backfill.

## Troubleshooting récurrent

### "BuildKit cache" : modifications de code pas visibles après rebuild

Symptôme classique vécu **5+ fois** dans l'historique du projet. Le build "réussit" mais le bundle/code servi est l'ancien.

**Diagnostic** :
```bash
# Compare source dans le container vs bundle compilé
docker exec edgeai-frontend-1 grep -c "ma_nouvelle_string" /app/src/app/.../page.tsx
docker exec edgeai-frontend-1 grep -c "ma_nouvelle_string" /app/.next/static/chunks/.../page-*.js
```

Si source = 1 et bundle = 0 → BuildKit cache.

**Fix radical** :
```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml down frontend
docker image rm edgeai-frontend:latest 2>/dev/null
docker builder prune -af
DOCKER_BUILDKIT=0 docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  build --no-cache --pull frontend
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  up -d --force-recreate frontend
```

Note `DOCKER_BUILDKIT=0` désactive le builder qui cache par contenu hash — c'est la cause racine.

### Container redémarre en boucle

```bash
docker logs edgeai-<service>-1 --tail=100
docker compose ps  # voir le statut
```

Causes typiques :
- Modèle joblib corrompu après crash mid-write → supprimer + relancer training
- DB connection refused → check `edgeai-postgres-1` healthy + `.env` correct
- Variable d'env manquante → check `docker-compose.prod.yml` env_file

### Modèle pas chargé alors qu'il existe

```bash
docker exec edgeai-ml_worker-1 ls -la /app/artifacts/models/
docker logs edgeai-ml_worker-1 --tail=50 | grep -i error
```

Causes typiques :
- joblib version mismatch (model entraîné avec sklearn X, runtime sklearn Y)
- Features schema mismatch (modèle 36 features mais code attend 67) → warning `per_league_model_schema_mismatch_skipped`, fallback global

### Logs ml_worker spammés par DeprecationWarning

Connu (`datetime.utcnow()` deprecated Python 3.12+). Corrigé sur `scheduler.py:946`. Si réapparaît, vérifier qu'aucune nouvelle utilisation de `datetime.utcnow()` n'a été ajoutée. Préférer `datetime.now(timezone.utc)`.

## Commandes utiles

```bash
# État global de la stack
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps

# Logs en live
docker logs edgeai-ml_worker-1 -f --tail=50
docker logs edgeai-backend-1 -f --tail=50
docker logs edgeai-frontend-1 -f --tail=50

# Force restart un service (sans rebuild)
docker compose -f docker-compose.yml -f docker-compose.prod.yml restart ml_worker

# Check Postgres
docker exec edgeai-postgres-1 psql -U edgeai -d edgeai -c "SELECT version();"
docker exec edgeai-postgres-1 psql -U edgeai -d edgeai -c "\dt"

# Check Redis (Redis a un mot de passe depuis le fix sécurité 20/05/2026)
# Le -a "$REDIS_PASSWORD" est obligatoire sinon NOAUTH error.
RP=$(grep '^REDIS_PASSWORD=' /opt/edgeai/.env | cut -d= -f2-)
docker exec edgeai-redis-1 redis-cli -a "$RP" --no-auth-warning keys "*" | head -20
docker exec edgeai-redis-1 redis-cli -a "$RP" --no-auth-warning get odds_api:remaining

# Stats de paris en DB
docker exec edgeai-postgres-1 psql -U edgeai -d edgeai -c "
SELECT sport, status, COUNT(*) FROM matches GROUP BY 1,2;
"
```

## Rapport hebdomadaire par email

Un rapport HTML est envoyé automatiquement chaque **jeudi à partir de 21h Europe/Paris** par email à `NOTIFICATION_EMAIL_TO` via Brevo (réutilise l'infra notifications existante).

**Contenu** :
- KPIs semaine en cours (ROI, P&L, hit rate, CLV) + delta vs semaine -1
- État global du modèle sur 730 jours (ROI cumul, drawdown max, sample size)
- Top 3 victoires + Top 3 défaites de la semaine
- Recommandations à venir (matchs 7j avec value bets)
- Alertes auto si ROI semaine < -10%, drawdown > 70%, ou CLV moyen < -2%

**Mécanisme** :
- Le pipeline ml_worker tourne toutes les 6h. La fonction `send_weekly_report_if_due()`
  dans `ml/pipeline/weekly_report.py` vérifie à chaque cycle si on est jeudi 21h+.
- Lock Redis (`weekly_report:YYYY-WNN`) empêche les doublons : 1 envoi par semaine ISO.
- Si le cycle de 21h tombe sur jeudi → envoi cette nuit-là. Sinon : prochain cycle (max ~3h plus tard).

**Reconfiguration du destinataire** :
```bash
# dans /opt/edgeai/.env
NOTIFICATION_EMAIL_TO=nouvel-email@example.com
docker compose -f docker-compose.yml -f docker-compose.prod.yml restart ml_worker
```

**Force un envoi pour test** (one-shot, ignore le lock) :
```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  run --rm ml_worker python -c "
import asyncio, os
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
import redis.asyncio as aioredis
from pipeline.weekly_report import _build_report_html, _kpis_window, _fetch_upcoming_value_bets, _send_brevo_email
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
PARIS_TZ = ZoneInfo('Europe/Paris')

raw = os.environ['DATABASE_URL']
url = raw.replace('postgresql://', 'postgresql+asyncpg://').split('?')[0]
engine = create_async_engine(url, pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False)

class S:
    value_bet_leagues=['Ligue 1', 'Bundesliga', 'Serie A']
    value_bet_ou_leagues=[]
    value_bet_ah_leagues=['Ligue 1', 'Premier League', 'Serie A']
    value_bet_edge_min=0.05
    value_bet_edge_max=0.20

async def main():
    settings = S()
    async with Session() as s:
        now = datetime.now(PARIS_TZ)
        monday = (now - timedelta(days=now.weekday())).replace(hour=0,minute=0,second=0,microsecond=0)
        prev_monday = monday - timedelta(days=7)
        week = await _kpis_window(s, settings, monday.astimezone(timezone.utc), now.astimezone(timezone.utc), 0.05)
        prev = await _kpis_window(s, settings, prev_monday.astimezone(timezone.utc), monday.astimezone(timezone.utc), 0.05)
        glob = await _kpis_window(s, settings, now.astimezone(timezone.utc) - timedelta(days=730), now.astimezone(timezone.utc), 0.05)
        glob['edge_min_pct'] = 5
        glob['max_drawdown_pct'] = 38.0  # approximation pour test
        upcoming = await _fetch_upcoming_value_bets(s, settings, 0.05)
        html = _build_report_html(week, prev, glob, upcoming, os.environ.get('APP_BASE_URL', 'https://edgeai-betting.duckdns.org'), f'du {monday.strftime(\"%d/%m\")} au {now.strftime(\"%d/%m\")}')
        ok = await _send_brevo_email(
            os.environ['BREVO_API_KEY'],
            os.environ['NOTIFICATION_EMAIL_FROM'],
            os.environ['NOTIFICATION_EMAIL_TO'],
            f'[TEST] edgeAI hebdo : ROI {week[\"roi_percent\"]:+.1f}%',
            html,
        )
        print('OK' if ok else 'FAIL')
asyncio.run(main())
"
```

## Maintenance hebdo recommandée

À mettre dans un cron côté hôte (pas encore fait) :

```bash
# Backup DB
docker exec edgeai-postgres-1 pg_dump -U edgeai edgeai \
  | gzip > /opt/backups/edgeai-$(date +%Y%m%d).sql.gz

# Garde 30 derniers backups
find /opt/backups -name "edgeai-*.sql.gz" -mtime +30 -delete

# Clean Docker (recommandé après plusieurs deploys)
docker image prune -af --filter "until=168h"  # supprime images > 7 jours
docker builder prune -af --filter "until=72h" # cache > 3 jours
```

## Restauration en cas de drame

1. Stack down :
```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml down
```

2. Restore backup DB :
```bash
gunzip -c /opt/backups/edgeai-YYYYMMDD.sql.gz | \
  docker exec -i edgeai-postgres-1 psql -U edgeai -d edgeai
```

3. Restart :
```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

Sans backup automatisé actuellement, donc en cas de drame total → re-importer l'historique avec `ml/import_matches_to_prod.py` puis re-run `backfill_predictions.py` + `backfill_odds.py`. Compte 30-45 min.
