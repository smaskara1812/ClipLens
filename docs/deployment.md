# Production deployment

This guide deploys ClipLens to a single Ubuntu 22.04 / 24.04 server. Multi-server / HA setups are a future doc.

---

## 1. Server prerequisites

Recommended specs:
- 8 CPU cores
- 32 GB RAM (16 GB minimum; AI models eat memory)
- 200 GB+ SSD (more for media)
- Optional GPU for faster AI processing (NVIDIA with CUDA toolkit)
- Ubuntu 22.04 LTS or 24.04 LTS

```bash
# Base packages
sudo apt update && sudo apt upgrade -y
sudo apt install -y \
    build-essential python3.11 python3.11-venv python3.11-dev \
    postgresql-15 postgresql-15-pgvector postgresql-contrib \
    redis-server nginx \
    ffmpeg \
    git curl certbot python3-certbot-nginx \
    libpq-dev libjpeg-dev zlib1g-dev libsm6 libxext6 libgl1
```

---

## 2. PostgreSQL setup

```bash
sudo -u postgres psql <<EOF
CREATE USER cliplens WITH PASSWORD 'CHANGE_ME_STRONG_PASSWORD';
ALTER USER cliplens CREATEDB;       -- needed because tenant provisioning creates DBs
CREATE DATABASE freestream OWNER cliplens;          -- default DB (legacy)
CREATE DATABASE freestream_control OWNER cliplens;  -- control DB
\c freestream
CREATE EXTENSION vector;
CREATE EXTENSION pg_trgm;
CREATE EXTENSION unaccent;
\c freestream_control
CREATE EXTENSION vector;
CREATE EXTENSION pg_trgm;
CREATE EXTENSION unaccent;
EOF
```

Increase `shared_buffers` and `work_mem` in `/etc/postgresql/15/main/postgresql.conf` for a media workload — at minimum `shared_buffers = 4GB`, `work_mem = 64MB`. Restart Postgres after.

---

## 3. Application install

```bash
# Create app user + directory
sudo useradd -r -m -s /bin/bash cliplens
sudo mkdir -p /srv/cliplens && sudo chown cliplens:cliplens /srv/cliplens
sudo -iu cliplens

# Clone + virtualenv
cd /srv/cliplens
git clone <your-repo-url> app
cd app
python3.11 -m venv venv
source venv/bin/activate
pip install --upgrade pip wheel
pip install -r requirements.txt

# Create media + logs dirs
mkdir -p /srv/cliplens/media/tenants
mkdir -p /srv/cliplens/logs
mkdir -p /srv/cliplens/nginx_tmp/client_body
chmod 750 /srv/cliplens/media
```

---

## 4. `.env` (production)

```bash
# /srv/cliplens/app/.env
DJANGO_SETTINGS_MODULE=cliplens.settings
SECRET_KEY=<generate with: python -c "import secrets; print(secrets.token_urlsafe(50))">
DEBUG=False
ALLOWED_HOSTS=cliplens.com,*.cliplens.com

SITE_URL=https://cliplens.com

USE_POSTGRES=true
POSTGRES_DB=freestream
POSTGRES_USER=cliplens
POSTGRES_PASSWORD=<strong-password>
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
CONTROL_DB_NAME=freestream_control

MULTI_TENANT=true

REDIS_URL=redis://127.0.0.1:6379/0

# Stripe (live keys for production!)
STRIPE_SECRET_KEY=sk_live_...
STRIPE_PUBLISHABLE_KEY=pk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...   # from https://dashboard.stripe.com/webhooks

# AI pipeline (same as dev)
WHISPER_MODEL_SIZE=small
WHISPER_DEVICE=cuda                # or cpu
WHISPER_COMPUTE_TYPE=float16       # or int8 for CPU
YOLO_MODEL=yolov8s                 # bigger for prod
HF_TOKEN=<your-huggingface-token>  # for pyannote diarization
USE_OLLAMA=false                   # set true if you run Ollama locally

# Email (Django will send onboarding/billing notifications)
EMAIL_HOST=smtp.sendgrid.net
EMAIL_PORT=587
EMAIL_HOST_USER=apikey
EMAIL_HOST_PASSWORD=<your-sendgrid-api-key>
EMAIL_USE_TLS=True
DEFAULT_FROM_EMAIL=hello@cliplens.com
```

Permissions: `chmod 600 .env`

---

## 5. First-run migrations

```bash
cd /srv/cliplens/app
source venv/bin/activate

# Create the control DB schema + seed default plans
python manage.py setup_multitenancy --platform-owner-username=<your-username>

# Collect static
python manage.py collectstatic --noinput

# Create your Django superuser (for /admin/)
python manage.py createsuperuser
```

