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
        # Allow inactive tenants through ONLY for the /onboard/ flow so the
        # admin can claim their invite. Everything else is gated by is_active.
        try:
            from .models import Tenant
            tenant = Tenant.objects.using('control').get(slug=subdomain)
        except Exception:
            tenant = None

        if tenant is None:
            return HttpResponse(
                f"<h1>Organisation '{subdomain}' not found</h1>"
                f"<p>This subdomain is not registered. Please check the URL.</p>",
                status=404,
                content_type='text/html',
            )

        # Paths that must work even on inactive tenants (so the admin can
        # actually claim their invite + redirect to login afterwards)
        public_inactive_paths = (
            '/onboard/',
            '/login/',
            '/static/',
            '/favicon.ico',
        )
        is_public_path = any(request.path.startswith(p) for p in public_inactive_paths)
        if not tenant.is_active and not is_public_path:
            return HttpResponse(
                f"<h1>Organisation '{subdomain}' is not active yet</h1>"
                f"<p>The administrator hasn't completed onboarding. Please use the invite link.</p>",
                status=403,
                content_type='text/html',
            )

        # ── Per-tenant maintenance mode (media relocation in progress) ──────
        # When the platform admin is moving this tenant's files, block every
        # request to the tenant subdomain so nothing reads/writes during the
        # copy. The control plane (admin subdomain) is unaffected because it
        # never reaches this branch (subdomain == 'admin' returned earlier).
        if getattr(tenant, 'media_relocating', False):
            # Allow static + favicon through so the maintenance page renders
            allowed_during_maintenance = ('/static/', '/favicon.ico')
            if not any(request.path.startswith(p) for p in allowed_during_maintenance):
                return HttpResponse(
                    "<!doctype html><html><head><meta charset='utf-8'>"
                    "<title>Maintenance — ClipLens</title>"
                    "<meta http-equiv='refresh' content='30'>"
                    "<style>body{font-family:system-ui,sans-serif;max-width:560px;"
                    "margin:80px auto;padding:0 24px;color:#222;line-height:1.5}"
                    "h1{font-size:24px;margin-bottom:8px}p{color:#555}"
                    ".badge{display:inline-block;background:#fef3c7;color:#92400e;"
                    "padding:4px 10px;border-radius:999px;font-size:12px;"
                    "font-weight:600;margin-bottom:16px}</style></head>"
                    f"<body><div class='badge'>Scheduled maintenance</div>"
                    f"<h1>{tenant.name} is temporarily offline</h1>"
                    "<p>Your platform administrator is migrating media storage "
                    "to a new location. The application will be back online "
                    "automatically once the move completes — no action needed.</p>"
                    "<p style='color:#888;font-size:13px;margin-top:32px'>"
                    "This page refreshes every 30 seconds.</p></body></html>",
                    status=503,
                    content_type='text/html',
                )

        # ── Wire up the tenant DB + media root for this request ──────────────
        request.tenant = tenant
        set_db(tenant.db_name)
        # Honour per-tenant custom absolute path if set
        if getattr(tenant, 'media_root_absolute', '').strip():
            media_abs = tenant.media_root_absolute.strip()
        else:
            media_abs = str(
                (settings.BASE_DIR / 'media' / tenant.media_folder).resolve()
            )
        # URL prefix is ALWAYS tenants/<slug> regardless of disk location —
        # the prefix only describes how URLs map to this tenant's bucket.
        url_prefix = (tenant.media_folder or '').strip('/') or f'tenants/{tenant.slug}'
        set_media_root(media_abs, url_prefix=url_prefix)

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
