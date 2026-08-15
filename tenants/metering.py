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

def _check_and_fire_quota_warnings(tenant_slug: str) -> None:
    """
    Compute current usage vs effective plan limits (plan + addons + credits).
    If a threshold (80% or 95%) was crossed by the most recent event, dispatch
    a notification to org admins. Dedupe happens at the notification layer.

    Designed to be called AFTER a UsageEvent is written — cheap, ~2 queries.
    Failures are logged but never raised — metering must not break.
    """
    try:
        usage = get_monthly_usage(tenant_slug)
    except Exception:
        logger.exception('quota check: get_monthly_usage failed for %s', tenant_slug)
        return

    plan = usage.get('plan')
    if not plan:
        return

    # AI minutes — use total capacity (plan + purchased credits) for accurate thresholds
    ai_used  = usage.get('ai_minutes', 0) or 0
    ai_limit = usage.get('ai_minutes_capacity') or (plan.ai_minutes_limit or 0)
    if ai_limit > 0:
        pct = (ai_used / ai_limit) * 100
        _maybe_notify_quota(tenant_slug, 'AI processing minutes',
                            pct, ai_used, ai_limit, unit='min')

    # Storage
    st_used  = usage.get('storage_gb', 0) or 0
    st_limit = (plan.storage_limit_gb or 0) + (usage.get('addon_storage_gb', 0) or 0)
    if st_limit > 0:
        pct = (st_used / st_limit) * 100
        _maybe_notify_quota(tenant_slug, 'Storage',
                            pct, st_used, st_limit, unit='GB')


def _maybe_notify_quota(tenant_slug: str, resource: str,
                        pct: float, used: float, limit: float, unit: str) -> None:
    """Fire at 80%, 90%, 95%, 100% thresholds. Dedupe (30-day window) is in dispatch."""
    if pct < 80:
        return
    try:
        # Import inside the function — videos.notifications lives in the tenant
        # app; calling out from metering needs the tenant DB context to be live,
        # which it is when this is called from log_ai_minutes / log_storage_delta
        # under the Celery task_postrun signal.
        from videos.notifications import notify_quota_warning
        notify_quota_warning(
            tenant_slug=tenant_slug,
            resource=resource,
            pct=pct,
            used=used,
            limit=limit,
            unit=unit,
        )
    except Exception:
        logger.exception('quota notify failed for %s / %s', tenant_slug, resource)


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

    # Check 80% / 95% thresholds and notify org admins if crossed
    _check_and_fire_quota_warnings(tenant_slug)


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
    # Only fire on additions (no point alerting when storage is freed)
    if bytes_delta > 0:
        _check_and_fire_quota_warnings(tenant_slug)


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
    credit_minutes_remaining = get_credit_minutes_available(tenant)  # unconsumed (for blocking)
    addon_storage_gb   = get_storage_addon_gb(tenant)

    # Capacity for display: max(actual used, plan baseline) + remaining active credits.
    # Using max(ai_minutes, plan_limit) ensures expiring a partially-consumed pack
    # never makes the usage percentage spike — consumed minutes stay in the denominator.
    ai_minutes_f = float(ai_minutes)
    ai_minutes_capacity = max(ai_minutes_f, float(plan_ai_limit)) + credit_minutes_remaining

    return {
        'ai_minutes':              round(ai_minutes_f, 2),
        'storage_gb':              round(float(storage_bytes) / 1024**3, 4),
        'ai_minutes_limit_plan':   plan_ai_limit,
        'ai_minutes_credits':      round(credit_minutes_remaining, 2),  # remaining in active packs
        'ai_minutes_effective':    plan_ai_limit + credit_minutes_remaining,  # for blocking/check_quota
        'ai_minutes_capacity':     round(ai_minutes_capacity, 1),            # for display/remaining
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
        limit = usage['ai_minutes_capacity']    # plan + total purchased credits
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
