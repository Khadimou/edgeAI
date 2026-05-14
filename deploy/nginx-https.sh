#!/bin/bash
# ─────────────────────────────────────────────────────────────────
# Setup nginx reverse proxy + HTTPS (Let's Encrypt) pour edgeAI.
# Usage : bash nginx-https.sh <ton.domaine.com>
#
# Prérequis :
#   - DNS A record du domaine pointe vers l'IP du VPS
#   - hetzner-setup.sh exécuté (nginx + certbot installés)
#   - Stack edgeAI démarrée (backend:8000, frontend:3000)
# ─────────────────────────────────────────────────────────────────
set -euo pipefail

DOMAIN="${1:?Usage : bash nginx-https.sh <domain>}"
EMAIL="${2:-admin@$DOMAIN}"

echo "Configuration nginx pour $DOMAIN..."

cat > /etc/nginx/sites-available/edgeai <<EOF
# Frontend Next.js
server {
    listen 80;
    server_name $DOMAIN;

    # Proxy frontend
    location / {
        proxy_pass http://localhost:3000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_cache_bypass \$http_upgrade;
        client_max_body_size 10M;
    }

    # Backend API
    location /api/ {
        proxy_pass http://localhost:8000/api/;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 90s;
        client_max_body_size 10M;
    }

    # Health check
    location /health {
        proxy_pass http://localhost:8000/health;
    }
}
EOF

ln -sf /etc/nginx/sites-available/edgeai /etc/nginx/sites-enabled/edgeai
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

# Certbot HTTPS
echo "Obtention certificat Let's Encrypt..."
certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --email "$EMAIL" --redirect

echo ""
echo "✓ HTTPS activé pour https://$DOMAIN"
echo "  Renouvellement auto via systemd timer."
