# Modèles IA d'edgeAI — conception & entraînement quotidien

Ce document explique **comment les modèles sont conçus** et **comment ils s'entraînent
chaque jour pour devenir plus performants**. C'est le cœur technique de la plateforme.

> Pour le contexte d'architecture global (DB, flux, déploiement) voir `ARCHITECTURE.md`.
> Pour l'exploitation (retrain manuel, troubleshooting) voir `OPERATIONS.md`.

---

## 1. Philosophie : pourquoi des modèles, et qu'est-ce qu'ils prédisent

Le but n'est **pas** de prédire qui gagne — c'est de trouver les **value bets** :
des paris où la **probabilité estimée par le modèle** est supérieure à celle
**implicite dans la cote du bookmaker**.

```
edge = probabilité_modèle × cote_marché − 1
```

Si `edge > 0`, le pari a une espérance positive à long terme. On ne mise que si
l'edge est dans `[5%, 20%]` (en-dessous = bruit/vigorish, au-dessus = cote
probablement erronée → piège).

**Conséquence sur la conception** : ce qui compte n'est pas tant l'*accuracy*
(deviner le bon résultat) que la **calibration** — c'est-à-dire que quand le
modèle dit "45% de chances", l'événement doit vraiment arriver 45% du temps.
Un modèle bien calibré mais peu précis bat un modèle précis mais mal calibré
pour le value betting. C'est pourquoi tous nos modèles XGBoost sont wrappés
dans un `CalibratedClassifierCV`.

---

## 2. Les modèles par marché

| Marché | Modèle | Pourquoi ce choix |
|---|---|---|
| **Foot 1X2** | Dixon-Coles (maison) | Capture explicitement attaque/défense par équipe + corrélation des scores faibles. Bat XGBoost de +4 pts ROI au backtest. |
| **Foot O/U 2.5** | XGBoost calibré (52 features Phase 1) | Les shots/SOT dégradent l'OU (-12 pts ROI au backtest), donc on reste en Phase 1. *Désactivé en prod (perdant).* |
| **Foot Asian Handicap** | XGBoost calibré (67 features Phase 2) | Les shots/SOT boostent l'AH (+2 pts ROI). |
| **NBA Moneyline (1X2)** | XGBoost calibré | Features pace/efficacité/forme. |
| **NBA Totals (Over/Under)** | XGBoost binaire calibré | Prédit P(points totaux > ligne du book). |

Deux familles d'algorithmes, deux philosophies, détaillées ci-dessous.

---

## 3. Feature engineering : de la donnée brute aux features

Avant tout modèle, on transforme les matchs bruts en **features** (variables
explicatives). C'est souvent là que se gagne ou se perd la performance.

### Football — `MatchFeatures` (67 champs, `ml/pipeline/features.py`)

Calculées par `FOOT_STATE.compute_features_sync(home, away, date, league)` qui
n'utilise QUE les matchs **antérieurs** à la date (pas de fuite de données).

Familles de features :
- **ELO** : un rating par équipe mis à jour après chaque match (système d'échecs
  adapté au foot). Capture la force relative globale.
- **Forme récente** : moyennes glissantes sur 5/10 derniers matchs (buts marqués,
  encaissés, résultats), pondérées par recency.
- **Pythagorean expectation** : ratio buts marqués²/(marqués²+encaissés²),
  proxy de la "vraie" force vs résultats chanceux.
- **form_vs_expected** : écart entre la forme récente et ce que l'ELO prédisait
  → détecte les équipes en sur/sous-performance (régression à la moyenne à venir).
- **Phase 2 (shots/SOT/corners)** : tirs, tirs cadrés, corners glissants. Signal
  de domination plus stable que les buts (moins de variance).
- **Contexte** : avantage domicile, jours de repos, position au classement.

**Phase 1 (52 features)** = tout sauf shots/SOT/corners.
**Phase 2 (67 features)** = Phase 1 + shots/SOT/corners.
Le choix Phase 1 vs Phase 2 est par marché (cf. tableau §2), validé empiriquement
par backtest.

### NBA — `NBAFeatures` (`ml/pipeline/nba_features.py`)

Même principe : `compute_nba_features()` sur l'historique antérieur. Features de
pace (rythme de jeu), efficacité offensive/défensive, forme récente, repos.

---

## 4. Dixon-Coles en détail (le modèle phare du foot 1X2)

### Le principe

Modèle statistique de 1997 (Dixon & Coles), standard chez les parieurs pros.
Il modélise le nombre de buts de chaque équipe comme deux **lois de Poisson**
corrélées :

```
buts_domicile ~ Poisson(λ_home)
buts_extérieur ~ Poisson(λ_away)

λ_home = exp(attaque_home − défense_away + avantage_domicile)
λ_away = exp(attaque_away − défense_home)
```

