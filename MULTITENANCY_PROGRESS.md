# Multi-Tenancy Progress Log
**Project:** ClipLens SaaS conversion  
**Last updated:** 2026-05-20  
**Status:** Phases 0–4 complete · Phases 5–7 pending

---

## Commit History (multi-tenancy work is uncommitted)

All multi-tenancy code is currently **uncommitted** — it sits as working changes on top of the last pre-SaaS commit:

```
38594c3  Auto-infer channel on video upload when key has single scope
70b519c  api key tested and sorted
b16f572  Fix 500 on API keys page
56ab53b  Full search coverage + multi-select scopes in API key system
...
```

### Files modified (not yet committed)
| File | What changed |
|---|---|
| `cliplens/settings.py` | MULTI_TENANT flag, control DB, DATABASE_ROUTERS, TenantMiddleware, DEFAULT_FILE_STORAGE, new context processor |
| `cliplens/urls.py` | Added `/platform/` → tenants.urls |
| `videos/models.py` | Added `is_platform_owner` to UserProfile |
| `videos/migrations/0051_add_is_platform_owner.py` | Migration for above |
| `videos/tasks.py` | All task signatures updated with `**kwargs`; tenant_slug forwarded in all chained dispatches |
| `videos/views.py` | `_tenant_slug()` helper; all 12 task dispatch sites pass tenant_slug; quota pre-flight on upload; storage delta logging on upload/delete |
| `videos/urls.py` | Routes for user_create_api, user_delete_api |
| `videos/context_processors.py` | `site_url` fixed for multi-tenant; new `tenant_usage_warning` context processor |
| `videos/templates/videos/base.html` | Usage warning banner + CSS |
| `videos/templates/videos/user_management.html` | Create User button + modal + JS |

### New directory (entirely new — not yet committed)
```
tenants/
├── __init__.py
├── admin.py
├── apps.py                  # TenantsConfig.ready() — loads DB aliases + wires Celery signals
├── celery_utils.py          # task_prerun/postrun signals: tenant context + AI minutes metering
├── db_router.py             # TenantDatabaseRouter — thread-local DB alias selection
├── metering.py              # log_ai_minutes, log_storage_delta, check_quota, QuotaExceeded
├── middleware.py            # TenantMiddleware — subdomain → Tenant → set_db + set_media_root
├── models.py                # Plan, Tenant, UsageEvent (all live in control DB)
├── provisioning.py          # provision_tenant(), _register_db_alias(), _run_migrations(), load_all_tenant_dbs()
├── storage.py               # TenantFileSystemStorage — location resolves from thread-local at save-time
├── urls.py                  # Control plane URL routes
├── views.py                 # Control plane views: dashboard, tenant detail, create tenant, plans
├── migrations/
│   └── 0001_initial.py
├── management/commands/
│   └── setup_multitenancy.py   # One-time bootstrap command
└── templates/tenants/
    ├── base.html
    ├── 403.html
    ├── dashboard.html          # All-tenants overview with usage bars
    ├── create_tenant.html      # Provision new org form
    ├── tenant_detail.html      # Per-org detail + users
    └── manage_plans.html
```

> **Recommendation:** Commit this as a single `feat: multi-tenancy phases 0–4` commit once you've verified it end-to-end on testorg2.

---

## What's Done

### ✅ Phase 0 — Environment Setup
- nginx wildcard config: `*.cliplens.local → 127.0.0.1:8000`
  - Config at: `/opt/homebrew/etc/nginx/servers/cliplens-local.conf`
- **dnsmasq installed** (just now): wildcard `*.cliplens.local → 127.0.0.1`
  - Config at: `/opt/homebrew/etc/dnsmasq.d/cliplens.conf`
  - **Action needed:** Run `sudo brew services start dnsmasq` and set up `/etc/resolver/cliplens.local` (see DNS section below)
- PostgreSQL control DB: `freestream_control`
- Bootstrap run: `python manage.py setup_multitenancy`

### ✅ Phase 1 — Tenant Foundation
- `tenants/` Django app with `app_label = 'tenants'`
- `Plan`, `Tenant`, `UsageEvent` models in the `control` DB
- `TenantDatabaseRouter`: reads thread-local → routes `tenants` app → `control`, everything else → active tenant DB
- `TenantMiddleware`: subdomain → Tenant lookup → `set_db()` + `set_media_root()` before request, both cleared in `finally`
- Auto-provisioning UI: create org from `admin.cliplens.local/platform/` → creates PostgreSQL DB, enables pgvector + pg_trgm, runs all migrations via `MigrationExecutor`, creates media folder, creates org admin user
- `load_all_tenant_dbs()` in `TenantsConfig.ready()` — registers all active tenant DBs at Django startup
- User management for org admins: Create User modal + delete on `/admin-panel/`
- `site_url` context processor fixed to derive origin from `request.get_host()` in MULTI_TENANT mode (fixes CORS errors)

