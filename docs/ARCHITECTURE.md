# Architecture edgeAI

Tout ce qu'il faut comprendre pour modifier les modèles ML, le flux de données, ou le schéma DB.

## Vue d'ensemble — le cycle complet

```
                ┌─────────────────────────────────────────────┐
                │            ml_worker (cron 1×/heure)        │
                └─────────────────────────────────────────────┘
                              │
                              ▼
         ┌─────────────────────────────────────────────────────┐
         │  1. INGESTION (external APIs)                       │
         │  ───────────────────────────                        │
         │  • football-data.org → matchs upcoming + finished   │
         │  • the-odds-api → cotes 1X2/OU/AH (foot) + h2h/totals (NBA) │
         │  • football-data.co.uk (backfill historique)        │
         └─────────────────────────────────────────────────────┘
                              │
                              ▼
         ┌─────────────────────────────────────────────────────┐
         │  2. FEATURE ENGINEERING                             │
         │  ──────────────────────                             │
         │  • FOOT_STATE.compute_features_sync(home, away, date) │
         │     → 67 features Phase 2 (ELO, rolling stats, shots) │
         │     → ou 52 features Phase 1 (sans shots/SOT)        │
         │  • compute_nba_features() pour la NBA               │
         └─────────────────────────────────────────────────────┘
                              │
                              ▼
         ┌─────────────────────────────────────────────────────┐
         │  3. INFÉRENCE                                       │
         │  ───────────                                        │
         │  • Foot 1X2 : Dixon-Coles per-league (priorité)     │
         │     fallback XGB si équipe inconnue dans la ligue   │
         │  • Foot OU 2.5 : XGB calibré (52 features Phase 1)  │
         │  • Foot AH : XGB calibré (67 features Phase 2)      │
         │  • NBA 1X2 : EdgeAIModelNBA (XGB)                   │
         │  • NBA Totals : CalibratedClassifierCV binaire     │
         └─────────────────────────────────────────────────────┘
                              │
                              ▼
         ┌─────────────────────────────────────────────────────┐
         │  4. UPSERT en DB                                    │
         │  ────────────                                       │
         │  • matches (cotes + scores + line totals)           │
         │  • predictions (prob_home/draw/away/over/under/ah)  │
         │  • opening_* : capturé 1ère fois pour CLV           │
         └─────────────────────────────────────────────────────┘
                              │
                              ▼
                  Frontend Next.js consulte
                  via FastAPI /api/v1/*
```

Le pipeline complet tourne en ~30 secondes pour 5 ligues foot + NBA. Logs structurés (structlog) avec préfixes lisibles : `foot_inference_loaded`, `nba_upcoming_ingested`, etc.

## Schéma DB (Prisma)

3 tables principales :

### `matches`
Une ligne par match (foot ou NBA), créée à l'ingestion, mise à jour à chaque ré-ingestion.

```sql
matches(
  id UUID PK,
  external_id TEXT UNIQUE,  -- "foot:1234" ou "nba:abcde"
  sport TEXT,               -- "FOOTBALL" ou "NBA"
  league TEXT,              -- "Ligue 1", "NBA", etc.
  home_team, away_team TEXT,
  match_date TIMESTAMP,
  status TEXT,              -- "SCHEDULED" | "FINISHED"
  home_score, away_score INT,

  -- Cotes closing (mises à jour à chaque ingestion)
  home_odds, draw_odds, away_odds DOUBLE,
  over_25_odds, under_25_odds DOUBLE,   -- réutilisé pour NBA totals
  nba_total_line DOUBLE,                -- ligne du book NBA (ex 224.5)
  ah_line, ah_home_odds, ah_away_odds DOUBLE,

  -- Cotes opening (figées au 1er fetch) pour calcul CLV
  opening_home_odds, opening_draw_odds, opening_away_odds DOUBLE,
  opening_over_25_odds, opening_under_25_odds DOUBLE,
  opening_nba_total_line DOUBLE,
  opening_ah_line, opening_ah_home_odds, opening_ah_away_odds DOUBLE,
  opening_captured_at TIMESTAMP,

  -- Phase 2 features bruts (shots/SOT/corners)
  home_shots, away_shots, home_shots_on_target, away_shots_on_target,
  home_corners, away_corners INT
)
```

### `predictions`
Une ligne par prédiction. Plusieurs prédictions possibles par match (versions de modèle différentes). Le tracking prend la plus récente via `JOIN LATERAL ... ORDER BY computed_at DESC LIMIT 1`.

