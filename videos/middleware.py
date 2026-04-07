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
