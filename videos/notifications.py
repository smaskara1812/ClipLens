"""
Notification dispatch service.

Single entry point — `dispatch(...)` — that:
  • respects per-user preferences (NotificationPreference)
  • writes the in-app row if wanted
  • queues an email if wanted (via tenants.tasks.queue_email)
  • is tenant-aware (caller passes db_alias or it's inferred from the user)
  • is idempotent for crossing-based notifications (quota warnings)

System callers (Celery tasks, metering) should ALWAYS use this — never write
to Notification directly.
"""

import logging

logger = logging.getLogger(__name__)


def _db_alias_for_user(user) -> str:
    """Best-guess DB alias for a User instance loaded from a tenant DB."""
    return getattr(user, '_state').db or 'default'


def dispatch(
    *,
    recipient,
    notification_type: str,
    title: str,
    message: str,
    link: str = '',
    video=None,
    photo=None,
    tenant_slug: str = '',
    dedupe_key: str = '',
    dedupe_window_hours: int = 24,
    db_alias: str = '',
    email_scope_override: str = '',  # 'platform' or 'tenant' — overrides auto-detection
) -> 'Notification | None':
    """
    Create a Notification row (if user opted in) and queue an email (if user
    opted in). Returns the Notification instance or None if everything was
    suppressed.

    `dedupe_key`: optional — when set, we look for an existing notification
    of the same (recipient, type) created in the last 24h with the same title.
    If found, we skip. Used for quota crossings ("don't spam at 81%, 82%, ...").
    """
    from .models import Notification, NotificationPreference

    db = db_alias or _db_alias_for_user(recipient)
    prefs = NotificationPreference.for_user(recipient, db_alias=db)

    want_inapp = prefs.wants_inapp(notification_type)
    want_email = prefs.wants_email(notification_type)
    if not (want_inapp or want_email):
        logger.debug('notify: user %s opted out of %s — skipping',
                     recipient.username, notification_type)
        return None

    # Dedupe — only fire if no matching row within the window
    if dedupe_key:
        from django.utils import timezone
        from datetime import timedelta
        cutoff = timezone.now() - timedelta(hours=dedupe_window_hours)
        existing = Notification.objects.using(db).filter(
            recipient=recipient,
            notification_type=notification_type,
            title__icontains=dedupe_key[:60],
            created_at__gte=cutoff,
        ).exists()
        if existing:
            logger.debug('notify: dedupe hit for %s/%s — skipping',
                         recipient.username, notification_type)
            return None

    notif = None
    if want_inapp:
        notif = Notification.objects.using(db).create(
            recipient=recipient,
            notification_type=notification_type,
            video=video,
            photo=photo,
            title=title[:160],
            message=message[:500],
            link=link[:255],
        )

    if want_email and getattr(recipient, 'email', ''):
        try:
            from tenants.tasks import queue_email
            from tenants.models import Tenant
            from tenants.email_utils import notification_email_html, LOGO_PATH, LOGO_CID
            tenant = None
            if tenant_slug:
                try:
                    tenant = Tenant.objects.using('control').get(slug=tenant_slug, is_active=True)
                except Exception:
                    tenant = None
            body = (
                f'Hi {recipient.username},\n\n'
                f'{message}\n\n'
            )
            full_link = ''
            if link:
                if tenant and not link.startswith('http'):
                    full_link = f'https://{tenant.slug}.cliplens.in{link}'
                else:
                    full_link = link
                body += f'View: {full_link}\n\n'
            body += '— ClipLens'
            queue_email(
                scope=email_scope_override or ('tenant' if tenant else 'platform'),
                subject=f'[ClipLens] {title}',
                body=body,
                html=notification_email_html(
                    title=title,
                    message=f'Hi <strong>{recipient.username}</strong>,<br><br>{message}',
                    link_url=full_link,
                    org_name=tenant.name if tenant else '',
                ),
                inline_images={LOGO_CID: LOGO_PATH},
                recipients=[recipient.email],
                tenant=tenant,
                trigger_source=f'notify:{notification_type}',
                triggered_by_username=recipient.username,
                template_key=notification_type,
            )
            if notif:
                notif.email_sent = True
                notif.save(update_fields=['email_sent'])
        except Exception:
            logger.exception('notify: email dispatch failed for %s', recipient.username)

    return notif


# ─────────────────────────────────────────────────────────────────────────────
# Convenience helpers — typed wrappers for the common pipelines
# ─────────────────────────────────────────────────────────────────────────────

def notify_video_processed(video, *, tenant_slug='', db_alias=''):
    """Fired by process_video_task / analyze_video_frames_task when a video is READY."""
    if not video.uploaded_by:
        return
    from django.contrib.auth.models import User
    db = db_alias or video._state.db or 'default'
    try:
        user = User.objects.using(db).get(username=video.uploaded_by)
    except User.DoesNotExist:
        return
    dispatch(
        recipient=user,
        notification_type='upload_complete',
        title=f'Your video "{video.title[:80]}" is ready',
        message=(
            f'"{video.title}" has finished processing — captions, faces, '
            f'and AI analysis are now searchable.'
        ),
        link=f'/watch/{video.id}/',
        video=video,
        tenant_slug=tenant_slug,
        db_alias=db,
    )


