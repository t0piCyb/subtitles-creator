# Subtitles Creator

Upload une vidéo, transcription automatique mot par mot (Whisper), édition des sous-titres dans le navigateur, puis burn dans la vidéo (police Slabo 27px).

## Fonctionnalités

- Upload drag-and-drop
- Transcription Whisper mot par mot avec timestamps
- Éditeur de sous-titres : modifier texte, timing, ajouter/supprimer
- Preview live des sous-titres sur la vidéo
- Génération et re-génération après modifications
- Téléchargement de la vidéo sous-titrée
- Police Slabo 27px

## Stack

- **Backend** : Python 3.11, FastAPI, faster-whisper, FFmpeg
- **Frontend** : HTML/CSS/JS vanilla
- **Infra** : Docker Compose, Nginx, Let's Encrypt

## Déploiement sur VPS (subtitles.topilo.dev)

### Prérequis

- Docker et Docker Compose installés sur le VPS
- DNS : enregistrement A `subtitles.topilo.dev` → IP du VPS
- Ports 80 et 443 ouverts

### Premier déploiement

```bash
# Sur le VPS
git clone https://github.com/t0piCyb/subtitles-creator.git
cd subtitles-creator

# Obtenir le certificat SSL + démarrer
chmod +x init-ssl.sh
./init-ssl.sh ton@email.com

# C'est prêt → https://subtitles.topilo.dev
```

### Mises à jour

```bash
cd subtitles-creator
git pull
docker compose up -d --build
```

### Logs et debug

```bash
docker compose logs -f app       # Logs backend (transcription, FFmpeg)
docker compose logs -f nginx     # Logs nginx
docker compose ps                # État des services
```

## Modal (compute cloud, optionnel)

Par défaut, la transcription Whisper et le burn FFmpeg tournent sur le VPS (CPU). Pour accélérer le traitement, on peut les déporter sur [Modal](https://modal.com) (serveurs avec CPUs rapides, ~$0.001/vidéo).

### Setup Modal (une seule fois)

```bash
# Sur ta machine locale
brew install pipx && pipx install modal
modal setup                          # crée un compte + authentification

# Déployer les fonctions
cd subtitles-creator
modal deploy modal_deploy.py
```

### Créer un token pour le VPS

```bash
modal token new --label subtitles-vps
# → Affiche MODAL_TOKEN_ID et MODAL_TOKEN_SECRET
```

### Activer Modal sur le VPS

Créer un fichier `.env` à côté du `docker-compose.yml` :

```env
USE_MODAL=true
MODAL_TOKEN_ID=ak-xxxxx
MODAL_TOKEN_SECRET=as-xxxxx
```

Puis rebuild :

```bash
docker compose up -d --build
```

### Désactiver Modal

Supprimer le `.env` ou mettre `USE_MODAL=false`, puis `docker compose up -d --build`.

## Dev local

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

## API

| Méthode  | Endpoint                | Description                          |
|----------|-------------------------|--------------------------------------|
| `POST`   | `/api/upload`           | Upload + transcription → sous-titres |
| `POST`   | `/api/generate`         | Burn les sous-titres édités          |
| `GET`    | `/api/video/{id}`       | Vidéo originale (preview)            |
| `GET`    | `/api/download/{id}`    | Télécharger la vidéo sous-titrée     |
| `DELETE` | `/api/cleanup/{id}`     | Nettoyage des fichiers               |
| `GET`    | `/health`               | Health check                         |

## Configuration

| Variable             | Défaut                  | Description                              |
|----------------------|-------------------------|------------------------------------------|
| `WHISPER_MODEL`      | `base`                  | tiny/base/small/medium                   |
| `DOMAIN`             | `subtitles.topilo.dev`  | Domaine pour nginx + SSL                 |
| `USE_MODAL`          | `false`                 | Activer Modal pour le compute            |
| `MODAL_TOKEN_ID`     | —                       | Token Modal (requis si USE_MODAL=true)   |
| `MODAL_TOKEN_SECRET` | —                       | Secret Modal (requis si USE_MODAL=true)  |
