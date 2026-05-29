# Multi-tenancy guide

ClipLens uses **physical isolation** for tenant data — one PostgreSQL database and one media folder per organisation. This document explains how it all hangs together.

---

## 1. Subdomain → Tenant routing

### nginx (production)

```nginx
# /etc/nginx/sites-available/cliplens
server {
    listen 80;
    listen 443 ssl http2;
    # Matches both bare cliplens.com (landing) AND *.cliplens.com (tenants/admin)
    server_name ~^((?<tenant>[a-z0-9\-]+)\.)?cliplens\.com$;

    ssl_certificate     /etc/letsencrypt/live/cliplens.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/cliplens.com/privkey.pem;

    client_max_body_size 4096m;
    client_body_temp_path /var/lib/nginx/client_body;
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;

    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    location / {
        proxy_pass http://127.0.0.1:8000;
    }

    # Optional: serve media directly from nginx for speed
    location /media/ {
        alias /srv/cliplens/media/;
        add_header Access-Control-Allow-Origin *;
    }

    location /static/ {
        alias /srv/cliplens/staticfiles/;
        expires 30d;
    }
}
```

You'll need a **wildcard SSL cert** covering `*.cliplens.com` AND `cliplens.com`:

```bash
sudo certbot certonly --dns-cloudflare \
    --dns-cloudflare-credentials ~/.cloudflare.ini \
    -d cliplens.com -d '*.cliplens.com'
```

### Django middleware

The `TenantMiddleware` (in `tenants/middleware.py`) handles every request:

1. Extract subdomain from `Host` header
2. Look up matching `Tenant` row in the control DB
3. If found: set thread-local DB alias + media root, attach `request.tenant`
4. If not found: 404
5. If found but inactive: 403 (except for `/onboard/`, `/login/`, `/static/`, `/favicon.ico`)

Reserved subdomains: `www`, `admin`, `api`, `static`, `media` — these never match a tenant.

---

## 2. Database router

```python
# tenants/db_router.py
class TenantDatabaseRouter:
    def db_for_read(self, model, **hints):
        if model._meta.app_label == 'tenants':
            return 'control'
        return _local.db or 'default'

    def db_for_write(self, model, **hints):
        # same as above
```

The thread-local `_local.db` is set by:
- **`TenantMiddleware`** for web requests
- **`setup_tenant_context()`** inside Celery tasks (called by `task_prerun` signal AND explicitly at the top of each task body — both are needed because `--pool=solo` has unreliable signal timing)

### `freestream_control` schema

Holds platform-wide rows:

| Model | Purpose |
|-------|---------|
| `Plan` | Subscription tiers with prices and limits |
| `Tenant` | One row per organisation (slug, db_name, plan, stripe customer/sub IDs, plan_status) |
| `OnboardingInvite` | One-time tokens for new org admins to claim |
| `TopUpProduct` | Sellable storage addons + AI credit packs (SKUs) |
| `StorageAddon` | Active monthly storage subscriptions per tenant |
| `AICreditPack` | One-time AI minutes purchases, 12-month expiry, FIFO drain |
| `UsageEvent` | Append-only ledger of AI task time + storage deltas |
| `LeadRequest` | Contact form submissions from landing page |

### Per-tenant schema (`freestream_<slug>`)

Identical to the legacy single-tenant schema — all `videos` app models: `Video`, `Photo`, `Channel`, `FaceIdentity`, `DetectedFace`, `Subtitle`, `VideoSegment`, `VideoFrame`, `NamedPlace`, `User` (Django auth), `UserProfile`, `ActivityLog`, etc.

Created fresh by `provisioning.py` when a new tenant is provisioned. All `videos.*` migrations are applied to each tenant DB.

---

## 3. Tenant-aware file storage

`tenants/storage.py` provides `TenantFileSystemStorage`:

- `location` reads the thread-local tenant root (e.g. `/srv/cliplens/media/tenants/org1/`)
- `base_url` returns `/media/tenants/org1/` when a tenant is active, falls back to `/media/` otherwise
- All ImageField / FileField operations write into the tenant subfolder
- `.url` properties auto-include the tenant prefix without any view code

Set globally in `settings.py`:
```python
STORAGES = {'default': {'BACKEND': 'tenants.storage.TenantFileSystemStorage'}, ...}
# Or, for Django < 4.2:
DEFAULT_FILE_STORAGE = 'tenants.storage.TenantFileSystemStorage'
```

### CharField-stored paths (HLS, sprites, face crops)

These are NOT FileFields — they're plain CharFields holding paths relative to `MEDIA_ROOT`. They include the tenant prefix in their stored value:

```
Video.hls_path       = "tenants/org1/hls/<uuid>/master.m3u8"
Video.seek_sprite    = "tenants/org1/seek_sprites/<uuid>.jpg"
DetectedFace.crop_path = "tenants/org1/faces/<uuid>/face_001.jpg"
```

So `/media/<path>` resolves directly without any tenant-aware URL rewriting.

---

## 4. Tenant-aware Celery tasks

Every task that touches a tenant's DB or media MUST receive `tenant_slug` and call `setup_tenant_context()` at the top:

