# ClipLens

**AI-powered media intelligence platform for organisations.**

ClipLens ingests video and photo libraries, transcribes every word, recognises every face, tags every object, and translates captions to 200+ languages — making any archive instantly searchable. Built as a multi-tenant SaaS with per-organisation data isolation, Stripe billing, and a self-host option.

---

## Quick links

- **[Architecture overview](#architecture)** — how the pieces fit together
- **[Local development](#local-development)** — get running in 15 minutes
- **[Multi-tenancy guide](docs/multitenancy.md)** — provisioning, subdomains, DB isolation
- **[Billing & Stripe](docs/billing.md)** — plans, top-ups, webhooks
- **[AI minutes & hardware](docs/ai-minutes-and-hardware.md)** — how compute is metered, hosted vs self-host design
- **[Deployment](docs/deployment.md)** — production hosting + systemd + nginx
- **[Operations](docs/operations.md)** — day-to-day server tasks
- **[API reference](docs/api.md)** — REST endpoints
- **[Future roadmap](future_plans.md)** — what's coming next

---

## What it does

| Capability | Stack |
|------------|-------|
| Adaptive video streaming | FFmpeg HLS encoding, scrub-thumbnail sprite sheets |
| Transcription | OpenAI Whisper (faster-whisper) — word-level timestamps |
| Translation | Meta NLLB-200 — 200+ languages |
| Face recognition | InsightFace buffalo_l (ArcFace embeddings) |
| Object detection | Ultralytics YOLOv8n |
| Scene description | Salesforce BLIP / Microsoft Florence-2 |
| Semantic visual search | OpenAI CLIP ViT-B/32 → pgvector HNSW |
| Speaker diarization | pyannote.audio 3.x |
| Audio events | PANNs CNN14 (applause, music, silence, etc.) |
| Geo search | Leaflet + per-tenant NamedPlace records |
| Multi-tenant DB | Postgres per organisation + control DB |
| Billing | Stripe (subscriptions, top-ups, customer portal, webhooks) |

**Every AI model runs locally** on your infrastructure. No tenant data ever leaves your boundary — not to OpenAI, not to Anthropic, not to anyone.

---

## Architecture

```
                                  ┌──────────────────────────────┐
                                  │   admin.cliplens.com         │
   *.cliplens.com  ───┐           │   Control Plane              │
                      │           │   - tenants list             │
                      │  nginx    │   - plans + top-up products  │
                      │ (wildcard │   - leads inbox              │
                      │  cert)    │   - usage / billing overview │
                      ▼           └──────────────────────────────┘
            ┌──────────────────┐                ▲
            │  Django :8000    │ ── subdomain ──┤
            │                  │     routing    │
            │  TenantMiddleware│                ▼
            │  TenantDBRouter  │  ┌──────────────────────────────┐
            │  TenantStorage   │  │  org1.cliplens.com           │
            └──────────────────┘  │  org2.cliplens.com           │
                  │      │       │  org3.cliplens.com           │
                  │      │       │  ┌────────────────────────┐   │
                  ▼      ▼       │  │ Per-org App            │   │
          ┌────────┐ ┌──────────┐│  │ - upload, search       │   │
          │ Redis  │ │ Postgres ││  │ - usage page           │   │
          └────────┘ │  - control  │ - billing + top-ups    │   │
              │     │  - freestream_<slug> per tenant   │   │
              ▼     └───────────┘  └────────────────────────┘   │
        ┌──────────────────────────────────────────────────────┘
        │ Celery Workers (--pool=solo for AI fork-safety)
        │ - main (HLS, captions, default)
        │ - audio (PANNs, silence)
        │ - translation (NLLB-200)
        │ - live (FFmpeg live streaming)
        └─────────────────────────────────────────────────────
```

### Three-tier subdomain model

| Subdomain | Purpose | Authentication |
|-----------|---------|----------------|
| `cliplens.com` (bare) | Public marketing landing + Privacy + Terms + contact form | None |
| `admin.cliplens.com` | Control plane — platform owner manages all orgs, plans, leads | Platform owner role |
| `<slug>.cliplens.com` | Per-organisation app — uploads, search, admin panel, billing | Org user (admin/editor/viewer) |

### Database layout

- **`freestream_control`** — single shared DB holding `Tenant`, `Plan`, `TopUpProduct`, `StorageAddon`, `AICreditPack`, `UsageEvent`, `OnboardingInvite`, `LeadRequest`
- **`freestream_<slug>`** — one isolated DB per tenant holding all media records (Videos, Photos, Channels, FaceIdentity, etc.) and user accounts
- **TenantDatabaseRouter** reads a thread-local set by `TenantMiddleware` (from subdomain) or by `setup_tenant_context()` inside Celery tasks (from `tenant_slug` kwarg)

### Media layout

```
media/
├── tenants/
│   ├── org1/
│   │   ├── originals/         ← uploaded video/photo source files
│   │   ├── hls/<video_id>/    ← adaptive bitrate streams + master.m3u8
│   │   ├── thumbnails/
│   │   ├── seek_sprites/      ← scrub-preview JPEG grids
│   │   ├── faces/<video_id>/  ← cropped face images
│   │   ├── subtitles/         ← VTT files (auto + translated)
│   │   ├── audio/             ← extracted WAVs for diarization
│   │   └── photos/
│   └── org2/...
└── live/                       ← live stream HLS segments (not tenant-scoped)
```

`TenantFileSystemStorage` overrides `location` and `base_url` per request so `FileField.url` automatically produces `/media/tenants/org1/...` without any extra logic in views.

---

## Local development

### Prerequisites

- macOS or Linux
- Python 3.11 (3.12 also works)
- PostgreSQL 15+ with `pgvector` extension
- Redis
- FFmpeg
- nginx (for multi-tenant subdomain routing)
- Stripe CLI (optional, for testing payments locally)

### One-time setup

```bash
# 1. Clone + virtualenv
git clone <repo> && cd Freestream
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 2. Copy .env.example → .env and edit
#    Set MULTI_TENANT=true if you want subdomain routing
#    Set STRIPE_SECRET_KEY for real test checkout (optional)
cp .env.example .env

# 3. Create the control DB and run migrations
python manage.py setup_multitenancy

# 4. Install pgvector extension (one-time)
psql freestream -c "CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS pg_trgm;"
```

### `/etc/hosts` for subdomain routing

```bash
sudo tee -a /etc/hosts <<EOF
127.0.0.1  cliplens.local
127.0.0.1  admin.cliplens.local
127.0.0.1  testorg1.cliplens.local
127.0.0.1  testorg2.cliplens.local
EOF
```

### nginx config

The wildcard server block lives in `/opt/homebrew/etc/nginx/servers/cliplens-local.conf` (macOS) or `/etc/nginx/sites-enabled/cliplens` (Linux). See [docs/multitenancy.md](docs/multitenancy.md) for the exact content.

### Start everything

```bash
./start.sh
```

This boots Django on `:8000`, Celery workers (main / audio / translation / live), Flower on `:5556`, and the Stripe CLI webhook tunnel (if `STRIPE_SECRET_KEY` is set). Ctrl-C kills all of them cleanly.

### First-run checklist

1. Visit `http://cliplens.local` → marketing landing
2. Visit `http://admin.cliplens.local` → log in as platform owner (Django superuser with `is_platform_owner=True`)
3. **Create plans** at `/plans/` with prices
4. **Create top-up products** at `/topups/` (storage addons + AI credit packs)
5. **Provision a test org** at `/tenants/new/` → copy the invite link → claim it at `<slug>.cliplens.local/onboard/<token>/`
6. **Upload a test video** in the new org's app

---

## Project structure

```
Freestream/
├── cliplens/                  ← Django project config
│   ├── settings.py            ← env-driven config, MULTI_TENANT switch
│   ├── celery.py
│   └── urls.py                ← root URL routing (landing, onboarding, webhooks, /platform/, app)
│
├── tenants/                   ← Multi-tenancy app (control plane)
│   ├── models.py              ← Tenant, Plan, TopUpProduct, StorageAddon,
│   │                            AICreditPack, OnboardingInvite, UsageEvent, LeadRequest
│   ├── middleware.py          ← TenantMiddleware (subdomain → request.tenant)
│   ├── db_router.py           ← Routes ORM queries to the right DB
│   ├── storage.py             ← TenantFileSystemStorage (per-org media folders)
│   ├── provisioning.py        ← Creates DBs, runs migrations, creates invites
│   ├── metering.py            ← Quota helpers, credit pack draining
│   ├── stripe_utils.py        ← Stripe Checkout, webhooks, Customer Portal
│   ├── celery_utils.py        ← task_prerun signal → setup_tenant_context
│   ├── media_serve.py         ← Tenant-aware /media/ serving with access checks
│   ├── views.py               ← Control plane + landing + onboarding + leads
│   ├── templates/tenants/
│   │   ├── landing.html       ← Public marketing site
│   │   ├── privacy.html       ← Privacy policy
│   │   ├── terms.html         ← Terms of service
│   │   ├── onboard.html       ← Org admin claims invite + picks plan
│   │   ├── dashboard.html     ← Platform owner main dashboard
│   │   ├── tenant_detail.html ← Per-tenant deep dive
│   │   ├── manage_plans.html
│   │   ├── manage_topups.html
│   │   └── manage_leads.html
│   └── management/commands/
│       ├── setup_multitenancy.py
│       └── cleanup_orphans.py
│
├── videos/                    ← Main app (per-tenant data)
│   ├── models.py              ← Video, Photo, Channel, FaceIdentity, etc.
│   ├── views.py               ← ~5000 lines: player, upload, search, billing, top-ups
│   ├── tasks.py               ← Celery tasks for the AI pipeline
│   ├── services.py            ← FFmpeg HLS encoding, thumbnail extraction
│   ├── upscale.py             ← Lanczos upscale pipeline
│   ├── middleware.py          ← LoginRequiredMiddleware, HLS CORS headers
│   ├── context_processors.py  ← Injects usage warnings, user role
│   └── templates/videos/
│       ├── _sidebar.html      ← Left nav with Admin / Plan / Top Up / Billing
│       ├── _admin_nav.html    ← Top tab strip for admin pages
│       ├── org_usage.html     ← AI minutes + storage dashboard for org admin
│       ├── org_topup.html     ← Buy storage addons / credit packs
│       ├── org_billing.html   ← Plan + payment method + invoices + history
│       └── org_plan_upgrade.html
│
├── docs/                      ← Detailed guides (see links above)
├── start.sh                   ← Dev launcher (Django + Celery + Stripe CLI)
├── manage.py
├── requirements.txt
└── .env                       ← Local config (gitignored)
```

---

## Multi-tenancy: how a request flows

```
Browser → orgX.cliplens.com/upload/
            ↓
nginx  ── proxies to localhost:8000, preserves Host header
            ↓
TenantMiddleware ── parses subdomain "orgX"
                 ── looks up Tenant in control DB
                 ── set_db("freestream_orgX")  (thread-local)
                 ── set_media_root("media/tenants/orgX/")
                 ── attaches request.tenant
            ↓
LoginRequiredMiddleware ── ensures user is authenticated
            ↓
Django view ── any ORM query auto-routes to freestream_orgX via TenantDatabaseRouter
            ── any file upload writes under media/tenants/orgX/ via TenantFileSystemStorage
            ↓
Celery dispatch ── task.apply_async(args=[id], kwargs={'tenant_slug': 'orgX'})
            ↓
Worker (separate process) ── task_prerun signal calls setup_tenant_context('orgX')
                          ── sets the same thread-locals as the web request
                          ── task body runs with correct DB + media context
            ↓
task_postrun signal ── records elapsed time as UsageEvent in control DB
                    ── drains credit packs if over plan quota
```

---

## Billing: pricing model

| Item | Model | Cancellation |
|------|-------|--------------|
| **Plan** (Starter / Pro / Enterprise) | Monthly recurring Stripe subscription. Free tier supported (`price_usd=0`). | Cancel anytime — access until period end |
| **Storage addon** | Monthly recurring Stripe subscription, adds GB to plan limit | Cancel anytime — GB stays until period end |
| **AI credit pack** | One-time Stripe payment, 12-month expiry on credits | Non-refundable; credits drain FIFO past plan limit |

### Effective limits per tenant
```
ai_minutes_effective  = plan.ai_minutes_limit + Σ(unconsumed credit packs)
storage_effective_gb  = plan.storage_limit_gb + Σ(active storage addons)
```

### Stripe events handled
- `checkout.session.completed` → activates plan or creates addon/credit pack
- `customer.subscription.updated` → syncs plan status, handles `cancel_at_period_end`
- `customer.subscription.deleted` → marks addon/plan terminated
- `invoice.payment_failed` → flips tenant to `past_due`

See [docs/billing.md](docs/billing.md) for full payment lifecycle and webhook handling.

---

## Roles & access control

### Platform-level
- **Platform owner** (`is_platform_owner=True` on UserProfile) — full access to control plane at `admin.cliplens.com`. Sees all tenants, plans, leads. Cannot directly access a tenant's app data without logging in as a tenant user.

### Org-level (inside each tenant)
- **Superadmin** — full control of one organisation. Manages users, plans, billing, top-ups.
- **Editor** — can upload, edit, delete media. Cannot manage users or billing.
- **Viewer** — read-only across the entire library.

Decorators: `@platform_owner_required`, `@superuser_required` (org admin), `@editor_required`, `@login_required`.

---

## AI minutes metering

Every Celery task is timed by `task_prerun` / `task_postrun` signals. On success or failure, a `UsageEvent` row is written to the control DB with the task's wall-clock duration.

Counted (drains plan minutes, then credit packs):
- `process_video_task`, `generate_seek_thumbnails_task`, `extract_audio_tracks_task`
- `generate_captions_task`, `translate_subtitles_task`
- `analyze_video_frames_task`, `analyze_photo_task`
- `run_diarization_task`, `detect_audio_events_task`
- `upscale_video_task`, `upscale_photo_task`
- `generate_video_summary_task`

Not counted (no AI compute):
- `reindex_segments_task` (just VTT parsing)
- `run_live_ffmpeg` (live streaming, billed separately if at all)

Storage is **always computed live by walking the tenant directory** — not from `storage_delta` events — so orphaned files still count toward the limit.

---

## Daily ops cheat sheet

```bash
# Provision a new org (control plane)
# Easier: use the UI at admin.cliplens.local/tenants/new/
# Programmatic:
python manage.py shell -c "
from tenants.provisioning import provision_tenant_with_invite
r = provision_tenant_with_invite(slug='acme', name='Acme Corp',
                                  admin_email='admin@acme.com',
                                  admin_username='admin')
print(r['token'])  # share the URL: <slug>.cliplens.com/onboard/<token>/
"

# Run migrations on every tenant DB (after a schema change)
python manage.py migrate                          # default DB
python manage.py migrate --database=control       # control DB
# (per-tenant migrations are handled automatically inside provisioning;
#  for an existing fleet, loop through Tenants and call migrate per alias)

# Clean up orphaned media files
python manage.py cleanup_orphans                  # dry run
python manage.py cleanup_orphans --delete         # for real
python manage.py cleanup_orphans --tenant acme --delete   # one tenant

# Check Stripe webhook health (Stripe CLI must be running)
tail -f /tmp/stripe-listen.log

# Tail Django logs
tail -f /var/log/cliplens/django.log

# Restart a Celery worker (production)
sudo systemctl restart cliplens-celery-main
```

---

## Documentation index

| File | What's inside |
|------|---------------|
| **[CLAUDE.md](CLAUDE.md)** | Guidance for AI assistants working on this repo |
| **[docs/multitenancy.md](docs/multitenancy.md)** | Detailed multi-tenancy architecture, nginx config, DB router internals |
| **[docs/billing.md](docs/billing.md)** | Stripe integration, webhook events, billing lifecycle |
| **[docs/ai-minutes-and-hardware.md](docs/ai-minutes-and-hardware.md)** | What "AI minute" measures, hardware-dependence problem, recommended fixes for hosted vs self-hosted |
| **[docs/deployment.md](docs/deployment.md)** | Production deployment guide |
| **[docs/operations.md](docs/operations.md)** | Day-to-day server operations |
| **[docs/api.md](docs/api.md)** | REST API reference |
| **[future_plans.md](future_plans.md)** | Planned features (coupons, white-label branding, etc.) |
| **[deploy_linux.md](deploy_linux.md)** | Legacy single-tenant Linux deployment (kept for reference) |

---

## Contributing

This is a private commercial project. See `CLAUDE.md` if you're using an AI assistant to make changes.

## License

Proprietary. © Soham Maskara. All rights reserved.
