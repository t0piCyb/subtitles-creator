#!/bin/bash
# =============================================================================
# Initialisation SSL pour subtitles.topilo.dev
# Usage: ./init-ssl.sh [email]
# =============================================================================
set -e

DOMAIN="${DOMAIN:-subtitles.topilo.dev}"
EMAIL="${1:-}"

if [ -z "$EMAIL" ]; then
    echo "Usage: ./init-ssl.sh votre@email.com"
    exit 1
fi

echo "=== Initialisation SSL pour $DOMAIN ==="

# Step 1: Démarrer un nginx temporaire HTTP-only pour le challenge ACME
echo "[1/4] Démarrage nginx temporaire en mode HTTP..."
docker run -d \
    --name subtitles-nginx-init \
    -p 80:80 \
    -v "$(pwd)/nginx/nginx.init.conf:/etc/nginx/templates/default.conf.template:ro" \
    -v "subtitles-creator_certbot-webroot:/var/www/certbot:ro" \
    -e "DOMAIN=$DOMAIN" \
    nginx:alpine

echo "[2/4] Attente que nginx soit prêt..."
sleep 3

# Step 2: Obtenir le certificat
echo "[3/4] Obtention du certificat Let's Encrypt..."
docker run --rm \
    -v "subtitles-creator_certbot-webroot:/var/www/certbot" \
    -v "subtitles-creator_certbot-certs:/etc/letsencrypt" \
    --network host \
    certbot/certbot certonly \
    --webroot \
    -w /var/www/certbot \
    -d "$DOMAIN" \
    --email "$EMAIL" \
    --agree-tos \
    --no-eff-email \
    --force-renewal

# Step 3: Arrêter le nginx temporaire et démarrer le vrai stack
echo "[4/4] Redémarrage avec la config SSL..."
docker stop subtitles-nginx-init && docker rm subtitles-nginx-init
DOMAIN=$DOMAIN docker compose up -d

echo ""
echo "=== Terminé ! ==="
echo "https://$DOMAIN est prêt."
