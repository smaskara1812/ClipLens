"""
Celery application for ClipLens.

Start a worker with:
    celery -A cliplens worker -l info

In development you can also use:
    celery -A cliplens worker -l info --concurrency=2
"""
import os
from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'cliplens.settings')

app = Celery('cliplens')

# Read config from CELERY_* keys in Django settings
app.config_from_object('django.conf:settings', namespace='CELERY')

# Auto-discover tasks in all INSTALLED_APPS
app.autodiscover_tasks()


# ── Beat schedule ────────────────────────────────────────────────────────────
# Run only if a celery-beat process is started (e.g. `celery -A cliplens beat`).
from celery.schedules import crontab   # noqa: E402

app.conf.beat_schedule = {
    # Phase 7: purge soft-deleted media dirs once their grace period expires.
    # Runs daily at 03:17 UTC (off-peak).
    'purge-expired-media-relocations': {
        'task':     'tenants.purge_expired_media_relocations',
        'schedule': crontab(hour=3, minute=17),
    },
    # Automated health sweep — fast checks only (skips model-cache scans);
    # emails the platform owner on errors, throttled to one alert per 6h.
    'system-health-sweep': {
        'task':     'tenants.run_system_health_checks',
        'schedule': crontab(minute=7),   # hourly at :07
    },
}