Make sure to mark your user as platform owner so you can log in to `admin.cliplens.com`:

```bash
python manage.py shell <<EOF
from django.contrib.auth.models import User
u = User.objects.get(username='YOUR_USERNAME')
u.profile.is_platform_owner = True
u.profile.save()
EOF
```

---

## 6. SSL certificate

Wildcard cert is required because of the multi-tenant subdomain model. DNS-01 challenge is the easiest path.

### Using Cloudflare DNS (recommended)

```bash
sudo pip install certbot-dns-cloudflare

# Get an API token from Cloudflare with Zone.DNS edit permission
sudo tee ~/.cloudflare.ini <<EOF
dns_cloudflare_api_token = <your-token>
EOF
sudo chmod 600 ~/.cloudflare.ini

sudo certbot certonly \
    --dns-cloudflare \
    --dns-cloudflare-credentials ~/.cloudflare.ini \
    -d cliplens.com \
    -d '*.cliplens.com' \
    --email you@cliplens.com \
    --agree-tos
```

Auto-renewal: certbot installs a systemd timer by default. Test with `sudo certbot renew --dry-run`.

---

## 7. nginx config

```nginx
# /etc/nginx/sites-available/cliplens
server {
    listen 80;
    server_name ~^((?<tenant>[a-z0-9\-]+)\.)?cliplens\.com$;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name ~^((?<tenant>[a-z0-9\-]+)\.)?cliplens\.com$;

    ssl_certificate     /etc/letsencrypt/live/cliplens.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/cliplens.com/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers off;
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

    client_max_body_size 4096m;
    client_body_temp_path /srv/cliplens/nginx_tmp/client_body;

    proxy_read_timeout 300s;
    proxy_send_timeout 300s;
    proxy_connect_timeout 10s;

    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto https;

    location /static/ {
        alias /srv/cliplens/app/staticfiles/;
        expires 30d;
        access_log off;
    }

    location /media/ {
        # Django still authorises tenant access — proxy through it for safety
        proxy_pass http://127.0.0.1:8000;
        proxy_buffering off;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
    }
}
```

Enable + reload:

```bash
sudo ln -s /etc/nginx/sites-available/cliplens /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

---

## 8. systemd services

### `/etc/systemd/system/cliplens-web.service`

```ini
[Unit]
Description=ClipLens Django + Gunicorn
After=network.target postgresql.service redis-server.service

[Service]
Type=simple
User=cliplens
Group=cliplens
WorkingDirectory=/srv/cliplens/app
EnvironmentFile=/srv/cliplens/app/.env
ExecStart=/srv/cliplens/app/venv/bin/gunicorn \
    --bind 127.0.0.1:8000 \
    --workers 4 \
    --threads 2 \
    --timeout 300 \
    --access-logfile /srv/cliplens/logs/gunicorn-access.log \
    --error-logfile /srv/cliplens/logs/gunicorn-error.log \
    cliplens.wsgi:application
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### `/etc/systemd/system/cliplens-celery-main.service`

```ini
[Unit]
Description=ClipLens Celery main worker (processing, captions, default)
After=network.target redis-server.service

[Service]
Type=simple
User=cliplens
Group=cliplens
WorkingDirectory=/srv/cliplens/app
EnvironmentFile=/srv/cliplens/app/.env
Environment=OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
ExecStart=/srv/cliplens/app/venv/bin/celery -A cliplens worker \
    -l info \
    -Q processing,captions,default \
    -n main@%%h \
    --pool=prefork --concurrency=2 \
    --logfile=/srv/cliplens/logs/celery-main.log
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

> **Note**: On Linux you can use `--pool=prefork` (the default) safely. The `--pool=solo` workaround is only needed on macOS dev machines where PyTorch/InsightFace SIGSEGV in forked children.

### `/etc/systemd/system/cliplens-celery-audio.service`

```ini
[Unit]
Description=ClipLens Celery audio worker (PANNs, silence)
After=network.target redis-server.service

[Service]
Type=simple
User=cliplens
Group=cliplens
WorkingDirectory=/srv/cliplens/app
EnvironmentFile=/srv/cliplens/app/.env
ExecStart=/srv/cliplens/app/venv/bin/celery -A cliplens worker \
    -l info -Q audio -n audio@%%h --concurrency=1 \
    --logfile=/srv/cliplens/logs/celery-audio.log
Restart=always

[Install]
WantedBy=multi-user.target
```

### `/etc/systemd/system/cliplens-celery-translation.service`

```ini
[Unit]
Description=ClipLens Celery translation worker (NLLB-200)
After=network.target redis-server.service