Chaque équipe a deux paramètres appris : sa **force d'attaque** et sa **force
de défense**. À partir de λ_home et λ_away, on calcule la probabilité de chaque
score exact (0-0, 1-0, 2-1...), puis on agrège en P(victoire domicile), P(nul),
P(victoire extérieur).

### La correction τ (tau)

Poisson seul sous-estime les scores faibles corrélés (0-0, 1-1). Dixon-Coles
ajoute un facteur de correction `τ` sur les scores ≤ 1-1, calibré via le
paramètre `ρ` (rho). C'est ce qui le rend supérieur à un Poisson naïf.

### Pourquoi "per-league"

On entraîne **5 modèles DC séparés** (un par ligue) plutôt qu'un global. Raison :
les équipes d'une ligue ne jouent qu'entre elles, donc mélanger les pools
biaiserait les ratings d'attaque (un buteur de Ligue 1 et de Bundesliga ne sont
pas comparables directement).

### Configuration finale (`ml/dixon_coles.py`)

```python
fit(df,
    decay_half_life=180.0,    # demi-vie 6 mois : un match d'il y a 6 mois pèse 2× moins
    reg_lambda=0.05,          # régularisation L2 sur attaque/défense (anti-overfit)
    gamma_prior=0.30,         # avantage domicile a priori = 0.30
    gamma_prior_strength=100, # force du prior (assez fort pour converger)
    min_team_games=20)        # ignore les équipes avec < 20 matchs (data insuffisante)
```

Le `decay_half_life` est important : le modèle **oublie progressivement** le
passé lointain, donc il s'adapte aux changements de forme d'effectif au fil de
la saison. C'est une forme d'apprentissage continu intégrée.

---

## 5. XGBoost + calibration (OU, AH, NBA)

### Le pipeline d'entraînement

```
Données historiques → features → XGBoost → CalibratedClassifierCV (sigmoid)
                                            └─ recalibre les probas brutes
```

