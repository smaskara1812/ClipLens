"""
Tenant-aware media file serving
────────────────────────────────
Replaces Django's bare django.views.static.serve for the /media/ URL so that
cross-tenant file access is blocked.

Path structure on disk:
    media/tenants/<slug>/videos/...    ← tenant-isolated uploads
    media/tenants/<slug>/photos/...
    media/live/...                     ← live stream HLS segments (not tenant-scoped)
    media/<anything else>              ← legacy single-tenant files

Rules:
    • If the path starts with "tenants/<slug>/":
        - The request MUST come from a tenant whose slug matches <slug>.
        - Direct access (localhost:8000) or the admin subdomain (tenant=None)
          is blocked for tenant files unless MULTI_TENANT is False.
    • All other paths (live/, legacy flat files) are served without restriction.
    • In single-tenant mode (MULTI_TENANT=False), all paths are served freely.
"""

import os

from django.conf import settings
from django.http import Http404, HttpResponseForbidden
from django.views.static import serve


def protected_media(request, path):
    """
    Serve a media file, enforcing tenant ownership when MULTI_TENANT=True.

    Drop-in replacement for:
        re_path(r'^media/(?P<path>.*)$', serve, {'document_root': settings.MEDIA_ROOT})
    """
    multi_tenant = getattr(settings, 'MULTI_TENANT', False)

    if multi_tenant and path.startswith('tenants/'):
        # Extract the slug from the path: "tenants/<slug>/..."
        parts = path.split('/', 2)   # ['tenants', '<slug>', 'rest/of/path']
        if len(parts) < 2 or not parts[1]:
            raise Http404

        file_slug = parts[1]
        request_tenant = getattr(request, 'tenant', None)

        # Block if:
        #   • No active tenant on this request (admin subdomain / localhost direct)
        #   • Active tenant slug doesn't match the file's slug
        if request_tenant is None or request_tenant.slug != file_slug:
            return HttpResponseForbidden(
                "<h1>403 Forbidden</h1>"
                "<p>You do not have permission to access this file.</p>",
                content_type='text/html',
            )

    # Path is allowed — serve it normally via Django's static serve
    return serve(request, path, document_root=settings.MEDIA_ROOT)