[Service]
Type=simple
User=cliplens
Group=cliplens
WorkingDirectory=/srv/cliplens/app
EnvironmentFile=/srv/cliplens/app/.env
ExecStart=/srv/cliplens/app/venv/bin/celery -A cliplens worker \
    -l info -Q translation -n translation@%%h --concurrency=1 \
    --logfile=/srv/cliplens/logs/celery-translation.log
Restart=always

[Install]
WantedBy=multi-user.target
```

### `/etc/systemd/system/cliplens-celery-live.service`

```ini
[Unit]
Description=ClipLens Celery live worker (FFmpeg streaming)
After=network.target redis-server.service

[Service]
Type=simple
User=cliplens
Group=cliplens
WorkingDirectory=/srv/cliplens/app
EnvironmentFile=/srv/cliplens/app/.env
ExecStart=/srv/cliplens/app/venv/bin/celery -A cliplens worker \
    -l info -Q live -n live@%%h --concurrency=4 \
    --logfile=/srv/cliplens/logs/celery-live.log
Restart=always

[Install]
WantedBy=multi-user.target
```

### Enable + start all

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now \
    cliplens-web \
    cliplens-celery-main \
    cliplens-celery-audio \
    cliplens-celery-translation \
    cliplens-celery-live

# Check status
sudo systemctl status cliplens-web cliplens-celery-main
```

---

## 9. Stripe webhook (production)

In the Stripe dashboard (live mode):
1. Go to https://dashboard.stripe.com/webhooks
2. **Add endpoint** → `https://cliplens.com/api/stripe/webhook/`
3. Select events:
   - `checkout.session.completed`
   - `customer.subscription.updated`
   - `customer.subscription.deleted`
   - `invoice.payment_failed`
4. Copy the **Signing secret** (`whsec_...`) → set as `STRIPE_WEBHOOK_SECRET` in `.env`
5. Restart cliplens-web: `sudo systemctl restart cliplens-web`

You do **not** need `stripe listen` in production — that's only for local dev.

---

## 10. DNS records

In your DNS provider (Cloudflare / Route 53 / etc.):

```
cliplens.com           A      <server-ip>
*.cliplens.com         A      <server-ip>
admin.cliplens.com     A      <server-ip>   (or use the wildcard above)
```

Set TTL to ~300 seconds while testing, raise to 3600+ once stable.

---

## 11. AI model caches (one-time download)

The first time each Celery task runs, it downloads model weights to disk. To pre-warm so the first user request isn't 5 minutes slow:

```bash
sudo -iu cliplens
cd /srv/cliplens/app
source venv/bin/activate

# Whisper
python -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8')"

# YOLO
python -c "from ultralytics import YOLO; YOLO('yolov8s.pt')"

# CLIP / BLIP / NLLB / InsightFace will download on first task — easiest to just upload one test video
```

Models live in `~cliplens/.cache/huggingface/`, `~cliplens/.insightface/`, `~cliplens/panns_data/`.

---

## 12. Backups

### Daily DB backup

```bash
# /etc/cron.daily/cliplens-backup
#!/bin/bash
DATE=$(date +%Y%m%d)
mkdir -p /srv/cliplens/backups/$DATE
pg_dumpall -U cliplens -h localhost | gzip > /srv/cliplens/backups/$DATE/postgres.sql.gz
find /srv/cliplens/backups -mtime +30 -delete
```

### Media backup

`rsync` or `restic` to off-server storage. Don't try to put `media/` into the DB backup — it can be terabytes.

---

## 13. Health checks

A `/api/health/` endpoint exists and is exempt from auth — point your monitor at it.

```bash
curl https://cliplens.com/api/health/
# {"status": "ok"}
```

---

## 14. Updating

```bash
sudo -iu cliplens
cd /srv/cliplens/app
git pull
source venv/bin/activate
pip install -r requirements.txt
python manage.py migrate                        # default DB
python manage.py migrate --database=control     # control DB
# Then for each tenant DB (see operations.md for the loop)
python manage.py collectstatic --noinput

sudo systemctl restart cliplens-web cliplens-celery-*
```

---

## 15. Hardening (recommended)

- [ ] UFW firewall: only ports 22, 80, 443 open
- [ ] SSH: disable password auth, use keys only
- [ ] Fail2Ban for SSH brute-force
- [ ] PostgreSQL: only listen on `localhost`
- [ ] Redis: only listen on `localhost`, set `requirepass`
- [ ] Run `pg_dump` backups to off-server storage daily
- [ ] Monitor disk usage on `/srv/cliplens/media/` — set alerts at 80%
- [ ] Rotate logs in `/srv/cliplens/logs/` via logrotate
- [ ] Enable Postgres slow-query log for tuning
- [ ] Set up Sentry / similar for error monitoring

---

See also: [docs/operations.md](operations.md) for day-2 ops.