```sql
predictions(
  id UUID PK,
  match_id UUID FK,
  model_version TEXT,       -- "dc_20260518", "backfill_dc_20260517", etc.
  prob_home, prob_draw, prob_away DOUBLE,
  prob_over_25, prob_under_25 DOUBLE,  -- foot O/U 2.5 OU NBA totals (selon sport)
  prob_ah_home, prob_ah_away DOUBLE,
  confidence DOUBLE,
  shap_values JSONB,        -- pour /admin/explain
  computed_at TIMESTAMP
)
```

**Convention `model_version`** :
- `dc_YYYYMMDD` : Dixon-Coles foot 1X2
- `perleague_<ligue>_YYYYMMDD_HHMMSS` : XGB per-league (Serie A actif)
- `xgb_foot_YYYYMMDD` : XGB foot global fallback
- `nba_YYYYMMDD` : NBA 1X2
- `backfill_*` : préfixe pour les prédictions rétroactives (cf. `ml/backfill_predictions.py`)

### `bets`
Paris placés par l'utilisateur (différent des recommandations). Schema dans `prisma/schema.prisma`.

## Modèles ML

### Dixon-Coles (foot 1X2) — `ml/dixon_coles.py`

Modèle bivarié Poisson avec correction τ pour low scores (Dixon-Coles 1997). Bat XGBoost de +4 pts ROI sur le backtest.

**Pourquoi** : XGBoost capte bien les patterns moyens mais traite chaque match comme indépendant. DC capture explicitement les paramètres attack/defense par équipe et la corrélation des scores faibles (0-0, 1-1, etc. sont plus fréquents que ce que Poisson seul prédit).

**Per-league** : on fit DC séparément pour chaque ligue car les équipes ne jouent qu'entre elles. Un pool global biaiserait les attack ratings.

**Config finale** :
```python
fit(df,
    decay_half_life=180.0,    # demi-vie 6 mois pour le poids des matchs récents
    reg_lambda=0.05,          # L2 sur attack/defense
    gamma_prior=0.30,         # avantage domicile a priori 0.30
    gamma_prior_strength=100, # force du prior (assez fort pour converger)
    min_team_games=20         # filtre les équipes avec < 20 matchs
)
```

**Convergence** : 5 itérations historiques pour trouver la bonne config (cf. commits avril-mai 2026). Smart init from empirical goal ratios + L2 + gamma prior strength=100 a fini par donner γ ∈ [0.17, 0.27] selon ligue.

### XGBoost (foot OU, AH, NBA) — `ml/pipeline/model.py` + scripts dédiés

