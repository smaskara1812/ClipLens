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
            tenant=tenant, html=html, from_override=from_override, reply_to=reply_to,
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
