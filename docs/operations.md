# Day-to-day operations

How to run, monitor, and troubleshoot ClipLens in production.

---

## Restart services

```bash
sudo systemctl restart cliplens-web
sudo systemctl restart cliplens-celery-main
sudo systemctl restart cliplens-celery-audio
sudo systemctl restart cliplens-celery-translation
sudo systemctl restart cliplens-celery-live

# Or all at once
sudo systemctl restart 'cliplens-*'
```

After any code update, restart **web** AND every **celery** worker — workers cache Python modules.

---

## Tail logs

```bash
# Application
sudo journalctl -u cliplens-web -f
tail -f /srv/cliplens/logs/gunicorn-error.log
tail -f /srv/cliplens/logs/celery-main.log

# nginx
sudo tail -f /var/log/nginx/access.log /var/log/nginx/error.log

# PostgreSQL
sudo tail -f /var/log/postgresql/postgresql-15-main.log

# Stripe webhook arrivals (look for POST /api/stripe/webhook/ in nginx logs)
sudo grep stripe /var/log/nginx/access.log | tail -20
```

---

## Provision a new tenant (CLI)

```bash
sudo -iu cliplens
cd /srv/cliplens/app && source venv/bin/activate

python manage.py shell <<EOF
from tenants.provisioning import provision_tenant_with_invite
r = provision_tenant_with_invite(
    slug='acmecorp',
    name='Acme Corporation',
    admin_email='admin@acmecorp.com',
    admin_username='admin',
)
print(f"Token:  {r['token']}")
print(f"URL:    https://acmecorp.cliplens.com/onboard/{r['token']}/")
print(f"Expires: {r['expires_at']}")
EOF
```

---

## Deactivate / reactivate a tenant

```bash
python manage.py shell <<EOF
from tenants.models import Tenant
t = Tenant.objects.using('control').get(slug='acmecorp')
t.is_active = False    # or True to reactivate
t.save(using='control')
EOF
```

Inactive tenants return 403 to all paths except `/onboard/`, `/login/`, `/static/`. Their subscription continues billing on Stripe unless you cancel it manually.

---

## Hard-delete a tenant (after billing cleanup)

```bash
python manage.py shell <<EOF
from tenants.models import Tenant
from tenants.stripe_utils import cancel_stripe_subscription
import subprocess, shutil
from pathlib import Path
from django.conf import settings

t = Tenant.objects.using('control').get(slug='acmecorp')

# 1. Cancel Stripe subscription (immediate, no period-end grace)
if t.stripe_plan_subscription_id:
    cancel_stripe_subscription(t.stripe_plan_subscription_id, at_period_end=False)

# 2. Drop the database
subprocess.run(['psql', '-U', 'cliplens', '-d', 'postgres', '-c',
                f'DROP DATABASE {t.db_name}'])

# 3. Remove media folder
media_path = Path(settings.MEDIA_ROOT) / t.media_folder
if media_path.exists():
    shutil.rmtree(media_path)

# 4. Delete control rows (cascades to UsageEvents, addons, packs, invites)
t.delete(using='control')
print("Deleted.")
EOF
```

---

## Apply migrations to all tenant DBs

After a `videos/` schema change:

```bash
python manage.py shell <<EOF
from tenants.models import Tenant
from tenants.provisioning import _register_db_alias
from django.core.management import call_command

for t in Tenant.objects.using('control').filter(is_active=True):
    print(f"Migrating {t.db_name}...")
    _register_db_alias(t.db_name)
    call_command('migrate', database=t.db_name, verbosity=0)
print("Done.")
EOF
```

A management command `migrate_all_tenants` could automate this.

---

## Clean up orphan files

```bash
# Dry run — shows what would be deleted
python manage.py cleanup_orphans

# Actually delete
python manage.py cleanup_orphans --delete

# Only one tenant
python manage.py cleanup_orphans --tenant acmecorp --delete

# Only legacy global subdirs
python manage.py cleanup_orphans --legacy-only --delete
```

Run this monthly or after big batch deletes.

---

## Check disk usage per tenant

```bash
python manage.py shell <<EOF
from pathlib import Path
from django.conf import settings
from tenants.models import Tenant

for t in Tenant.objects.using('control').order_by('slug'):
    p = Path(settings.MEDIA_ROOT) / t.media_folder
    if p.exists():
        sz = sum(f.stat().st_size for f in p.rglob('*') if f.is_file())
        print(f"{t.slug:30s}  {sz/1024**3:8.2f} GB  ({t.plan.storage_limit_gb if t.plan else 0} GB plan limit)")
EOF
```

