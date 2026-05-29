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

def set_media_root(path: str) -> None:
    _media_state.root = path


def get_media_root() -> str:
    return getattr(_media_state, 'root', None) or str(settings.MEDIA_ROOT)


def clear_media_root() -> None:
    _media_state.root = None


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

        root = get_media_root()
        media_root = str(settings.MEDIA_ROOT)

        # If the tenant root is a subdirectory of MEDIA_ROOT, prefix the URL
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