```python
@shared_task(bind=True)
def my_task(self, video_id, **kwargs):
    tenant_slug = kwargs.get('tenant_slug', '')
    if tenant_slug:
        from tenants.celery_utils import setup_tenant_context
        setup_tenant_context(tenant_slug)
    # ... task body
```

The `task_prerun` signal does this too, but `--pool=solo` has unreliable timing — calling it twice is harmless and the explicit call inside the task body is the source of truth.

When dispatching a task from a view:
```python
my_task.apply_async(
    args=[str(video.id)],
    kwargs={'tenant_slug': request.tenant.slug},
    queue='processing',
)
```

When chaining tasks, **forward `tenant_slug`** in the chained call's kwargs.

---

## 5. Provisioning a new tenant

### Via the UI

1. Platform owner visits `admin.cliplens.com/tenants/new/`
2. Enters org name, slug, admin email, admin username — no password, no plan
3. System runs `provision_tenant_with_invite()`:
   - Creates `Tenant` row (inactive, no plan)
   - Creates PostgreSQL DB `freestream_<slug>`
   - Enables `vector`, `pg_trgm`, `unaccent` extensions
   - Registers new DB alias in `settings.DATABASES`
   - Runs all `videos.*` migrations on the new DB
   - Creates `media/tenants/<slug>/{originals,hls,thumbnails,...}/` folders
   - Generates a 7-day `OnboardingInvite` token
4. Owner sees the invite URL: `<slug>.cliplens.com/onboard/<token>/`
5. Owner shares the URL with the org admin (email / Slack / etc.)

### Via Python shell

```python
from tenants.provisioning import provision_tenant_with_invite
result = provision_tenant_with_invite(
    slug='acme',
    name='Acme Corp',
    admin_email='admin@acme.com',
    admin_username='admin',
)
print(result['token'])
```

### Claiming an invite

1. Admin visits the invite URL
2. Sets password + chooses a plan
3. If plan is **free** → tenant activates immediately, admin logs in
4. If plan is **paid** → redirected to Stripe Checkout. Webhook fires `checkout.session.completed` → activates tenant + assigns plan + saves Stripe customer/subscription IDs

---

## 6. Adding a new column to all tenant DBs

Schema changes follow the normal Django flow + a fan-out step:

```bash
# 1. Edit the model in videos/models.py
# 2. Create the migration
python manage.py makemigrations videos

# 3. Apply to every tenant DB (control + all freestream_<slug>)
python manage.py shell <<EOF
from tenants.models import Tenant
from django.core.management import call_command
for t in Tenant.objects.using('control').filter(is_active=True):
    print(f"Migrating {t.db_name}...")
    call_command('migrate', database=t.db_name, verbosity=1)
EOF
```

A management command `migrate_all_tenants` could be built to automate this — currently a manual loop.

---

## 7. Deactivating / deleting a tenant

```python
# Soft-deactivate — keeps data, blocks access
t = Tenant.objects.using('control').get(slug='acme')
t.is_active = False
t.save(using='control')

# Hard delete (after billing cleanup):
# 1. Cancel Stripe subscription if any
# 2. Drop the DB:    psql -c "DROP DATABASE freestream_acme;"
# 3. rm -rf media/tenants/acme/
# 4. Delete Tenant row:  t.delete()
```

A cleanup command for the 30-day deletion window from the Privacy Policy is on the roadmap.

---

## 8. Reserved subdomains & special routes

| URL | Lives at | Notes |
|-----|----------|-------|
| `cliplens.com/` (bare) | `tenants.views.landing_page` | Public landing |
| `cliplens.com/privacy/` | `tenants.views.privacy_page` | Public |
| `cliplens.com/terms/` | `tenants.views.terms_page` | Public |
| `cliplens.com/contact/` | `tenants.views.submit_lead` | POST only |
| `cliplens.com/api/stripe/webhook/` | `tenants.views.stripe_webhook` | Public, signature-verified |
| `admin.cliplens.com/*` | `tenants/urls.py` mounted at `/platform/` | Platform owner only |
| `<slug>.cliplens.com/*` | `videos/urls.py` mounted at `/` | Tenant app |
| `<slug>.cliplens.com/onboard/<token>/` | `tenants.views.onboard` | No login required |

---

## 9. Common gotchas

| Issue | Fix |
|-------|-----|
| `Tenant matching query does not exist` in a Celery task | Task didn't call `setup_tenant_context()` at the top |
| Files saved to `media/originals/` instead of tenant folder | Same — thread-local not set in the task |
| `tenant.plan_status='incomplete'` after onboarding | Stripe webhook never fired. Check `stripe listen` tunnel is running |
| `/admin/` shows wrong tenant's data | Django admin uses the subdomain you visit — visit it under the right subdomain |
| Migration fails with "relation does not exist" | New tenant DB hasn't been migrated yet. Run `migrate --database=freestream_<slug>` |
| `WorkerLostError: signal 11 (SIGSEGV)` | macOS fork-safety; use `--pool=solo` for any worker running AI models |

See [docs/operations.md](operations.md) for more troubleshooting.
