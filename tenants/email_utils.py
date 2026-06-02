"""
Managed email subsystem
───────────────────────
All outbound emails go through `send_managed_email()`, which:

  1. Respects the global EMAIL_ENABLED kill switch.
  2. If EMAIL_USE_CONSOLE is True → prints to terminal (dev mode).
  3. Otherwise → looks up the active EmailConnection for the given scope
     and uses it as a per-message backend.
  4. Records every attempt (success, failure, drop) into EmailLog.

Two scopes exist:
  • 'platform'  — managed by the platform owner. Used for onboarding invites,
                  lead notifications, payment receipts.
  • 'tenant'    — managed by the org's superadmin. Used for org-internal
                  notifications (quota warnings, etc.). Requires a Tenant.

The helper never raises — failures are logged and return False so callers
don't have to wrap every send in try/except.
"""

import logging
import time
from typing import Iterable, Optional

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.core.mail.backends.base import BaseEmailBackend
from django.utils import timezone

logger = logging.getLogger(__name__)


# ── Backend resolution ────────────────────────────────────────────────────────

def _console_backend() -> BaseEmailBackend:
    from django.core.mail.backends.console import EmailBackend
    return EmailBackend()


def _smtp_backend_from_connection(conn) -> BaseEmailBackend:
    """Build a fresh SMTP backend instance from an EmailConnection row."""
    from django.core.mail.backends.smtp import EmailBackend
    return EmailBackend(
        host=conn.host,
        port=int(conn.port or 587),
        username=conn.username or None,
        password=conn.get_password() or None,
        use_tls=bool(conn.use_tls),
        use_ssl=bool(conn.use_ssl),
        timeout=10,
    )


def get_active_connection(scope: str, tenant=None):
    """
    Return the active EmailConnection for the given scope, or None.
    Pass tenant only for scope='tenant'.
    """
    from .models import EmailConnection
    qs = EmailConnection.objects.using('control').filter(scope=scope, is_active=True)
    if scope == EmailConnection.SCOPE_TENANT:
        if tenant is None:
            return None
        qs = qs.filter(tenant=tenant)
    else:
        qs = qs.filter(tenant__isnull=True)
    return qs.first()


def _resolve_backend_info(scope: str, tenant=None):
    """
    Returns (backend, backend_type_str, connection_or_None).
    `backend` may be None when EMAIL_ENABLED=False or no connection + no Django default.
    """
    from .models import EmailLog
    if not getattr(settings, 'EMAIL_ENABLED', True):
        return None, EmailLog.BACKEND_DISABLED, None
    if getattr(settings, 'EMAIL_USE_CONSOLE', False):
        return _console_backend(), EmailLog.BACKEND_CONSOLE, None
    conn = get_active_connection(scope, tenant=tenant)
    if conn is not None:
        return _smtp_backend_from_connection(conn), EmailLog.BACKEND_SMTP, conn
    return None, EmailLog.BACKEND_DEFAULT, None


# ── Log helpers ──────────────────────────────────────────────────────────────

def _create_log(
    *, scope, tenant, subject, body, recipients,
    from_email, reply_to, has_html, template_key,
    trigger_source, triggered_by_username, celery_task_id,
    connection, backend_type, initial_status,
):
    from .models import EmailLog
    try:
        return EmailLog.objects.using('control').create(
            scope                    = scope,
            tenant                   = tenant,
            connection               = connection,
            connection_name_snapshot = connection.name if connection else '',
            backend_type             = backend_type,
            subject                  = (subject or '')[:300],
            from_email               = (from_email or '')[:320],
            to_emails                = list(recipients or []),
            reply_to                 = (reply_to or '')[:320],
            body_preview             = (body or '')[:500],
            has_html                 = bool(has_html),
            template_key             = (template_key or '')[:64],
            trigger_source           = (trigger_source or '')[:64],
            triggered_by_username    = (triggered_by_username or '')[:150],
            celery_task_id           = (celery_task_id or '')[:64],
            status                   = initial_status,
        )
    except Exception:
        logger.exception('EmailLog.create failed (non-fatal)')
        return None


