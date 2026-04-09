from __future__ import annotations

import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.views import LoginView

from .forms import TrimmedAuthenticationForm

logger = logging.getLogger("videos.auth")


class DebugLoginView(LoginView):
    template_name = "registration/login.html"
    form_class = TrimmedAuthenticationForm

    def form_invalid(self, form):
        if settings.DEBUG:
            username = (self.request.POST.get("username") or "").strip()
            password = self.request.POST.get("password") or ""
            User = get_user_model()
            user_exists = User.objects.filter(username=username).exists() if username else False
            logger.info(
                "Login failed (user_exists=%s, username_len=%s, password_len=%s, origin=%s)",
                user_exists,
                len(username),
                len(password),
                self.request.headers.get("Origin"),
            )
        return super().form_invalid(form)

