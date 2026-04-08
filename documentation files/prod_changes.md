# Production Deployment Checklist

Changes required in `cliplens/settings.py` and `.env` before going live on Linux.
Items marked ⚠️ are security-critical and must be done before any public deployment.

---

## 1. ⚠️ SECRET_KEY — must fail hard if missing

**File:** `cliplens/settings.py`

**Current:**
```python
SECRET_KEY = os.getenv('SECRET_KEY', 'freestream-dev-secret-key-2024')
```

**Change to:**
```python
SECRET_KEY = os.getenv('SECRET_KEY')
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = 'dev-only-insecure-key'
    else:
        raise ImproperlyConfigured("SECRET_KEY must be set in production")
```

**Why:** If SECRET_KEY is not set in .env on the Linux server, Django silently runs with a public default key. Session tokens and CSRF tokens become forgeable.

---

## 2. ⚠️ CORS — lock down for production

**File:** `cliplens/settings.py`

**Current:**
```python
CORS_ALLOW_ALL_ORIGINS = True
CORS_ALLOW_CREDENTIALS = True
```

**Change to:**
```python
if DEBUG:
    CORS_ALLOW_ALL_ORIGINS = True
else:
    CORS_ALLOWED_ORIGINS = [o.strip() for o in os.getenv('CORS_ALLOWED_ORIGINS', '').split(',') if o.strip()]
    CORS_ALLOW_ALL_ORIGINS = False
```

**Linux .env:**
```
CORS_ALLOWED_ORIGINS=https://yourdomain.com
```

**Why:** `CORS_ALLOW_ALL_ORIGINS=True` + `CORS_ALLOW_CREDENTIALS=True` allows any website to make credentialed API requests from a visitor's browser.

---

## 3. ⚠️ CSRF_TRUSTED_ORIGINS — required behind Nginx

**File:** `cliplens/settings.py`

**Add:**
```python
CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in os.getenv('CSRF_TRUSTED_ORIGINS', 'http://localhost:8000').split(',')
]
```

**Linux .env:**
```
CSRF_TRUSTED_ORIGINS=https://yourdomain.com
```

**Why:** Django 4.0+ rejects CSRF tokens when Nginx proxies requests and the host header changes. Login will silently fail without this.

---

## 4. debug_toolbar — exclude from production

**File:** `cliplens/settings.py`

**Current:** `'debug_toolbar'` is always in `INSTALLED_APPS` and `DebugToolbarMiddleware` always in `MIDDLEWARE`.

**Change:** Wrap both in `if DEBUG:` guards.

```python
# In INSTALLED_APPS, remove 'debug_toolbar' from the list, then add:
if DEBUG:
    INSTALLED_APPS += ['debug_toolbar']

# In MIDDLEWARE, remove DebugToolbarMiddleware, then add:
if DEBUG:
    MIDDLEWARE.insert(1, 'debug_toolbar.middleware.DebugToolbarMiddleware')
```

**Why:** Debug toolbar is loaded in production unnecessarily, adds overhead and exposes internals.

---

## 5. Database connection pooling

**File:** `cliplens/settings.py`

**Add to the PostgreSQL DATABASES block:**
```python
'CONN_MAX_AGE': int(os.getenv('CONN_MAX_AGE', '60')),
```

**Why:** Without this, every Django request opens and closes a new PostgreSQL connection (~5–10ms overhead per request). `CONN_MAX_AGE=60` reuses connections for 60 seconds.

---

## 6. HTTPS security headers (only when using SSL)

**File:** `cliplens/settings.py`

**Add:**
```python
USE_X_FORWARDED_HOST = True
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    SECURE_SSL_REDIRECT     = True
    CSRF_COOKIE_SECURE      = True
```

**Why:** Behind Nginx, Django doesn't know the request came in over HTTPS. Without `SECURE_PROXY_SSL_HEADER`, `request.build_absolute_uri()` generates `http://` URLs and session cookies may not be marked secure.

---

## 7. Structured logging

**File:** `cliplens/settings.py`

**Add:**
```python
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'standard': {'format': '%(asctime)s [%(levelname)s] %(name)s: %(message)s'},
    },
    'handlers': {
        'console': {'class': 'logging.StreamHandler', 'formatter': 'standard'},
    },
    'root': {'handlers': ['console'], 'level': 'INFO'},
    'loggers': {
        'django':  {'handlers': ['console'], 'level': 'WARNING'},
        'videos':  {'handlers': ['console'], 'level': 'INFO'},
    },
}
```

