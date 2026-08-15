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
import os
import time
from typing import Iterable, Optional

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.core.mail.backends.base import BaseEmailBackend
from django.utils import timezone

logger = logging.getLogger(__name__)

# ── Shared branding constants ──────────────────────────────────────────────────

LOGO_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'videos', 'static', 'videos', 'logos', 'logo-long.png',
)
LOGO_CID = 'logo@cliplens.in'

# ── HTML email templates ───────────────────────────────────────────────────────

def _base_email(*, preheader: str, hero_title: str, hero_sub: str, body_html: str) -> str:
    """Shared wrapper: logo header + purple hero + body + feature strip + footer."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>ClipLens</title></head>
<body style="margin:0;padding:0;background:#f2f2f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;">{preheader}</div>
<table width="100%" cellpadding="0" cellspacing="0" role="presentation">
  <tr><td align="center" style="padding:40px 16px 48px;">
    <table width="560" cellpadding="0" cellspacing="0" role="presentation" style="max-width:560px;width:100%;">

      <!-- Logo bar -->
      <tr><td style="background:#ffffff;border-radius:16px 16px 0 0;padding:28px 36px;border-bottom:1px solid #ebebed;">
        <img src="cid:{LOGO_CID}" alt="ClipLens" width="130" height="auto" style="display:block;border:0;outline:0;">
      </td></tr>

      <!-- Hero -->
      <tr><td style="background:linear-gradient(135deg,#1e1250 0%,#3b1fa8 60%,#5b21b6 100%);padding:36px 36px 32px;">
        <p style="margin:0 0 10px;font-size:26px;font-weight:700;color:#ffffff;letter-spacing:-0.6px;line-height:1.2;">{hero_title}</p>
        <p style="margin:0;font-size:15px;color:#c4b5fd;line-height:1.5;">{hero_sub}</p>
      </td></tr>

      <!-- Body -->
      <tr><td style="background:#ffffff;padding:36px 36px 32px;">
        {body_html}
      </td></tr>

      <!-- Footer -->
      <tr><td style="background:#f2f2f5;border-radius:0 0 16px 16px;padding:24px 36px;">
        <p style="margin:0 0 4px;font-size:12px;color:#9ca3af;">Questions? Reply to this email — we read every one.</p>
        <p style="margin:0;font-size:12px;color:#b0b0b8;">&copy; 2026 ClipLens &nbsp;&middot;&nbsp; <a href="https://cliplens.in" style="color:#9ca3af;text-decoration:none;">cliplens.in</a></p>
      </td></tr>

    </table>
  </td></tr>
</table>
</body>
</html>"""


def _cta_button(label: str, url: str) -> str:
    return (
        f'<table cellpadding="0" cellspacing="0" role="presentation" style="margin-bottom:28px;">'
        f'<tr><td style="background:#5b21b6;border-radius:10px;">'
        f'<a href="{url}" style="display:inline-block;padding:15px 34px;font-size:15px;'
        f'font-weight:600;color:#ffffff;text-decoration:none;letter-spacing:-0.1px;">'
        f'{label} &rarr;</a></td></tr></table>'
    )


def _fallback_url_box(url: str) -> str:
    return (
        f'<table width="100%" cellpadding="0" cellspacing="0" role="presentation" style="margin-bottom:24px;">'
        f'<tr><td style="background:#f8f7ff;border:1px solid #e0d9ff;border-radius:8px;padding:13px 15px;">'
        f'<p style="margin:0 0 4px;font-size:11px;font-weight:600;text-transform:uppercase;'
        f'letter-spacing:0.7px;color:#7c3aed;">Or copy this link</p>'
        f'<p style="margin:0;font-size:12px;color:#4b5563;word-break:break-all;line-height:1.6;">{url}</p>'
        f'</td></tr></table>'
    )


def onboarding_email_html(username: str, org_name: str, onboard_url: str, login_url: str) -> str:
    body = (
        f'<p style="margin:0 0 18px;font-size:15px;color:#374151;line-height:1.7;">'
        f'Hi <strong style="color:#111827;">{username}</strong>,</p>'
        f'<p style="margin:0 0 28px;font-size:15px;color:#4b5563;line-height:1.75;">'
        f'Your AI-powered media archive is set up and waiting. Click below to set your password '
        f'and choose a plan — you\'ll be searching your archive in minutes.</p>'
        + _cta_button('Activate workspace', onboard_url)
        + _fallback_url_box(onboard_url)
        + f'<table width="100%" cellpadding="0" cellspacing="0" role="presentation">'
        f'<tr><td style="border-top:1px solid #f0eff5;padding-top:20px;">'
        f'<p style="margin:0 0 4px;font-size:13px;color:#9ca3af;">This link expires in 7 days. After activating, log in at:</p>'
        f'<a href="{login_url}" style="font-size:13px;color:#5b21b6;text-decoration:none;font-weight:500;">{login_url}</a>'
        f'</td></tr></table>'
    )
    return _base_email(
        preheader=f'Your ClipLens workspace {org_name} is ready to activate.',
        hero_title='Your workspace is ready.',
        hero_sub=f'<strong style="color:#ede9fe;">{org_name}</strong> has been provisioned on ClipLens.',
        body_html=body,
    )


