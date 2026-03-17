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

# Step 1: Démarrer nginx en mode HTTP-only pour le challenge ACME
echo "[1/4] Démarrage nginx en mode HTTP..."
DOMAIN=$DOMAIN docker compose -f docker-compose.yml up -d app

# Temporairement utiliser la config init (HTTP-only)
docker compose -f docker-compose.yml run -d \
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
docker compose run --rm certbot certonly \
    --webroot \
    -w /var/www/certbot \
    -d "$DOMAIN" \
    --email "$EMAIL" \
    --agree-tos \
    --no-eff-email \
    --force-renewal

# Step 3: Arrêter le nginx temporaire et démarrer le vrai
echo "[4/4] Redémarrage avec la config SSL..."
docker stop subtitles-nginx-init && docker rm subtitles-nginx-init
docker compose up -d

echo ""
echo "=== Terminé ! ==="
echo "https://$DOMAIN est prêt."
