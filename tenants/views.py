"""
Control Plane Views — accessible at admin.cliplens.local (or /platform/ in single-tenant mode)
Gated by is_platform_owner flag on the user's profile.
"""

import json
import logging
from functools import wraps

from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required
from django.contrib import messages

from .models import Plan, Tenant, UsageEvent
from .provisioning import provision_tenant

logger = logging.getLogger(__name__)


# ── Auth guard ─────────────────────────────────────────────────────────────────

def platform_owner_required(view_func):
    """Decorator: only the platform owner (Soham) can access control plane views."""
    @wraps(view_func)
    @login_required
    def _wrapped(request, *args, **kwargs):
        profile = getattr(request.user, 'profile', None)
        if not profile or not profile.is_platform_owner:
            return render(request, 'tenants/403.html', status=403)
        return view_func(request, *args, **kwargs)
    return _wrapped


# ── Dashboard ──────────────────────────────────────────────────────────────────

@platform_owner_required
def dashboard(request):
    """Main control plane: list of all tenants with usage summaries."""
    from django.db.models import Sum, Count
    from django.utils import timezone
    from datetime import timedelta

    tenants = Tenant.objects.using('control').select_related('plan').order_by('name')

    # Build per-tenant usage summary for the current calendar month
    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    tenant_data = []
    for t in tenants:
        events = UsageEvent.objects.using('control').filter(
            tenant=t, timestamp__gte=month_start
        )
        ai_minutes = events.filter(
            event_type__in=[UsageEvent.TYPE_VIDEO_PROCESSING,
                            UsageEvent.TYPE_PHOTO_PROCESSING,
                            UsageEvent.TYPE_TRANSLATION]
        ).aggregate(total=Sum('value'))['total'] or 0

        storage_bytes = events.filter(
            event_type=UsageEvent.TYPE_STORAGE_DELTA
        ).aggregate(total=Sum('value'))['total'] or 0

        tenant_data.append({
            'tenant': t,
            'ai_minutes_used': round(ai_minutes, 1),
            'ai_minutes_pct': min(100, round(ai_minutes / max(t.plan.ai_minutes_limit, 1) * 100)),
            'storage_gb_used': round(storage_bytes / 1024**3, 2),
            'storage_pct': min(100, round(
                (storage_bytes / 1024**3) / max(t.plan.storage_limit_gb, 1) * 100
            )),
        })

    plans = Plan.objects.using('control').all()
    return render(request, 'tenants/dashboard.html', {
        'tenant_data': tenant_data,
        'plans': plans,
    })


# ── Tenant detail ──────────────────────────────────────────────────────────────

@platform_owner_required
def tenant_detail(request, tenant_id):
    tenant = get_object_or_404(
        Tenant.objects.using('control').select_related('plan'),
        pk=tenant_id
    )

    from django.db.models import Sum
    from django.utils import timezone

    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    recent_events = (
        UsageEvent.objects.using('control')
        .filter(tenant=tenant)
        .order_by('-timestamp')[:50]
    )

    events_this_month = UsageEvent.objects.using('control').filter(
        tenant=tenant, timestamp__gte=month_start
    )

    usage = {
        'ai_minutes': round(events_this_month.filter(
            event_type__in=[UsageEvent.TYPE_VIDEO_PROCESSING,
                            UsageEvent.TYPE_PHOTO_PROCESSING,
                            UsageEvent.TYPE_TRANSLATION]
        ).aggregate(total=Sum('value'))['total'] or 0, 1),
        'storage_gb': round((events_this_month.filter(
            event_type=UsageEvent.TYPE_STORAGE_DELTA
        ).aggregate(total=Sum('value'))['total'] or 0) / 1024**3, 2),
    }

    plans = Plan.objects.using('control').all()

    return render(request, 'tenants/tenant_detail.html', {
        'tenant': tenant,
        'recent_events': recent_events,
        'usage': usage,
        'plans': plans,
    })


# ── Create tenant ──────────────────────────────────────────────────────────────

