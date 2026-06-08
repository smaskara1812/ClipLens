"""
Tenant-aware file storage
─────────────────────────
Replaces Django's default FileSystemStorage so every uploaded file lands
in the tenant's isolated media folder rather than the shared MEDIA_ROOT.

Thread-local state (set by TenantMiddleware alongside set_db):
    _media_state.root  → e.g. "/path/to/project/media/tenants/orga/"

When no tenant is active (single-tenant mode or admin subdomain) it falls
back to settings.MEDIA_ROOT transparently.

Path convention
───────────────
FileField / ImageField names stored in the DB are relative to the tenant
storage root (self.location), exactly as standard FileSystemStorage works:

  Disk path:  <MEDIA_ROOT>/tenants/org1/subtitles/uuid_en_auto.vtt
  DB name:    subtitles/uuid_en_auto.vtt          ← relative to tenant root
  URL:        /media/tenants/org1/subtitles/uuid_en_auto.vtt
                  ↑ base_url is tenant-aware

CharField-based paths (hls_path, seek_sprite, crop_path, etc.) are stored
relative to the GLOBAL MEDIA_ROOT so that the model's URL property
"/media/<path>" resolves correctly at render time without needing tenant
context:

  Disk path:  <MEDIA_ROOT>/tenants/org1/hls/uuid/master.m3u8
  DB value:   tenants/org1/hls/uuid/master.m3u8
  URL:        /media/tenants/org1/hls/uuid/master.m3u8
"""

import threading
from pathlib import Path

from django.conf import settings
from django.core.files.storage import FileSystemStorage

_media_state = threading.local()


# ── Public helpers ─────────────────────────────────────────────────────────────

def set_media_root(path: str, url_prefix: str = '') -> None:
    """
    Set the active tenant's media root for this thread.

    `path` is the absolute on-disk root (may be inside MEDIA_ROOT OR
    completely outside it when `tenant.media_root_absolute` is set).

    `url_prefix` is the URL sub-path under /media/ that maps to this root —
    always `tenants/<slug>` in multi-tenant mode regardless of where the
    files actually live on disk. Empty string in single-tenant mode.
    """
    _media_state.root = path
    _media_state.url_prefix = (url_prefix or '').strip('/')


def get_media_root() -> str:
    return getattr(_media_state, 'root', None) or str(settings.MEDIA_ROOT)


def get_url_prefix() -> str:
    """
    URL prefix (without leading/trailing slashes) under /media/ for the
    active tenant. Empty in single-tenant mode.
    """
    return getattr(_media_state, 'url_prefix', '') or ''


def clear_media_root() -> None:
    _media_state.root = None
    _media_state.url_prefix = ''


# ── Path translation helpers ──────────────────────────────────────────────────

def to_storage_path(absolute_path) -> str:
    """
    Convert an absolute filesystem path to the storage form used in CharFields
    like `hls_path`, `seek_sprite`, `crop_path` — always
    `tenants/<slug>/<rest>` in multi-tenant mode so the URL `/media/<value>`
    works regardless of where the files actually live on disk.

    Used in tasks/services to replace the brittle
        out_path.relative_to(settings.MEDIA_ROOT)
    pattern, which raises ValueError when out_path is outside MEDIA_ROOT
    (i.e. on a tenant with a custom media_root_absolute).
    """
    abs_path = Path(absolute_path).resolve()
    media_root = Path(settings.MEDIA_ROOT).resolve()

    # Case 1: path is under the global MEDIA_ROOT (default / single-tenant) —
    # use the legacy MEDIA_ROOT-relative form. Works for both modes.
    try:
        return str(abs_path.relative_to(media_root))
    except ValueError:
        pass

    # Case 2: path is under the tenant's custom root — build
    # tenants/<slug>/<rest> from the URL prefix + on-disk relative path.
    root = Path(get_media_root()).resolve()
    try:
        rel = abs_path.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f'to_storage_path: {abs_path} is outside both MEDIA_ROOT and '
            f'tenant root {root}'
        ) from exc

    prefix = get_url_prefix()
    if prefix:
        return f'{prefix}/{rel}'.replace('\\', '/')
    return str(rel).replace('\\', '/')


def from_storage_path(stored: str) -> Path:
    """
    Resolve a CharField-stored path back to an absolute on-disk location,
    honouring the current tenant's media root.

    Replaces the pattern
        Path(settings.MEDIA_ROOT) / video.hls_path
    which only works for tenants on the default disk layout.
    """
    s = (stored or '').lstrip('/')
    prefix = get_url_prefix()

    # Multi-tenant: stored values start with `tenants/<slug>/`. Strip the
    # prefix so we can join with the actual on-disk root.
    if prefix and (s == prefix or s.startswith(prefix + '/')):
        rest = s[len(prefix):].lstrip('/')
        return Path(get_media_root()) / rest

    # Single-tenant or legacy global file — resolve under MEDIA_ROOT.
    return Path(settings.MEDIA_ROOT) / s


# ── Storage backend ────────────────────────────────────────────────────────────

class TenantFileSystemStorage(FileSystemStorage):
    """
    FileSystemStorage whose root (location) and URL prefix (base_url) both
    resolve at request/task time from the thread-local media root.

    - location  → tenant media root  (files are written here)
    - base_url  → /media/tenants/<slug>/  (so .url includes the tenant prefix)
    - path(name) → location / name  (standard FileSystemStorage behaviour)

    FileField names in the DB remain relative to the tenant root
    (e.g. "subtitles/uuid.vtt"), and the URL becomes
    "/media/tenants/org1/subtitles/uuid.vtt" via the tenant-aware base_url.
    """

    def _get_location(self) -> str:
        return get_media_root()

    @property
    def location(self) -> str:            # type: ignore[override]
        return self._get_location()

    @location.setter
    def location(self, value):
        # Django's FileSystemStorage.__init__ tries to assign location;
        # we ignore it and always derive from the thread-local instead.
        pass

    def _get_base_url(self) -> str:
        """
        Return the URL prefix for this storage root.

        In single-tenant mode (or when no tenant is active) this is just
        /media/. In multi-tenant mode it includes the tenant sub-path so
        that ImageField/FileField .url properties produce correct URLs
        without any extra logic in models or views.

        Example:
            location = /path/to/media/tenants/org1
            media_root = /path/to/media
            → base_url = /media/tenants/org1/
        """
        if not getattr(settings, 'MULTI_TENANT', False):
            return settings.MEDIA_URL

        # Prefer the explicit URL prefix set by middleware/celery — this
        # works even when the tenant's storage root lives outside MEDIA_ROOT
        # (custom media_root_absolute).
        prefix = get_url_prefix()
        if prefix:
            return settings.MEDIA_URL + prefix.strip('/') + '/'

        # Fallback: derive from on-disk path (legacy behaviour, only works
        # when the storage root is a subdirectory of MEDIA_ROOT).
        root = get_media_root()
        media_root = str(settings.MEDIA_ROOT)
        if root != media_root:
            try:
                suffix = Path(root).relative_to(media_root)
                return settings.MEDIA_URL + str(suffix).replace('\\', '/') + '/'
            except ValueError:
                pass

        return settings.MEDIA_URL

    @property
    def base_url(self) -> str:            # type: ignore[override]
        return self._get_base_url()

    @base_url.setter
    def base_url(self, value):
        pass