**Why:** Currently no logging config — errors are unstructured. Systemd journal captures stdout automatically, so logging to console is all you need.

---

## 8. Linux .env — full production template

Create `/var/www/cliplens/.env` on the server:

```bash
# Security
SECRET_KEY=<generate with: python -c "import secrets; print(secrets.token_hex(50))">
DEBUG=False
ALLOWED_HOSTS=yourdomain.com,www.yourdomain.com

# Site
SITE_URL=https://yourdomain.com
CSRF_TRUSTED_ORIGINS=https://yourdomain.com
CORS_ALLOWED_ORIGINS=https://yourdomain.com

# Database
USE_POSTGRES=true
POSTGRES_DB=cliplens
POSTGRES_USER=cliplens
POSTGRES_PASSWORD=<strong password>
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
CONN_MAX_AGE=60

# Redis
REDIS_URL=redis://localhost:6379/0
CELERY_BROKER_URL=redis://localhost:6379/0

# AI pipeline
FRAME_ANALYSIS_ENABLED=true
FACE_RECOGNITION_ENABLED=true
SCENE_DESCRIPTION_ENABLED=true
CLIP_ENABLED=true
CLIP_SIMILARITY_THRESHOLD=0.24
CLIP_PHOTO_SIMILARITY_THRESHOLD=0.28
FUZZY_SEARCH_ENABLED=true
FUZZY_SEARCH_SIMILARITY_THRESHOLD=0.35
WHISPER_MODEL_SIZE=small
WHISPER_DEVICE=cpu

# Flower
FLOWER_BASIC_AUTH=admin:<strong password>
```

---

## Priority order

| # | Change | Risk if skipped |
|---|--------|----------------|
| 1 | SECRET_KEY hard fail | Sessions forgeable |
| 2 | CORS lockdown | Cross-site credential theft |
| 3 | CSRF_TRUSTED_ORIGINS | Login broken behind Nginx |
| 4 | Remove debug_toolbar | Minor — overhead + info leak |
| 5 | CONN_MAX_AGE | Performance only |
| 6 | HTTPS headers | Wrong URLs, insecure cookies |
| 7 | Logging | Operational blind spot |





Good question — but actually `localhost` stays correct for a typical single-server deployment.

## What the architecture looks like on Linux

Everything runs on **one server**:

```
Linux Server
├── Nginx          (port 80/443)
├── Gunicorn       (Django, port 8000 internally)
├── Celery worker  (background process)
├── Flower         (port 5556)
├── PostgreSQL     (port 5432)
└── Redis          (port 6379)
```

Redis is installed on the **same machine** as Django and Celery. So `redis://localhost:6379/0` is exactly right — it's "localhost" relative to that Linux server, not your Mac.

The value in `.env` doesn't change. You just need Redis running on the server before you start Django/Celery:

```bash
# Ubuntu/Debian
sudo apt install redis-server
sudo systemctl enable redis-server
sudo systemctl start redis-server

# Verify it's up
redis-cli ping   # should return PONG
```

---

## When you WOULD need a different value

| Scenario | REDIS_URL |
|---|---|
| Single server (your plan) | `redis://localhost:6379/0` ✓ same as now |
| Redis on a separate VM | `redis://192.168.1.50:6379/0` |
| Redis with password | `redis://:yourpassword@localhost:6379/0` |
| Managed Redis (AWS ElastiCache, Upstash) | `redis://xxxx.cache.amazonaws.com:6379/0` |

---

## One thing worth adding for production

Redis by default listens on `localhost` only (no external access) and has no password. That's fine for a single-server setup. But if you ever open Redis to a network, add a password in `/etc/redis/redis.conf`:

```
requirepass yourStrongPassword
```

Then update `.env`:
```
REDIS_URL=redis://:yourStrongPassword@localhost:6379/0
CELERY_BROKER_URL=redis://:yourStrongPassword@localhost:6379/0
```

For your initial deploy: install Redis, confirm `redis-cli ping` returns `PONG`, and your existing `.env` values work as-is.