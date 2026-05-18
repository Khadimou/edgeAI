# Roadmap edgeAI

État au 18 mai 2026. Ce qui reste à faire, par priorité et par effort estimé.

## Reviews planifiées

### 🔴 2026-06-01 : Reset quota the-odds-api

Le plan gratuit (500 req/mois) est épuisé. Au 1er juin :
- Vérifier que les credits sont revenus : `docker exec edgeai-redis-1 redis-cli get odds_api:remaining`
- Décider : upgrade à $30/mois (20k req) OU continuer à économiser (allonger les locks de 1h → 6h pour foot)

### 🟡 2026-06-28 : Recalibrage edge_min

6 semaines de forward tracking propre après le changement `edge_min 0.08 → 0.05`. Voir le rappel direct dans `backend/app/core/config.py` et la tâche #11 du système de tâches.

Critères de décision (depuis tracking 60j) :
- ROI live ≥ +3% à edge 5% → modèle valide, garder config
- ROI live < +3% → sweet spot était gonflé par leak DC. Remonter à 0.08 + refit DC en rolling-window
- ROI live négatif → audit modèle, peut-être abandonner DC pour XGB pur

## Priorité 1 — Stabilité / observabilité (à faire avant scale)

### Backups DB automatisés (1-2h)

Aucun backup actuellement. Risque maximal. Cf. `docs/OPERATIONS.md` → section maintenance hebdo. Cron côté hôte + S3/Wasabi pour off-site.

### Drift detection effective (3-4h)

Logs montrent `drift_check status=no_deployed_model`. À implémenter dans `ml/pipeline/drift.py` :
- Compare distribution des probas modèle sur les 30 derniers jours vs baseline
- Alert Sentry si KS-test p < 0.05
- Évite de réagir à un modèle qui dérive sans s'en rendre compte

### Tests unitaires backend (1-2 jours)

Pas de tests. À muscler :
- `tracking.py` : test du calcul Kelly + edge sweep
- `chat.py` : test du rate limiter Redis
- `auth.py` : test JWT refresh flow

Fixtures Postgres via `testcontainers` ou DB in-memory.

## Priorité 2 — Améliorations modèles ML

### Refit DC en rolling-window (1 jour)

Le data leak du DC est le caveat principal du backfill. Pour le tracking historique propre, refit DC à chaque date de prédiction en n'utilisant que les matchs antérieurs. Coûteux à backtester mais possible en batch (1 fit par jour de prédiction, avec décay).

### Re-entraîner Serie A per-league avec 67 features (30 min)

Le modèle Serie A dédié est entraîné mais affichait un warning `per_league_model_schema_mismatch_skipped` jusqu'à ce qu'on retrain avec Phase 2. Maintenant OK (modèle daté du 17 mai 2026). À vérifier dans 1 mois si toujours utilisé (logs `using_per_league_model league=Serie A`).

### Investigation Bundesliga +29.6% ROI (1-2h)

Anomalie : Bundesliga affiche +29.6% ROI sur 126 paris vs +1.8% Serie A et +2.6% Ligue 1. Soit le modèle DC est extraordinaire sur cette ligue, soit calibration biaisée. À analyser :
- distribution des edges détectés par ligue
- répartition des outcomes (HOME/DRAW/AWAY) prédits vs réalisés
- hit rate vs cote moyenne

### Per-league pour Ligue 1 + Premier League (3h)

Actuellement seul Serie A utilise un modèle dédié. À tester pour les autres (sans toucher Bundesliga qui marche bien en global) — entrainer + comparer ROI au backtest, activer seulement si > global.

## Priorité 3 — Features produit

### Backfill historique NBA (1 jour)

Le foot a son backfill 2 ans via football-data.co.uk. NBA n'a aucun backfill historique → tracking NBA reste petit jusqu'à octobre 2026 (saison régulière). Sources possibles :
- sportsbookreviewsonline.com (déjà utilisé pour le training)
- Basketball-reference.com (scrape gratuit, exhaustif)
- NBA Stats API officielle (gratuite, complète)