def team_invite_email_html(username: str, inviter: str, org_name: str, role: str, accept_url: str) -> str:
    body = (
        f'<p style="margin:0 0 18px;font-size:15px;color:#374151;line-height:1.7;">'
        f'Hi <strong style="color:#111827;">{username}</strong>,</p>'
        f'<p style="margin:0 0 28px;font-size:15px;color:#4b5563;line-height:1.75;">'
        f'<strong>{inviter}</strong> has invited you to join '
        f'<strong style="color:#111827;">{org_name}</strong> on ClipLens as a <strong>{role}</strong>. '
        f'Click below to set your password and get started.</p>'
        + _cta_button('Accept invitation', accept_url)
        + _fallback_url_box(accept_url)
        + f'<p style="margin:0;font-size:13px;color:#9ca3af;">This link expires in 7 days. '
        f'If you weren\'t expecting this, you can safely ignore it.</p>'
    )
    return _base_email(
        preheader=f'{inviter} invited you to join {org_name} on ClipLens.',
        hero_title="You've been invited.",
        hero_sub=f'Join <strong style="color:#ede9fe;">{org_name}</strong> as a {role}.',
        body_html=body,
    )


def notification_email_html(title: str, message: str, link_url: str = '', org_name: str = '') -> str:
    link_block = ''
    if link_url:
        link_block = _cta_button('View', link_url)
    body = (
        f'<p style="margin:0 0 20px;font-size:15px;color:#4b5563;line-height:1.75;">{message}</p>'
        + link_block
    )
    sub = f'From <strong style="color:#ede9fe;">{org_name}</strong>' if org_name else 'ClipLens system notification'
    return _base_email(
        preheader=title,
        hero_title=title,
        hero_sub=sub,
        body_html=body,
    )


def support_reply_email_html(ticket_id: int, subject: str, body_text: str) -> str:
    body = (
        f'<p style="margin:0 0 16px;font-size:15px;color:#374151;line-height:1.7;">'
        f'ClipLens support has replied to your ticket <strong>#{ticket_id}</strong>.</p>'
        f'<div style="background:#f8f7ff;border-left:3px solid #5b21b6;border-radius:0 8px 8px 0;'
        f'padding:16px 20px;margin-bottom:24px;">'
        f'<p style="margin:0;font-size:14px;color:#374151;line-height:1.7;white-space:pre-wrap;">{body_text}</p>'
        f'</div>'
        f'<p style="margin:0;font-size:13px;color:#9ca3af;">You can view the full thread in your admin panel under <strong>Support</strong>.</p>'
    )
    return _base_email(
        preheader=f'Reply to your ticket #{ticket_id}: {subject}',
        hero_title='Support reply',
        hero_sub=f'Ticket <strong style="color:#ede9fe;">#{ticket_id}</strong> — {subject}',
        body_html=body,
    )


def lead_notification_email_html(name: str, email: str, company: str, message: str) -> str:
    rows = [
        ('Name', name),
        ('Email', email),
        ('Company', company or '—'),
    ]
    rows_html = ''.join(
        f'<tr><td style="padding:8px 12px;font-size:13px;font-weight:600;color:#6b7280;'
        f'white-space:nowrap;width:80px;">{k}</td>'
        f'<td style="padding:8px 12px;font-size:13px;color:#111827;">{v}</td></tr>'
        for k, v in rows
    )
    body = (
        f'<p style="margin:0 0 20px;font-size:15px;color:#374151;line-height:1.7;">'
        f'A new beta access request was submitted via the landing page.</p>'
        f'<table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #e5e7eb;'
        f'border-radius:8px;overflow:hidden;margin-bottom:24px;">'
        f'<tbody>{rows_html}</tbody></table>'
        + (f'<p style="margin:0 0 8px;font-size:13px;font-weight:600;color:#6b7280;">Message</p>'
           f'<p style="margin:0;font-size:14px;color:#374151;line-height:1.7;'
           f'white-space:pre-wrap;">{message}</p>' if message else '')
    )
    return _base_email(
        preheader=f'New lead: {name}' + (f' from {company}' if company else ''),
        hero_title='New beta request',
        hero_sub=f'<strong style="color:#ede9fe;">{name}</strong>' + (f' · {company}' if company else ''),
        body_html=body,
    )


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
    inline_images: Optional[dict] = None,
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
    if html and inline_images:
        from email.mime.image import MIMEImage
        msg.mixed_subtype = 'related'
        for cid, file_path in (inline_images or {}).items():
            try:
                with open(file_path, 'rb') as f:
                    mime_img = MIMEImage(f.read())
                mime_img.add_header('Content-ID', f'<{cid}>')
                mime_img.add_header('Content-Disposition', 'inline')
                msg.attach(mime_img)
            except (OSError, IOError):
                logger.warning('send_managed_email: inline image %s not found at %s', cid, file_path)

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