@platform_owner_required
def create_tenant(request):
    if request.method == 'GET':
        plans = Plan.objects.using('control').all()
        return render(request, 'tenants/create_tenant.html', {'plans': plans})

    # POST — provision the new org
    plan_id = request.POST.get('plan_id')
    slug = request.POST.get('slug', '').strip().lower()
    name = request.POST.get('name', '').strip()
    admin_email = request.POST.get('admin_email', '').strip()
    admin_username = request.POST.get('admin_username', '').strip()
    admin_password = request.POST.get('admin_password', '').strip()

    if not all([plan_id, slug, name, admin_email, admin_username, admin_password]):
        messages.error(request, "All fields are required.")
        return redirect('tenants:create_tenant')

    result = provision_tenant(
        slug=slug,
        name=name,
        plan_id=int(plan_id),
        admin_email=admin_email,
        admin_username=admin_username,
        admin_password=admin_password,
    )

    if result['success']:
        messages.success(request, f"Organisation '{name}' provisioned successfully!")
        return redirect('tenants:tenant_detail', tenant_id=result['tenant_id'])
    else:
        messages.error(request, f"Provisioning failed: {result['error']}")
        return redirect('tenants:create_tenant')


# ── Change plan ────────────────────────────────────────────────────────────────

@platform_owner_required
@require_POST
def change_plan(request, tenant_id):
    tenant = get_object_or_404(
        Tenant.objects.using('control'), pk=tenant_id
    )
    plan_id = request.POST.get('plan_id')
    try:
        plan = Plan.objects.using('control').get(pk=plan_id)
        tenant.plan = plan
        tenant.save(using='control')
        messages.success(request, f"Plan changed to '{plan.name}'.")
    except Plan.DoesNotExist:
        messages.error(request, "Plan not found.")
    return redirect('tenants:tenant_detail', tenant_id=tenant_id)


# ── Toggle active ──────────────────────────────────────────────────────────────

@platform_owner_required
@require_POST
def toggle_tenant(request, tenant_id):
    tenant = get_object_or_404(
        Tenant.objects.using('control'), pk=tenant_id
    )
    tenant.is_active = not tenant.is_active
    tenant.save(using='control')
    status = "activated" if tenant.is_active else "deactivated"
    messages.success(request, f"Tenant '{tenant.name}' {status}.")
    return redirect('tenants:tenant_detail', tenant_id=tenant_id)


# ── Plans management ───────────────────────────────────────────────────────────

@platform_owner_required
def manage_plans(request):
    if request.method == 'POST':
        data = {
            'name': request.POST.get('name', '').strip(),
            'storage_limit_gb': int(request.POST.get('storage_limit_gb', 100)),
            'ai_minutes_limit': int(request.POST.get('ai_minutes_limit', 300)),
            'max_users': int(request.POST.get('max_users', 3)),
            'max_videos': int(request.POST.get('max_videos', 0)),
        }
        Plan.objects.using('control').create(**data)
        messages.success(request, f"Plan '{data['name']}' created.")
        return redirect('tenants:manage_plans')

    from django.db.models import Count
    plans = Plan.objects.using('control').annotate(tenant_count=Count('tenants'))
    return render(request, 'tenants/manage_plans.html', {'plans': plans})


# ── API: Usage data (JSON) ─────────────────────────────────────────────────────

@platform_owner_required
def api_usage(request, tenant_id):
    """Return last 30 days of daily usage as JSON for charts."""
    from django.db.models import Sum
    from django.db.models.functions import TruncDate
    from django.utils import timezone
    from datetime import timedelta

    tenant = get_object_or_404(Tenant.objects.using('control'), pk=tenant_id)
    since = timezone.now() - timedelta(days=30)

    daily = (
        UsageEvent.objects.using('control')
        .filter(tenant=tenant, timestamp__gte=since)
        .annotate(day=TruncDate('timestamp'))
        .values('day', 'event_type')
        .annotate(total=Sum('value'))
        .order_by('day')
    )

    return JsonResponse({'usage': list(daily), 'tenant': tenant.name})
