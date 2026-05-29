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
    except Exception:
        return None


# ── Addon helpers ──────────────────────────────────────────────────────────────

def get_storage_addon_gb(tenant) -> int:
    """
    Sum of GB granted by all currently-active StorageAddons for this tenant.
    Active means: not yet expired (paid period still running).
    A cancelled-but-not-expired addon is still active — the user paid for the
    month, they keep the storage until the period ends.
    """
    from django.db.models import Q, Sum
    from django.utils import timezone
    from .models import StorageAddon
    now = timezone.now()
    return StorageAddon.objects.using('control').filter(
        Q(tenant=tenant) & (
            # Still actively renewing (no end date set)
            (Q(cancelled_at__isnull=True) & Q(expires_at__isnull=True)) |
            # Cancelled but paid period not yet over
            Q(expires_at__gt=now)
        )
    ).aggregate(total=Sum('gb_amount'))['total'] or 0


def get_credit_minutes_available(tenant) -> float:
    """Sum of unconsumed minutes across all non-expired credit packs."""
    from django.utils import timezone
    from .models import AICreditPack
    total = 0.0
    packs = AICreditPack.objects.using('control').filter(
        tenant=tenant, expires_at__gt=timezone.now()
    )
    for p in packs:
        total += max(0.0, p.minutes_purchased - p.minutes_consumed)
    return total


def drain_credit_packs(tenant, minutes_to_drain: float) -> float:
    """
    Consume `minutes_to_drain` from active credit packs, oldest first (FIFO).
    Updates `minutes_consumed` in-place.
    Returns the actual amount drained (may be less than requested if no credits).
    """
    from django.utils import timezone
    from .models import AICreditPack
    if minutes_to_drain <= 0:
        return 0.0
    remaining = minutes_to_drain
    drained = 0.0
    packs = AICreditPack.objects.using('control').filter(
        tenant=tenant, expires_at__gt=timezone.now()
    ).order_by('purchased_at')
    for p in packs:
        avail = max(0.0, p.minutes_purchased - p.minutes_consumed)
        if avail <= 0:
            continue
        take = min(avail, remaining)
        p.minutes_consumed = (p.minutes_consumed or 0) + take
        p.save(using='control', update_fields=['minutes_consumed'])
        drained += take
        remaining -= take
        if remaining <= 0:
            break
    return drained


# ── Logging ────────────────────────────────────────────────────────────────────

def log_ai_minutes(tenant_slug: str, minutes: float,
                   event_type: str = 'video_processing',
                   task_id: str = '') -> None:
    """
    Log AI processing minutes for a tenant.
    If the tenant's monthly usage now exceeds their plan limit, the overage
    portion is drained from their AI credit packs (FIFO, oldest first).
    """
    from django.db.models import Sum
    from django.utils import timezone
    from .models import UsageEvent

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

    # Drain credit packs to cover overage beyond the plan limit
    if not tenant.plan:
        return
    plan_limit = tenant.plan.ai_minutes_limit
    if plan_limit <= 0:
        return  # unlimited plan

    now         = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    monthly_used = UsageEvent.objects.using('control').filter(
        tenant=tenant, timestamp__gte=month_start
    ).exclude(event_type=UsageEvent.TYPE_STORAGE_DELTA).aggregate(
        total=Sum('value')
    )['total'] or 0

    overage = monthly_used - plan_limit
    if overage <= 0:
        return

    # How much of the overage is already covered by previously-drained credits?
    # Walk packs and sum consumed; any uncovered overage is the new amount to drain.
    from .models import AICreditPack
    consumed_so_far = AICreditPack.objects.using('control').filter(
        tenant=tenant
    ).aggregate(total=Sum('minutes_consumed'))['total'] or 0
    new_drain = overage - consumed_so_far
    if new_drain > 0:
        drained = drain_credit_packs(tenant, new_drain)
        if drained < new_drain:
            logger.warning(
                "metering: tenant '%s' is over plan limit and out of credits "
                "(overage=%.2f, drained=%.2f)", tenant_slug, new_drain, drained
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
    Return current-month usage totals + effective limits (plan + addons) for a tenant.
    Returns:
        {
          'ai_minutes':            float,   # used this month
          'storage_gb':            float,   # storage_delta sum (legacy, prefer disk scan)
          'ai_minutes_limit_plan': int,
          'ai_minutes_credits':    float,   # unconsumed minutes in active packs
          'ai_minutes_effective':  float,   # plan + credits
          'storage_addon_gb':      int,
          'storage_limit_plan':    int,
          'storage_limit_effective': int,
          'tenant', 'plan'
        }
    """
    from django.db.models import Sum
    from django.utils import timezone
    from .models import UsageEvent

    tenant = _get_tenant_from_slug(tenant_slug)
    if not tenant:
        return {'ai_minutes': 0, 'storage_gb': 0}

    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    events = UsageEvent.objects.using('control').filter(
        tenant=tenant, timestamp__gte=month_start
    )

    ai_minutes = events.exclude(
        event_type=UsageEvent.TYPE_STORAGE_DELTA
    ).aggregate(total=Sum('value'))['total'] or 0

    storage_bytes = events.filter(
        event_type=UsageEvent.TYPE_STORAGE_DELTA
    ).aggregate(total=Sum('value'))['total'] or 0

    # Addons
    plan = tenant.plan
    plan_ai_limit      = plan.ai_minutes_limit  if plan else 0
    plan_storage_limit = plan.storage_limit_gb  if plan else 0
    credit_minutes     = get_credit_minutes_available(tenant)
    addon_storage_gb   = get_storage_addon_gb(tenant)

    return {
        'ai_minutes':              round(float(ai_minutes), 2),
        'storage_gb':              round(float(storage_bytes) / 1024**3, 4),
        'ai_minutes_limit_plan':   plan_ai_limit,
        'ai_minutes_credits':      round(credit_minutes, 2),
        'ai_minutes_effective':    plan_ai_limit + credit_minutes,
        'storage_addon_gb':        addon_storage_gb,
        'storage_limit_plan':      plan_storage_limit,
        'storage_limit_effective': plan_storage_limit + addon_storage_gb,
        'tenant':                  tenant,
        'plan':                    plan,
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

    if resource == 'ai_minutes':
        used  = usage['ai_minutes'] + additional
        limit = usage['ai_minutes_effective']   # plan + credit packs
        if limit > 0 and used >= limit:
            raise QuotaExceeded('ai_minutes', used, limit)

    elif resource == 'storage_gb':
        used  = usage['storage_gb'] + additional
        limit = usage['storage_limit_effective']   # plan + storage addons
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
