from __future__ import annotations

from django.contrib.auth.forms import AuthenticationForm


class TrimmedAuthenticationForm(AuthenticationForm):
    """
    Chrome copy/paste / password managers sometimes include invisible whitespace
    (leading/trailing spaces/newlines). Django does not strip passwords, so this
    can cause "correct password" reports that fail only in some browsers.
    """

    def clean(self):
        if "username" in self.data:
            self.data = self.data.copy()
            self.data["username"] = (self.data.get("username") or "").strip()
            self.data["password"] = (self.data.get("password") or "").strip()
        return super().clean()