### Backtests live par ligue / par marché (2-3h)

`/backtest` actuel est un dump global. Ajouter filtres :
- par ligue
- par marché
- par fenêtre temporelle (rolling 30/60/90j)

### Alertes WebSocket (plan ELITE)

Promis dans le plan ELITE (cf. `billing.py`). Pas encore implémenté.

Stack possible : FastAPI WebSocket + Redis pubsub. Émettre un event quand une nouvelle value bet est détectée (edge > 8%).

### Sweet spot edge per-marché (1-2h)

Le sweep edge actuel agrège tous les marchés. Décliner par marché (1X2 / AH / NBA / NBA Totals). Probablement le sweet spot diffère significativement.

## Priorité 4 — Refactors techniques

### Stopper ignoreBuildErrors Next.js (1 jour)

Hack actuel dans `next.config.js` pour contourner des bugs Next.js 15 avec recharts. Investigue + corrige proprement. Permet de bloquer les régressions TypeScript en CI.

### Remplacer Anthropic Haiku par Sonnet sur questions complexes (30 min)

Haiku 4.5 est rapide mais répond parfois à côté sur les questions techniques précises (Kelly, calcul edge). Sonnet 4.5 est 5× plus lent et coûteux mais bien plus précis. Stratégie : router Haiku → Sonnet si la question contient certains keywords ou si l'historique est long.

### Migrer Stripe vers Paddle (1-2 jours)

Stripe demande beaucoup de paperasse pour les jeux d'argent. Paddle est merchant of record (gère la TVA et la conformité). À discuter si on passe en plan payant réel.

### Cleanup `model_perleague_*_latest.joblib` inutilisés (5 min)

Ligue 1, Premier League, Bundesliga, La Liga ont leurs modèles per-league entraînés mais pas utilisés (le global les bat). Garder seulement Serie A pour économiser l'espace disque.

## Priorité 5 — Long terme / nice to have

### App mobile React Native (3-4 semaines)

Promis dans le marketing initial. Le frontend Next.js est déjà responsive, donc pas urgent.

### Expansion sports (variable)

- **Tennis** : modèle déjà entraîné (`ml/tennis_pipeline.py`), pas branché live
- **NHL / MLB** : marchés liquides, modèles à construire
- **Rugby** : marché niche, demande de la côte

### API publique B2B (1-2 semaines)

Permettre à d'autres apps de consommer les prédictions edgeAI via une API key. Tarification par appel ou flat fee.

### White-label B2B (1 mois+)

Vendre la stack à des bookmakers privés / syndicats. Demande surtout du commercial.

## Limites connues (assumed, non-bugs)

1. **CLV peu informatif sur backfill** : opening_odds = closing_odds pour les matchs backfillés (on a juste copié la cote closing). Le vrai CLV ne sera mesurable qu'après 3-6 mois de forward tracking propre.

2. **Pas de hedging / position sizing avancé** : ¼ Kelly fixe, pas de portfolio optimization sur la corrélation entre paris (un match foot avec value sur 1X2 ET AH ET OU = 3 paris corrélés).

3. **Pas de live in-play** : tous les paris sont pre-match. Le live betting demanderait une infra tout autre (WebSocket bookmakers, latence < 1s).

4. **Frontend ne supporte pas le SSR pur** : `ignoreBuildErrors=true` à cause de bugs recharts. À régler avant audit sérieux.

5. **Pas de mode "dry-run"** : les recommandations sont toujours générées avec mises Kelly. Pour quelqu'un qui veut tester sans miser réellement, il doit utiliser `/tracking` (forward test sur bankroll virtuelle 100€).

## Convention pour ajouter une nouvelle tâche

Si tu reprends et identifies un truc à faire :
1. Ajoute-le à ce fichier dans la bonne section
2. Si c'est lié à une décision data-driven future (genre "recalibrer edge_min"), ajoute aussi un commentaire `# REVIEW DUE : YYYY-MM-DD` dans le code concerné
3. Si c'est urgent (bug en prod), gère-le tout de suite et documente le post-mortem ici sous une section "Post-mortems"
