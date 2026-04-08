# ClipLens — Linux Deployment Guide

Step-by-step instructions for deploying ClipLens on a Linux server (Ubuntu 22.04 LTS recommended).
This covers a single-server setup: Nginx → Gunicorn (Django) + Celery + Flower, all on one machine.

---

## Architecture Overview

```
Internet
   │
Nginx (port 80/443)
   ├── /static/  → serve directly from staticfiles/ (no Django involved)
   ├── /media/   → serve directly from media/ (no Django involved)
   ├── /flower/  → proxy to Flower (port 5556)
   └── everything else → proxy to Gunicorn (unix socket)
                              │
                         Django (cliplens.wsgi)
                              │
                    PostgreSQL + Redis
                              │
                    Celery worker (background)
```

---

## Step 1 — Server prerequisites

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Python 3.11
sudo apt install -y python3.11 python3.11-venv python3.11-dev

# Build tools (needed for psycopg2, onnxruntime, insightface)
sudo apt install -y build-essential pkg-config libssl-dev libffi-dev

# PostgreSQL 15
sudo apt install -y postgresql-15 postgresql-contrib libpq-dev

# Redis
sudo apt install -y redis-server

# Nginx
sudo apt install -y nginx

# FFmpeg (required for HLS transcoding and audio extraction)
sudo apt install -y ffmpeg

# Git
sudo apt install -y git
```

---

## Step 2 — PostgreSQL setup

```bash
sudo -u postgres psql << 'EOF'
CREATE USER cliplens WITH PASSWORD 'your_strong_password';
CREATE DATABASE cliplens OWNER cliplens;
\c cliplens
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
EOF
```

> **Note:** `vector` (pgvector) and `pg_trgm` must both be enabled — ClipLens uses them
> for CLIP semantic search (HNSW index) and fuzzy text search respectively.
> If `CREATE EXTENSION vector` fails, install the package first:
> `sudo apt install postgresql-15-pgvector`

---

## Step 3 — Project setup

```bash
# Create app user (never run as root)
sudo useradd -m -s /bin/bash deploy

# Create app directory
sudo mkdir -p /var/www/cliplens
sudo chown deploy:deploy /var/www/cliplens

# Switch to deploy user
sudo -u deploy -i

# Clone your repo
git clone https://github.com/yourname/ClipLens.git /var/www/cliplens
cd /var/www/cliplens

# Create virtualenv and install dependencies
python3.11 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

---

## Step 4 — Environment configuration

Create `/var/www/cliplens/.env`:

```bash
# SECURITY
SECRET_KEY=<generate: python -c "import secrets; print(secrets.token_hex(50))">
DEBUG=False
ALLOWED_HOSTS=yourdomain.com,www.yourdomain.com

# SITE
SITE_URL=https://yourdomain.com
CSRF_TRUSTED_ORIGINS=https://yourdomain.com

# DATABASE
USE_POSTGRES=true
POSTGRES_DB=cliplens
POSTGRES_USER=cliplens
POSTGRES_PASSWORD=your_strong_password
POSTGRES_HOST=localhost
POSTGRES_PORT=5432

# REDIS
REDIS_URL=redis://localhost:6379/1
CELERY_BROKER_URL=redis://localhost:6379/0

# AI PIPELINE
FRAME_ANALYSIS_ENABLED=true
FACE_RECOGNITION_ENABLED=true
SCENE_DESCRIPTION_ENABLED=true
SCENE_CAPTION_MODEL=blip
CLIP_ENABLED=true
CLIP_SIMILARITY_THRESHOLD=0.24
CLIP_PHOTO_SIMILARITY_THRESHOLD=0.28
FUZZY_SEARCH_ENABLED=true
FUZZY_SEARCH_SIMILARITY_THRESHOLD=0.35
WHISPER_MODEL_SIZE=small
WHISPER_DEVICE=cpu
FRAME_INTERVAL_SECONDS=5
YOLO_MODEL=yolov8n

# HLS
HLS_MULTI_QUALITY=true
HLS_QUALITIES=1080,720,480,360
HLS_SEGMENT_DURATION=6

# FLOWER
FLOWER_BASIC_AUTH=admin:your_strong_password

# MEDIA
EMBED_ALLOW_ORIGINS=*
```