def notify_video_failed(video, error_message: str = '', *, tenant_slug='', db_alias=''):
    """Fired by process_video_task on terminal failure."""
    if not video.uploaded_by:
        return
    from django.contrib.auth.models import User
    db = db_alias or video._state.db or 'default'
    try:
        user = User.objects.using(db).get(username=video.uploaded_by)
    except User.DoesNotExist:
        return
    dispatch(
        recipient=user,
        notification_type='upload_failed',
        title=f'Video "{video.title[:60]}" failed to process',
        message=(
            f'"{video.title}" did not finish processing. '
            f'{("Error: " + error_message[:200]) if error_message else ""}'
        ),
        link=f'/watch/{video.id}/',
        video=video,
        tenant_slug=tenant_slug,
        db_alias=db,
    )


def notify_photo_processed(photo, *, tenant_slug='', db_alias=''):
    """Fired by analyze_photo_task end."""
    if not photo.uploaded_by:
        return
    from django.contrib.auth.models import User
    db = db_alias or photo._state.db or 'default'
    try:
        user = User.objects.using(db).get(username=photo.uploaded_by)
    except User.DoesNotExist:
        return
    dispatch(
        recipient=user,
        notification_type='photo_complete',
        title=f'Photo "{photo.title[:80]}" is ready',
        message=f'"{photo.title}" has finished analysis.',
        link=f'/photos/{photo.id}/',
        photo=photo,
        tenant_slug=tenant_slug,
        db_alias=db,
    )


def notify_photo_failed(photo, error_message: str = '', *, tenant_slug='', db_alias=''):
    """Fired by analyze_photo_task on terminal failure."""
    if not photo.uploaded_by:
        return
    from django.contrib.auth.models import User
    db = db_alias or photo._state.db or 'default'
    try:
        user = User.objects.using(db).get(username=photo.uploaded_by)
    except User.DoesNotExist:
        return
    dispatch(
        recipient=user,
        notification_type='photo_failed',
        title=f'Photo "{photo.title[:60]}" failed to process',
        message=(
            f'"{photo.title}" did not finish analysis. '
            f'{("Error: " + error_message[:200]) if error_message else ""}'
        ),
        link=f'/photos/{photo.id}/',
        photo=photo,
        tenant_slug=tenant_slug,
        db_alias=db,
    )


def notify_quota_warning(tenant_slug: str, resource: str, pct: float,
                         used: float, limit: float, unit: str = ''):
    """
    Quota threshold crossing — fired from tenants.metering.
    Notifies every active org admin (superadmin role) in the tenant.

    `resource`  — display name e.g. "AI processing minutes" or "Storage"
    `pct`       — current usage as a percentage (0-100)
    `used/limit/unit` — numbers + unit for the message
    """
    from django.contrib.auth.models import User
    from tenants.models import Tenant
    from .models import UserProfile

    try:
        tenant = Tenant.objects.using('control').get(slug=tenant_slug, is_active=True)
    except Tenant.DoesNotExist:
        return

    db = tenant.db_name

    if pct >= 100:
        threshold_type = 'quota_100'
        severity = 'critical'
    elif pct >= 95:
        threshold_type = 'quota_95'
        severity = 'critical'
    elif pct >= 90:
        threshold_type = 'quota_90'
        severity = 'warning'
    else:
        threshold_type = 'quota_80'
        severity = 'warning'

    # Find org admins
    admin_user_ids = list(
        UserProfile.objects.using(db)
        .filter(role='superadmin')
        .values_list('user_id', flat=True)
    )
    admins = list(User.objects.using(db).filter(id__in=admin_user_ids, is_active=True))
    if not admins:
        return

    title = f'{resource} {severity}: {pct:.0f}% used'
    message = (
        f'Your org has used {used:.1f} {unit} of {limit:.1f} {unit} '
        f'({pct:.0f}% of plan limit) of {resource.lower()}. '
        f'{"Uploads are now blocked — top up or upgrade to continue." if pct >= 100 else "Upload will be blocked at 100%." if severity == "critical" else "Consider topping up or upgrading."}'
    )
    # Dedupe key: same threshold + resource — window is 30 days (once per billing month)
    dedupe = f'{threshold_type}:{resource}'
    for u in admins:
        dispatch(
            recipient=u,
            notification_type=threshold_type,
            title=title,
            message=message,
            link='/admin-panel/',
            tenant_slug=tenant_slug,
            db_alias=db,
            email_scope_override='platform',  # quota/billing emails use platform SMTP
            dedupe_key=dedupe,
            dedupe_window_hours=24 * 30,  # once per billing month per threshold
        )
