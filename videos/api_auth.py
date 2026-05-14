"""
api_auth.py — helpers for ClipLens external API authentication.

Authentication flow
-------------------
1. Caller passes the raw key in one of two headers:
     X-API-Key: cliplens_<token>
     Authorization: Bearer cliplens_<token>

2. `authenticate_api_key(request)` hashes the raw key and looks it up.
   Returns an APIKey instance, or raises APIAuthError.

3. `has_permission(api_key, permission, scope_id='')` checks whether
   the key has the requested permission (and correct scope).

4. Convenience helpers for the most common checks used in api_v1.py.
"""

import hashlib
import secrets

from django.utils import timezone

from .models import APIKey, APIKeyPermission


# ── Key generation ────────────────────────────────────────────────────────────

def generate_api_key():
    """
    Generate a new raw key.

    Returns (raw_key, key_prefix, key_hash):
      raw_key    — full string to show the user once, e.g. "cliplens_abc123..."
      key_prefix — first 16 chars of the raw key, stored for display
      key_hash   — SHA-256 hex digest of raw_key, stored in DB
    """
    token   = secrets.token_urlsafe(32)
    raw_key = f'cliplens_{token}'
    prefix  = raw_key[:16]
    digest  = hashlib.sha256(raw_key.encode()).hexdigest()
    return raw_key, prefix, digest


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


# ── Authentication ────────────────────────────────────────────────────────────

class APIAuthError(Exception):
    """Raised when API key authentication fails.  Carries an HTTP status code."""
    def __init__(self, message, status=401):
        super().__init__(message)
        self.message = message
        self.status  = status


def authenticate_api_key(request):
    """
    Extract and validate the API key from the request.

    Accepted header formats:
      X-API-Key: cliplens_<token>
      Authorization: Bearer cliplens_<token>

    Returns the APIKey instance on success.
    Raises APIAuthError on any failure.
    """
    raw_key = None

    # Try X-API-Key first
    xkey = request.META.get('HTTP_X_API_KEY', '').strip()
    if xkey:
        raw_key = xkey
    else:
        # Try Authorization: Bearer ...
        auth = request.META.get('HTTP_AUTHORIZATION', '').strip()
        if auth.lower().startswith('bearer '):
            raw_key = auth[7:].strip()

    if not raw_key:
        raise APIAuthError('API key required. Pass it via X-API-Key or Authorization: Bearer header.')

    digest = hash_key(raw_key)

    try:
        key = APIKey.objects.select_related('owner').get(key_hash=digest, is_active=True)
    except APIKey.DoesNotExist:
        raise APIAuthError('Invalid or revoked API key.')

    # Check expiry
    if key.expires_at and key.expires_at < timezone.now():
        raise APIAuthError('API key has expired.', status=403)

    # Update last_used_at (fire-and-forget, don't fail auth on DB error)
    try:
        APIKey.objects.filter(pk=key.pk).update(last_used_at=timezone.now())
    except Exception:
        pass

    return key


# ── Permission checks ─────────────────────────────────────────────────────────

def has_permission(api_key, permission, scope_id=''):
    """
    Return True if the key has the given permission.

    For scoped permissions (search:channel, search:playlist, video:upload)
    pass the UUID as scope_id.  An empty scope_id always returns False for
    those permissions.
    """
    qs = api_key.permissions.filter(permission=permission)
    if scope_id:
        qs = qs.filter(scope_id=str(scope_id))
    return qs.exists()


def require_permission(api_key, permission, scope_id=''):
    """Like has_permission but raises APIAuthError on failure."""
    if not has_permission(api_key, permission, scope_id):
        raise APIAuthError(
            f'This API key does not have the "{permission}" permission.',
            status=403,
        )


def can_search(api_key):
    """Return True if the key has ANY search permission (global or scoped)."""
    return api_key.permissions.filter(
        permission__in=[
            APIKeyPermission.PERM_SEARCH_GLOBAL,
            APIKeyPermission.PERM_SEARCH_CHANNEL,
            APIKeyPermission.PERM_SEARCH_PLAYLIST,
        ]
    ).exists()


def get_search_scope(api_key):
    """
    Return a dict describing the search scope for this key:
      {
        'is_global': bool,
        'channel_ids': [list of UUID strings],
        'playlist_ids': [list of UUID strings],
      }

    The caller uses this to filter the queryset appropriately.
    """
    perms = list(api_key.permissions.filter(
        permission__in=[
            APIKeyPermission.PERM_SEARCH_GLOBAL,
            APIKeyPermission.PERM_SEARCH_CHANNEL,
            APIKeyPermission.PERM_SEARCH_PLAYLIST,
        ]
    ))

    if any(p.permission == APIKeyPermission.PERM_SEARCH_GLOBAL for p in perms):
        return {'is_global': True, 'channel_ids': [], 'playlist_ids': []}

    channel_ids  = [p.scope_id for p in perms if p.permission == APIKeyPermission.PERM_SEARCH_CHANNEL]
    playlist_ids = [p.scope_id for p in perms if p.permission == APIKeyPermission.PERM_SEARCH_PLAYLIST]
    return {'is_global': False, 'channel_ids': channel_ids, 'playlist_ids': playlist_ids}
