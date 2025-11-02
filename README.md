# TikTok-Style Subtitle Creator

Automatically transcribe videos and burn TikTok-style subtitles directly into them. Upload a video and download it back with permanently embedded, word-by-word animated subtitles.

## Features

- Upload videos via drag-and-drop interface
- Automatic transcription with Whisper AI (word-level timestamps)
- TikTok-style subtitle formatting (bold, uppercase, positioned)
- Subtitles burned directly into video with FFmpeg
- Download processed video with permanent subtitles
- Simple, modern UI with progress tracking
- RESTful API with FastAPI

## Architecture

- **Backend**: FastAPI + Stable-TS (Whisper)
- **Frontend**: HTML/CSS/JavaScript vanilla
- **Containerisation**: Docker + Docker Compose
- **Transcription**: OpenAI Whisper via stable-ts

## Installation Locale

### Prérequis

- Docker et Docker Compose
- Au moins 4GB de RAM disponible

### Démarrage

```bash
# Cloner le projet
git clone <repo-url>
cd subtitles-creator

# Lancer avec Docker Compose
docker-compose up --build

# L'application sera disponible sur http://localhost:8000
```

## Déploiement sur AWS

### Option 1: EC2 avec Docker (Recommandé pour débuter)

#### 1. Créer une instance EC2

```bash
# Lancer une instance EC2 avec les caractéristiques suivantes:
- Type: t3.large (minimum) ou t3.xlarge (recommandé)
- AMI: Ubuntu 22.04 LTS
- Storage: 30 GB minimum
- Security Group: Autoriser les ports 22 (SSH) et 8000 (HTTP)
```

#### 2. Se connecter à l'instance

```bash
ssh -i your-key.pem ubuntu@<EC2-PUBLIC-IP>
```

#### 3. Installer Docker et Docker Compose

```bash
# Mettre à jour le système
sudo apt update && sudo apt upgrade -y

# Installer Docker
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo usermod -aG docker ubuntu

# Installer Docker Compose
sudo curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
sudo chmod +x /usr/local/bin/docker-compose

# Redémarrer la session
exit
# Se reconnecter
ssh -i your-key.pem ubuntu@<EC2-PUBLIC-IP>
```

#### 4. Déployer l'application

```bash
# Cloner le projet
git clone <repo-url>
cd subtitles-creator

# Lancer l'application
docker-compose up -d

# Vérifier les logs
docker-compose logs -f
```

#### 5. Accéder à l'application

```
http://<EC2-PUBLIC-IP>:8000
```

#### 6. Configuration d'un nom de domaine (optionnel)

```bash
# Installer Nginx
sudo apt install nginx -y

# Configurer le reverse proxy
sudo nano /etc/nginx/sites-available/subtitles

# Ajouter la configuration suivante:
server {
    listen 80;
    server_name your-domain.com;

    location / {
        proxy_pass http://localhost:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        client_max_body_size 500M;
    }
}

# Activer la configuration
sudo ln -s /etc/nginx/sites-available/subtitles /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl restart nginx

# Installer SSL avec Let's Encrypt
sudo apt install certbot python3-certbot-nginx -y
sudo certbot --nginx -d your-domain.com
```

### Option 2: AWS ECS (Fargate)

#### 1. Préparer l'image Docker

```bash
# Se connecter à AWS ECR
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin <account-id>.dkr.ecr.us-east-1.amazonaws.com

# Créer un repository ECR
aws ecr create-repository --repository-name subtitles-creator --region us-east-1

# Builder et pousser l'image
docker build -t subtitles-creator .
docker tag subtitles-creator:latest <account-id>.dkr.ecr.us-east-1.amazonaws.com/subtitles-creator:latest
docker push <account-id>.dkr.ecr.us-east-1.amazonaws.com/subtitles-creator:latest
```

#### 2. Créer un cluster ECS

```bash
# Via AWS Console ou CLI
aws ecs create-cluster --cluster-name subtitles-cluster --region us-east-1
```

#### 3. Créer une Task Definition

Créer un fichier `task-definition.json`:

```json
{
  "family": "subtitles-creator",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "2048",
  "memory": "4096",
  "containerDefinitions": [
    {
      "name": "subtitles-creator",
      "image": "<account-id>.dkr.ecr.us-east-1.amazonaws.com/subtitles-creator:latest",
      "portMappings": [
        {
          "containerPort": 8000,
          "protocol": "tcp"
        }
      ],
      "environment": [
        {
          "name": "WHISPER_MODEL",
          "value": "base"
        }
      ],
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group": "/ecs/subtitles-creator",
          "awslogs-region": "us-east-1",
          "awslogs-stream-prefix": "ecs"
        }
      }
    }
  ]
}
```

