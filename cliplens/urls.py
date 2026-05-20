from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.views.static import serve
from django.urls import re_path
from django.contrib.auth import views as auth_views
from videos.auth_views import DebugLoginView
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/schema/', SpectacularAPIView.as_view(), name='api_schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='api_schema'), name='api_docs'),

    # ── Authentication ──────────────────────────────────────────────────────────
    path('login/', DebugLoginView.as_view(), name='login'),
    path('logout/', auth_views.LogoutView.as_view(next_page='/player/'), name='logout'),

    # ── Control plane (accessible at admin.cliplens.* or /platform/ locally) ──
    path('platform/', include('tenants.urls', namespace='tenants')),

    path('', include('videos.urls')),

    # Serve media files in all environments (dev + production)
    re_path(r'^media/(?P<path>.*)$', serve, {'document_root': settings.MEDIA_ROOT}),
]

if settings.DEBUG:
    import debug_toolbar
    urlpatterns = [path('__debug__/', include(debug_toolbar.urls))] + urlpatterns
