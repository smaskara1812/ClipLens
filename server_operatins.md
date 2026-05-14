---
name: Server Operations & Configuration Reference
description: Complete guide for managing ClipLens on DAMSERVER — deploy, services, logs, env, nginx, systemd configs
type: reference
originSessionId: 743862a7-a0d3-4c05-82b0-19a5235487a1
---
## Server Info

| What | Value |
|---|---|
| IP | 65.20.81.122 |
| Hostname | DAMSERVER |
| User | root |
| OS | Ubuntu 24.04 (Linux 6.8.0-110-generic) |
| Project root | `/var/www/cliplens/` |
| Python venv | `/var/www/cliplens/venv/` |
| Media files | `/var/www/cliplens/media/` |
| Static files | `/var/www/cliplens/staticfiles/` |
| Environment vars | `/var/www/cliplens/.env` |
| Nginx config | `/etc/nginx/sites-enabled/cliplens` |

---

## Stack Overview

| Component | What | Version |
|---|---|---|
| Django | Web framework | 4.2 |
| Gunicorn | WSGI server (behind nginx) | — |
| Nginx | Reverse proxy + static/media serving | — |
| PostgreSQL | Primary database | — |
| Redis | Celery broker + Django cache | — |
| Celery | Background task queue | 5.6.3 |
| mediamtx | RTMP server for live streaming | v1.9.3 |
| Ollama | Local LLM for AI summaries | 0.23.3 |
| Model | llama3.1:8b (pulled, ~5 GB) | — |

---

## Systemd Services

| Service | What it does | Restart on deploy? |
|---|---|---|
| `cliplens.service` | Gunicorn web app (Django) | ✅ Always |
| `cliplens-celery.service` | Main worker — processing, captions, default queues | ✅ Always |
| `cliplens-celery-audio.service` | Audio worker — PANNs, silence detection | ✅ Always |
| `cliplens-celery-translation.service` | Translation worker — NLLB-200 | ✅ Always |
| `cliplens-celery-live.service` | Live stream FFmpeg worker (live queue only) | ✅ Always |
| `mediamtx.service` | RTMP server port 1935 + internal API 127.0.0.1:9997 | ✅ Always |
| `ollama.service` | Ollama LLM server — auto-started, unloads model after 5min idle | ❌ Never (self-manages) |
| `redis` | Redis broker/cache | ❌ Never |
| `postgresql` | Database | ❌ Never |
| `nginx` | Reverse proxy | ❌ Only if nginx config changes |

---

## Standard Deploy (new code version)

```bash
cd /var/www/cliplens
git stash                                      # discard any local edits
git fetch --tags --force                       # get all tags from remote
git checkout v<tag>                            # e.g. v1.3.4

source venv/bin/activate
pip install -r requirements.txt
python manage.py migrate --noinput
python manage.py collectstatic --noinput

sudo systemctl restart cliplens.service \
  cliplens-celery.service \
  cliplens-celery-audio.service \
  cliplens-celery-translation.service \
  cliplens-celery-live.service \
  mediamtx.service
```

> If only templates or static files changed (no Python/DB changes):
> ```bash
> git checkout v<tag>
> python manage.py collectstatic --noinput
> sudo systemctl restart cliplens.service
> ```

---

## Service Management Commands

```bash
# ── Restart all ClipLens services ────────────────────────────────────────────
sudo systemctl restart cliplens.service cliplens-celery.service cliplens-celery-audio.service cliplens-celery-translation.service cliplens-celery-live.service mediamtx.service

# ── Check status of all services ─────────────────────────────────────────────
sudo systemctl status cliplens.service cliplens-celery.service cliplens-celery-audio.service cliplens-celery-translation.service cliplens-celery-live.service mediamtx.service --no-pager

# ── Restart individual services ───────────────────────────────────────────────
sudo systemctl restart cliplens.service
sudo systemctl restart cliplens-celery.service
sudo systemctl restart cliplens-celery-live.service
sudo systemctl restart mediamtx.service
sudo systemctl restart nginx
sudo systemctl restart ollama

# ── Stop / Start ──────────────────────────────────────────────────────────────
sudo systemctl stop  cliplens.service
sudo systemctl start cliplens.service
```

---

## Log Commands

```bash
# ── Web app (Django/Gunicorn) ─────────────────────────────────────────────────
sudo journalctl -u cliplens.service -f                    # live
sudo journalctl -u cliplens.service -n 100 --no-pager     # last 100 lines

# ── Main Celery worker ────────────────────────────────────────────────────────
sudo journalctl -u cliplens-celery.service -f
sudo journalctl -u cliplens-celery.service -n 100 --no-pager

# ── Live stream worker ────────────────────────────────────────────────────────
sudo journalctl -u cliplens-celery-live.service -f

# ── Translation worker ────────────────────────────────────────────────────────
sudo journalctl -u cliplens-celery-translation.service -f

# ── Audio worker ──────────────────────────────────────────────────────────────
sudo journalctl -u cliplens-celery-audio.service -f

# ── mediamtx (RTMP) ───────────────────────────────────────────────────────────
sudo journalctl -u mediamtx.service -f

# ── Ollama ────────────────────────────────────────────────────────────────────
sudo journalctl -u ollama.service -f

# ── Nginx ─────────────────────────────────────────────────────────────────────
sudo tail -f /var/log/nginx/error.log
sudo tail -f /var/log/nginx/access.log

# ── Filter for errors only ────────────────────────────────────────────────────
sudo journalctl -u cliplens-celery.service -n 200 --no-pager | grep -i "error\|failed\|exception"
```