---

## Check AI minutes usage this month

```bash
python manage.py shell <<EOF
from tenants.models import Tenant
from tenants.metering import get_monthly_usage

for t in Tenant.objects.using('control').filter(is_active=True).order_by('slug'):
    u = get_monthly_usage(t.slug)
    print(f"{t.slug:30s}  {u['ai_minutes']:8.1f} min  "
          f"(plan: {u['ai_minutes_limit_plan']}, credits: {u['ai_minutes_credits']:.0f})")
EOF
```

---

## Stripe troubleshooting

### Force re-sync a tenant's subscription status

```bash
python manage.py shell <<EOF
import stripe
from django.conf import settings
from tenants.models import Tenant

stripe.api_key = settings.STRIPE_SECRET_KEY
t = Tenant.objects.using('control').get(slug='acmecorp')

if t.stripe_plan_subscription_id:
    sub = stripe.Subscription.retrieve(t.stripe_plan_subscription_id)
    print(f"Stripe status: {sub.status}")
    print(f"Cancel at period end: {sub.cancel_at_period_end}")
    print(f"Current period end: {sub.current_period_end}")
EOF
```

### Replay a failed webhook event

```bash
# From the Stripe CLI on your laptop (NOT production):
stripe events resend evt_1XXXXX

# Or via the Stripe dashboard → Webhooks → click endpoint → click event → Resend
```

### Manually trigger our webhook handler with a dummy event (dev)

```bash
python manage.py shell <<EOF
from tenants.stripe_utils import handle_webhook_event
event = {
    'type': 'checkout.session.completed',
    'data': {'object': {
        'id': 'cs_test_FAKE',
        'subscription': None,
        'payment_intent': 'pi_test_FAKE',
        'customer': '',
        'metadata': {
            'tenant_slug': 'acmecorp',
            'topup_product_id': '1',
            'kind': '',
        },
    }},
}
print(handle_webhook_event(event))
EOF
```

---

## Common errors & fixes

### `column tenants_tenant.stripe_customer_id does not exist`
Tenant DB not migrated against latest schema. Run `migrate --database=freestream_<slug>` for the affected tenant.

### `Tenant matching query does not exist` (Celery)
The task body didn't call `setup_tenant_context(tenant_slug)` at the top. Open the task, add it (see existing tasks for the pattern).

### `WorkerLostError: signal 11 (SIGSEGV)` (Linux production)
Should not happen on Linux with `--pool=prefork`. If it does, the offending task is using a non-fork-safe library. Move that task to its own worker with `--pool=solo`.

### Storage shows wrong number compared to disk
The admin platform uses live disk scan and is authoritative. The org topup page sums per-asset; if it's lower, there are orphaned files. Run `cleanup_orphans`.

### Stripe webhook returns 400 (invalid signature)
`STRIPE_WEBHOOK_SECRET` in `.env` doesn't match the one configured in the Stripe dashboard. Verify both, then restart `cliplens-web`.

### Onboarding URL returns 404 "Organisation not found"
The tenant's invite was provisioned but the subdomain isn't registered in DNS or `/etc/hosts` (dev). Add it.

### Inactive tenant can't even reach `/onboard/<token>/`
Middleware also exempts `/login/`, `/static/`, `/favicon.ico`. If you've added a new path that should work on inactive tenants, add it to `public_inactive_paths` in `tenants/middleware.py`.

---

## Backup restore

```bash
# Restore one tenant's DB from backup
gunzip -c /backups/postgres.sql.gz | grep -A 999999 "freestream_acmecorp" | psql -U cliplens

# Restore media
rsync -av /backups/media/tenants/acmecorp/ /srv/cliplens/media/tenants/acmecorp/
```

---

## Performance tuning

### Postgres
- Set `shared_buffers = 25% of RAM`
- `effective_cache_size = 75% of RAM`
- `work_mem = 64MB` (rises memory under heavy concurrent queries)
- `maintenance_work_mem = 1GB` (for `VACUUM` / index builds)
- Enable `pg_stat_statements` to find slow queries

### Celery
- Increase `--concurrency` on workers if you have headroom — but each AI worker holds ~6 GB resident
- Add more workers (e.g. `main2`, `main3`) to process multiple videos in parallel
- Put audio + translation on dedicated CPU cores via systemd `CPUAffinity`

### Storage
- Use SSD for `/srv/cliplens/media` — face-crop reads are random I/O heavy
- If you grow past ~5 TB, consider an object store (S3 + django-storages backend) for HLS segments and originals