def _mark_log_sent(log, *, duration_ms):
    if log is None:
        return
    from .models import EmailLog
    try:
        log.status      = EmailLog.STATUS_SENT
        log.sent_at     = timezone.now()
        log.duration_ms = duration_ms
        log.save(using='control', update_fields=['status', 'sent_at', 'duration_ms'])
    except Exception:
        logger.exception('EmailLog.save (sent) failed (non-fatal)')


def _mark_log_failed(log, *, exc, duration_ms):
    if log is None:
        return
    from .models import EmailLog
    try:
        log.status        = EmailLog.STATUS_FAILED
        log.failed_at     = timezone.now()
        log.duration_ms   = duration_ms
        log.error_class   = type(exc).__name__[:120]
        log.error_message = str(exc)[:4000]
        log.save(using='control', update_fields=[
            'status', 'failed_at', 'duration_ms', 'error_class', 'error_message',
        ])
    except Exception:
        logger.exception('EmailLog.save (failed) failed (non-fatal)')


# ── Public API ────────────────────────────────────────────────────────────────

def _filter_suspended_recipients(recipients, tenant):
    """
    Drop email addresses that belong to deactivated users inside the tenant DB.
    Returns (kept_recipients, dropped_addresses).

    Suspended/deactivated users (User.is_active=False) should NEVER receive
    mail from us. For platform-scope emails (tenant=None) we don't filter —
    those go to platform owners or external addresses (leads, etc.).
    """
    if not tenant or not recipients:
        return list(recipients), []
    try:
        from django.contrib.auth.models import User
        inactive_emails = set(
            User.objects.using(tenant.db_name)
            .filter(is_active=False, email__in=recipients)
            .exclude(email='')
            .values_list('email', flat=True)
        )
    except Exception:
        logger.exception('email suspended-recipient filter failed (non-fatal)')
        return list(recipients), []
    if not inactive_emails:
        return list(recipients), []
    kept = [r for r in recipients if r not in inactive_emails]
    return kept, sorted(inactive_emails)


