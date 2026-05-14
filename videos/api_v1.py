"""
api_v1.py — ClipLens External REST API  (v1)

All endpoints require a valid API key (X-API-Key or Authorization: Bearer).
See api_auth.py for authentication details.

Endpoints
---------
GET  /api/v1/search/           — search videos (and optionally photos)
GET  /api/v1/videos/<id>/      — video metadata + HLS URL
GET  /api/v1/videos/<id>/transcript/  — full transcript (VTT or JSON)
GET  /api/v1/videos/<id>/chapters/    — named chapters
POST /api/v1/videos/upload/    — upload a new video to a channel
"""

import json
import os
import uuid

from django.conf import settings
from django.db.models import Q
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .api_auth import (
    APIAuthError,
    authenticate_api_key,
    can_search,
    get_search_scope,
    has_permission,
    require_permission,
)
from .models import (
    APIKeyPermission,
    Channel,
    Photo,
    Subtitle,
    Video,
    VideoChapter,
    VideoSegment,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _err(message, status=400):
    return JsonResponse({'error': message}, status=status)


def _auth(request):
    """Authenticate and return (api_key, None) or (None, error_response)."""
    try:
        key = authenticate_api_key(request)
        return key, None
    except APIAuthError as e:
        return None, JsonResponse({'error': e.message}, status=e.status)


def _video_to_dict(video, request=None):
    """Serialize a Video to the standard API response shape."""
    site_url = getattr(settings, 'SITE_URL', '')
    thumb = ''
    if video.thumbnail:
        thumb = f'{site_url}/media/{video.thumbnail}'

    hls_url = ''
    if video.hls_path:
        hls_url = f'{site_url}/media/{video.hls_path}'

    return {
        'id':          str(video.id),
        'title':       video.title,
        'description': video.description or '',
        'duration':    video.duration,
        'channel': {
            'id':   str(video.channel.id),
            'name': video.channel.name,
            'slug': video.channel.slug,
        },
        'thumbnail_url': thumb,
        'hls_url':       hls_url,
        'status':        video.status,
        'visibility':    video.visibility,
        'tags':          video.tags or '',
        'created_at':    video.created_at.isoformat(),
        'ai_summary':    video.ai_summary or '',
    }


# ── Search ────────────────────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(['GET'])
def search(request):
    """
    GET /api/v1/search/?q=<query>[&limit=20][&offset=0]

    Required permission: search:global  OR  search:channel  OR  search:playlist

    Returns a list of matching videos. Each result includes a `match` object
    describing where the query matched (title, transcript, label, etc.)

    Response:
    {
      "count": 12,
      "results": [
        {
          "id": "uuid",
          "title": "...",
          "channel": {"id": ..., "name": ..., "slug": ...},
          "thumbnail_url": "...",
          "duration": 183,
          "match": {
            "source": "title|transcript|label|scene|chapter",
            "snippet": "...surrounding text..."
          }
        },
        ...
      ]
    }
    """
    api_key, err = _auth(request)
    if err:
        return err

    if not can_search(api_key):
        return _err('This key has no search permissions.', 403)

    q = request.GET.get('q', '').strip()
    if not q:
        return _err('Missing required parameter: q')

    try:
        limit  = max(1, min(100, int(request.GET.get('limit', 20))))
        offset = max(0, int(request.GET.get('offset', 0)))
    except (ValueError, TypeError):
        return _err('limit and offset must be integers.')

    scope     = get_search_scope(api_key)
    results   = []
    seen_ids  = set()

    def _base_qs():
        qs = Video.objects.filter(status='ready', visibility='public').select_related('channel')
        if not scope['is_global']:
            channel_filter  = Q(channel_id__in=scope['channel_ids']) if scope['channel_ids'] else Q()
            playlist_filter = Q(playlist_items__playlist_id__in=scope['playlist_ids']) if scope['playlist_ids'] else Q()
            qs = qs.filter(channel_filter | playlist_filter).distinct()
        return qs

    def _add(video, source, snippet=''):
        if video.id not in seen_ids:
            seen_ids.add(video.id)
            site_url = getattr(settings, 'SITE_URL', '')
            thumb = f'{site_url}/media/{video.thumbnail}' if video.thumbnail else ''
            results.append({
                'id':            str(video.id),
                'title':         video.title,
                'description':   (video.description or '')[:200],
                'duration':      video.duration,
                'channel': {
                    'id':   str(video.channel.id),
                    'name': video.channel.name,
                    'slug': video.channel.slug,
                },
                'thumbnail_url': thumb,
                'status':        video.status,
                'visibility':    video.visibility,
                'created_at':    video.created_at.isoformat(),
                'match': {
                    'source':  source,
                    'snippet': snippet[:300],
                },
            })

    base = _base_qs()
    q_lower = q.lower()

    # 1. Title / tags
    for v in base.filter(Q(title__icontains=q) | Q(tags__icontains=q))[:50]:
        _add(v, 'title', v.title)

    # 2. Chapter names
    from .models import VideoChapter
    chapter_video_ids = (
        VideoChapter.objects
        .filter(video__in=base, title__icontains=q)
        .values_list('video_id', flat=True)
        .distinct()[:30]
    )
    for v in base.filter(id__in=chapter_video_ids):
        ch = VideoChapter.objects.filter(video=v, title__icontains=q).first()
        _add(v, 'chapter', ch.title if ch else '')

    # 3. Transcript
    from .models import VideoSegment
    seg_video_ids = (
        VideoSegment.objects
        .filter(video__in=base, text__icontains=q)
        .values_list('video_id', flat=True)
        .distinct()[:50]
    )
    for v in base.filter(id__in=seg_video_ids):
        seg = VideoSegment.objects.filter(video=v, text__icontains=q).first()
        _add(v, 'transcript', seg.text[:200] if seg else '')

    # 4. YOLO object labels
    from .models import VideoFrame
    label_video_ids = (
        VideoFrame.objects
        .filter(video__in=base, labels__icontains=q)
        .values_list('video_id', flat=True)
        .distinct()[:30]
    )
    for v in base.filter(id__in=label_video_ids):
        _add(v, 'label', '')

    # 5. Scene descriptions
    scene_video_ids = (
        VideoFrame.objects
        .filter(video__in=base, description__icontains=q)
        .values_list('video_id', flat=True)
        .distinct()[:30]
    )
    for v in base.filter(id__in=scene_video_ids):
        fr = VideoFrame.objects.filter(video=v, description__icontains=q).first()
        _add(v, 'scene', fr.description[:200] if fr else '')

    total = len(results)
    page  = results[offset: offset + limit]

    return JsonResponse({'count': total, 'results': page})


# ── Video detail ──────────────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(['GET'])
def video_detail(request, video_id):
    """
    GET /api/v1/videos/<uuid>/

    Required permission: video:read

    Returns full video metadata including HLS URL and AI summary.
    """
    api_key, err = _auth(request)
    if err:
        return err

    if not has_permission(api_key, APIKeyPermission.PERM_VIDEO_READ):
        return _err('This key does not have the "video:read" permission.', 403)

    try:
        video = Video.objects.select_related('channel').get(pk=video_id, status='ready')
    except Video.DoesNotExist:
        return _err('Video not found.', 404)

    return JsonResponse(_video_to_dict(video, request))


# ── Transcript ────────────────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(['GET'])
def video_transcript(request, video_id):
    """
    GET /api/v1/videos/<uuid>/transcript/?format=json|vtt

    Required permission: video:read

    Returns the full transcript.
    format=json (default) — array of {start, end, text, speaker}
    format=vtt  — raw VTT subtitle file content
    """
    api_key, err = _auth(request)
    if err:
        return err

    if not has_permission(api_key, APIKeyPermission.PERM_VIDEO_READ):
        return _err('This key does not have the "video:read" permission.', 403)

    try:
        video = Video.objects.get(pk=video_id)
    except Video.DoesNotExist:
        return _err('Video not found.', 404)

    fmt = request.GET.get('format', 'json').lower()

    if fmt == 'vtt':
        # Return the first English subtitle track's VTT content
        sub = Subtitle.objects.filter(video=video, language='en').first()
        if not sub:
            sub = Subtitle.objects.filter(video=video).first()
        if not sub:
            return _err('No subtitles available for this video.', 404)
        from django.http import HttpResponse
        return HttpResponse(sub.content, content_type='text/vtt')

    # JSON format — VideoSegment rows
    segments = (
        VideoSegment.objects
        .filter(video=video)
        .order_by('start_seconds')
        .values('start_seconds', 'end_seconds', 'text', 'speaker_label')
    )
    data = [
        {
            'start':   s['start_seconds'],
            'end':     s['end_seconds'],
            'text':    s['text'],
            'speaker': s['speaker_label'] or '',
        }
        for s in segments
    ]
    return JsonResponse({'video_id': str(video_id), 'segments': data})


# ── Chapters ──────────────────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(['GET'])
def video_chapters(request, video_id):
    """
    GET /api/v1/videos/<uuid>/chapters/

    Required permission: video:read

    Returns named chapters with timestamps.
    """
    api_key, err = _auth(request)
    if err:
        return err

    if not has_permission(api_key, APIKeyPermission.PERM_VIDEO_READ):
        return _err('This key does not have the "video:read" permission.', 403)

    try:
        video = Video.objects.get(pk=video_id)
    except Video.DoesNotExist:
        return _err('Video not found.', 404)

    chapters = list(
        VideoChapter.objects
        .filter(video=video)
        .order_by('start_seconds')
        .values('title', 'start_seconds', 'end_seconds', 'description')
    )
    return JsonResponse({'video_id': str(video_id), 'chapters': chapters})


# ── Upload ────────────────────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(['POST'])
def video_upload(request):
    """
    POST /api/v1/videos/upload/

    Required permission: video:upload  (scoped to a channel)

    Multipart form fields:
      file        — video file (required)
      title       — video title (required)
      channel_id  — UUID of target channel (required — must match key scope)
      description — optional
      tags        — optional comma-separated
      visibility  — public | private | unlisted  (default: private)

    Returns the created video object (status will be 'processing').
    """
    api_key, err = _auth(request)
    if err:
        return err

    channel_id = request.POST.get('channel_id', '').strip()
    if not channel_id:
        return _err('channel_id is required.')

    if not has_permission(api_key, APIKeyPermission.PERM_VIDEO_UPLOAD, scope_id=channel_id):
        return _err(
            f'This key does not have "video:upload" permission for channel {channel_id}.',
            403,
        )

    try:
        channel = Channel.objects.get(pk=channel_id)
    except Channel.DoesNotExist:
        return _err('Channel not found.', 404)

    title = request.POST.get('title', '').strip()
    if not title:
        return _err('title is required.')

    if 'file' not in request.FILES:
        return _err('file is required.')

    video_file  = request.FILES['file']
    description = request.POST.get('description', '')
    tags        = request.POST.get('tags', '')
    visibility  = request.POST.get('visibility', 'private')
    if visibility not in ('public', 'private', 'unlisted'):
        visibility = 'private'

    video = Video(
        title=title,
        description=description,
        tags=tags,
        channel=channel,
        visibility=visibility,
        status='uploading',
        original_file=video_file,
    )
    video.save()

    # Kick off the processing pipeline
    from .tasks import process_video_task
    process_video_task.delay(str(video.id))

    return JsonResponse(_video_to_dict(video), status=201)
