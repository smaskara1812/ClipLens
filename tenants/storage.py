"""
Tenant-aware file storage
─────────────────────────
Replaces Django's default FileSystemStorage so every uploaded file lands
in the tenant's isolated media folder rather than the shared MEDIA_ROOT.

Thread-local state (set by TenantMiddleware alongside set_db):
    _media_state.root  → e.g. "/path/to/project/media/tenants/orga/"

When no tenant is active (single-tenant mode or admin subdomain) it falls
back to settings.MEDIA_ROOT transparently.
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
    A FileSystemStorage subclass whose location resolves at save-time from the
    thread-local media root rather than being fixed at import time.

    Django calls _get_location() (via the `location` property) each time it
    needs the root directory, so setting the thread-local before each request
    is sufficient.
    """

    def _get_location(self) -> str:
        return get_media_root()

    @property
    def location(self) -> str:            # type: ignore[override]
        return self._get_location()

    @location.setter
    def location(self, value):
        # Needed so Django's FileSystemStorage.__init__ can assign location
        # without crashing; we deliberately ignore the stored value and always
        # derive it from the thread-local at request time.
        pass

    def _get_base_url(self) -> str:
        """Return /media/ for relative URLs — same for all tenants."""
        return settings.MEDIA_URL

    @property
    def base_url(self) -> str:            # type: ignore[override]
        return self._get_base_url()

    @base_url.setter
    def base_url(self, value):
        pass
