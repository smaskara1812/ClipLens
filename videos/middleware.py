import re
from django.shortcuts import redirect
from django.conf import settings


class LoginRequiredMiddleware:
    """
    Forces all users to be authenticated before accessing any page.
    Exempt: login, register, embed pages, HLS stream API, health check, media files.
    """

    # Paths that are always public — regex patterns matched against request.path
    EXEMPT = re.compile(
        r'^('
        r'/login/'
        r'|/register/'
        r'|/embed/[^/]+/'          # public embed iframe
        r'|/api/videos/[^/]+/stream/'   # HLS manifest for embed players
        r'|/api/health/'
        r'|/media/'                # dev media file serving
        r'|/static/'
        r'|/admin/'                # Django admin has its own auth
        r'|/__debug__/'            # Django debug toolbar
        r'|/api/streams/on-publish/'    # mediamtx webhook — internal, has its own secret auth
        r'|/api/streams/on-unpublish/'  # mediamtx webhook — internal, has its own secret auth
        r'|/api/channels/[^/]+/live/'  # live status polling — public (shows LIVE badge)
        r'|/api/streams/'              # live streams list — public
        r'|/live/\d+/'                 # live watch page — public
        r'|/api/v1/'                   # external API — has its own key-based auth
        r'|/onboard/'                  # tenant onboarding flow — claimed via invite token
        r'|/api/stripe/webhook/'       # Stripe webhook — signature-verified
        r'|/$'                         # bare root → marketing landing (decides auth itself)
        r'|/contact/'                  # public contact form submission
        r')'
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.user.is_authenticated and not self.EXEMPT.match(request.path):
            login_url = settings.LOGIN_URL
            next_url  = request.get_full_path()
            return redirect(f'{login_url}?next={next_url}')
        return self.get_response(request)


class HLSHeadersMiddleware:
    """
    Adds correct CORS + Content-Type headers for HLS media files (.m3u8, .ts).
    Django's dev static/media server does not do this by default, which breaks
    Safari (strict CORS) and any cross-origin HLS player.
    """

    HLS_TYPES = {
        '.m3u8': 'application/vnd.apple.mpegurl',
        '.ts':   'video/mp2t',
    }

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        path = request.path.lower()

        for ext, content_type in self.HLS_TYPES.items():
            if path.endswith(ext):
                response['Content-Type'] = content_type
                response['Access-Control-Allow-Origin'] = '*'
                response['Access-Control-Allow-Methods'] = 'GET, HEAD, OPTIONS'
                response['Access-Control-Allow-Headers'] = '*'
                # Disable range-request caching issues on Safari
                response['Accept-Ranges'] = 'bytes'
                break

        return response
