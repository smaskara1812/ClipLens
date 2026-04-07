from rest_framework.authentication import SessionAuthentication


class CsrfExemptSessionAuthentication(SessionAuthentication):
    """
    Standard DRF SessionAuthentication but with CSRF enforcement removed.

    Why: DRF's default SessionAuthentication rejects requests that don't carry
    a CSRF token.  Our API is called from our own templates via XHR / fetch, so
    the browser sends the session cookie automatically (same-origin), but we
    don't want to manage CSRF tokens in JavaScript.  Removing the CSRF check is
    safe here because:
      - CORS is locked to trusted origins (or we rely on same-origin policy).
      - All state-changing endpoints are additionally protected by
        @api_login_required which checks request.user.is_authenticated.
    """

    def enforce_csrf(self, request):
        return  # skip CSRF check — intentional
