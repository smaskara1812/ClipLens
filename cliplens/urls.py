from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.urls import re_path
from django.contrib.auth import views as auth_views
from videos.auth_views import DebugLoginView
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from tenants.media_serve import protected_media
from tenants.views import (
    onboard as tenant_onboard,
    onboard_success as tenant_onboard_success,
    stripe_webhook as tenant_stripe_webhook,
    landing_page as tenant_landing_page,
    submit_lead as tenant_submit_lead,
)

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/schema/', SpectacularAPIView.as_view(), name='api_schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='api_schema'), name='api_docs'),

    # ── Authentication ──────────────────────────────────────────────────────────
    path('login/', DebugLoginView.as_view(), name='login'),
    path('logout/', auth_views.LogoutView.as_view(next_page='/player/'), name='logout'),

    # ── Onboarding — reachable at the TENANT subdomain (orgX.cliplens.local/onboard/<token>/)
    path('onboard/<str:token>/', tenant_onboard, name='onboard'),
    path('onboard/<str:token>/success/', tenant_onboard_success, name='onboard_success'),

    # ── Stripe webhook (no auth, signature-verified inside)
    path('api/stripe/webhook/', tenant_stripe_webhook, name='stripe_webhook'),

    # ── Public marketing site (only matched at the BARE root domain)
    # tenant subdomain hits / → middleware sets request.tenant → landing_page
    # forwards to /player/ so the app still works at orgX.cliplens.local/
    path('', tenant_landing_page, name='landing'),
    path('contact/', tenant_submit_lead, name='submit_lead'),

    # ── Control plane (accessible at admin.cliplens.* or /platform/ locally) ──
    path('platform/', include('tenants.urls', namespace='tenants')),

    path('', include('videos.urls')),

    # Serve media files — tenant-aware in MULTI_TENANT mode, open in single-tenant
    re_path(r'^media/(?P<path>.*)$', protected_media),
]

if settings.DEBUG:
    import debug_toolbar
    urlpatterns = [path('__debug__/', include(debug_toolbar.urls))] + urlpatterns