`CalibratedClassifierCV` (sigmoid/Platt) wrappant un `XGBClassifier`. Validation `TimeSeriesSplit` (5 folds pour entraînement, 3 pour l'auto-retrain).

**Features foot** : 67 fields (Phase 2 = Phase 1 + shots/SOT/corners). Pour OU on bascule en Phase 1 (52 fields) car les shots dégradent (-12pts ROI au backtest — contre-intuitif mais empirique).

**Auto-retrain** : `maybe_auto_retrain_*` dans `ml/pipeline/trainer.py`. Conditions :
- ≥ `RETRAIN_MIN_SAMPLES` (= 50) nouveaux samples depuis le dernier retrain
- cooldown 24h respecté (`RETRAIN_COOLDOWN_HOURS`)
- nouveau log_loss ne régresse pas de plus de 5% vs current (`MAX_LOG_LOSS_REGRESSION`)
- seuils absolus : log_loss < 1.10, accuracy > 0.44
- Bypass de la régression si `features_hash` a changé (schema features modifié)

Le cycle ml_worker check ces conditions à chaque tour. Sur prod ça retrain ~1-2×/semaine.

> **Explication détaillée** de la conception et de l'entraînement quotidien des
> modèles : voir `docs/MODELS.md`.

### NBA Totals — `ml/nba_totals_pipeline.py`

Modèle binaire `P(total_points > closing_line)`. Source historique : sportsbookreviewsonline.com (2020-21 à 2022-23). Cotes Over/Under standard -110 = 1.91.

L'inférence live utilise la **ligne du bookmaker** (capturée par odds-api). Le modèle prédit une proba en regardant les features du match (forme, pace, défense récente) — la ligne ne lui est pas explicitement passée car les features sont calibrées sur le passé.

## Flux value betting

Calcul de l'**edge** = `probabilité_modèle × cote_marché − 1`. Si > 0, on a un avantage théorique.

Le tracking (`backend/app/api/routes/tracking.py`) calcule pour chaque match :
1. Tous les outcomes value (edge dans `[edge_min, edge_max]`)
2. Garde le meilleur edge
3. Calcule la mise Kelly fractionnelle : `f* = (p × b − q) / b`, plafonnée à `MAX_STAKE_FRACTION × bankroll`, fraction `KELLY_FRACTION = 0.25`
4. Si match FINISHED → calcule P&L réel

Constants dans `tracking.py` (hardcodés, pas dans config car référence "stratégie déployée") :
```python
INITIAL_BANKROLL = 100.0
KELLY_FRACTION = 0.25       # ¼ Kelly = compromise variance/croissance
MAX_STAKE_FRACTION = 0.05   # cap 5% bankroll par pari
EDGE_MIN = 0.08             # (override par config.value_bet_edge_min)
EDGE_MAX = 0.20
```

L'endpoint `/tracking/edge-sweep` rejoue le même calcul pour plusieurs seuils d'edge (`[0.02, 0.03, 0.05, 0.08, 0.10, 0.12, 0.15, 0.20]`) pour permettre la calibration empirique.

## CLV (Closing Line Value)

Formule : `(opening_odds / closing_odds) − 1`.

- **Positif** = la cote a baissé entre l'opening (notre détection) et le closing (kickoff). Le marché valide notre prédiction → on a anticipé.
- **Négatif** = la cote a monté → mauvaise détection.

Le CLV moyen est le **gold standard des pros** pour valider qu'un modèle a un vrai alpha indépendamment de la chance court terme. Sur un sample ≥ 30 paris avec CLV moyen > 0%, le modèle bat le marché à long terme.

Aujourd'hui (mai 2026) le CLV moyen edgeAI est +1.31% à 48% positif → faiblement positif. Insuffisant pour confirmer le sweet spot edge sans plus de live data.

## Pourquoi le backfill ?

Le forward tracking pur (sans backfill) aurait pris 12-18 mois avant d'avoir un sample stat significatif (≥ 500 paris settled). Pour accélérer :

1. **`ml/backfill_predictions.py`** : génère des prédictions rétroactives sur tous les matchs FINISHED historiques (730 jours = 2 saisons). Utilise les modèles actuellement déployés. **Caveat** : data leak partiel (le modèle DC a été entraîné sur tout l'historique → fait des "prédictions" qu'il a déjà vues). Le ROI absolu est gonflé, **mais le ranking inter-edge reste valide** car le biais affecte tous les seuils similairement.

2. **`ml/backfill_odds.py`** : populer les colonnes `home_odds`/`draw_odds`/`away_odds`/`over_25_odds`/etc. à partir des CSV football-data.co.uk (gratuits, complet 2020-2025). Sans ces cotes, les prédictions ne peuvent pas être valuées en value bet.

Résultat : 958 paris settled au tracking 2 ans, sweet spot empirique edge 5%, drawdown 46%, ROI +5.5%. Décision : passer edge_min de 0.08 → 0.05 (commit 124372c).

**Recalibrage prévu** : 28 juin 2026 (6 semaines de forward tracking propre depuis le changement).

## Décisions techniques principales

1. **Postgres self-hosted** (migré depuis Prisma Cloud le 17 mai 2026 pour cause de plan limit reached). 35× plus rapide à l'import historique. Backup à mettre en place via `pg_dump` (cf. ROADMAP).

2. **Pas de Recommendation table dédiée** : les value bets sont calculées à la volée depuis `predictions` + `matches`. Permet d'ajuster les filtres edge sans backfill.

3. **Frontend en SSR Next.js** mais avec ignoreBuildErrors=true pour cause de bugs Next.js 15 avec certains imports recharts. À nettoyer.

4. **Chatbot pédagogique** (Claude Haiku 4.5) : 20 questions/heure/user, system prompt avec glossaire complet (Kelly, CLV, AH, DC, etc.). Pas de conversation persistée côté serveur — localStorage côté browser (MAX 50 messages).

5. **Pas de tests unitaires** sur le backend (legacy). Le ml/ a quelques tests dispersés. À muscler (cf. ROADMAP).

## Conventions de code

- **Python** : structlog pour logs (préfixes `feature_module_action`, ex `nba_odds_fetched`). Pas de print en prod.
- **Type hints** : obligatoires sur tout code Python ajouté. Le runtime utilise pydantic v2 pour les inputs API.
- **Tests SQL** : préférer `ON CONFLICT DO NOTHING` ou `COALESCE(EXCLUDED.col, table.col)` pour ne jamais écraser de la donnée prod sans intention.
- **Commits** : `type(scope): description` (feat, fix, doc, refactor, tune, chore). En français.