XGBoost (gradient boosting d'arbres) capture des interactions non-linéaires
complexes entre features. Mais ses probabilités brutes sont mal calibrées
(trop confiantes). Le `CalibratedClassifierCV` (méthode Platt/sigmoid) les
recalibre pour qu'elles reflètent les vraies fréquences.

### Validation temporelle (anti-fuite)

On utilise **`TimeSeriesSplit`** (5 folds à l'entraînement complet, 3 à l'auto-
retrain) : on entraîne toujours sur le passé, on valide sur le futur. Jamais
l'inverse. C'est crucial — une validation aléatoire classique laisserait fuiter
de l'info du futur et gonflerait artificiellement les performances.

### Métriques suivies (OOF = out-of-fold)

- **log_loss** : pénalise les probas confiantes et fausses (la métrique reine
  pour la calibration)
- **accuracy** : % de bons résultats prédits
- **brier_score** : erreur quadratique sur les probabilités

---

## 6. ⭐ L'entraînement quotidien — comment le modèle s'améliore tout seul

C'est la partie qui répond à "comment ils deviennent plus performants au quotidien".

### Le cycle automatique

À chaque cycle du `ml_worker` (toutes les ~6h), après l'ingestion des nouveaux
matchs terminés, la fonction `maybe_auto_retrain_all()` (`ml/pipeline/trainer.py`)
tente de ré-entraîner les 3 modèles foot (1X2, OU, AH). Pour chacun :

```
1. Combien de nouveaux matchs FINISHED depuis le dernier entraînement ?
2. Le cooldown de 24h est-il respecté ?
3. Si oui aux deux → on ré-entraîne sur TOUT l'historique (matchs récents inclus)
4. On évalue le nouveau modèle (log_loss OOF)
5. GATE de déploiement (cf. §7) : on ne remplace le modèle en prod QUE s'il
   passe les critères de qualité
6. Si validé → déploiement + enregistrement de la version en DB
```

### Les conditions de déclenchement (`trainer.py`)

```python
RETRAIN_MIN_SAMPLES   = 50      # min 50 nouveaux matchs depuis le dernier retrain
RETRAIN_COOLDOWN_HOURS = 24     # max 1 retrain / 24h par marché
```

En pratique sur 5 ligues, on accumule ~50 nouveaux matchs en quelques jours, donc
chaque modèle se ré-entraîne en moyenne **1 à 2 fois par semaine** — dès qu'il y
a assez de matière fraîche, et jamais plus d'1×/jour.

### Pourquoi ça améliore le modèle

1. **Données fraîches** : les nouveaux résultats enrichissent le dataset →
   meilleure estimation des forces actuelles des équipes.
2. **Adaptation aux dynamiques** : transferts, changements d'entraîneur,
   blessures longues → le modèle réapprend les nouvelles forces.
3. **Recency-decay (DC)** : même sans retrain complet, le decay_half_life fait
   que les matchs récents pèsent plus.
4. **Calibration continue** : si le marché ou le championnat dérive (saison
   plus/moins prolifique en buts), la recalibration suit.

### Dixon-Coles : le retrain

Le DC est ré-entraîné séparément via `train_dc.py` (per-league). Le pipeline
`post-pull.sh` le force au déploiement. En routine, le decay assure l'adaptation
continue ; un refit complet périodique (ex. mensuel) garde les ratings frais.

---

## 7. Les gates de déploiement — ne jamais déployer un mauvais modèle

Un retrain ne déploie le nouveau modèle **que s'il passe ces critères**
(`trainer.py`). C'est la sécurité qui empêche une régression de casser la prod.

```python
MAX_DEPLOY_LOG_LOSS    = 1.10   # log_loss au-dessus = refusé (modèle trop mauvais)
MIN_DEPLOY_ACCURACY    = 0.44   # accuracy en-dessous = refusé
MAX_LOG_LOSS_REGRESSION = 0.05  # le nouveau ne peut pas être >5% pire que l'actuel
```

Logique :
- **Seuils absolus** : un modèle dont le log_loss > 1.10 ou l'accuracy < 44% est
  rejeté d'office (qualité insuffisante).
- **Protection anti-régression** : même si les seuils absolus passent, on refuse
  un modèle plus de 5% pire que celui en prod. On ne dégrade jamais volontairement.
- **Bypass si changement de schema** : si on a modifié les features
  (`features_hash` change), on bypasse la protection anti-régression — le nouveau
  modèle est obligatoire car l'ancien tourne sur d'autres features.

Si un retrain est rejeté, le modèle en prod reste inchangé et on loggue
`auto_retrain_rejected` avec la raison. Aucune interruption de service.

---

## 8. Détection de dérive (drift)

À chaque cycle, `check_drift_and_rollback()` surveille si le modèle déployé
dérive (ses prédictions deviennent systématiquement biaisées). Si une dérive
sévère est détectée, un rollback vers une version antérieure est possible.

> Statut actuel (mai 2026) : `drift_check status=no_deployed_model` dans les
> logs = le mécanisme tourne mais n'a pas encore de baseline active. À muscler
> (cf. `ROADMAP.md`).

---

## 9. Cas particulier : per-league pour Serie A

Au-delà du DC per-league, on a aussi un **modèle XGBoost dédié à la Serie A**
(`train_per_league.py`). Le backtest a montré qu'il bat le modèle global de
+5.87% ROI sur cette ligue spécifiquement. Les autres ligues utilisent le modèle
global (qui les bat). C'est un exemple de spécialisation validée par les données :
on n'active un modèle dédié que s'il prouve sa supériorité au backtest.

---

## 10. Comment on mesure si tout ça marche vraiment

Trois niveaux de validation, du moins au plus fiable :

1. **Backtest** (`/backtest`) : performance OOF sur l'historique. Utile mais
   optimiste (le modèle a été tuné sur ces données).
2. **Forward tracking** (`/tracking`) : performance des prédictions RÉELLES de
   la prod sur les matchs joués depuis. Plus honnête que le backtest.
3. **CLV (Closing Line Value)** : est-ce que les cotes bougent dans notre sens
   après qu'on détecte une value ? C'est le **gold standard** : un CLV moyen
   positif sur ≥100 paris prouve un vrai edge, indépendamment de la chance court
   terme. Voir `ARCHITECTURE.md` → section CLV.

**Limite honnête** : le tracking actuel mélange du backfill (avec leak DC partiel)
et du forward live. Le vrai verdict sur l'edge du modèle viendra après plusieurs
mois de forward tracking propre (review prévue 28/06/2026, cf. `ROADMAP.md`).

---

## TL;DR

- **Conception** : Dixon-Coles (foot 1X2, attaque/défense par équipe + correction
  scores faibles) + XGBoost calibré (autres marchés). Tout est optimisé pour la
  **calibration** des probabilités, pas juste l'accuracy.
- **Entraînement quotidien** : le `ml_worker` ré-entraîne automatiquement chaque
  modèle dès qu'il y a ≥50 nouveaux matchs (max 1×/24h), sur tout l'historique
  frais. Un système de **gates** empêche de déployer un modèle qui régresse.
- **Amélioration continue** : données fraîches + recency-decay + recalibration +
  spécialisation par ligue quand c'est prouvé rentable.
- **Honnêteté** : on mesure la vraie performance via le CLV et le forward tracking,
  pas seulement le backtest (optimiste).