> **Disk space warning:** HLS at multiple qualities uses significant disk.
> A 1-hour video at 4 qualities ≈ 8–12 GB.
> Point MEDIA_ROOT at a large mounted volume if needed:
> Add `MEDIA_ROOT=/mnt/storage/cliplens/media` to .env and update settings.py accordingly.

---

## Step 5 — Database migrations and static files

```bash
cd /var/www/cliplens
source venv/bin/activate

python manage.py migrate
python manage.py collectstatic --noinput

# Create your first superadmin
python manage.py createsuperuser

# Assign superadmin role (ClipLens role system, separate from Django admin)
python manage.py assign_role <username> superadmin
```

---

## Step 6 — Systemd: Gunicorn (Django)

Create `/etc/systemd/system/cliplens.service`:

```ini
[Unit]
Description=ClipLens Gunicorn
After=network.target postgresql.service redis.service

[Service]
Type=notify
User=deploy
Group=www-data
WorkingDirectory=/var/www/cliplens
EnvironmentFile=/var/www/cliplens/.env
ExecStart=/var/www/cliplens/venv/bin/gunicorn \
    cliplens.wsgi:application \
    --bind unix:/run/cliplens/gunicorn.sock \
    --workers 3 \
    --timeout 300 \
    --keep-alive 5 \
    --max-requests 1000 \
    --max-requests-jitter 100 \
    --log-level info
ExecReload=/bin/kill -s HUP $MAINPID
RuntimeDirectory=cliplens
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
```

> **Workers:** Set to `(2 × CPU cores) + 1`. For a 2-core server use 5.
> Keep lower if RAM is limited — each worker loads the Django app into memory.
> The AI models (YOLO, CLIP etc.) are only loaded by the Celery worker, NOT Gunicorn workers.

---

## Step 7 — Systemd: Celery worker

Create `/etc/systemd/system/cliplens-celery.service`:

```ini
[Unit]
Description=ClipLens Celery Worker
After=network.target redis.service

[Service]
Type=simple
User=deploy
Group=www-data
WorkingDirectory=/var/www/cliplens
EnvironmentFile=/var/www/cliplens/.env
ExecStart=/var/www/cliplens/venv/bin/celery \
    -A cliplens worker \
    --loglevel=info \
    -Q processing,captions,default \
    --concurrency=2
Restart=on-failure
RestartSec=10s
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

> **Concurrency:** Keep at 2 (or 1 if CPU-only). Each concurrent worker tries to load
> YOLO + InsightFace + BLIP + CLIP into memory simultaneously. Too many = OOM crashes.
> If you have a GPU, set `WHISPER_DEVICE=cuda` in .env and increase concurrency only
> after confirming VRAM is sufficient.

---

## Step 8 — Systemd: Flower monitor

Create `/etc/systemd/system/cliplens-flower.service`:

```ini
[Unit]
Description=ClipLens Flower Monitor
After=network.target redis.service cliplens-celery.service

[Service]
Type=simple
User=deploy
Group=www-data
WorkingDirectory=/var/www/cliplens
EnvironmentFile=/var/www/cliplens/.env
ExecStart=/var/www/cliplens/venv/bin/celery \
    -A cliplens flower \
    --port=5556 \
    --basic_auth=${FLOWER_BASIC_AUTH}
Restart=on-failure
RestartSec=10s
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

---

## Step 9 — Enable and start all services

```bash
sudo systemctl daemon-reload

sudo systemctl enable cliplens cliplens-celery cliplens-flower
sudo systemctl start  cliplens cliplens-celery cliplens-flower

# Verify all three are running
sudo systemctl status cliplens
sudo systemctl status cliplens-celery
sudo systemctl status cliplens-flower

# Live logs
sudo journalctl -u cliplens -f
sudo journalctl -u cliplens-celery -f
```

---

## Step 10 — Nginx configuration

Create `/etc/nginx/sites-available/cliplens`:

```nginx
upstream cliplens_gunicorn {
    server unix:/run/cliplens/gunicorn.sock fail_timeout=0;
}

server {
    listen 80;
    server_name yourdomain.com www.yourdomain.com;

    # Max upload size — set higher than Django's FILE_UPLOAD_MAX_MEMORY_SIZE
    # Large videos need this to be large
    client_max_body_size 10G;

    # Increase timeouts for long video uploads
    proxy_read_timeout    600s;
    proxy_connect_timeout 75s;
    proxy_send_timeout    600s;

    # Static files — served directly by Nginx, no Django involved
    location /static/ {
        alias /var/www/cliplens/staticfiles/;
        expires 1y;
        add_header Cache-Control "public, immutable";
        access_log off;
    }

    # Media files — served directly by Nginx
    # HLS segments (.ts, .m3u8) need correct MIME types and CORS for HLS.js
    location /media/ {
        alias /var/www/cliplens/media/;
        expires 7d;

        # Required for HLS.js cross-origin requests
        add_header Access-Control-Allow-Origin  *;
        add_header Access-Control-Allow-Methods "GET, HEAD, OPTIONS";

        # Correct MIME type for HLS manifests
        types {
            application/vnd.apple.mpegurl  m3u8;
            video/mp2t                     ts;
        }

        access_log off;
    }

    # Flower monitoring — basic auth handled by Flower itself
    location /flower/ {
        proxy_pass         http://127.0.0.1:5556/flower/;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_redirect     off;
    }

    # Everything else → Gunicorn → Django
    location / {
        proxy_pass         http://cliplens_gunicorn;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_redirect     off;
    }
}
```

```bash
# Enable site
sudo ln -s /etc/nginx/sites-available/cliplens /etc/nginx/sites-enabled/
sudo nginx -t        # test config — must say "ok"
sudo systemctl reload nginx
```

> **HTTPS:** Once the site is accessible on HTTP, add SSL with Certbot:
> ```bash
> sudo apt install certbot python3-certbot-nginx
> sudo certbot --nginx -d yourdomain.com -d www.yourdomain.com
> ```
> Certbot automatically modifies the Nginx config and sets up auto-renewal.

---

## Step 11 — Flower URL prefix (if proxying at /flower/)

The Flower service needs to know it's being served under `/flower/` so its internal links work.
Update the Flower systemd unit's `ExecStart` to add `--url_prefix`:

```ini
ExecStart=/var/www/cliplens/venv/bin/celery \
    -A cliplens flower \
    --port=5556 \
    --url_prefix=flower \
    --basic_auth=${FLOWER_BASIC_AUTH}
```

Then reload: `sudo systemctl daemon-reload && sudo systemctl restart cliplens-flower`

---

## Quick reference: common commands

```bash
# Restart Django after a code update
sudo systemctl restart cliplens

# Restart Celery after tasks.py changes
sudo systemctl restart cliplens-celery

# Pull latest code and restart everything
cd /var/www/cliplens
sudo -u deploy git pull
sudo -u deploy bash -c "source venv/bin/activate && python manage.py migrate && python manage.py collectstatic --noinput"
sudo systemctl restart cliplens cliplens-celery

# Check all three services at once
sudo systemctl status cliplens cliplens-celery cliplens-flower

# View recent errors
sudo journalctl -u cliplens --since "1 hour ago" --no-pager

# Test Gunicorn manually (before systemd)
cd /var/www/cliplens
source venv/bin/activate
gunicorn cliplens.wsgi:application --bind 0.0.0.0:8000 --workers 2
```

---

## Checklist before going live

- [ ] `.env` has a real `SECRET_KEY` (not the dev default)
- [ ] `DEBUG=False` in `.env`
- [ ] `ALLOWED_HOSTS` set to your domain
- [ ] `CSRF_TRUSTED_ORIGINS` set to `https://yourdomain.com`
- [ ] PostgreSQL `vector` and `pg_trgm` extensions enabled
- [ ] `python manage.py migrate` completed with no errors
- [ ] `python manage.py collectstatic` completed
- [ ] All three systemd services show `Active: active (running)`
- [ ] `sudo nginx -t` shows `syntax is ok` + `test is successful`
- [ ] Flower accessible at `/flower/` with basic auth
- [ ] Upload a test video and confirm Celery processes it (check Flower)
- [ ] HLS playback works in browser