def send_managed_email(
    *,
    scope: str,
    subject: str,
    body: str,
    recipients: Iterable[str],
    tenant=None,
    html: Optional[str] = None,
    from_override: Optional[str] = None,
    reply_to: Optional[str] = None,
    # ── Log context (optional but recommended) ──
    trigger_source: str = '',
    triggered_by_username: str = '',
    template_key: str = '',
    celery_task_id: str = '',
) -> bool:
    """
    Send one email through the managed system. Records the attempt in EmailLog.

    Returns True if Django's send() succeeded, False on any failure or when
    EMAIL_ENABLED=False / no valid recipients (which are logged as 'dropped').

    Recipients belonging to suspended users in the given tenant are silently
    dropped before send.
    """
    recipients = [r for r in (recipients or []) if r and '@' in r]

    # ── Filter out suspended users ──
    if tenant is not None:
        recipients, dropped = _filter_suspended_recipients(recipients, tenant)
        if dropped:
            logger.info("send_managed_email[%s]: dropped %d suspended recipient(s): %s",
                        scope, len(dropped), dropped)

    # ── Validate recipients ──
    if not recipients:
        logger.info("send_managed_email: no valid recipients, dropping")
        _create_log(
            scope=scope, tenant=tenant, subject=subject, body=body, recipients=[],
            from_email=from_override or '', reply_to=reply_to, has_html=bool(html),
            template_key=template_key, trigger_source=trigger_source,
            triggered_by_username=triggered_by_username, celery_task_id=celery_task_id,
            connection=None, backend_type='disabled',
            initial_status='dropped',
        )
        return False

    # ── EMAIL_ENABLED kill switch ──
    if not getattr(settings, 'EMAIL_ENABLED', True):
        logger.info("send_managed_email: EMAIL_ENABLED=false, dropping to %s", recipients)
        _create_log(
            scope=scope, tenant=tenant, subject=subject, body=body, recipients=recipients,
            from_email=from_override or '', reply_to=reply_to, has_html=bool(html),
            template_key=template_key, trigger_source=trigger_source,
            triggered_by_username=triggered_by_username, celery_task_id=celery_task_id,
            connection=None, backend_type='disabled',
            initial_status='dropped',
        )
        return False

    # ── Resolve backend + From: address ──
    backend, backend_type, conn = _resolve_backend_info(scope, tenant=tenant)

    from_email = from_override
    if not from_email and conn and conn.from_email:
        if conn.from_name:
            from_email = f'"{conn.from_name}" <{conn.from_email}>'
        else:
            from_email = conn.from_email
    if not from_email:
        from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@example.com')

    # ── Start log row in 'queued' state ──
    log = _create_log(
        scope=scope, tenant=tenant, subject=subject, body=body, recipients=recipients,
        from_email=from_email, reply_to=reply_to, has_html=bool(html),
        template_key=template_key, trigger_source=trigger_source,
        triggered_by_username=triggered_by_username, celery_task_id=celery_task_id,
        connection=conn, backend_type=backend_type,
        initial_status='queued',
    )

    # ── Send ──
    msg = EmailMultiAlternatives(
        subject=subject,
        body=body,
        from_email=from_email,
        to=list(recipients),
        connection=backend,
        reply_to=[reply_to] if reply_to else None,
    )
    if html:
        msg.attach_alternative(html, 'text/html')

    t0 = time.monotonic()
    try:
        msg.send(fail_silently=False)
        elapsed = int((time.monotonic() - t0) * 1000)
        logger.info("send_managed_email[%s]: sent '%s' to %s (%dms)",
                    scope, subject[:60], recipients, elapsed)
        _mark_log_sent(log, duration_ms=elapsed)
        return True
    except Exception as exc:
        elapsed = int((time.monotonic() - t0) * 1000)
        logger.exception("send_managed_email[%s]: failed: %s", scope, exc)
        _mark_log_failed(log, exc=exc, duration_ms=elapsed)
        return False


def test_connection(conn, recipient: str, triggered_by_username: str = '') -> tuple[bool, str]:
    """
    Send a test email through the given EmailConnection. Updates last_tested_at,
    last_test_result, AND writes a row to EmailLog. Returns (ok, message).
    """
    from .models import EmailLog
    backend = _smtp_backend_from_connection(conn)
    from_email = f'"{conn.from_name}" <{conn.from_email}>' if conn.from_name else conn.from_email

    log = _create_log(
        scope=conn.scope, tenant=conn.tenant,
        subject='ClipLens — email connection test',
        body='Test message',
        recipients=[recipient],
        from_email=from_email, reply_to=None, has_html=False,
        template_key='', trigger_source='manual_test',
        triggered_by_username=triggered_by_username, celery_task_id='',
        connection=conn, backend_type=EmailLog.BACKEND_SMTP,
        initial_status=EmailLog.STATUS_QUEUED,
    )

    msg = EmailMultiAlternatives(
        subject='ClipLens — email connection test',
        body=(
            'This is a test message from your ClipLens email connection '
            f'"{conn.name}". If you can read this, your SMTP credentials are '
            'configured correctly and outbound mail is working.'
        ),
        from_email=from_email,
        to=[recipient],
        connection=backend,
    )
    ok, msg_text = True, 'OK'
    t0 = time.monotonic()
    try:
        msg.send(fail_silently=False)
        _mark_log_sent(log, duration_ms=int((time.monotonic() - t0) * 1000))
    except Exception as exc:
        ok = False
        msg_text = f'{type(exc).__name__}: {exc}'[:380]
        _mark_log_failed(log, exc=exc, duration_ms=int((time.monotonic() - t0) * 1000))

    conn.last_tested_at   = timezone.now()
    conn.last_test_result = msg_text
    conn.save(using='control', update_fields=['last_tested_at', 'last_test_result'])
    return ok, msg_text
