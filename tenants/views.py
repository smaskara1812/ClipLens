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
    """
    Decorator: only the platform owner can access control plane views.

    Two-layer check:
      1. request.tenant must be None — control plane is only reachable via the
         admin subdomain (where TenantMiddleware always sets tenant=None).
         This blocks any org admin who flips is_platform_owner=True in their
         own tenant DB and tries to hit /platform/ via their subdomain.
      2. The authenticated user must have is_platform_owner=True on their
         UserProfile in the default (control-plane) DB.
    """
    @wraps(view_func)
    @login_required
    def _wrapped(request, *args, **kwargs):
        # Gate 1: must be on the admin subdomain, not a tenant subdomain
        if getattr(request, 'tenant', None) is not None:
            return render(request, 'tenants/403.html', status=403)
        # Gate 2: user must be the platform owner in the default DB
        profile = getattr(request.user, 'profile', None)
        if not profile or not profile.is_platform_owner:
            return render(request, 'tenants/403.html', status=403)
        return view_func(request, *args, **kwargs)
    return _wrapped


# ── Dashboard ──────────────────────────────────────────────────────────────────

def _tenant_content_counts(db_alias):
    """
    Query a tenant's DB for user / video / photo counts.
    Auto-registers the DB alias if it isn't in settings.DATABASES yet
    (happens when an org was provisioned after the server started).
    Returns a dict with safe defaults on any error (DB down, not migrated, etc.).
    """
    counts = {'user_count': 0, 'video_count': 0, 'photo_count': 0}
    try:
        from django.conf import settings
        from .provisioning import _register_db_alias
        if db_alias not in settings.DATABASES:
            _register_db_alias(db_alias)

        from django.contrib.auth.models import User
        from videos.models import Video, Photo
        counts['user_count'] = User.objects.using(db_alias).count()
        counts['video_count'] = Video.objects.using(db_alias).filter(
            deleted_at__isnull=True
        ).count()
        counts['photo_count'] = Photo.objects.using(db_alias).filter(
            deleted_at__isnull=True
        ).count()
    except Exception as exc:
        logger.warning("Could not query counts for DB '%s': %s", db_alias, exc)
    return counts


@platform_owner_required
def dashboard(request):
    """Main control plane: list of all tenants with usage summaries."""
    from django.db.models import Sum
    from django.utils import timezone

    tenants = Tenant.objects.using('control').select_related('plan').order_by('name')

    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    tenant_data = []
    total_users = total_videos = total_photos = 0

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

        counts = _tenant_content_counts(t.db_name)
        total_users  += counts['user_count']
        total_videos += counts['video_count']
        total_photos += counts['photo_count']

        tenant_data.append({
            'tenant':          t,
            'ai_minutes_used': round(ai_minutes, 1),
            'ai_minutes_pct':  min(100, round(ai_minutes / max(t.plan.ai_minutes_limit, 1) * 100)),
            'storage_gb_used': round(storage_bytes / 1024**3, 2),
            'storage_pct':     min(100, round(
                (storage_bytes / 1024**3) / max(t.plan.storage_limit_gb, 1) * 100
            )),
            **counts,
        })

    active_count = sum(1 for td in tenant_data if td['tenant'].is_active)
    plans = Plan.objects.using('control').all()

    return render(request, 'tenants/dashboard.html', {
        'tenant_data':   tenant_data,
        'plans':         plans,
        'active_count':  active_count,
        'total_users':   total_users,
        'total_videos':  total_videos,
        'total_photos':  total_photos,
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

    # ── Cross-DB: users, videos, photos in this tenant's DB ───────────────────
    users = []
    counts = _tenant_content_counts(tenant.db_name)
    try:
        from django.contrib.auth.models import User
        from videos.models import UserProfile
        raw_users = (
            User.objects.using(tenant.db_name)
            .order_by('username')
            .values('id', 'username', 'email', 'date_joined', 'is_active')
        )
        # Fetch profiles in one query keyed by user_id
        profiles = {
            p['user_id']: p
            for p in UserProfile.objects.using(tenant.db_name).values('user_id', 'role', 'is_platform_owner')
        }
        for u in raw_users:
            profile = profiles.get(u['id'], {})
            users.append({
                'username':    u['username'],
                'email':       u['email'],
                'date_joined': u['date_joined'],
                'is_active':   u['is_active'],
                'role':        profile.get('role', '—'),
            })
    except Exception as exc:
        logger.warning("Could not fetch users for tenant '%s': %s", tenant.slug, exc)

    # Build the org URL for the "Open Org" button
    scheme = 'https' if request.is_secure() else 'http'
    host = request.get_host()
    parts = host.split('.')
    if len(parts) >= 3:
        parts[0] = tenant.slug
        org_host = '.'.join(parts)
    else:
        org_host = f"{tenant.slug}.cliplens.local"
    org_url = f"{scheme}://{org_host}/"

    plans = Plan.objects.using('control').all()

    return render(request, 'tenants/tenant_detail.html', {
        'tenant':        tenant,
        'recent_events': recent_events,
        'usage':         usage,
        'plans':         plans,
        'users':         users,
        'org_url':       org_url,
        **counts,
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
