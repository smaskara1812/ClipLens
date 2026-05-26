from django.conf import settings
from django.db.models import Q
from django.core.cache import cache as _cache


def sidebar_context(request):
    """
    Injects data needed by the persistent sidebar into every template context:
      - subscribed_channels  — channels the user subscribes to
      - owned_channels       — channels the user can manage (owner or editor)
      - unread_count         — unread notification count (cached 60 s)
    Views must NOT re-fetch these — they are always available in every template.
    """
    if not request.user.is_authenticated:
        return {'subscribed_channels': [], 'owned_channels': [], 'unread_count': 0}
    try:
        from django.core.cache import cache as _cache
        from videos.models import Channel, Notification

        subscribed_channels = list(
            Channel.objects.filter(subscribers__user=request.user).order_by('name')
        )
        owned_channels = getattr(request, '_owned_channels_cache', None)
        if owned_channels is None:
            owned_channels = list(
                Channel.objects.filter(
                    Q(owner=request.user) | Q(editors=request.user)
                )
                .distinct()
                .order_by('name')
            )
            request._owned_channels_cache = owned_channels

        _unread_key = f'unread_{request.user.pk}'
        unread_count = _cache.get(_unread_key)
        if unread_count is None:
            unread_count = Notification.objects.filter(
                recipient=request.user, is_read=False
            ).count()
            _cache.set(_unread_key, unread_count, 60)

        return {
            'subscribed_channels': subscribed_channels,
            'owned_channels':      owned_channels,
            'unread_count':        unread_count,
        }
    except Exception:
        return {'subscribed_channels': [], 'owned_channels': [], 'unread_count': 0}


def site_url(request):
    """
    Injects SITE_URL into every Django template context.

    In single-tenant mode this returns settings.SITE_URL (from .env).
    In multi-tenant mode it derives the origin from the actual request host
    so that {{ SITE_URL }} on testorg1.cliplens.local returns
    http://testorg1.cliplens.local — not the hardcoded localhost value.
    This fixes all CORS errors caused by cross-origin API calls in JS templates.
    """
    if getattr(settings, 'MULTI_TENANT', False) and request is not None:
        try:
            scheme = 'https' if request.is_secure() else 'http'
            host = request.get_host()  # includes port if non-standard
            return {'SITE_URL': f'{scheme}://{host}'}
        except Exception:
            pass
    return {
        'SITE_URL': getattr(settings, 'SITE_URL', 'http://localhost:8000'),
    }


def tenant_usage_warning(request):
    """
    Injects usage warning flags into every template context when MULTI_TENANT=True.

    Available variables:
        usage_warning        — 'warning' | 'critical' | None
        usage_warning_detail — dict with resource name, used, limit, pct (or None)

    Cached for 5 minutes per tenant to avoid hammering the control DB on every request.
    """
    if not getattr(settings, 'MULTI_TENANT', False):
        return {'usage_warning': None, 'usage_warning_detail': None}

    tenant = getattr(request, 'tenant', None)
    if not tenant:
        return {'usage_warning': None, 'usage_warning_detail': None}

    cache_key = f'usage_warning_{tenant.slug}'
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        from tenants.metering import get_monthly_usage, usage_warning_level
        usage = get_monthly_usage(tenant.slug)
        plan = usage.get('plan')
        if not plan:
            return {'usage_warning': None, 'usage_warning_detail': None}

        # Check both resources; surface the most severe
        ai_level = usage_warning_level(usage['ai_minutes'], plan.ai_minutes_limit)
        storage_level = usage_warning_level(usage['storage_gb'], plan.storage_limit_gb)

        severity_rank = {None: 0, 'warning': 1, 'critical': 2}
        if severity_rank.get(ai_level, 0) >= severity_rank.get(storage_level, 0):
            worst_level = ai_level
            if ai_level and plan.ai_minutes_limit > 0:
                detail = {
                    'resource': 'AI processing',
                    'used': round(usage['ai_minutes'], 1),
                    'limit': plan.ai_minutes_limit,
                    'pct': round(usage['ai_minutes'] / plan.ai_minutes_limit * 100, 1),
                    'unit': 'min',
                }
            else:
                detail = None
        else:
            worst_level = storage_level
            if storage_level and plan.storage_limit_gb > 0:
                detail = {
                    'resource': 'Storage',
                    'used': round(usage['storage_gb'], 2),
                    'limit': plan.storage_limit_gb,
                    'pct': round(usage['storage_gb'] / plan.storage_limit_gb * 100, 1),
                    'unit': 'GB',
                }
            else:
                detail = None

        result = {'usage_warning': worst_level, 'usage_warning_detail': detail}
        _cache.set(cache_key, result, 300)  # cache 5 min
        return result
    except Exception:
        return {'usage_warning': None, 'usage_warning_detail': None}


def user_role(request):
    """
    Injects role flags into every template context.

    Available variables:
        is_editor           — True for editors and superadmins
        is_superadmin       — True only for superadmins
        user_role           — raw role string ('superadmin' / 'editor' / 'viewer') or None
        is_platform_owner   — True only for the platform owner (Soham); gates control plane
        control_plane_url   — absolute URL to admin.cliplens.local/platform/ (platform owner only)
    """
    base = {'is_editor': False, 'is_superadmin': False, 'user_role': None,
            'is_platform_owner': False, 'control_plane_url': ''}

    if not request.user.is_authenticated:
        return base

    try:
        profile = request.user.profile
        role = profile.role
        is_platform_owner = getattr(profile, 'is_platform_owner', False)

        # Build control plane URL for platform owners in multi-tenant mode
        control_plane_url = ''
        if is_platform_owner and getattr(settings, 'MULTI_TENANT', False):
            try:
                scheme = 'https' if request.is_secure() else 'http'
                host = request.get_host()  # e.g. testorg1.cliplens.local or admin.cliplens.local
                # Replace subdomain (or bare host) with 'admin'
                parts = host.split('.')
                if len(parts) >= 3:
                    parts[0] = 'admin'
                    admin_host = '.'.join(parts)
                else:
                    admin_host = host  # fallback — already on admin or bare host
                control_plane_url = f'{scheme}://{admin_host}/platform/'
            except Exception:
                pass

        return {
            'is_editor':         profile.is_editor,
            'is_superadmin':     profile.is_superadmin,
            'user_role':         role,
            'is_platform_owner': is_platform_owner,
            'control_plane_url': control_plane_url,
        }
    except Exception:
        # Profile doesn't exist yet (e.g. superuser created via createsuperuser)
        if request.user.is_superuser:
            return {**base, 'is_editor': True, 'is_superadmin': True, 'user_role': 'superadmin'}
        return base

