"""
Celery tasks for the tenants/control-plane app.

Email is sent through a queued task with a default rate limit of 60 messages
per minute (per worker). Override by setting CELERY_EMAIL_RATE_LIMIT in Django
settings or by passing your own rate_limit when calling the task.

Usage:

    from tenants.tasks import queue_email

    queue_email(
        scope='platform',
        subject='Welcome',
        body='Plain text',
        recipients=['a@b.com'],
        html='<b>Welcome</b>',
        tenant=None,
        trigger_source='create_tenant',
        triggered_by_username='soham_m',
    )
"""
import logging

from celery import shared_task
from django.conf import settings

logger = logging.getLogger(__name__)

_RATE_LIMIT = getattr(settings, 'CELERY_EMAIL_RATE_LIMIT', '60/m')


@shared_task(
    name='tenants.send_email_async',
    bind=True,
    queue='default',
    rate_limit=_RATE_LIMIT,
    max_retries=3,
    default_retry_delay=60,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
)
def send_email_async(
    self,
    *,
    scope,
    subject,
    body,
    recipients,
    tenant_id=None,
    html=None,
    inline_images=None,
    from_override=None,
    reply_to=None,
    trigger_source='',
    triggered_by_username='',
    template_key='',
):
    """Queued send. Returns True/False (the boolean from send_managed_email)."""
    from .email_utils import send_managed_email
    from .models import Tenant

    tenant = None
    if tenant_id:
        try:
            tenant = Tenant.objects.using('control').get(pk=tenant_id)
        except Tenant.DoesNotExist:
            logger.warning('send_email_async: tenant_id=%s not found', tenant_id)

    ok = send_managed_email(
        scope=scope,
        subject=subject,
        body=body,
        recipients=list(recipients),
        tenant=tenant,
        html=html,
        inline_images=inline_images or {},
        from_override=from_override,
        reply_to=reply_to,
        trigger_source=trigger_source,
        triggered_by_username=triggered_by_username,
        template_key=template_key,
        celery_task_id=self.request.id or '',
    )
    if not ok and getattr(settings, 'EMAIL_ENABLED', True):
        # Trigger retry — the failure log was already written
        raise RuntimeError('send_managed_email returned False')
    return ok


def queue_email(
    *,
    scope,
    subject,
    body,
    recipients,
    tenant=None,
    html=None,
    inline_images=None,
    from_override=None,
    reply_to=None,
    trigger_source='',
    triggered_by_username='',
    template_key='',
):
    """
    Enqueue an email to be sent asynchronously.

    If Celery isn't available, falls back to synchronous send so callers
    never silently drop mail.
    """
    tenant_id = getattr(tenant, 'pk', None) if tenant is not None else None
    try:
        send_email_async.delay(
            scope=scope,
            subject=subject,
            body=body,
            recipients=list(recipients),
            tenant_id=tenant_id,
            html=html,
            inline_images=inline_images or {},
            from_override=from_override,
            reply_to=reply_to,
            trigger_source=trigger_source,
            triggered_by_username=triggered_by_username,
            template_key=template_key,
        )
        return True
    except Exception as exc:
        logger.warning('queue_email: broker unavailable (%s) — sending sync', exc)
        from .email_utils import send_managed_email
        return send_managed_email(
            scope=scope, subject=subject, body=body, recipients=recipients,
            tenant=tenant, html=html, inline_images=inline_images or {},
            from_override=from_override, reply_to=reply_to,
            trigger_source=trigger_source, triggered_by_username=triggered_by_username,
            template_key=template_key,
        )


# ── Per-tenant media relocation (Phase 7) ────────────────────────────────────

@shared_task(
    name='tenants.relocate_tenant_media',
    bind=True,
    queue='default',
    acks_late=True,
    max_retries=0,    # never auto-retry — operator-initiated
)
def relocate_tenant_media(self, relocation_id):
    """Run a single tenant-media relocation. See media_relocate.run_relocation."""
    from .media_relocate import run_relocation
    return run_relocation(relocation_id)


@shared_task(
    name='tenants.purge_expired_media_relocations',
    queue='default',
)
def purge_expired_media_relocations():
    """Daily cleanup — drops soft-deleted old paths past their grace period."""
    from .media_relocate import purge_expired
    n = purge_expired()
    logger.info('purge_expired_media_relocations: purged %d', n)
    return {'ok': True, 'purged': n}


# ── Automated system health sweep ────────────────────────────────────────────

@shared_task(
    name='tenants.run_system_health_checks',
    queue='default',
    soft_time_limit=240,
    time_limit=300,
)
def run_system_health_checks():
    """
    Hourly automated health sweep (Celery beat). Runs the fast checks
    (DBs, Redis, disk, workers, per-tenant roots, failure counts) and
    emails the platform owner when anything is in 'error' state.

    Alert throttle: at most one alert email per 6 hours (cache key), so a
    persistent outage doesn't flood the inbox every hour.
    """
    from django.core.cache import cache
    from .system_health import run_all_checks

    results = list(run_all_checks(automated=True))
    errors = [r for r in results if r.get('status') == 'error']
    warns  = [r for r in results if r.get('status') == 'warn']

    logger.info('health sweep: %d checks, %d errors, %d warnings',
                len(results), len(errors), len(warns))

    if not errors:
        # All clear — drop the throttle so the NEXT failure alerts immediately
        cache.delete('health_alert_sent')
        return {'ok': True, 'errors': 0, 'warnings': len(warns)}

    # Throttle: skip if we alerted within the last 6 hours
    if cache.get('health_alert_sent'):
        logger.info('health sweep: %d errors but alert throttled', len(errors))
        return {'ok': False, 'errors': len(errors), 'alerted': False}

    # Find platform owner email(s) from the default (control-plane) DB
    try:
        from django.contrib.auth.models import User
        owners = list(
            User.objects.using('default')
            .filter(profile__is_platform_owner=True, is_active=True)
            .exclude(email='')
            .values_list('email', flat=True)
        )
    except Exception:
        logger.exception('health sweep: could not resolve platform owner emails')
        owners = []

    if not owners:
        logger.warning('health sweep: %d errors but no platform-owner email configured',
                       len(errors))
        return {'ok': False, 'errors': len(errors), 'alerted': False}

    lines = [f'  ✗ {r["name"]}: {r["detail"]}' + (f'\n    Hint: {r["hint"]}' if r.get('hint') else '')
             for r in errors]
    warn_lines = [f'  ⚠ {r["name"]}: {r["detail"]}' for r in warns]
    body = (
        'ClipLens automated health sweep found problems:\n\n'
        + '\n'.join(lines)
        + (('\n\nWarnings:\n' + '\n'.join(warn_lines)) if warn_lines else '')
        + '\n\nFull dashboard: /system/health/ on the admin subdomain.\n'
        + 'Failed-task log: /tasks/failed/\n'
    )
    sent = queue_email(
        scope='platform',
        subject=f'[ClipLens ALERT] {len(errors)} health check(s) failing',
        body=body,
        recipients=owners,
        trigger_source='health_sweep',
    )
    if sent:
        cache.set('health_alert_sent', True, 6 * 60 * 60)   # 6h throttle
    return {'ok': False, 'errors': len(errors), 'alerted': bool(sent)}