**Key bugs fixed during Phase 1:**
- `setup_multitenancy` "User not found" → username was `soham_m` not `soham`
- `_register_db_alias` KeyError `TIME_ZONE` → added all required Django DB settings keys
- `NOT NULL violation on is_platform_owner` in RunPython migrations → switched to direct `MigrationExecutor` + `set_db()` before running migrations
- POST /login/ 500 on tenant subdomain → added `DEBUG_PROPAGATE_EXCEPTIONS`, resolved itself
- Create User modal not opening → modal HTML was after `{% endblock %}`, silently discarded
- TemplateSyntaxError on users page → `{% block content %}` inside HTML comment was being parsed

### ✅ Phase 2 — Media Isolation
- `TenantFileSystemStorage` — custom `FileSystemStorage` subclass
- `location` property reads from thread-local `_media_state.root` at save-time (not import-time)
- `DEFAULT_FILE_STORAGE = 'tenants.storage.TenantFileSystemStorage'` when MULTI_TENANT=True
- All uploads (video, photo, VTT, thumbnails, face crops) land in `media/tenants/<slug>/`

### ✅ Phase 3 — Celery Tenant Context
- `tenants/celery_utils.py` — `connect_celery_signals()` wired into `TenantsConfig.ready()`
- `task_prerun` signal: sets tenant DB alias + media root from `tenant_slug` kwarg
- `task_postrun` + `task_failure` signals: clear context, record elapsed time for metering
- All task function signatures updated with `**kwargs` to accept `tenant_slug` without breaking
- `_tenant_slug(request)` helper in `views.py` — safely returns `''` in single-tenant mode
- All 12 dispatch call sites in `views.py` now pass `tenant_slug`
- Chained dispatches inside tasks forward `tenant_slug`:
  - `process_video_task` → `generate_captions_task`, `analyze_video_frames_task`, `generate_seek_thumbnails_task`
  - `run_diarization_task` → `detect_audio_events_task` (both occurrences)
  - `run_live_ffmpeg` → `process_livestream_recording`
  - `process_livestream_recording` → `process_video_task`

### ✅ Phase 4 — Usage Metering
- `tenants/metering.py`: `log_ai_minutes()`, `log_storage_delta()`, `check_quota()`, `QuotaExceeded`, `get_monthly_usage()`, `usage_warning_level()`
- AI minutes logged automatically via `task_postrun` signal — maps task name → event type
- Storage delta logged on video/photo upload (positive) and permanent delete (negative)
- Quota pre-flight on video and photo upload endpoints: HTTP 402 if storage or AI minutes limit hit
- `tenant_usage_warning` context processor: checks usage every 5 min (cached), injects `usage_warning` + `usage_warning_detail` into all templates
- Usage warning banner in `base.html`: yellow at ≥80%, red at ≥95%, dismissible

---

## Pending Phases

### 🔲 Phase 5 — Role Changes (1–2 hrs)
**Goal:** Platform owner (you) vs Org Admin vs Editor/Viewer — currently all roles are org-scoped by behaviour but the guards aren't explicitly wired for multi-tenant.

