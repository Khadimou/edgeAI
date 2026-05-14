#!/bin/bash
# ─────────────────────────────────────────────────────────────────
# Hetzner VPS setup script — Ubuntu 22.04
# À exécuter en root sur un VPS fraîchement provisionné.
#
# Usage :
#   1. ssh root@<vps_ip>
#   2. bash <(curl -fsSL https://raw.githubusercontent.com/Khadimou/edgeAI/main/deploy/hetzner-setup.sh)
#
# Ou manuellement : copier ce fichier sur le VPS et `bash hetzner-setup.sh`
# ─────────────────────────────────────────────────────────────────
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Khadimou/edgeAI.git}"
APP_DIR="${APP_DIR:-/opt/edgeai}"
APP_USER="${APP_USER:-edgeai}"

echo "=================================================="
echo "edgeAI — Hetzner VPS setup"
echo "=================================================="

# ─── 1. Mise à jour système + paquets de base ──────────────────
echo "[1/7] Mise à jour système..."
apt-get update -y
apt-get upgrade -y
apt-get install -y \
    ca-certificates curl gnupg lsb-release \
    git ufw fail2ban htop nano \
    nginx certbot python3-certbot-nginx

# ─── 2. Docker + Compose ──────────────────────────────────────
echo "[2/7] Installation Docker..."
if ! command -v docker >/dev/null 2>&1; then
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
      > /etc/apt/sources.list.d/docker.list
    apt-get update -y
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
docker --version
docker compose version

# ─── 3. User non-root pour l'app ───────────────────────────────
echo "[3/7] User '$APP_USER'..."
if ! id "$APP_USER" >/dev/null 2>&1; then
    useradd -m -s /bin/bash "$APP_USER"
    usermod -aG docker "$APP_USER"
fi

# ─── 4. Firewall ───────────────────────────────────────────────
echo "[4/7] Firewall UFW..."
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh
ufw allow 80/tcp
ufw allow 443/tcp
echo "y" | ufw enable || true
ufw status

# ─── 5. Clone du repo ──────────────────────────────────────────
echo "[5/7] Clone du repo dans $APP_DIR..."
mkdir -p "$APP_DIR"
chown "$APP_USER:$APP_USER" "$APP_DIR"
if [ ! -d "$APP_DIR/.git" ]; then
    sudo -u "$APP_USER" git clone "$REPO_URL" "$APP_DIR"
else
    cd "$APP_DIR" && sudo -u "$APP_USER" git pull origin main
fi

# ─── 6. .env template ──────────────────────────────────────────
echo "[6/7] Préparation .env..."
ENV_FILE="$APP_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
    cat > "$ENV_FILE" <<'EOF'
# ─── Required ─────────────────────────────────────────────────
DATABASE_URL=postgres://USER:PWD@db.prisma.io:5432/postgres?sslmode=require
ODDS_API_KEY=
FOOTBALL_DATA_API_KEY=

# ─── Optional ─────────────────────────────────────────────────
SECRET_KEY=change-me-to-something-random-in-prod-32-chars-min
SENTRY_DSN=
PER_LEAGUE_MODEL_LEAGUES=Serie A
EOF
    chown "$APP_USER:$APP_USER" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo ""
    echo "  ⚠ ÉDITE $ENV_FILE et remplis DATABASE_URL, ODDS_API_KEY, FOOTBALL_DATA_API_KEY"
fi

# ─── 7. Systemd service ────────────────────────────────────────
echo "[7/7] Systemd service edgeai.service..."
cat > /etc/systemd/system/edgeai.service <<EOF
[Unit]
Description=edgeAI Docker Compose stack
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=$APP_DIR
User=$APP_USER
ExecStartPre=-/usr/bin/docker compose down
ExecStart=/usr/bin/docker compose up -d --remove-orphans
ExecStop=/usr/bin/docker compose down
TimeoutStartSec=600

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable edgeai.service

echo ""
echo "=================================================="
echo "✓ Setup terminé"
echo "=================================================="
echo ""
echo "Étapes suivantes :"
echo "  1. Éditer le .env : nano $ENV_FILE"
echo "  2. Démarrer le stack : systemctl start edgeai"
echo "  3. Voir les logs : su - $APP_USER -c 'cd $APP_DIR && docker compose logs -f --tail=50'"
echo "  4. (Optionnel) Reverse proxy + HTTPS :"
echo "     bash $APP_DIR/deploy/nginx-https.sh <ton.domaine.com>"
echo ""
