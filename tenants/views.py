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

from .models import Plan, Tenant, UsageEvent, OnboardingInvite, LeadRequest
from .provisioning import provision_tenant, provision_tenant_with_invite, claim_onboarding_invite

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

def _disk_usage_bytes(media_folder: str) -> int:
    """
    Walk the tenant's media directory and return actual bytes on disk.
    This is more accurate than summing storage_delta events because it
    includes HLS segments, thumbnails, face crops, captions, etc. that
    Celery generates after the original upload.
    """
    from pathlib import Path
    from django.conf import settings
    path = Path(settings.MEDIA_ROOT) / media_folder
    if not path.exists():
        return 0
    try:
        return sum(f.stat().st_size for f in path.rglob('*') if f.is_file())
    except Exception:
        return 0


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

    pending_slugs = set(
        OnboardingInvite.objects.using('control')
        .filter(consumed_at__isnull=True)
        .values_list('tenant__slug', flat=True)
    )

    for t in tenants:
        events = UsageEvent.objects.using('control').filter(
            tenant=t, timestamp__gte=month_start
        )
        ai_minutes = events.exclude(
            event_type=UsageEvent.TYPE_STORAGE_DELTA
        ).aggregate(total=Sum('value'))['total'] or 0

        storage_bytes = _disk_usage_bytes(t.media_folder)

        counts = _tenant_content_counts(t.db_name)
        total_users  += counts['user_count']
        total_videos += counts['video_count']
        total_photos += counts['photo_count']

        # Effective limits = plan + addons (credits & storage subscriptions)
        from .metering import get_credit_minutes_available, get_storage_addon_gb
        plan_ai      = t.plan.ai_minutes_limit if t.plan else 0
        plan_storage = t.plan.storage_limit_gb if t.plan else 0
        credit_min   = get_credit_minutes_available(t)
        addon_gb     = get_storage_addon_gb(t)
        ai_limit      = plan_ai      + credit_min
        storage_limit = plan_storage + addon_gb

        tenant_data.append({
            'tenant':              t,
            'has_pending_invite':  t.slug in pending_slugs,
            'ai_minutes_used':     round(ai_minutes, 1),
            'ai_minutes_limit':    round(ai_limit, 1),
            'ai_minutes_pct':      min(100, round(ai_minutes / max(ai_limit, 1) * 100)) if ai_limit else 0,
            'ai_credit_minutes':   round(credit_min, 1),
            'storage_gb_used':     round(storage_bytes / 1024**3, 2),
            'storage_gb_limit':    storage_limit,
            'storage_pct':         min(100, round(
                (storage_bytes / 1024**3) / max(storage_limit, 1) * 100
            )) if storage_limit else 0,
            'storage_addon_gb':    addon_gb,
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

    from .metering import get_credit_minutes_available, get_storage_addon_gb
    from .models import StorageAddon, AICreditPack
    credits_minutes = get_credit_minutes_available(tenant)
    addon_storage   = get_storage_addon_gb(tenant)
    from django.db.models import Q as _Q
    active_storage_addons = list(StorageAddon.objects.using('control').filter(
        _Q(tenant=tenant) & (
            (_Q(cancelled_at__isnull=True) & _Q(expires_at__isnull=True)) |
            _Q(expires_at__gt=now)
        )
    ).order_by('-started_at'))
    active_credit_packs = list(AICreditPack.objects.using('control').filter(
        tenant=tenant, expires_at__gt=now
    ).order_by('purchased_at'))

    usage = {
        'ai_minutes': round(events_this_month.exclude(
            event_type=UsageEvent.TYPE_STORAGE_DELTA
        ).aggregate(total=Sum('value'))['total'] or 0, 1),
        'storage_gb':              round(_disk_usage_bytes(tenant.media_folder) / 1024**3, 2),
        'credit_minutes':          round(credits_minutes, 1),
        'addon_storage_gb':        addon_storage,
        'ai_minutes_effective':    (tenant.plan.ai_minutes_limit if tenant.plan else 0) + credits_minutes,
        'storage_limit_effective': (tenant.plan.storage_limit_gb if tenant.plan else 0) + addon_storage,
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

    # Pending invite (if onboarding not yet completed)
    invite = OnboardingInvite.objects.using('control').filter(
        tenant=tenant, consumed_at__isnull=True
    ).first()
    onboard_url = f"{scheme}://{org_host}/onboard/{invite.token}/" if invite else None

    return render(request, 'tenants/tenant_detail.html', {
        'tenant':                tenant,
        'recent_events':         recent_events,
        'usage':                 usage,
        'plans':                 plans,
        'users':                 users,
        'org_url':               org_url,
        'invite':                invite,
        'onboard_url':           onboard_url,
        'active_storage_addons': active_storage_addons,
        'active_credit_packs':   active_credit_packs,
        **counts,
    })


# ── Create tenant ──────────────────────────────────────────────────────────────

@platform_owner_required
def create_tenant(request):
    """Provision a new org in INVITE mode — admin sets password + plan later."""
    if request.method == 'GET':
        return render(request, 'tenants/create_tenant.html')

    slug           = request.POST.get('slug', '').strip().lower()
    name           = request.POST.get('name', '').strip()
    admin_email    = request.POST.get('admin_email', '').strip()
    admin_username = request.POST.get('admin_username', '').strip()

    if not all([slug, name, admin_email, admin_username]):
        messages.error(request, "All fields are required.")
        return redirect('tenants:create_tenant')

    result = provision_tenant_with_invite(
        slug=slug,
        name=name,
        admin_email=admin_email,
        admin_username=admin_username,
    )

    if result['success']:
        messages.success(
            request,
            f"Organisation '{name}' provisioned. Share the onboarding link with the admin."
        )
        return redirect('tenants:tenant_detail', tenant_id=result['tenant_id'])
    else:
        messages.error(request, f"Provisioning failed: {result['error']}")
        return redirect('tenants:create_tenant')


def onboard(request, token: str):
    """
    Public onboarding page reached via invite link.
    Org admin sets their password and chooses a plan; the tenant becomes active.
    Lives at: <slug>.cliplens.local/onboard/<token>/
    """
    try:
        invite = OnboardingInvite.objects.using('control').select_related('tenant').get(token=token)
    except OnboardingInvite.DoesNotExist:
        return render(request, 'tenants/onboard_invalid.html',
                      {'reason': 'This invite link is invalid.'}, status=404)

    if not invite.is_valid():
        reason = ('This invite has already been used.' if invite.consumed_at
                  else 'This invite link has expired.')
        return render(request, 'tenants/onboard_invalid.html',
                      {'reason': reason}, status=410)

    plans = list(Plan.objects.using('control').all().order_by('storage_limit_gb'))

    if request.method == 'GET':
        return render(request, 'tenants/onboard.html', {
            'invite':  invite,
            'tenant':  invite.tenant,
            'plans':   plans,
        })

    # POST — claim
    password         = request.POST.get('password', '')
    password_confirm = request.POST.get('password_confirm', '')
    plan_id          = request.POST.get('plan_id', '')

    if len(password) < 8:
        messages.error(request, 'Password must be at least 8 characters.')
        return render(request, 'tenants/onboard.html', {
            'invite': invite, 'tenant': invite.tenant, 'plans': plans,
        }, status=400)

    if password != password_confirm:
        messages.error(request, 'Passwords do not match.')
        return render(request, 'tenants/onboard.html', {
            'invite': invite, 'tenant': invite.tenant, 'plans': plans,
        }, status=400)

    if not plan_id:
        messages.error(request, 'Please choose a plan.')
        return render(request, 'tenants/onboard.html', {
            'invite': invite, 'tenant': invite.tenant, 'plans': plans,
        }, status=400)

    result = claim_onboarding_invite(token=token, password=password, plan_id=int(plan_id))
    if not result['success']:
        messages.error(request, result['error'])
        return render(request, 'tenants/onboard.html', {
            'invite': invite, 'tenant': invite.tenant, 'plans': plans,
        }, status=400)

    tenant = result['tenant']

    # Paid plan — redirect to Stripe Checkout. Tenant stays inactive until webhook fires.
    if result.get('needs_payment'):
        from django.conf import settings as dj_settings
        if not getattr(dj_settings, 'STRIPE_ENABLED', False):
            # Stripe not configured — fall through to mock activation
            tenant.is_active   = True
            tenant.plan_status = Tenant.PLAN_STATUS_ACTIVE
            tenant.save(using='control', update_fields=['is_active', 'plan_status'])
            messages.success(request, "Plan activated in mock mode (Stripe not configured).")
            return redirect('/login/')

        from .stripe_utils import create_plan_checkout_session
        # Build URLs on the tenant subdomain (the user is already on it)
        scheme   = 'https' if request.is_secure() else 'http'
        base_url = f"{scheme}://{request.get_host()}"
        success_url = base_url + f'/onboard/{token}/success/'
        cancel_url  = base_url + f'/onboard/{token}/'
        try:
            checkout_url = create_plan_checkout_session(
                tenant=tenant,
                plan=tenant.plan,
                success_url=success_url,
                cancel_url=cancel_url,
                customer_email=invite.admin_email,
            )
        except Exception as exc:
            logger.exception("Stripe plan checkout failed for tenant=%s", tenant.slug)
            messages.error(request, f'Could not start checkout: {exc}')
            return render(request, 'tenants/onboard.html', {
                'invite': invite, 'tenant': invite.tenant, 'plans': plans,
            }, status=500)
        if checkout_url:
            return redirect(checkout_url)
        messages.error(request, 'Could not start Stripe checkout.')
        return redirect(request.path)

    # Free plan — already activated, log in
    messages.success(
        request,
        f"Welcome! Your account '{result['username']}' is ready. Please log in."
    )
    return redirect('/login/')


def landing_page(request):
    """
    Public marketing site shown at the BARE root domain (cliplens.local / cliplens.com).
    Shows product overview, plans, and a contact form.

    Special-case routing per host:
      • <slug>.cliplens.*    (tenant set by middleware) → go to the app
      • admin.cliplens.*     → go straight to the control plane (or its login)
      • bare cliplens.*      → marketing landing
    """
    # 1. Tenant subdomain → send them to the app
    if getattr(request, 'tenant', None) is not None:
        return redirect('/player/')

    # 2. admin subdomain → control plane (don't show marketing here)
    host = request.get_host().lower().split(':')[0]
    is_admin_subdomain = host.startswith('admin.')
    if is_admin_subdomain:
        if request.user.is_authenticated:
            return redirect('/platform/')
        return redirect('/login/?next=/platform/')

    # 3. Logged in to the bare root → bump them to the control plane too
    if request.user.is_authenticated:
        return redirect('/platform/')

    # 4. Unauthenticated visitor on bare root → marketing landing
    plans = list(Plan.objects.using('control').all().order_by('price_usd'))
    return render(request, 'tenants/landing.html', {
        'plans': plans,
    })


def submit_lead(request):
    """
    Public contact form POST → creates a LeadRequest in the control DB.
    Open to unauthenticated visitors. Basic anti-abuse: honeypot field.
    """
    if request.method != 'POST':
        return redirect('/')

    # Honeypot — bots will fill this; humans won't see it
    if request.POST.get('hp_company_website', '').strip():
        return redirect('/?contact=1')   # silently accept

    name    = request.POST.get('name', '').strip()
    email   = request.POST.get('email', '').strip()
    company = request.POST.get('company', '').strip()
    phone   = request.POST.get('phone', '').strip()
    interest = request.POST.get('interest', '').strip()
    message = request.POST.get('message', '').strip()

    if not name or not email:
        messages.error(request, 'Please provide your name and email.')
        return redirect('/?contact=1')

    LeadRequest.objects.using('control').create(
        name=name,
        email=email,
        company=company,
        phone=phone,
        interest=interest,
        message=message,
        referrer=request.META.get('HTTP_REFERER', '')[:300],
        user_agent=request.META.get('HTTP_USER_AGENT', '')[:300],
        ip_address=(request.META.get('HTTP_X_FORWARDED_FOR') or request.META.get('REMOTE_ADDR') or '').split(',')[0].strip() or None,
    )
    messages.success(request, "Thanks! We'll be in touch within one business day.")
    return redirect('/?contact=ok#contact')


def privacy_page(request):
    """Public privacy policy."""
    return render(request, 'tenants/privacy.html')


def terms_page(request):
    """Public terms of service."""
    return render(request, 'tenants/terms.html')


def onboard_success(request, token: str):
    """Landing page after Stripe Checkout for the initial plan subscription."""
    try:
        invite = OnboardingInvite.objects.using('control').select_related('tenant').get(token=token)
    except OnboardingInvite.DoesNotExist:
        return render(request, 'tenants/onboard_invalid.html',
                      {'reason': 'Invite not found.'}, status=404)
    messages.success(
        request,
        "Payment received! Your organisation is being activated. "
        "Please log in — if the page says 'not active' yet, just refresh in a few seconds."
    )
    return redirect('/login/')


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
            'price_usd': float(request.POST.get('price_usd', 0) or 0),
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


# ── System health ─────────────────────────────────────────────────────────────

@platform_owner_required
def system_health(request):
    """
    Page shell. Renders skeleton rows for every check; the browser then fans
    out individual fetches to system_health_check() in parallel so the page
    feels instant and results populate as they arrive.
    """
    from .system_health import get_check_manifest
    manifest = get_check_manifest()

    # Group rows for the template
    groups = {}
    for entry in manifest:
        groups.setdefault(entry['group'], []).append(entry)

    return render(request, 'tenants/system_health.html', {
        'check_groups': groups,
        'total_checks': len(manifest),
    })


@platform_owner_required
def system_databases(request):
    """
    Platform owner view of every database the system uses:
      - default (legacy / Django bootstrap)
      - control (platform metadata)
      - freestream_<slug> per tenant

    Shows host / port / name / user with size + reachability.
    Passwords are masked by default; revealing them is done via the
    POST endpoint below and is logged.
    """
    from django.conf import settings as dj_settings
    from django.db import connections
    from .models import Tenant

    tenants_by_dbname = {
        t.db_name: t
        for t in Tenant.objects.using('control').all()
    }

    rows = []
    for alias, cfg in dj_settings.DATABASES.items():
        # Classify the DB for the UI
        if alias == 'default':
            kind, label = 'legacy', 'Default (legacy)'
        elif alias == 'control':
            kind, label = 'control', 'Control plane'
        elif alias.startswith('freestream_'):
            t = tenants_by_dbname.get(cfg.get('NAME', ''))
            kind = 'tenant'
            label = f'Tenant: {t.name}' if t else f'Tenant DB (orphaned)'
        else:
            kind, label = 'other', alias

        # Probe size + reachability — bounded by Postgres query timeout
        size_bytes, reachable, version, err = None, False, '', ''
        try:
            with connections[alias].cursor() as cur:
                cur.execute("SELECT pg_database_size(current_database()), version()")
                size_bytes, version = cur.fetchone()
                reachable = True
        except Exception as exc:
            err = str(exc)[:120]

        rows.append({
            'alias':       alias,
            'kind':        kind,
            'label':       label,
            'tenant':      tenants_by_dbname.get(cfg.get('NAME', '')),
            'engine':      cfg.get('ENGINE', '').split('.')[-1],
            'name':        cfg.get('NAME', ''),
            'host':        cfg.get('HOST', '') or 'localhost',
            'port':        cfg.get('PORT', '') or '5432',
            'user':        cfg.get('USER', ''),
            'has_password': bool(cfg.get('PASSWORD', '')),
            'reachable':   reachable,
            'size_bytes':  size_bytes,
            'size_display': _fmt_db_size(size_bytes),
            'version':     ' '.join((version or '').split()[:2]) if version else '',
            'error':       err,
        })

    # Sort: control, default, then tenant DBs alphabetically
    sort_order = {'control': 0, 'legacy': 1, 'tenant': 2, 'other': 3}
    rows.sort(key=lambda r: (sort_order.get(r['kind'], 9), r['alias']))

    total_size = sum(r['size_bytes'] or 0 for r in rows)

    return render(request, 'tenants/system_databases.html', {
        'rows':       rows,
        'total_size': _fmt_db_size(total_size),
        'count':      len(rows),
    })


@platform_owner_required
@require_POST
def system_database_reveal(request, alias: str):
    """
    POST-only endpoint that returns the password for a single database alias.
    Logs every successful reveal so there's an audit trail of who looked at what.
    """
    from django.conf import settings as dj_settings
    cfg = dj_settings.DATABASES.get(alias)
    if not cfg:
        return JsonResponse({'error': f'Unknown alias: {alias}'}, status=404)

    password = cfg.get('PASSWORD', '') or ''
    logger.warning(
        "DB CREDENTIAL REVEALED: alias=%s user=%s by=%s (%s)",
        alias, cfg.get('USER', '?'), request.user.username,
        request.META.get('REMOTE_ADDR', '?'),
    )
    return JsonResponse({
        'alias':    alias,
        'password': password,
        'user':     cfg.get('USER', ''),
        'host':     cfg.get('HOST', '') or 'localhost',
        'port':     cfg.get('PORT', '') or '5432',
        'name':     cfg.get('NAME', ''),
    })


def _fmt_db_size(n):
    if n is None:
        return '—'
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024:
            return f'{n:.1f} {unit}' if unit != 'B' else f'{int(n)} B'
        n /= 1024
    return f'{n:.1f} PB'


@platform_owner_required
def system_health_check(request, check_id: str):
    """JSON endpoint that runs a single named check and returns the result."""
    from .system_health import run_check_by_id
    result = run_check_by_id(check_id)
    if result is None:
        return JsonResponse({'error': f'Unknown check: {check_id}'}, status=404)
    return JsonResponse(result)


# ── Leads (contact-form inbox) ─────────────────────────────────────────────────

@platform_owner_required
def manage_leads(request):
    """Inbox of contact-form submissions from the public landing page."""
    if request.method == 'POST':
        lead_id = request.POST.get('lead_id')
        action  = request.POST.get('action', '')
        try:
            lead = LeadRequest.objects.using('control').get(pk=int(lead_id))
        except (LeadRequest.DoesNotExist, ValueError):
            messages.error(request, "Lead not found.")
            return redirect('tenants:manage_leads')

        if action == 'update_status':
            new_status = request.POST.get('status', '')
            valid_statuses = dict(LeadRequest.STATUS_CHOICES)
            if new_status in valid_statuses:
                lead.status = new_status
                lead.save(using='control', update_fields=['status', 'updated_at'])
                messages.success(request, f"Marked '{lead.name}' as {valid_statuses[new_status]}.")
        elif action == 'update_notes':
            lead.notes = request.POST.get('notes', '').strip()
            lead.save(using='control', update_fields=['notes', 'updated_at'])
            messages.success(request, "Notes saved.")
        elif action == 'delete':
            lead.delete(using='control')
            messages.success(request, f"Deleted lead '{lead.name}'.")
        return redirect('tenants:manage_leads')

    status_filter = request.GET.get('status', '').strip()
    qs = LeadRequest.objects.using('control').all()
    if status_filter:
        qs = qs.filter(status=status_filter)
    leads = list(qs[:200])

    # Counts per status for the filter pills
    from django.db.models import Count
    counts_qs = LeadRequest.objects.using('control').values('status').annotate(n=Count('pk'))
    counts = {row['status']: row['n'] for row in counts_qs}
    counts['total'] = LeadRequest.objects.using('control').count()

    return render(request, 'tenants/manage_leads.html', {
        'leads':         leads,
        'status_filter': status_filter,
        'counts':        counts,
        'status_choices': LeadRequest.STATUS_CHOICES,
    })


# ── Top-up product management (platform owner) ────────────────────────────────

@platform_owner_required
def manage_topups(request):
    """CRUD for TopUpProduct SKUs — storage addons and AI credit packs."""
    from .models import TopUpProduct

    if request.method == 'POST':
        action = request.POST.get('action', 'create')

        if action == 'create':
            try:
                TopUpProduct.objects.using('control').create(
                    kind=request.POST.get('kind', ''),
                    name=request.POST.get('name', '').strip(),
                    amount=int(request.POST.get('amount', 0)),
                    price_usd=float(request.POST.get('price_usd', 0)),
                    sort_order=int(request.POST.get('sort_order', 0) or 0),
                    is_active=bool(request.POST.get('is_active')),
                )
                messages.success(request, "Top-up product created.")
            except Exception as exc:
                messages.error(request, f"Could not create product: {exc}")

        elif action == 'update':
            try:
                p = TopUpProduct.objects.using('control').get(pk=int(request.POST.get('product_id')))
                p.name       = request.POST.get('name', '').strip() or p.name
                p.amount     = int(request.POST.get('amount', p.amount))
                p.price_usd  = float(request.POST.get('price_usd', p.price_usd))
                p.sort_order = int(request.POST.get('sort_order', p.sort_order) or 0)
                p.is_active  = bool(request.POST.get('is_active'))
                p.save(using='control')
                messages.success(request, f"Updated '{p.name}'.")
            except Exception as exc:
                messages.error(request, f"Could not update: {exc}")

        elif action == 'delete':
            try:
                p = TopUpProduct.objects.using('control').get(pk=int(request.POST.get('product_id')))
                name = p.name
                p.delete(using='control')
                messages.success(request, f"Deleted '{name}'.")
            except Exception as exc:
                messages.error(request, f"Could not delete: {exc}")

        return redirect('tenants:manage_topups')

    products = TopUpProduct.objects.using('control').order_by('kind', 'sort_order', 'amount')
    storage_products = [p for p in products if p.kind == TopUpProduct.KIND_STORAGE]
    credit_products  = [p for p in products if p.kind == TopUpProduct.KIND_CREDITS]

    return render(request, 'tenants/manage_topups.html', {
        'storage_products': storage_products,
        'credit_products':  credit_products,
    })


# ── Stripe webhook ─────────────────────────────────────────────────────────────

from django.views.decorators.csrf import csrf_exempt
from django.http import HttpResponse


@csrf_exempt
def stripe_webhook(request):
    """
    Receives signed events from Stripe (test or live).
    Forward locally with:
        stripe listen --forward-to localhost:8000/api/stripe/webhook/
    """
    from django.conf import settings as dj_settings

    if request.method != 'POST':
        return HttpResponse(status=405)

    payload   = request.body
    sig_header = request.META.get('HTTP_STRIPE_SIGNATURE', '')
    secret     = dj_settings.STRIPE_WEBHOOK_SECRET

    try:
        import stripe
        stripe.api_key = dj_settings.STRIPE_SECRET_KEY
        if secret:
            event = stripe.Webhook.construct_event(payload, sig_header, secret)
        else:
            # No secret configured — parse without verification (DEV ONLY).
            event = json.loads(payload.decode('utf-8'))
    except Exception as exc:
        logger.warning("Stripe webhook: invalid signature/payload: %s", exc)
        return HttpResponse(status=400)

    from .stripe_utils import handle_webhook_event
    try:
        result = handle_webhook_event(event)
        logger.info("Stripe webhook handled: %s", result)
    except Exception as exc:
        logger.exception("Stripe webhook handler crashed: %s", exc)
        return HttpResponse(status=500)

    return HttpResponse(status=200)


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
