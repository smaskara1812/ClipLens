"""
TenantMiddleware
────────────────
Reads the subdomain from the Host header → looks up the matching Tenant row
in the control DB → stores it on request.tenant and sets the thread-local DB
alias so TenantDatabaseRouter routes every subsequent DB query to that tenant.

Subdomain extraction examples:
  orgA.cliplens.local  → slug = "orga"
  orgb.example.com     → slug = "orgb"
  admin.cliplens.local → slug = "admin"  (control plane, handled separately)
  localhost:8000       → no subdomain → request.tenant = None (single-tenant dev)

The middleware is transparent when MULTI_TENANT is False (single-tenant legacy mode).
"""

import logging
from django.conf import settings
from django.http import HttpResponse
from .db_router import set_db, clear_db
from .storage import set_media_root, clear_media_root

logger = logging.getLogger(__name__)

# Subdomains that are NOT tenant slugs but have special meaning
RESERVED_SUBDOMAINS = {'www', 'admin', 'api', 'static', 'media'}


def _extract_subdomain(request) -> str | None:
    """
    Pull the leftmost label from the Host header if it looks like a subdomain
    (i.e. the host has ≥ 2 dots, or matches *.cliplens.local pattern).
    Returns None when running on bare localhost / 127.0.0.1.
    """
    host = request.get_host().lower().split(':')[0]  # strip port
    parts = host.split('.')

    # bare hostname or IP — no subdomain
    if len(parts) < 2:
        return None
    # localhost, 127.0.0.1, or just "domain.tld" — no subdomain
    if len(parts) == 2:
        return None

    subdomain = parts[0]
    return subdomain if subdomain else None


class TenantMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self._multi_tenant = getattr(settings, 'MULTI_TENANT', False)

    def __call__(self, request):
        # ── Single-tenant legacy mode ─────────────────────────────────────────
        if not self._multi_tenant:
            request.tenant = None
            return self.get_response(request)

        # ── Resolve tenant from subdomain ─────────────────────────────────────
        subdomain = _extract_subdomain(request)

        if subdomain is None:
            # No subdomain: root domain hit — redirect to docs or 404
            request.tenant = None
            return self.get_response(request)

        if subdomain == 'admin':
            # Control plane — no tenant DB; handled by tenants.views
            request.tenant = None
            clear_db()
            return self.get_response(request)

        if subdomain in RESERVED_SUBDOMAINS:
            request.tenant = None
            return self.get_response(request)

        # ── Look up tenant in control DB ──────────────────────────────────────
        try:
            from .models import Tenant
            tenant = Tenant.objects.using('control').get(slug=subdomain, is_active=True)
        except Exception:
            tenant = None

        if tenant is None:
            return HttpResponse(
                f"<h1>Organisation '{subdomain}' not found</h1>"
                f"<p>This subdomain is not registered. Please check the URL.</p>",
                status=404,
                content_type='text/html',
            )

        # ── Wire up the tenant DB + media root for this request ──────────────
        request.tenant = tenant
        set_db(tenant.db_name)
        media_abs = str(
            (settings.BASE_DIR / 'media' / tenant.media_folder).resolve()
        )
        set_media_root(media_abs)

        try:
            response = self.get_response(request)
        except Exception:
            import traceback
            logger.exception("TenantMiddleware caught unhandled exception for tenant=%s path=%s",
                             subdomain, request.path)
            traceback.print_exc()
            raise
        finally:
            clear_db()        # always reset — thread pool reuses threads
            clear_media_root()

        return response
