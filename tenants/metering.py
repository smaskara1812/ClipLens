"""
Usage Metering
──────────────
Helpers for logging UsageEvents and enforcing plan quotas.

Usage in Celery tasks (Phase 3):
    from tenants.metering import log_ai_minutes, log_storage_delta, check_quota

Usage in upload views:
    from tenants.metering import check_quota, QuotaExceeded
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class QuotaExceeded(Exception):
    """Raised when a tenant has hit a plan limit."""
    def __init__(self, resource: str, used, limit):
        self.resource = resource
        self.used = used
        self.limit = limit
        super().__init__(f"{resource} quota exceeded: {used}/{limit}")


def _get_tenant_from_slug(slug: str):
    from .models import Tenant
    try:
        return Tenant.objects.using('control').select_related('plan').get(slug=slug)
    except Tenant.DoesNotExist:
        return None


# ── Logging ────────────────────────────────────────────────────────────────────

def log_ai_minutes(tenant_slug: str, minutes: float,
                   event_type: str = 'video_processing',
                   task_id: str = '') -> None:
    """Log AI processing minutes for a tenant."""
    from .models import UsageEvent, Tenant
    tenant = _get_tenant_from_slug(tenant_slug)
    if not tenant:
        logger.warning("metering: unknown tenant slug '%s'", tenant_slug)
        return
    UsageEvent.objects.using('control').create(
        tenant=tenant,
        event_type=event_type,
        value=minutes,
        task_id=task_id,
    )


def log_storage_delta(tenant_slug: str, bytes_delta: int) -> None:
    """Log storage change (positive = added, negative = freed) in bytes."""
    from .models import UsageEvent, Tenant
    tenant = _get_tenant_from_slug(tenant_slug)
    if not tenant:
        return
    UsageEvent.objects.using('control').create(
        tenant=tenant,
        event_type=UsageEvent.TYPE_STORAGE_DELTA,
        value=float(bytes_delta),
    )


# ── Quota checking ─────────────────────────────────────────────────────────────

def get_monthly_usage(tenant_slug: str) -> dict:
    """
    Return current-month usage totals for a tenant.
    Returns: {'ai_minutes': float, 'storage_gb': float}
    """
    from django.db.models import Sum
    from django.utils import timezone
    from .models import UsageEvent, Tenant

    tenant = _get_tenant_from_slug(tenant_slug)
    if not tenant:
        return {'ai_minutes': 0, 'storage_gb': 0}

    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    events = UsageEvent.objects.using('control').filter(
        tenant=tenant, timestamp__gte=month_start
    )

    ai_minutes = events.filter(
        event_type__in=[
            UsageEvent.TYPE_VIDEO_PROCESSING,
            UsageEvent.TYPE_PHOTO_PROCESSING,
            UsageEvent.TYPE_TRANSLATION,
        ]
    ).aggregate(total=Sum('value'))['total'] or 0

    storage_bytes = events.filter(
        event_type=UsageEvent.TYPE_STORAGE_DELTA
    ).aggregate(total=Sum('value'))['total'] or 0

    return {
        'ai_minutes': round(float(ai_minutes), 2),
        'storage_gb': round(float(storage_bytes) / 1024**3, 4),
        'tenant': tenant,
        'plan': tenant.plan,
    }


def check_quota(tenant_slug: str, resource: str = 'ai_minutes',
                additional: float = 0) -> dict:
    """
    Check if a tenant is within their plan quota.

    resource: 'ai_minutes' | 'storage_gb'
    additional: extra amount about to be consumed (for pre-flight checks)

    Returns usage dict.
    Raises QuotaExceeded if over limit.
    """
    usage = get_monthly_usage(tenant_slug)
    if not usage['tenant']:
        return usage  # unknown tenant — let it through

    plan = usage['plan']

    if resource == 'ai_minutes':
        used = usage['ai_minutes'] + additional
        limit = plan.ai_minutes_limit
        if limit > 0 and used >= limit:
            raise QuotaExceeded('ai_minutes', used, limit)

    elif resource == 'storage_gb':
        used = usage['storage_gb'] + additional
        limit = plan.storage_limit_gb
        if limit > 0 and used >= limit:
            raise QuotaExceeded('storage_gb', used, limit)

    return usage


def usage_warning_level(used: float, limit: float) -> Optional[str]:
    """
    Returns 'critical' (≥95%), 'warning' (≥80%), or None.
    Returns None if limit is 0 (unlimited).
    """
    if limit <= 0:
        return None
    pct = used / limit * 100
    if pct >= 95:
        return 'critical'
    if pct >= 80:
        return 'warning'
    return None