Tasks:
- [ ] `is_platform_owner` already added to `UserProfile` and migrated — ✅ done
- [ ] Update `@platform_owner_required` decorator in `tenants/views.py` to check `is_platform_owner` — ✅ already done
- [ ] Verify `@superuser_required` in `videos/views.py` still works org-scoped (it does — uses Django's `is_superuser` per-tenant)
- [ ] Test: org admin at `testorg1.cliplens.local/admin-panel/` can manage their org but cannot access `admin.cliplens.local`
- [ ] Test: your account (`is_platform_owner=True`) can access `admin.cliplens.local/platform/`

**Estimated effort:** Mostly testing — the code is already in place.

---

### 🔲 Phase 6 — Control Plane Dashboard Polish (2–3 hrs)
**Goal:** The dashboard at `admin.cliplens.local/platform/` is functional but needs a few additions.

Tasks:
- [ ] Show per-tenant **user count** on dashboard listing
- [ ] Show per-tenant **video + photo count** (requires cross-DB query — query each tenant DB)
- [ ] Tenant detail page: list users with role badges
- [ ] Tenant detail page: link to impersonate / log into that org (helpful for support)
- [ ] **Deactivate tenant** button (sets `is_active=False` → middleware returns 404)
- [ ] Change tenant plan from detail page
- [ ] Email notification to org admin when provisioning completes (optional)

---

### 🔲 Phase 7 — Demo Data & Seed Script (1 hr)
**Goal:** Two pre-seeded orgs for demos.

Tasks:
- [ ] Write `python manage.py seed_demo_tenants` command that:
  - Creates `testorg1` ("Acme Corp") on Starter plan — near the AI minutes limit
  - Creates `testorg2` ("Demo Media") on Pro plan — plenty of headroom
  - Seeds a couple of sample channels and placeholder videos for each
- [ ] Verify warning banner appears for testorg1 (near limit)
- [ ] Verify testorg2 shows no warning

---

### 🔲 Phase 8 — Production Deployment (future)
**Goal:** Get multi-tenancy running on the production server.

Tasks:
- [ ] Add `MULTI_TENANT=true` to production `.env`
- [ ] Set up wildcard SSL cert (`*.cliplens.com` via Let's Encrypt + certbot DNS challenge)
- [ ] Update production nginx for wildcard subdomain routing
- [ ] Run `setup_multitenancy` on production PostgreSQL
- [ ] Set up wildcard DNS with domain registrar (`*.cliplens.com → server IP`)
- [ ] Migrate existing single-tenant data into `testorg1` or a "legacy" org DB
- [ ] Update `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` for production domain

---

### 🔲 Phase 9 — Stripe Billing (future / optional)
**Goal:** Self-service signup with automated billing.

Tasks:
- [ ] `stripe.Customer` per Tenant
- [ ] `stripe.Subscription` with Starter/Pro/Enterprise products
- [ ] `stripe.UsageRecord` for metered AI minutes
- [ ] Webhook handler: subscription status → update `tenant.plan` and `tenant.is_active`
- [ ] Self-service signup page at `cliplens.com/signup` → provisions org automatically
- [ ] Billing portal link in org admin settings

---

## DNS Setup — Action Required

**Problem:** macOS `/etc/hosts` doesn't support wildcards, so each new provisioned org needs a manual entry. `dnsmasq` was just installed to fix this permanently.

**Run this in your terminal now:**
```bash
# Start dnsmasq
sudo brew services start dnsmasq

# Tell macOS to use dnsmasq for *.cliplens.local
sudo mkdir -p /etc/resolver
echo "nameserver 127.0.0.1" | sudo tee /etc/resolver/cliplens.local

# Flush DNS cache
sudo dscacheutil -flushcache
sudo killall -HUP mDNSResponder

# Test — both should return 127.0.0.1
ping -c 1 testorg2.cliplens.local
ping -c 1 anyneworg.cliplens.local

# Optional: clean up the now-redundant /etc/hosts entries
sudo sed -i '' '/cliplens\.local/d' /etc/hosts
```

**Config files:**
- dnsmasq rule: `/opt/homebrew/etc/dnsmasq.d/cliplens.conf` → `address=/.cliplens.local/127.0.0.1`
- nginx wildcard: `/opt/homebrew/etc/nginx/servers/cliplens-local.conf`

---

## Architecture Recap

```
Browser: testorg1.cliplens.local
    ↓
dnsmasq → 127.0.0.1
    ↓
nginx :80 (wildcard *.cliplens.local) → Django :8000
    ↓
TenantMiddleware
  → reads subdomain "testorg1"
  → looks up Tenant in freestream_control DB
  → set_db("freestream_testorg1")
  → set_media_root("media/tenants/testorg1/")
    ↓
TenantDatabaseRouter
  → all ORM queries → freestream_testorg1
    ↓
Celery task dispatched with kwargs={'tenant_slug': 'testorg1'}
  → task_prerun signal fires → set_db + set_media_root in worker thread
  → task runs with correct DB + media root
  → task_postrun → logs AI minutes to freestream_control
```

```
Database layout:
  freestream_control  ← Plan, Tenant, UsageEvent (registry + metering)
  freestream_testorg1 ← all videos, photos, users, channels for testorg1
  freestream_testorg2 ← all videos, photos, users, channels for testorg2
  freestream          ← original single-tenant DB (still works when MULTI_TENANT=false)
```

---

## Key Environment Variables

```bash
MULTI_TENANT=true              # enables all multi-tenant behaviour
CONTROL_DB_NAME=freestream_control   # set automatically by setup_multitenancy
POSTGRES_USER=...
POSTGRES_PASSWORD=...
POSTGRES_HOST=localhost
```

## Management Commands

```bash
# One-time bootstrap (already run)
python manage.py setup_multitenancy --platform-owner soham_m

# Provision a new org (or use the UI at admin.cliplens.local/platform/create/)
# (No CLI command yet — only via UI)

# Check all registered tenant DBs
python manage.py shell -c "from django.conf import settings; print(list(settings.DATABASES.keys()))"
```
