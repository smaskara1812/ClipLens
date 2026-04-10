from django.conf import settings
from django.db.models import Q


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

    Usage in templates:
        {{ SITE_URL }}                          → http://192.168.1.100:8080
        {{ SITE_URL }}/api/videos/              → full API URL
        {{ SITE_URL }}{{ video.hls_url }}       → full HLS playlist URL

    Configured via SITE_URL in .env — no hardcoded hosts anywhere.
    """
    return {
        'SITE_URL': getattr(settings, 'SITE_URL', 'http://localhost:8000'),
    }


def user_role(request):
    """
    Injects role flags into every template context.

    Available variables:
        is_editor      — True for editors and superadmins
        is_superadmin  — True only for superadmins
        user_role      — raw role string ('superadmin' / 'editor' / 'viewer') or None
    """
    if not request.user.is_authenticated:
        return {'is_editor': False, 'is_superadmin': False, 'user_role': None}

    try:
        profile = request.user.profile
        role = profile.role
        return {
            'is_editor':     profile.is_editor,
            'is_superadmin': profile.is_superadmin,
            'user_role':     role,
        }
    except Exception:
        # Profile doesn't exist yet (e.g. superuser created via createsuperuser)
        if request.user.is_superuser:
            return {'is_editor': True, 'is_superadmin': True, 'user_role': 'superadmin'}
        return {'is_editor': False, 'is_superadmin': False, 'user_role': 'viewer'}

