# edgeAI — Plateforme de Paris Sportifs par IA

Plateforme SaaS B2C de conseil en paris sportifs basée sur un modèle prédictif XGBoost et le critère de Kelly fractionnel.

## Architecture

```
edgeAI/
├── frontend/        # Next.js 14 + TypeScript + Tailwind CSS
├── backend/         # FastAPI (Python) + Prisma
├── ml/              # Pipeline ML (XGBoost + MLflow)
├── prisma/          # Schéma base de données
└── docker-compose.yml
```

## Stack technique

| Couche | Technologie |
|--------|-------------|
| Frontend | Next.js 14, TypeScript, Tailwind CSS, Zustand, React Query |
| Backend | FastAPI, Pydantic, Prisma (asyncio) |
| Auth | JWT (RS256) + Supabase |
| Base de données | PostgreSQL + Redis |
| ML | XGBoost, scikit-learn, SHAP, MLflow |
| Paiements | Stripe Billing |
| Infrastructure | Docker Compose (dev) → AWS ECS (prod) |

## Démarrage rapide

### 1. Copier les variables d'environnement

```bash
cp .env.example .env
# Remplir les valeurs dans .env
```

### 2. Lancer avec Docker Compose

```bash
docker-compose up -d
```

### 3. Appliquer le schéma Prisma

```bash
cd backend
prisma generate
prisma db push
```

### 4. Accéder aux services

- **Frontend** : http://localhost:3000
- **API docs** : http://localhost:8000/docs
- **API** : http://localhost:8000/api/v1

## Développement local (sans Docker)

### Backend

```bash
cd backend
pip install -r requirements.txt
prisma generate
uvicorn app.main:app --reload
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

### Pipeline ML

```bash
cd ml
pip install -r requirements.txt
python -m pipeline.scheduler  # Lance le pipeline
```

## Pages frontend

| Route | Description | Plan requis |
|-------|-------------|-------------|
| `/` | Landing page | Public |
| `/register` | Inscription | Public |
| `/login` | Connexion | Public |
| `/onboarding` | Wizard configuration | Connecté |
| `/dashboard` | KPIs + opportunités + matchs | Connecté |
| `/match/[id]` | Analyse détaillée + Kelly | Connecté |
| `/bankroll` | Courbe + historique | Connecté |
| `/history` | Tous les paris + résultats | Connecté |
| `/stats` | ROI, win rate, par ligue | Connecté |
| `/settings` | Profil, bankroll, abonnement | Connecté |

## API Endpoints

```
POST   /api/v1/auth/register
POST   /api/v1/auth/login
POST   /api/v1/auth/refresh
GET    /api/v1/user/me
POST   /api/v1/user/profile
GET    /api/v1/matches/upcoming
GET    /api/v1/matches/{id}/analysis
GET    /api/v1/recommendations/
GET    /api/v1/recommendations/preview
POST   /api/v1/bets/
PATCH  /api/v1/bets/{id}/result
GET    /api/v1/bets/
GET    /api/v1/bankroll/history
GET    /api/v1/stats/performance
POST   /api/v1/webhooks/stripe
```

## Modèle ML

- **Algorithme** : XGBoost multi-classe (H/D/A) + CalibratedClassifierCV (isotonic)
- **Validation** : TimeSeriesSplit (5 folds) — pas de data leakage
- **40 features** : forme récente, xG, H2H, contexte match, signaux de marché
- **Critères** : Log-loss < 0.95, Accuracy > 54%, Brier Score < 0.22
- **Réentraînement** : Hebdomadaire via scheduler automatique
- **Versioning** : MLflow model registry

## Sécurité & Conformité

- JWT RS256 + refresh token rotation (15 min)
- Rate limiting : 100 req/min par user
- Chiffrement AES-256 des données sensibles
- RGPD Art. 17 & 20 : export et suppression des données
- Conformité ANJ : avertissements jeu responsable, vérification +18

## Roadmap

- **Phase 1 (MVP)** : ✅ Infrastructure + ML + API + Frontend
- **Phase 2** : SportRadar xG live, Tennis/Basketball, Alertes WebSocket, API publique
- **Phase 3** : App mobile React Native, White-label B2B, Expansion EU
