# edgeAI — Plateforme de paris sportifs par IA

**Objectif** : détecter des value bets sur le football (1X2 / Asian Handicap / O/U) et la NBA (Moneyline / Totals) en comparant les probabilités d'un modèle ML aux cotes des bookmakers. Recommandations affichées avec mise Kelly fractionnelle (¼ Kelly).

> **Dernier état stable** : 18 mai 2026 — backfill 2 ans, ROI live +3.5% sur 958 paris settled, sweet spot edge à 5%, modèles déployés DC + XGB(OU/AH) + NBA(1X2/Totals).

## Pour démarrer

- **Tu reprends le projet ?** → lis `docs/ARCHITECTURE.md` (modèles, flux de données, schéma DB, décisions techniques)
- **Tu veux déployer ou debugger ?** → `docs/OPERATIONS.md` (deploy VPS, commands utiles, troubleshooting récurrent)
- **Tu cherches quoi faire ensuite ?** → `docs/ROADMAP.md` (todos prioritaires, limites connues, idées d'évolution)

## Quick start (dev local)

```bash
# 1. Copier .env
cp .env.example .env
# Remplir : DATABASE_URL, REDIS_URL, JWT_SECRET, ODDS_API_KEY,
#          FOOTBALL_DATA_API_KEY, ANTHROPIC_API_KEY

# 2. Lancer la stack
docker compose up -d postgres redis backend frontend ml_worker

# 3. Schema Prisma
docker run --rm --network edgeai_default -v $(pwd):/app -w /app \
  -e DATABASE_URL="$DATABASE_URL" node:20-alpine \
  npx -y prisma@6 db push --skip-generate

# 4. Accès
# Frontend : http://localhost:3000
# API docs : http://localhost:8000/docs
```

Login par défaut : aucun seed user — crée le tien via `/register`.

## Architecture en une image

```
┌────────────┐    ┌──────────┐    ┌─────────────┐
│  Next.js   │───▶│ FastAPI  │───▶│  Postgres   │
│ (frontend) │    │ (backend)│    │  + Redis    │
└────────────┘    └──────────┘    └─────────────┘
                        ▲                ▲
                        │ reads          │ writes
                        │                │
                  ┌─────────────────────────────┐
                  │   ml_worker (cron hourly)   │
                  │  - fetch odds-api/football  │
                  │  - generate predictions     │
                  │  - upsert matches + bets    │
                  └─────────────────────────────┘
                        │
                        ▼
                  ┌─────────────┐
                  │  Modèles    │
                  │  joblib     │
                  │  DC + XGB   │
                  │  + NBA      │
                  └─────────────┘
```

3 services autonomes en `docker compose` :
- **frontend** (Next.js 15, SSR) sert l'UI sur le port 3000
- **backend** (FastAPI async, sqlalchemy+asyncpg) sert l'API sur le port 8000
- **ml_worker** (Python long-running, APScheduler) tourne en boucle, ingère les données externes et génère les prédictions

Le **frontend ne parle jamais directement à la DB**. Tout passe par l'API REST `/api/v1/*`.

Le **backend ne fait pas d'inférence ML** — il lit seulement les prédictions persistées en DB par le ml_worker.

## Stack

| Couche | Tech | Pourquoi |
|---|---|---|
| Frontend | Next.js 15 App Router + React Query | SSR + cache client efficace |
| UI | Tailwind + lucide-react + recharts | Stack standard, rapide |
| Backend | FastAPI (Pydantic v2) | async + OpenAPI gratis |
| DB | Postgres 16 self-hosted | Migré depuis Prisma Cloud (mai 2026) |
| Cache | Redis 7 | Locks d'ingestion + cache backtest |
| ML | XGBoost CalibratedClassifierCV + Dixon-Coles maison | DC bat XGB de +4pts ROI sur foot 1X2 |
| Auth | JWT custom (RS256) | Géré dans `backend/app/core/security.py` |
| Chatbot | Anthropic Claude Haiku 4.5 | Glossaire pédagogique pour débutants |
| Paiements | Stripe (PRO/ELITE) | Plans dans `backend/app/api/routes/billing.py` |
| Hébergement | Hetzner CCX13 (Docker Compose) | 2 vCPU, 8GB, 80GB SSD — €30/mois |
| Reverse proxy | duckdns + nginx | edgeai-betting.duckdns.org |
| Monitoring | Sentry + structlog | Erreurs front et back capturées |

## Marchés actifs (mai 2026)

| Marché | Modèle | Ligues whitelistées | edge_min | Statut |
|---|---|---|---|---|
| Foot 1X2 | Dixon-Coles per-league | Ligue 1, Bundesliga, Serie A | 5% | ✓ actif |
| Foot AH | XGBoost calibré | Ligue 1, Premier League, Serie A | 5% | ✓ actif |
| Foot O/U 2.5 | XGBoost calibré | — | 5% | ❌ désactivé (perd -8% ROI) |
| NBA 1X2 | XGBoost calibré | NBA | 5% | ✓ actif |
| NBA Totals | XGBoost calibré binaire | NBA | 5% | ✓ actif (déployé 18 mai) |

Config dans `backend/app/core/config.py`. Cap edge_max = 20% (filtre cotes overpriced).

## Tests en cours / décisions futures

- **2026-06-28** : recalibrer `edge_min` après 6 semaines de forward tracking propre (cf. commentaire dans `config.py` + tâche #11)
- **Continu** : si le ROI live à 5% reste > +3%, garder. Sinon remonter à 8% et refit DC en rolling-window pour éliminer le leak.

## Limitations connues

- **The Odds API quota** : 500 req/mois en plan gratuit. **Actuellement épuisé** (reset le 1er juin 2026). Foot continue via l'API football-data.org (clé indépendante). NBA en pause jusqu'au reset. Voir `docs/OPERATIONS.md` → section quotas.
- **Données NBA scraping** : pas de backfill historique 2 ans pour NBA (contrairement au foot via football-data.co.uk). Le tracking NBA sera petit jusqu'au début de saison régulière (octobre).
- **Per-league Serie A** : modèle dédié actif, +5.87% ROI au backtest mais 67 features (Phase 2 shots). Les autres ligues utilisent le modèle global (qui les bat).
- **Drift detection** : `drift_check status=no_deployed_model` dans les logs ml_worker = pas critique, juste pas encore activé.

## Structure du repo

```
edgeAI/
├── README.md                     # ce fichier
├── docs/
│   ├── ARCHITECTURE.md           # détails modèles, flux, schema DB
│   ├── OPERATIONS.md             # deploy, debug, commands utiles
│   └── ROADMAP.md                # todos + idées
│
├── docker-compose.yml            # base : postgres, redis, backend, ml_worker, frontend
├── docker-compose.prod.yml       # overrides prod (ports loopback, env file)
│
├── prisma/
│   └── schema.prisma             # schema DB (matches, predictions, bets, users)
│
├── backend/
│   ├── app/
│   │   ├── main.py               # FastAPI app + router includes
│   │   ├── core/
│   │   │   ├── config.py         # ⚙ TOUTES les flags + thresholds (edge_min, whitelist, etc.)
│   │   │   ├── deps.py           # get_db, get_current_user
│   │   │   ├── security.py       # JWT
│   │   │   └── redis.py          # connexion Redis
│   │   ├── api/routes/
│   │   │   ├── auth.py           # register/login/refresh
│   │   │   ├── matches.py        # GET /matches/upcoming, /matches/{id}/analysis
│   │   │   ├── recommendations.py# GET /recommendations
│   │   │   ├── tracking.py       # 🎯 GET /tracking/live + /tracking/edge-sweep
│   │   │   ├── backtest.py       # GET /backtest/latest (lit Redis cache)
│   │   │   ├── chat.py           # 💬 POST /chat/message (Anthropic Haiku)
│   │   │   ├── admin.py          # observability, explain SHAP
│   │   │   └── billing.py        # Stripe webhooks
│   │   └── db/
│   │       └── session.py        # AsyncEngine sqlalchemy
│   └── requirements.txt
│
├── frontend/
│   ├── src/
│   │   ├── app/
│   │   │   ├── (app)/
│   │   │   │   ├── dashboard/    # KPIs + opportunités
│   │   │   │   ├── today/        # 🔥 page principale : value bets du jour
│   │   │   │   ├── tracking/     # 🎯 forward test live + edge sweep
│   │   │   │   ├── backtest/     # résultats backtest historique
│   │   │   │   ├── model/        # SHAP + perf modèle
│   │   │   │   ├── plan/         # Mon Plan (PRO/ELITE)
│   │   │   │   └── settings/     # profil + bankroll
│   │   │   └── (auth)/           # login/register/onboarding
│   │   ├── components/
│   │   │   ├── ChatBubble.tsx    # 💬 chatbot pédagogique flottant
│   │   │   └── ExplainModal.tsx  # Modal modèle vs marché + SHAP
│   │   ├── lib/api.ts            # client axios + endpoints typés
│   │   └── store/auth.ts         # Zustand auth
│   ├── next.config.js
│   └── package.json
│
├── ml/
│   ├── pipeline/
│   │   ├── scheduler.py          # 🚀 entry point : cron hourly, ingère tout, prédit, upsert
│   │   ├── ingestion.py          # football-data.org + odds-api foot
│   │   ├── nba_ingestion.py      # NBA odds via the-odds-api (h2h + totals)
│   │   ├── football_inference.py # FOOT_STATE global : ELO + standings + rolling features
│   │   ├── nba_features.py       # NBAFeatures dataclass
│   │   ├── features.py           # MatchFeatures dataclass (67 fields foot)
│   │   ├── model.py              # EdgeAIModel (XGB wrapper foot)
│   │   ├── nba_model.py          # EdgeAIModelNBA (wrapper)
│   │   └── trainer.py            # maybe_auto_retrain_* (1X2, OU, AH)
│   │
│   ├── dixon_coles.py            # ⭐ modèle DC custom : bivariate Poisson + tau
│   ├── train_dc.py               # entraînement per-league DC (5 ligues)
│   ├── train_per_league.py       # XGB per-league (Serie A uniquement actif)
│   ├── backtest.py               # backtest 1X2 historique
│   ├── ou_train_model.py         # train OU
│   ├── ah_pipeline.py            # train AH + backtest
│   ├── nba_backtest.py           # backtest NBA 1X2
│   ├── nba_totals_pipeline.py    # train + backtest NBA Totals
│   ├── build_features.py         # construit dataset.csv à partir de la DB
│   ├── backfill_predictions.py   # ⭐ génère prédictions rétroactives (data leak doc)
│   ├── backfill_odds.py          # ⭐ populer cotes historiques depuis football-data.co.uk
│   ├── import_matches_to_prod.py # import 18k matchs historiques en DB
│   └── artifacts/models/         # modèles déployés (joblib)
│
├── deploy/
│   ├── post-pull.sh              # ⭐ déploiement complet : pull + rebuild + retrain + restart
│   └── migrate-to-local-postgres.sh
│
└── package.json                  # Prisma CLI (au niveau racine)
```

Les fichiers marqués ⭐ ou 🎯 sont des points d'entrée importants pour comprendre le projet.

## Comment l'argent rentre (business model)

Plans Stripe gérés dans `backend/app/api/routes/billing.py` :

| Plan | Prix | Limite | Marché cible |
|---|---|---|---|
| FREE | 0€ | 3 recommandations/jour | Acquisition |
| PRO | 9.99€/mois | Illimité | Parieur récréatif sérieux |
| ELITE | 19.99€/mois | + alertes WebSocket + API | Sharp / pro |

L'utilisateur `muminoun@icloud.com` est en ELITE pour les tests internes.

## Crédits

Projet personnel de [@Khadimou](https://github.com/Khadimou) (Rassoul Diop, dioprassoul@gmail.com). Déployé sur Hetzner depuis avril 2026.

Code et modèles sous licence MIT. Pas de garantie sur les performances de pari — **les paris comportent un risque de perte**.
