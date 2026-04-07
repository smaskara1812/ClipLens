from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.views.static import serve
from django.urls import re_path
from django.contrib.auth import views as auth_views

urlpatterns = [
    path('admin/', admin.site.urls),

    # ── Authentication ──────────────────────────────────────────────────────────
    path('login/',  auth_views.LoginView.as_view(template_name='registration/login.html'),  name='login'),
    path('logout/', auth_views.LogoutView.as_view(next_page='/player/'), name='logout'),

    path('', include('videos.urls')),

    # Serve media files in all environments (dev + production)
    re_path(r'^media/(?P<path>.*)$', serve, {'document_root': settings.MEDIA_ROOT}),
]

if settings.DEBUG:
    import debug_toolbar
    urlpatterns = [path('__debug__/', include(debug_toolbar.urls))] + urlpatterns