---

## Updating the .env File

The `.env` file is **NOT in git** (gitignored). It must be edited directly on the server.

```bash
nano /var/www/cliplens/.env
```

After saving any `.env` change, restart the affected services:
```bash
# .env changes affect Django + all Celery workers
sudo systemctl restart cliplens.service \
  cliplens-celery.service \
  cliplens-celery-audio.service \
  cliplens-celery-translation.service \
  cliplens-celery-live.service
```

> **Important**: Celery workers load env vars at startup. If you change `.env` and only restart `cliplens.service`, the workers keep running with the old values.

### Key .env variables to know

```bash
DEBUG=False
ALLOWED_HOSTS=yourdomain.com,65.20.81.122,localhost,127.0.0.1

# Database
POSTGRES_DB=freestream
POSTGRES_USER=postgres
POSTGRES_PASSWORD=<password>
POSTGRES_HOST=localhost

# Redis
REDIS_URL=redis://localhost:6379/1
CELERY_BROKER_URL=redis://localhost:6379/0

# Ollama AI summaries
USE_OLLAMA=true
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=llama3.1:8b

# Live streaming (mediamtx webhook secret — must match mediamtx.yml)
MEDIAMTX_SECRET=cliplens-mediamtx-secret

# Whisper captions
WHISPER_MODEL_SIZE=base
WHISPER_DEVICE=cpu
```

---

## Updating Non-Git Files

Files that live only on the server and must be edited manually:

### 1. Nginx config
```bash
nano /etc/nginx/sites-enabled/cliplens

# After editing — test then reload:
sudo nginx -t
sudo systemctl reload nginx
```

### 2. mediamtx config
```bash
nano /etc/mediamtx/mediamtx.yml
# or (if installed locally):
nano /var/www/cliplens/mediamtx.yml

sudo systemctl restart mediamtx.service
```

### 3. Systemd service files
```bash
ls /etc/systemd/system/cliplens*.service

nano /etc/systemd/system/cliplens-celery.service

# After editing any service file:
sudo systemctl daemon-reload
sudo systemctl restart <service-name>
```

### 4. The .env file
*(See section above)*

---

## Database Commands

```bash
source /var/www/cliplens/venv/bin/activate
cd /var/www/cliplens

# Django shell
python manage.py shell

# Raw SQL shell
python manage.py dbshell

# Run pending migrations
python manage.py migrate --noinput

# Check for unapplied migrations
python manage.py showmigrations | grep '\[ \]'
```

---

## Ollama Commands

```bash
# Check what models are downloaded
ollama list

# Pull a model
ollama pull llama3.1:8b

# Test Ollama is responding
curl -s http://localhost:11434/api/generate \
  -H "Content-Type: application/json" \
  -d '{"model":"llama3.1:8b","prompt":"Say hello in one word","stream":false}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['response'])"

# Ollama status
sudo systemctl status ollama
```

---

## Nginx Configuration Summary

The nginx config at `/etc/nginx/sites-enabled/cliplens` does:
- Proxies all HTTP traffic → Gunicorn socket (`/var/www/.gunicorn`)
- Serves `/static/` from `/var/www/cliplens/staticfiles/`
- Serves `/media/` from `/var/www/cliplens/media/`
- Serves `/media/live/` with no-cache + CORS headers (for HLS live streaming)
- Sets correct MIME types for `.m3u8` and `.ts` files

---

## mediamtx Configuration Summary

Config at `/var/www/cliplens/mediamtx.yml` (or `/etc/mediamtx/mediamtx.yml`):
- RTMP on port **1935** — OBS connects here
- Internal API on **127.0.0.1:9997** — Django queries this
- Path pattern `~^live/(.+)$` captures the stream key
- `runOnReady` → POST to `http://127.0.0.1:8000/api/streams/on-publish/`
- `runOnNotReady` → POST to `http://127.0.0.1:8000/api/streams/on-unpublish/`
- `writeQueueSize: 2048` — absorbs B-frame reordering from OBS

---

## Useful Shortcuts

```bash
# Quick health check — is the site up?
curl -s -o /dev/null -w "%{http_code}" http://localhost

# Check disk space (media files grow large)
df -h /var/www/cliplens/media/

# Check memory (Ollama uses ~5-6 GB when model is loaded)
free -h

# See all running ClipLens processes
ps aux | grep -E "gunicorn|celery|mediamtx|ollama"

# Activate venv (needed before any python/manage.py commands)
source /var/www/cliplens/venv/bin/activate
cd /var/www/cliplens
```
