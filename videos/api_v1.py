"""
api_v1.py — ClipLens External REST API  (v1)

All endpoints require a valid API key (X-API-Key or Authorization: Bearer).
See api_auth.py for authentication details.

Endpoints
---------
GET  /api/v1/search/                       — full 9-pass search
GET  /api/v1/videos/<id>/                  — video metadata + HLS URL
GET  /api/v1/videos/<id>/transcript/       — full transcript (VTT or JSON)
GET  /api/v1/videos/<id>/chapters/         — named chapters
POST /api/v1/videos/upload/                — upload a new video to a channel

Search passes (all applied when the key has permission):
  1. Title + tags          (icontains + fuzzy)
  2. Chapter names         (icontains)
  3. Transcript segments   (icontains)
  4. YOLO object labels    (icontains)
  5. Scene descriptions    (icontains)
  6. CLIP semantic         (pgvector cosine similarity — requires CLIP_ENABLED)
  7. Face identity names   (icontains)
  8. Photos                (title, tags, labels — global scope only)
  9. Named places          (name + description — global scope only)
"""

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
)
from .models import (
    APIKeyPermission,
    Channel,
    DetectedFace,
    FaceIdentity,
    NamedPlace,
    Photo,
    Subtitle,
    Video,
    VideoChapter,
    VideoFrame,
    VideoSegment,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _err(message, status=400):
    return JsonResponse({'error': message}, status=status)


def _auth(request):
    try:
        key = authenticate_api_key(request)
        return key, None
    except APIAuthError as e:
        return None, JsonResponse({'error': e.message}, status=e.status)


def _site_url():
    return getattr(settings, 'SITE_URL', '').rstrip('/')


def _video_to_dict(video):
    site = _site_url()
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
        'thumbnail_url': f'{site}/media/{video.thumbnail}' if video.thumbnail else '',
        'hls_url':       f'{site}/media/{video.hls_path}' if video.hls_path else '',
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

    Required permission: search:global OR search:channel OR search:playlist

    Runs up to 9 search passes across the library:
      1. Video title + tags
      2. Chapter names
      3. Transcript (speech)
      4. YOLO object labels
      5. Scene descriptions (BLIP/Florence-2)
      6. CLIP semantic (visual embedding)
      7. Face identity names
      8. Photos  (global scope only)
      9. Named places  (global scope only)

    Scoped keys (search:channel / search:playlist) run passes 1–7
    restricted to videos in the permitted channel(s)/playlist(s).
    Passes 8–9 require search:global.

    Response:
    {
      "count": <int>,
      "results": [
        {
          "id": "<uuid>",
          "title": "...",
          "type": "video" | "photo" | "place",
          "channel": {"id": ..., "name": ..., "slug": ...},
          "thumbnail_url": "...",
          "duration": <seconds | null>,
          "created_at": "ISO8601",
          "match": {"source": "title|chapter|transcript|label|scene|clip|face|photo|place",
                    "snippet": "..."}
        }
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
        limit  = max(1, min(100, int(request.GET.get('limit',  20))))
        offset = max(0,          int(request.GET.get('offset',  0)))
    except (ValueError, TypeError):
        return _err('limit and offset must be integers.')

    scope    = get_search_scope(api_key)
    results  = []
    seen_ids = set()           # prevents duplicates across passes
    site     = _site_url()

    # ── Scope helpers ─────────────────────────────────────────────────────
    def _video_base():
        """Base Video queryset filtered to scope and ready/public."""
        qs = (
            Video.objects
            .filter(status='ready', visibility='public')
            .select_related('channel')
        )
        if not scope['is_global']:
            channel_q  = Q(channel_id__in=scope['channel_ids'])  if scope['channel_ids']  else Q()
            playlist_q = Q(playlist_items__playlist_id__in=scope['playlist_ids']) if scope['playlist_ids'] else Q()
            qs = qs.filter(channel_q | playlist_q).distinct()
        return qs

    def _frame_base():
        """Base VideoFrame queryset scoped to permitted videos."""
        qs = VideoFrame.objects.filter(
            video__status='ready', video__visibility='public'
        ).select_related('video', 'video__channel')
        if not scope['is_global']:
            channel_q  = Q(video__channel_id__in=scope['channel_ids'])  if scope['channel_ids']  else Q()
            playlist_q = Q(video__playlist_items__playlist_id__in=scope['playlist_ids']) if scope['playlist_ids'] else Q()
            qs = qs.filter(channel_q | playlist_q).distinct()
        return qs

    def _seg_base():
        """Base VideoSegment queryset scoped to permitted videos."""
        qs = VideoSegment.objects.filter(
            video__status='ready', video__visibility='public'
        ).select_related('video', 'video__channel')
        if not scope['is_global']:
            channel_q  = Q(video__channel_id__in=scope['channel_ids'])  if scope['channel_ids']  else Q()
            playlist_q = Q(video__playlist_items__playlist_id__in=scope['playlist_ids']) if scope['playlist_ids'] else Q()
            qs = qs.filter(channel_q | playlist_q).distinct()
        return qs

    # ── Result builder ────────────────────────────────────────────────────
    def _add_video(video, source, snippet=''):
        vid_key = f'v:{video.id}'
        if vid_key in seen_ids:
            return
        seen_ids.add(vid_key)
        thumb = f'{site}/media/{video.thumbnail}' if video.thumbnail else ''
        results.append({
            'id':            str(video.id),
            'type':          'video',
            'title':         video.title,
            'description':   (video.description or '')[:200],
            'duration':      video.duration,
            'channel': {
                'id':   str(video.channel.id),
                'name': video.channel.name,
                'slug': video.channel.slug,
            },
            'thumbnail_url': thumb,
            'created_at':    video.created_at.isoformat(),
            'match': {'source': source, 'snippet': snippet[:300]},
        })

    def _add_photo(photo, source, snippet=''):
        pk = f'p:{photo.id}'
        if pk in seen_ids:
            return
        seen_ids.add(pk)
        thumb = f'{site}/media/{photo.thumbnail}' if photo.thumbnail else ''
        results.append({
            'id':            str(photo.id),
            'type':          'photo',
            'title':         photo.title or '',
            'description':   '',
            'duration':      None,
            'channel': {
                'id':   str(photo.channel.id),
                'name': photo.channel.name,
                'slug': photo.channel.slug,
            } if photo.channel else {},
            'thumbnail_url': thumb,
            'created_at':    photo.created_at.isoformat(),
            'match': {'source': source, 'snippet': snippet[:300]},
        })

    def _add_place(place):
        pk = f'place:{place.id}'
        if pk in seen_ids:
            return
        seen_ids.add(pk)
        results.append({
            'id':            str(place.id),
            'type':          'place',
            'title':         place.name,
            'description':   (place.description or '')[:200],
            'duration':      None,
            'channel':       {},
            'thumbnail_url': '',
            'created_at':    place.created_at.isoformat() if hasattr(place, 'created_at') else '',
            'match': {'source': 'place', 'snippet': place.description[:200] if place.description else place.name},
        })

    base = _video_base()

    # ── Pass 1: Title + tags ──────────────────────────────────────────────
    for v in base.filter(Q(title__icontains=q) | Q(tags__icontains=q))[:50]:
        _add_video(v, 'title', v.title)

    # ── Pass 2: Chapter names ─────────────────────────────────────────────
    chapter_vids = (
        VideoChapter.objects
        .filter(video__in=base, title__icontains=q)
        .values_list('video_id', flat=True)
        .distinct()[:30]
    )
    for v in base.filter(id__in=chapter_vids):
        ch = VideoChapter.objects.filter(video=v, title__icontains=q).first()
        _add_video(v, 'chapter', ch.title if ch else '')

    # ── Pass 3: Transcript ────────────────────────────────────────────────
    seg_vids = (
        _seg_base()
        .filter(text__icontains=q)
        .values_list('video_id', flat=True)
        .distinct()[:50]
    )
    for v in base.filter(id__in=seg_vids):
        seg = VideoSegment.objects.filter(video=v, text__icontains=q).first()
        _add_video(v, 'transcript', seg.text[:200] if seg else '')

    # ── Pass 4: YOLO labels ───────────────────────────────────────────────
    label_vids = (
        _frame_base()
        .filter(labels__icontains=q)
        .values_list('video_id', flat=True)
        .distinct()[:30]
    )
    for v in base.filter(id__in=label_vids):
        fr = VideoFrame.objects.filter(video=v, labels__icontains=q).first()
        _add_video(v, 'label', fr.labels[:100] if fr and fr.labels else '')

    # ── Pass 5: Scene descriptions ────────────────────────────────────────
    scene_vids = (
        _frame_base()
        .filter(description__icontains=q)
        .values_list('video_id', flat=True)
        .distinct()[:30]
    )
    for v in base.filter(id__in=scene_vids):
        fr = VideoFrame.objects.filter(video=v, description__icontains=q).first()
        _add_video(v, 'scene', fr.description[:200] if fr and fr.description else '')

    # ── Pass 6: CLIP semantic (pgvector) ──────────────────────────────────
    if getattr(settings, 'CLIP_ENABLED', True):
        try:
            from pgvector.django import CosineDistance
            from .clip_utils import get_clip_text_vector
            txt_vec = get_clip_text_vector(q)
            if txt_vec is not None:
                threshold = getattr(settings, 'CLIP_SIMILARITY_THRESHOLD', 0.24)
                max_dist  = 1.0 - threshold
                clip_frames = (
                    _frame_base()
                    .exclude(clip_embedding=None)
                    .annotate(_dist=CosineDistance('clip_embedding', txt_vec))
                    .filter(_dist__lte=max_dist)
                    .order_by('_dist')
                    [:150]
                )
                clip_seen_vids = set()
                for frm in clip_frames:
                    vid_id = str(frm.video_id)
                    if vid_id not in clip_seen_vids:
                        clip_seen_vids.add(vid_id)
                        _add_video(
                            frm.video, 'clip',
                            frm.description or frm.labels or '',
                        )
        except Exception:
            pass

    # ── Pass 7: Face identity names ───────────────────────────────────────
    face_qs = (
        DetectedFace.objects
        .filter(
            video__status='ready',
            video__visibility='public',
            identity__name__icontains=q,
        )
        .exclude(identity__name='')
        .select_related('video', 'video__channel', 'identity')
    )
    if not scope['is_global']:
        channel_q  = Q(video__channel_id__in=scope['channel_ids'])  if scope['channel_ids']  else Q()
        playlist_q = Q(video__playlist_items__playlist_id__in=scope['playlist_ids']) if scope['playlist_ids'] else Q()
        face_qs    = face_qs.filter(channel_q | playlist_q)

    face_seen = set()
    for df in face_qs.order_by('identity_id')[:100]:
        vid_id = str(df.video_id)
        if vid_id not in face_seen:
            face_seen.add(vid_id)
            _add_video(df.video, 'face', df.identity.name)

    # ── Pass 8: Photos (global scope only) ───────────────────────────────
    if scope['is_global']:
        photo_qs = (
            Photo.objects
            .filter(status='ready', visibility='public')
            .select_related('channel')
            .filter(
                Q(title__icontains=q)
                | Q(tags__icontains=q)
                | Q(labels__icontains=q)
            )[:40]
        )
        for p in photo_qs:
            _add_photo(p, 'photo', p.title or '')

        # Photo CLIP
        if getattr(settings, 'CLIP_ENABLED', True):
            try:
                from pgvector.django import CosineDistance
                from .clip_utils import get_clip_text_vector
                txt_vec = get_clip_text_vector(q)
                if txt_vec is not None:
                    ph_threshold = getattr(settings, 'CLIP_PHOTO_SIMILARITY_THRESHOLD', 0.28)
                    clip_photos = (
                        Photo.objects
                        .filter(status='ready', visibility='public')
                        .exclude(clip_embedding=None)
                        .select_related('channel')
                        .annotate(_dist=CosineDistance('clip_embedding', txt_vec))
                        .filter(_dist__lte=1.0 - ph_threshold)
                        .order_by('_dist')
                        [:30]
                    )
                    for p in clip_photos:
                        _add_photo(p, 'clip_photo', '')
            except Exception:
                pass

    # ── Pass 9: Named places (global scope only) ──────────────────────────
    if scope['is_global']:
        for place in NamedPlace.objects.filter(
            Q(name__icontains=q) | Q(description__icontains=q)
        )[:20]:
            _add_place(place)

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

    return JsonResponse(_video_to_dict(video))


# ── Transcript ────────────────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(['GET'])
def video_transcript(request, video_id):
    """
    GET /api/v1/videos/<uuid>/transcript/?format=json|vtt
    Required permission: video:read
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
        sub = Subtitle.objects.filter(video=video, language='en').first()
        if not sub:
            sub = Subtitle.objects.filter(video=video).first()
        if not sub:
            return _err('No subtitles available for this video.', 404)
        from django.http import HttpResponse
        return HttpResponse(sub.content, content_type='text/vtt')

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
    Required permission: video:upload (scoped to one or more channels)

    Multipart:
      file        — video file (required)
      title       — video title (required)
      channel_id  — UUID of target channel.
                    Optional when the key is scoped to exactly ONE channel
                    (it is inferred automatically).
                    Required when the key can upload to multiple channels.
      description — optional
      tags        — optional comma-separated
      visibility  — public | private | unlisted  (default: private)
    """
    api_key, err = _auth(request)
    if err:
        return err

    # Resolve which channel to upload to
    channel_id = request.POST.get('channel_id', '').strip()

    # Collect all upload-scoped channel IDs for this key
    upload_scopes = list(
        api_key.permissions
        .filter(permission=APIKeyPermission.PERM_VIDEO_UPLOAD)
        .exclude(scope_id='')
        .values_list('scope_id', flat=True)
    )

    if not upload_scopes:
        return _err('This key does not have the "video:upload" permission.', 403)

    if not channel_id:
        # Auto-infer only when scoped to exactly one channel
        if len(upload_scopes) == 1:
            channel_id = upload_scopes[0]
        else:
            names = list(
                api_key.permissions
                .filter(permission=APIKeyPermission.PERM_VIDEO_UPLOAD)
                .exclude(scope_id='')
                .values_list('scope_name', flat=True)
            )
            return _err(
                f'This key can upload to multiple channels: {", ".join(names)}. '
                f'Specify channel_id in your request.'
            )

    if channel_id not in upload_scopes:
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

    from .tasks import process_video_task
    process_video_task.delay(str(video.id))

    return JsonResponse(_video_to_dict(video), status=201)
