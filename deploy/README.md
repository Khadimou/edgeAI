# edgeAI — Déploiement Hetzner VPS

Guide pour déployer edgeAI sur un VPS Hetzner (Ubuntu 22.04+) pour un fonctionnement autonome 24/7.

## 1. Provisionner le VPS

1. Créer un compte sur [hetzner.com](https://www.hetzner.com/cloud)
2. Créer un VPS :
   - **CX22** (€4.51/mois) — 2 vCPU, 4 GB RAM, 40 GB SSD — suffisant pour le stack ML
   - Image : **Ubuntu 22.04**
   - Datacenter : Falkenstein ou Helsinki (proche Europe)
   - SSH key : ajouter ta clé publique pour login sans password
3. Note l'IP publique du VPS (ex: 195.201.x.x)

## 2. Setup initial

Connecte-toi en SSH :

```bash
ssh root@<vps_ip>
```

Lance le script de setup (installe Docker, clone repo, configure firewall + systemd) :

```bash
curl -fsSL https://raw.githubusercontent.com/Khadimou/edgeAI/main/deploy/hetzner-setup.sh -o setup.sh
bash setup.sh
```

Le script :
- Installe Docker + Docker Compose
- Crée un user `edgeai` non-root
- Configure UFW (ports 22, 80, 443)
- Clone le repo dans `/opt/edgeai`
- Crée `/opt/edgeai/.env` (template à remplir)
- Crée un systemd service `edgeai.service` (auto-start au boot)

## 3. Remplir le .env

```bash
nano /opt/edgeai/.env
```

Remplis :

```env
DATABASE_URL=postgres://user:password@db.prisma.io:5432/postgres?sslmode=require
ODDS_API_KEY=ta_clé_the-odds-api
FOOTBALL_DATA_API_KEY=ta_clé_football-data.org
SECRET_KEY=générer-une-string-random-32-chars-min
PER_LEAGUE_MODEL_LEAGUES=Serie A
```

Pour générer un SECRET_KEY solide :
```bash
openssl rand -hex 32
```

## 4. Démarrer le stack

```bash
systemctl start edgeai
systemctl status edgeai
```

Vérifier les logs :

```bash
cd /opt/edgeai
docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f --tail=50
```

Le pipeline ML va commencer ses cycles automatiquement (toutes les 6h).

## 5. Reverse proxy + HTTPS (optionnel mais recommandé)

Si tu as un nom de domaine pointant sur l'IP du VPS :

```bash
bash /opt/edgeai/deploy/nginx-https.sh ton-domaine.com
```

Le script :
- Configure nginx en reverse proxy (frontend:3000 + backend:8000)
- Obtient un certificat Let's Encrypt
- Active le renouvellement auto

Tu peux ensuite accéder à `https://ton-domaine.com`.

## 6. Mise à jour de l'app

Quand tu push du nouveau code sur GitHub :

```bash
su - edgeai
cd /opt/edgeai
git pull origin main
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

Ou pour automatiser : créer un webhook GitHub qui hit un endpoint qui pull + restart.

## 7. Backups

La DB est sur Prisma cloud → backuppée par eux.

Pour les modèles ML entraînés (`ml/artifacts/`) :
```bash
# Backup vers ton ordi
rsync -avz edgeai@<vps_ip>:/opt/edgeai/ml/artifacts/ ./ml-backup/
```

## 8. Monitoring

- **Logs en direct** : `journalctl -u edgeai -f`
- **Container status** : `docker ps`
- **Disk usage** : `df -h` et `du -sh /opt/edgeai/ml/artifacts/`
- **Page admin** : `https://<ton-domaine>/admin` (vue d'ensemble système)

## Coûts mensuels

| Item | Coût |
|---|---|
| VPS CX22 | €4.51 |
| Domaine .com | ~€1/mois |
| DB Prisma cloud | Free tier |
| Redis | Inclus VPS |
| Backup snapshots Hetzner | €0.45 |
| **Total** | **~€6/mois** |

## Troubleshooting

**Le pipeline ne tourne pas :**
```bash
docker compose logs ml_worker --tail=100
```

**Plus de credits the-odds-api :**
Voir `/admin` → credits remaining. Reset le 1er de chaque mois (free tier).

**Backend timeouts :**
Augmenter resources VPS (CX32 = 8 GB RAM, €7.50/mois) si beaucoup de matchs.

**HTTPS broken après renouvellement :**
```bash
certbot renew --dry-run  # test
systemctl reload nginx
```