```bash
# Enregistrer la task definition
aws ecs register-task-definition --cli-input-json file://task-definition.json
```

#### 4. Créer un service ECS

```bash
aws ecs create-service \
  --cluster subtitles-cluster \
  --service-name subtitles-service \
  --task-definition subtitles-creator \
  --desired-count 1 \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[subnet-xxx],securityGroups=[sg-xxx],assignPublicIp=ENABLED}"
```

#### 5. Configurer un Load Balancer (ALB)

Via la console AWS:
1. Créer un Application Load Balancer
2. Configurer un Target Group pointant vers le service ECS
3. Configurer les health checks sur `/health`

### Option 3: AWS App Runner (Le plus simple)

```bash
# Créer un service App Runner depuis ECR
aws apprunner create-service \
  --service-name subtitles-creator \
  --source-configuration '{
    "ImageRepository": {
      "ImageIdentifier": "<account-id>.dkr.ecr.us-east-1.amazonaws.com/subtitles-creator:latest",
      "ImageRepositoryType": "ECR",
      "ImageConfiguration": {
        "Port": "8000",
        "RuntimeEnvironmentVariables": {
          "WHISPER_MODEL": "base"
        }
      }
    },
    "AutoDeploymentsEnabled": true
  }' \
  --instance-configuration '{
    "Cpu": "2 vCPU",
    "Memory": "4 GB"
  }'
```

## Configuration

### Variables d'environnement

- `WHISPER_MODEL`: Modèle Whisper à utiliser (tiny, base, small, medium, large)
  - `tiny`: Rapide mais moins précis
  - `base`: Bon compromis (par défaut)
  - `small`: Plus précis mais plus lent
  - `medium/large`: Très précis mais nécessite beaucoup de ressources

### Recommandations de ressources

| Modèle  | CPU    | RAM    | Instance EC2 recommandée |
|---------|--------|--------|--------------------------|
| tiny    | 1 vCPU | 2 GB   | t3.small                 |
| base    | 2 vCPU | 4 GB   | t3.large                 |
| small   | 2 vCPU | 8 GB   | t3.xlarge                |
| medium  | 4 vCPU | 16 GB  | c5.2xlarge               |

## How It Works

1. **Upload**: Drag and drop your video or click to browse
2. **Processing**: The system automatically:
   - Transcribes audio using Whisper AI
   - Extracts word-level timestamps
   - Generates ASS subtitle file with TikTok styling
   - Burns subtitles into video using FFmpeg
3. **Download**: Get your video back with permanent TikTok-style subtitles

## Usage

1. Open the web interface at http://localhost:8000
2. Drag and drop a video file (MP4, MOV, AVI, WebM)
3. Wait for processing (typically 2-5 minutes depending on video length)
4. Click "Download Video" to get your subtitled video
5. The subtitles are now permanently embedded in the video

## API Endpoints

### `POST /api/process-video`

Upload and process a video with TikTok-style subtitles.

**Request:**
```bash
curl -X POST http://localhost:8000/api/process-video \
  -F "file=@video.mp4"
```

**Response:**
```json
{
  "success": true,
  "file_id": "uuid-here",
  "word_count": 245,
  "download_url": "/api/download/uuid-here.mp4"
}
```

### `GET /api/download/{file_id}`

Download the processed video with burned-in subtitles.

**Request:**
```bash
curl -O http://localhost:8000/api/download/uuid-here.mp4
```

### `GET /health`

Health check endpoint.

**Response:**
```json
{
  "status": "healthy",
  "model": "base",
  "device": "cpu"
}
```

### `POST /api/transcribe` (Legacy)

Legacy endpoint for transcription only (returns JSON with word timestamps, no video processing).

## Coûts AWS estimés (région us-east-1)

### EC2 (t3.large, 24/7)
- Instance: ~$60/mois
- Storage (30 GB): ~$3/mois
- **Total: ~$63/mois**

### ECS Fargate (2 vCPU, 4 GB, 24/7)
- Compute: ~$50/mois
- **Total: ~$50/mois**

### App Runner (2 vCPU, 4 GB)
- Base: $0.064/heure = ~$46/mois
- Requêtes: Variable selon utilisation
- **Total: ~$50-100/mois**

## Dépannage

### L'application ne démarre pas

```bash
# Vérifier les logs
docker-compose logs -f

# Vérifier la mémoire disponible
free -h
```

### Erreur de transcription

- Vérifier que ffmpeg est installé dans le container
- Vérifier le format de la vidéo (MP4 recommandé)
- Augmenter la mémoire disponible

### Performance lente

- Utiliser un modèle Whisper plus petit (tiny ou base)
- Augmenter les ressources CPU/RAM
- Utiliser une instance avec GPU (pour des performances optimales)

## Licence

MIT
