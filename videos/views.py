import threading
import shutil
import logging

# Module-level CLIP cache — loaded once on first search, reused for all subsequent searches
_clip_model_cache = None
_clip_proc_cache  = None
_clip_cache_lock  = threading.Lock()

_task_logger = logging.getLogger('videos.tasks')
from functools import wraps
from pathlib import Path

from django.conf import settings
from django.conf import settings as django_settings
from django.core.cache import cache
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.apps import apps
from django.db import connection
from django.db.models import Q, Sum, Count, F
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.text import slugify
from django.views.decorators.clickjacking import xframe_options_exempt
from rest_framework import status
from rest_framework.decorators import api_view, parser_classes
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.response import Response

from .models import (
    Video, Category, Channel, ChannelLink, ChannelSubscription,
    Comment, CommentLike, VideoChapter, VideoLike,
    Playlist, PlaylistItem, WatchHistory, SavedVideo,
    WatchTimeEntry, EndScreen, Notification, Subtitle, AudioTrack, VideoSegment, VideoFrame,
    FaceIdentity, DetectedFace, UserProfile, Photo, Album, AlbumPhoto,
    SpeakerIdentity,
)
from .serializers import (
    VideoListSerializer, VideoDetailSerializer, VideoUploadSerializer,
    VideoFeedSerializer,
    CategorySerializer, ChannelSerializer, ChannelLinkSerializer,
    CommentSerializer, VideoChapterSerializer,
    PlaylistSerializer, PlaylistListSerializer, PlaylistItemSerializer,
    WatchHistorySerializer, SavedVideoSerializer, NotificationSerializer,
    EndScreenSerializer, SubtitleSerializer, AudioTrackSerializer, VideoFrameSerializer,
    FaceIdentitySerializer, DetectedFaceSerializer,
)
from .services import process_video
from . import tasks as _tasks


def _highlight_query(text: str, raw_query: str) -> str:
    """
    Wrap query terms inside *text* with <mark> tags for display in search results.
    HTML-escapes the source text first so the output is safe to render with |safe.
    Handles both whole-word and substring matches (important for fuzzy hits like
    "essar" inside "necessary").  Returns plain escaped text if nothing matches.
    Caller must use {{ value|safe }} in the template.
    """
    import re as _re
    from django.utils.html import escape as _esc
    if not text or not raw_query:
        return _esc(text or '')
    safe = _esc(text)
    # Build pattern from individual terms, longest first to avoid partial overlaps
    terms = [t for t in raw_query.split() if len(t) >= 2]
    if not terms:
        return safe
    pattern = '|'.join(_re.escape(t) for t in sorted(terms, key=len, reverse=True))
    return _re.sub(f'({pattern})', r'<mark>\1</mark>', safe, flags=_re.IGNORECASE)


def _fmt_seconds_display(seconds: float) -> str:
    """Convert float seconds → human-readable M:SS or H:MM:SS string."""
    total = int(seconds)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    if h:
        return f'{h}:{m:02d}:{s:02d}'
    return f'{m}:{s:02d}'


def _apply_ai_video_filters(qs, ch_slug: str, cat_slug: str, duration_filter: str, date_filter: str):
    """
    Apply the same channel / category / duration / date filters that scope the main video
    grid to a VideoFrame or VideoSegment queryset (which reaches Video via a FK named 'video').
    Called for every AI sub-search so that semantic/CLIP/speech/YOLO results honour the
    same refinements the user picked in the filter bar.
    """
    if ch_slug:
        qs = qs.filter(video__channel__slug=ch_slug)
    if cat_slug:
        qs = qs.filter(video__category__slug=cat_slug)
    if duration_filter == 'short':
        qs = qs.filter(video__duration__lt=240)
    elif duration_filter == 'medium':
        qs = qs.filter(video__duration__gte=240, video__duration__lt=1200)
    elif duration_filter == 'long':
        qs = qs.filter(video__duration__gte=1200)
    if date_filter:
        from django.utils import timezone as _tz
        import datetime as _dt
        _now = _tz.now()
        if date_filter == 'today':
            qs = qs.filter(video__created_at__date=_now.date())
        elif date_filter == 'week':
            qs = qs.filter(video__created_at__gte=_now - _dt.timedelta(days=7))
        elif date_filter == 'month':
            qs = qs.filter(video__created_at__gte=_now - _dt.timedelta(days=30))
        elif date_filter == 'year':
            qs = qs.filter(video__created_at__gte=_now - _dt.timedelta(days=365))
    return qs


def _can_use_postgres_fts() -> bool:
    """Postgres full-text lookups require the DB backend *and* django.contrib.postgres."""
    return (
        connection.vendor == 'postgresql'
        and apps.is_installed('django.contrib.postgres')
    )


def _q_icontains_all_terms(field_name: str, raw: str) -> Q:
    """AND of icontains for each token — portable substitute for Postgres full-text search."""
    combined = Q()
    any_term = False
    for term in raw.split():
        term = term.strip()
        if not term:
            continue
        any_term = True
        combined &= Q(**{f'{field_name}__icontains': term})
    if not any_term:
        return Q(pk__in=[])
    return combined


def _can_use_fuzzy_search() -> bool:
    """Enable fuzzy search only when Postgres + django.contrib.postgres are available."""
    return (
        getattr(settings, 'FUZZY_SEARCH_ENABLED', True)
        and _can_use_postgres_fts()
    )


def _postgres_fuzzy_filter(qs, raw: str, fields: tuple[str, ...], threshold: float = None):
    """
    Fuzzy text match via pg_trgm TrigramWordSimilarity.

    TrigramWordSimilarity scores the *best matching word* inside the field value
    against the query — so searching "essar" will match "necessary" because the
    substring "essar" appears inside that word.  Plain TrigramSimilarity compares
    the whole field string to the query and scores far too low for short queries
    against long text fields.
    """
    if not _can_use_fuzzy_search() or not raw or not fields:
        return qs.none()
    try:
        from django.contrib.postgres.search import TrigramWordSimilarity
        from django.db.models.functions import Greatest
    except Exception:
        return qs.none()

    # Short queries (≤ 3 chars) have only 4 trigrams total, so coincidental
    # overlaps are unavoidable at 0.35.  e.g. "cow" shares " c" + "co" with
    # "course" / "couch" / "conduct" → score 0.36, false match.
    # For short queries we skip fuzzy entirely and rely on FTS + icontains.
    if len(raw.strip()) <= 3:
        return qs.none()

    # 0.35 is deliberately higher than 0.22:
    #   • "essar" → "necessary" scores ~0.60 — still matches ✓
    #   • "animals" → "animated" scores ~0.57 — still matches (user typo scenario)
    #   • Truly unrelated words score < 0.30 — filtered out ✓
    # Long AI-generated text fields (scene_description, ocr_text) should NOT be
    # passed here at all; FTS with stemming handles them correctly.
    if threshold is None:
        threshold = float(getattr(settings, 'FUZZY_SEARCH_SIMILARITY_THRESHOLD', 0.35))
    similarity_expr = None
    for field in fields:
        expr = TrigramWordSimilarity(raw, field)
        similarity_expr = expr if similarity_expr is None else Greatest(similarity_expr, expr)
    if similarity_expr is None:
        return qs.none()

    return (
        qs.annotate(_fuzzy_sim=similarity_expr)
        .filter(_fuzzy_sim__gte=threshold)
        .order_by('-_fuzzy_sim', '-pk')
    )


def _dispatch_process_video(video_id: str):
    """
    Send video processing to Celery if the broker is reachable,
    otherwise fall back to a daemon thread so dev works without Redis.
    """
    try:
        _tasks.process_video_task.apply_async(
            args=[str(video_id)],
            queue='processing',
        )
        _task_logger.info(f'Dispatched process_video_task via Celery for {video_id}')
    except Exception as exc:
        _task_logger.warning(
            f'Celery unavailable ({exc}), falling back to thread for {video_id}'
        )
        threading.Thread(
            target=process_video, args=(str(video_id),), daemon=True
        ).start()


# ─── Helper decorators ───────────────────────────────────────────────────────

def api_login_required(view_func):
    """Returns HTTP 401 JSON instead of redirecting to login."""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return Response(
                {'error': 'Authentication required. Please log in.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        return view_func(request, *args, **kwargs)
    return wrapper


def superuser_required(view_func):
    """Redirect non-superadmins to home."""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect(f'/login/?next={request.path}')
        if not _is_superadmin(request.user):
            raise Http404
        return view_func(request, *args, **kwargs)
    return wrapper


def editor_required(view_func):
    """Redirect non-editors (viewers / anonymous) to home with a message."""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect(f'/login/?next={request.path}')
        if not _is_editor(request.user):
            return redirect('player')
        return view_func(request, *args, **kwargs)
    return wrapper


# ─── RBAC helpers ────────────────────────────────────────────────────────────

def _is_editor(user) -> bool:
    """True for editors and superadmins."""
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    try:
        return user.profile.is_editor
    except Exception:
        return False


def _is_superadmin(user) -> bool:
    """True only for superadmins."""
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    try:
        return user.profile.is_superadmin
    except Exception:
        return False


def _user_channels(user):
    """All channels the user can manage — primary owner OR co-editor."""
    if not user or not user.is_authenticated:
        return Channel.objects.none()
    from django.db.models import Q
    return Channel.objects.filter(
        Q(owner=user) | Q(editors=user)
    ).distinct().order_by('name')


def _user_channel(user):
    """First channel the user can manage, or None."""
    return _user_channels(user).first()


def _is_channel_owner(user, channel) -> bool:
    """True if the user is the primary owner or a co-editor of this channel."""
    if not user or not user.is_authenticated or not channel:
        return False
    if channel.owner_id and channel.owner_id == user.pk:
        return True
    return channel.editors.filter(pk=user.pk).exists()


def _is_video_owner(user, video) -> bool:
    """True if the user can manage the channel this video belongs to."""
    if not user or not user.is_authenticated or not video.channel:
        return False
    return _is_channel_owner(user, video.channel)


# ─── Auth / onboarding helpers ───────────────────────────────────────────────

def _username_suggestions(base):
    suggestions = []
    for i in range(1, 20):
        candidate = f'{base}{i}'
        if not User.objects.filter(username=candidate).exists():
            suggestions.append(candidate)
        if len(suggestions) == 3:
            break
    return suggestions


def _channel_name_suggestions(base):
    suggestions = []
    for i in range(1, 20):
        candidate = f'{base} {i}'
        if not Channel.objects.filter(name=candidate).exists():
            suggestions.append(candidate)
        if len(suggestions) == 3:
            break
    return suggestions


def _make_unique_slug(base_name):
    slug = slugify(base_name)
    base_slug = slug
    counter = 1
    while Channel.objects.filter(slug=slug).exists():
        slug = f'{base_slug}-{counter}'
        counter += 1
    return slug


# ─── Auth pages ──────────────────────────────────────────────────────────────

def register_page(request):
    if request.user.is_authenticated:
        return redirect('player')

    errors = []
    username_suggestions = []

    if request.method == 'POST':
        username  = request.POST.get('username', '').strip()
        password1 = request.POST.get('password1', '')
        password2 = request.POST.get('password2', '')

        if not username:
            errors.append('Username is required.')
        elif len(username) < 3:
            errors.append('Username must be at least 3 characters.')
        elif User.objects.filter(username__iexact=username).exists():
            errors.append(f'The username "{username}" is already taken.')
            username_suggestions = _username_suggestions(username)

        if not password1:
            errors.append('Password is required.')
        elif password1 != password2:
            errors.append('Passwords do not match.')
        elif len(password1) < 8:
            errors.append('Password must be at least 8 characters.')

        if not errors:
            user = User.objects.create_user(username=username, password=password1)
            # UserProfile is created by signal (role=viewer by default)
            login(request, user)
            return redirect('player')

    return render(request, 'registration/register.html', {
        'errors': errors,
        'username_suggestions': username_suggestions,
    })


@editor_required
def setup_channel_page(request):
    errors = []
    suggestions = []

    if request.method == 'POST':
        channel_name = request.POST.get('channel_name', '').strip()
        if not channel_name:
            errors.append('Channel name is required.')
        elif Channel.objects.filter(name__iexact=channel_name).exists():
            errors.append(f'The channel name "{channel_name}" is already taken.')
            suggestions = _channel_name_suggestions(channel_name)

        if not errors:
            Channel.objects.create(
                owner=request.user,
                name=channel_name,
                slug=_make_unique_slug(channel_name),
            )
            return redirect('upload')

    return render(request, 'videos/setup_channel.html', {
        'errors': errors,
        'suggestions': suggestions,
    })


# ─── Frontend pages ──────────────────────────────────────────────────────────

@editor_required
def upload_page(request):
    channels = list(_user_channels(request.user))
    request._owned_channels_cache = channels
    if not channels:
        return redirect('setup_channel')
    categories = Category.objects.all()
    return render(request, 'videos/upload.html', {'channels': channels, 'categories': categories})


def player_page(request):
    videos = Video.objects.filter(
        visibility=Video.VISIBILITY_PUBLIC, status=Video.STATUS_READY
    ).select_related('channel', 'category')

    ch_slug = request.GET.get('channel')
    if ch_slug:
        videos = videos.filter(channel__slug=ch_slug)

    q           = request.GET.get('q', '').strip()
    use_semantic = request.GET.get('semantic') == '1'
    if q:
        if _can_use_postgres_fts():
            from django.contrib.postgres.search import SearchQuery, SearchVector
            _sq = SearchQuery(q, config='english', search_type='plain')
            _fts_qs = videos.annotate(
                _search=SearchVector('title', 'description', 'tags', config='english')
            ).filter(_search=_sq).distinct()
            _fuzzy_qs = _postgres_fuzzy_filter(videos, q, ('title', 'description', 'tags'))
            videos = videos.filter(
                Q(pk__in=_fts_qs.values('pk')) | Q(pk__in=_fuzzy_qs.values('pk'))
            ).distinct()
        else:
            videos = videos.filter(
                _q_icontains_all_terms('title', q)
                | _q_icontains_all_terms('description', q)
                | _q_icontains_all_terms('tags', q)
            ).distinct()

    # Search filters
    cat_slug = request.GET.get('category', '').strip()
    if cat_slug:
        videos = videos.filter(category__slug=cat_slug)

    duration_filter = request.GET.get('duration', '').strip()
    if duration_filter == 'short':
        videos = videos.filter(duration__lt=240)
    elif duration_filter == 'medium':
        videos = videos.filter(duration__gte=240, duration__lt=1200)
    elif duration_filter == 'long':
        videos = videos.filter(duration__gte=1200)

    date_filter = request.GET.get('date', '').strip()
    if date_filter:
        from django.utils import timezone
        import datetime
        now = timezone.now()
        if date_filter == 'today':
            videos = videos.filter(created_at__date=now.date())
        elif date_filter == 'week':
            videos = videos.filter(created_at__gte=now - datetime.timedelta(days=7))
        elif date_filter == 'month':
            videos = videos.filter(created_at__gte=now - datetime.timedelta(days=30))
        elif date_filter == 'year':
            videos = videos.filter(created_at__gte=now - datetime.timedelta(days=365))

    sort = request.GET.get('sort', '').strip()
    if sort == 'views':
        videos = videos.order_by('-views_count')
    elif sort == 'likes':
        videos = videos.order_by('-views_count')  # approximate; likes needs annotation
    elif sort == 'oldest':
        videos = videos.order_by('created_at')
    else:
        videos = videos.order_by('-created_at')

    subscribed_channels = []
    if request.user.is_authenticated:
        _sub_key = f'subs_{request.user.pk}'
        subscribed_channels = cache.get(_sub_key)
        if subscribed_channels is None:
            subscribed_channels = list(Channel.objects.filter(
                subscribers__user=request.user
            ).order_by('name'))
            cache.set(_sub_key, subscribed_channels, getattr(django_settings, 'CACHE_TTL_SUBSCRIPTIONS', 300))

    categories = cache.get('all_categories')
    if categories is None:
        categories = list(Category.objects.all())
        cache.set('all_categories', categories, getattr(django_settings, 'CACHE_TTL_CATEGORIES', 3600))

    unread_count = 0
    if request.user.is_authenticated:
        _unread_key = f'unread_{request.user.pk}'
        unread_count = cache.get(_unread_key)
        if unread_count is None:
            unread_count = Notification.objects.filter(recipient=request.user, is_read=False).count()
            cache.set(_unread_key, unread_count, getattr(django_settings, 'CACHE_TTL_UNREAD_COUNT', 60))

    # ── Paginate main feed (first 24 server-side; more via API infinite scroll) ──
    FEED_PAGE = 24
    # Fields needed by video cards in the template — thumbnail must be included
    # or accessing v.thumbnail_url triggers a deferred-field query per video (N+1).
    _CARD_FIELDS = (
        'id', 'title', 'thumbnail', 'duration', 'views_count', 'status',
        'created_at', 'channel_id', 'category_id',
    )

    feed_total_count = None
    if not q:
        feed_total_count = videos.count()
        videos = videos.only(*_CARD_FIELDS).select_related('channel')
        _peek = list(videos[:FEED_PAGE + 1])
        has_more_videos = len(_peek) > FEED_PAGE
        videos_page = _peek[:FEED_PAGE]
    else:
        videos = videos.only(*_CARD_FIELDS).select_related('channel')
        videos_page = list(videos[:100])
        has_more_videos = False

    # ── Chapter name search ───────────────────────────────────────────────────
    chapter_matches = []
    if q:
        _ch_qs = VideoChapter.objects.filter(
            video__visibility=Video.VISIBILITY_PUBLIC,
            video__status=Video.STATUS_READY,
        ).select_related('video', 'video__channel')
        _ch_qs = _apply_ai_video_filters(_ch_qs, ch_slug, cat_slug, duration_filter, date_filter)
        _fts_ch_pks = set()
        if _can_use_postgres_fts():
            from django.contrib.postgres.search import SearchQuery as _ChSQ
            _fts_ch_qs   = _ch_qs.filter(title__search=_ChSQ(q, config='english', search_type='plain'))
            _fts_ch_pks  = set(_fts_ch_qs.values_list('pk', flat=True)[:200])
            _fuzzy_ch_qs = _postgres_fuzzy_filter(_ch_qs, q, ('title',))
            _ch_qs = _ch_qs.filter(
                Q(pk__in=_fts_ch_pks) | Q(pk__in=_fuzzy_ch_qs.values('pk'))
            )
        else:
            _ch_qs = _ch_qs.filter(_q_icontains_all_terms('title', q))
            _fts_ch_pks = set(_ch_qs.values_list('pk', flat=True)[:200])
        seen_chapter_videos = {}
        for ch in _ch_qs.order_by('video_id', 'timestamp')[:200]:
            vid_id   = str(ch.video_id)
            is_exact = ch.pk in _fts_ch_pks
            if vid_id not in seen_chapter_videos:
                seen_chapter_videos[vid_id] = {'video': ch.video, 'moments': [], 'has_exact': False}
            entry = seen_chapter_videos[vid_id]
            if is_exact:
                entry['has_exact'] = True
            entry['moments'].append({
                'start':            ch.timestamp,
                'text':             ch.title,
                'highlighted_text': _highlight_query(ch.title, q),
                'time_fmt':         _fmt_seconds_display(ch.timestamp),
                'is_exact':         is_exact,
            })
        for entry in seen_chapter_videos.values():
            entry['moments'].sort(key=lambda m: (0 if m['is_exact'] else 1, m['start']))
        chapter_matches = sorted(
            seen_chapter_videos.values(),
            key=lambda v: (0 if v.get('has_exact') else 1, -len(v['moments'])),
        )

    # ── In-video speech search ────────────────────────────────────────────────
    segment_matches = []
    if q:
        _seg_qs = VideoSegment.objects.filter(
            video__visibility=Video.VISIBILITY_PUBLIC,
            video__status=Video.STATUS_READY,
        )
        # Apply channel / category / duration / date filters to speech search too
        _seg_qs = _apply_ai_video_filters(_seg_qs, ch_slug, cat_slug, duration_filter, date_filter)
        _fts_seg_pks = set()   # PKs of exact FTS hits — used for ordering
        if _can_use_postgres_fts():
            from django.contrib.postgres.search import SearchQuery as _SQ
            _fts_seg_qs = _seg_qs.filter(
                text__search=_SQ(q, config='english', search_type='plain')
            )
            _fts_seg_pks = set(_fts_seg_qs.values_list('pk', flat=True)[:500])
            # Speech text: FTS-only — same reasoning as scene_description.
            # Transcripts are long; word_similarity at any threshold produces
            # spurious matches (e.g. "soham" → "So how do we protect ourselves").
            # FTS with English stemming is already precise for spoken words.
            _seg_qs = _seg_qs.filter(pk__in=_fts_seg_pks)
        else:
            _seg_qs = _seg_qs.filter(_q_icontains_all_terms('text', q))
            # icontains results are all treated as "exact" for ordering purposes
            _fts_seg_pks = set(_seg_qs.values_list('pk', flat=True)[:500])
        # Cap: 500 segments across all videos; up to 30 moments stored per video
        matching_segments = (
            _seg_qs.select_related('video', 'video__channel')
            .order_by('video_id', 'start_seconds')[:500]
        )
        seen_videos = {}
        for seg in matching_segments:
            vid_id   = str(seg.video_id)
            is_exact = seg.pk in _fts_seg_pks
            if vid_id not in seen_videos:
                seen_videos[vid_id] = {'video': seg.video, 'moments': [], 'total_moments': 0, 'has_exact': False}
            entry = seen_videos[vid_id]
            entry['total_moments'] += 1
            if is_exact:
                entry['has_exact'] = True
            if len(entry['moments']) < 30:
                entry['moments'].append({
                    'start':            seg.start_seconds,
                    'end':              seg.end_seconds,
                    'text':             seg.text,
                    'highlighted_text': _highlight_query(seg.text, q),
                    'time_fmt':         _fmt_seconds_display(seg.start_seconds),
                    'is_exact':         is_exact,
                })
        # Sort: videos with ≥1 exact match first; within each video, exact moments before fuzzy
        for entry in seen_videos.values():
            entry['moments'].sort(key=lambda m: (0 if m['is_exact'] else 1, m['start']))
        segment_matches = sorted(
            seen_videos.values(),
            key=lambda v: (0 if v.get('has_exact') else 1, -v['total_moments']),
        )

    # ── In-video visual/scene search ─────────────────────────────────────────
    scene_matches  = []   # YOLO labels + descriptions + CLIP
    people_matches = []   # Face identity results (separate tab)
    if q:
        seen_scene_videos  = {}
        seen_people        = {}   # identity_id → {identity, videos: {vid_id: [moments]}}

        # 1. YOLO object-label search
        # Cap: 400 frames; channel/date/duration filters honoured.
        _yolo_qs = VideoFrame.objects.filter(
            video__visibility=Video.VISIBILITY_PUBLIC,
            video__status=Video.STATUS_READY,
        )
        _yolo_qs = _apply_ai_video_filters(_yolo_qs, ch_slug, cat_slug, duration_filter, date_filter)
        _fts_yolo_pks = set()
        if _can_use_postgres_fts():
            from django.contrib.postgres.search import SearchQuery as _SQ2
            _fts_yolo_qs = _yolo_qs.filter(
                labels__search=_SQ2(q, config='english', search_type='plain')
            )
            _fts_yolo_pks = set(_fts_yolo_qs.values_list('pk', flat=True)[:400])
            # YOLO labels are standardized COCO class names — raise threshold to 0.5
            # to prevent spurious matches (e.g. "elephant" matching "potted plant").
            # FTS handles exact/stemmed matches; fuzzy only catches clear typos here.
            _fuzzy_yolo_qs = _postgres_fuzzy_filter(_yolo_qs, q, ('labels',), threshold=0.5)
            _yolo_qs = _yolo_qs.filter(
                Q(pk__in=_fts_yolo_pks) | Q(pk__in=_fuzzy_yolo_qs.values('pk'))
            )
        else:
            _yolo_qs = _yolo_qs.filter(_q_icontains_all_terms('labels', q))
            _fts_yolo_pks = set(_yolo_qs.values_list('pk', flat=True)[:400])
        for frm in (
            _yolo_qs.select_related('video', 'video__channel')
            .defer('clip_embedding', 'description')
            .order_by('video_id', 'timestamp')[:400]
        ):
            vid_id   = str(frm.video_id)
            is_exact = frm.pk in _fts_yolo_pks
            if vid_id not in seen_scene_videos:
                seen_scene_videos[vid_id] = {'video': frm.video, 'moments': [], '_seen_ts': set(), 'total_moments': 0, 'has_exact': False}
            _e = seen_scene_videos[vid_id]
            _ts = round(frm.timestamp)
            _e['total_moments'] += 1
            if is_exact:
                _e['has_exact'] = True
            if _ts not in _e['_seen_ts'] and len(_e['moments']) < 30:
                _e['_seen_ts'].add(_ts)
                _e['moments'].append({
                    'timestamp':          frm.timestamp,
                    'labels':             frm.labels,
                    'labels_list':        frm.labels_list,
                    'highlighted_labels': [_highlight_query(lbl, q) for lbl in frm.labels_list],
                    'time_fmt':           _fmt_seconds_display(frm.timestamp),
                    'source':             'object',
                    'is_exact':           is_exact,
                })

        # 2. Scene description search (Postgres: FTS; other DBs: token icontains)
        # Cap: 400 frames; filters applied.
        _scene_qs = VideoFrame.objects.filter(
            video__visibility=Video.VISIBILITY_PUBLIC,
            video__status=Video.STATUS_READY,
        ).exclude(description='')
        _scene_qs = _apply_ai_video_filters(_scene_qs, ch_slug, cat_slug, duration_filter, date_filter)
        # Scene description: FTS ONLY — no fuzzy on long paragraphs (too noisy).
        # FTS with English stemming is already precise for free-text descriptions.
        _fts_scene_pks = set()
        if _can_use_postgres_fts():
            from django.contrib.postgres.search import SearchQuery as _SQ3
            _fts_scene_qs  = _scene_qs.filter(
                description__search=_SQ3(q, config='english', search_type='plain')
            )
            _fts_scene_pks = set(_fts_scene_qs.values_list('pk', flat=True)[:400])
            _scene_qs      = _scene_qs.filter(pk__in=_fts_scene_pks)
        else:
            _scene_qs      = _scene_qs.filter(_q_icontains_all_terms('description', q))
            _fts_scene_pks = set(_scene_qs.values_list('pk', flat=True)[:400])
        for frm in (
            _scene_qs.select_related('video', 'video__channel')
            .defer('clip_embedding', 'labels')
            .order_by('video_id', 'timestamp')[:400]
        ):
            vid_id   = str(frm.video_id)
            is_exact = frm.pk in _fts_scene_pks
            if vid_id not in seen_scene_videos:
                seen_scene_videos[vid_id] = {'video': frm.video, 'moments': [], '_seen_ts': set(), 'total_moments': 0, 'has_exact': False}
            _e = seen_scene_videos[vid_id]
            _ts = round(frm.timestamp)
            _e['total_moments'] += 1
            if is_exact:
                _e['has_exact'] = True
            if _ts not in _e['_seen_ts'] and len(_e['moments']) < 30:
                _e['_seen_ts'].add(_ts)
                _e['moments'].append({
                    'timestamp':          frm.timestamp,
                    'labels':             frm.description,
                    'labels_list':        [frm.description],
                    'highlighted_labels': [_highlight_query(frm.description, q)],
                    'time_fmt':           _fmt_seconds_display(frm.timestamp),
                    'source':             'scene',
                    'is_exact':           is_exact,
                })

        # 3. CLIP semantic search — SQL-side ANN via pgvector HNSW index
        # Cap: 250 best-matching frames (up from 100); channel/date/duration filters applied.
        if use_semantic and getattr(settings, 'CLIP_ENABLED', True):
            try:
                import torch
                from pgvector.django import CosineDistance
                global _clip_model_cache, _clip_proc_cache
                if _clip_model_cache is None:
                    with _clip_cache_lock:
                        if _clip_model_cache is None:
                            from transformers import CLIPProcessor, CLIPModel
                            _clip_proc_cache  = CLIPProcessor.from_pretrained('openai/clip-vit-base-patch32')
                            _clip_model_cache = CLIPModel.from_pretrained('openai/clip-vit-base-patch32')
                            _clip_model_cache.eval()
                _txt_inputs = _clip_proc_cache(text=[q], return_tensors='pt', padding=True)
                with torch.no_grad():
                    _txt_feat = _clip_model_cache.get_text_features(**_txt_inputs)
                    _txt_feat = _txt_feat / _txt_feat.norm(dim=-1, keepdim=True)
                _txt_vec = _txt_feat[0].tolist()
                _threshold = getattr(settings, 'CLIP_SIMILARITY_THRESHOLD', 0.24)
                _max_dist  = 1.0 - _threshold  # cosine distance = 1 − similarity
                # pgvector HNSW ANN search — no Python loop needed
                _clip_base_qs = (
                    VideoFrame.objects
                    .filter(video__visibility=Video.VISIBILITY_PUBLIC,
                            video__status=Video.STATUS_READY)
                    .exclude(clip_embedding=None)
                )
                _clip_base_qs = _apply_ai_video_filters(
                    _clip_base_qs, ch_slug, cat_slug, duration_filter, date_filter
                )
                for frm in (
                    _clip_base_qs
                    .annotate(_dist=CosineDistance('clip_embedding', _txt_vec))
                    .filter(_dist__lte=_max_dist)
                    .order_by('_dist')
                    .select_related('video', 'video__channel')
                    [:250]
                ):
                    vid_id = str(frm.video_id)
                    if vid_id not in seen_scene_videos:
                        seen_scene_videos[vid_id] = {'video': frm.video, 'moments': [], '_seen_ts': set(), 'total_moments': 0}
                    _e = seen_scene_videos[vid_id]
                    _ts = round(frm.timestamp)
                    _e['total_moments'] += 1
                    if _ts not in _e['_seen_ts'] and len(_e['moments']) < 30:
                        _e['_seen_ts'].add(_ts)
                        _e['moments'].append({
                            'timestamp':         frm.timestamp,
                            'labels':            frm.description or frm.labels or q,
                            'labels_list':       [frm.description or frm.labels or q],
                            'highlighted_labels': None,   # no highlight for semantic results
                            'time_fmt':          _fmt_seconds_display(frm.timestamp),
                            'source':            'clip',
                        })
            except Exception:
                pass

        # Strip internal dedup sets; sort moments (exact first) and video-blocks (exact-having first)
        for _v in seen_scene_videos.values():
            _v.pop('_seen_ts', None)
            # CLIP moments have no is_exact key; treat as fuzzy
            _v['moments'].sort(key=lambda m: (0 if m.get('is_exact') else 1, m['timestamp']))
        scene_matches = sorted(
            seen_scene_videos.values(),
            key=lambda v: (0 if v.get('has_exact') else 1, -v['total_moments']),
        )

        # 4. People search — grouped by identity, then by source (video/photo)
        _people_qs = (
            DetectedFace.objects
            .filter(
                video__visibility=Video.VISIBILITY_PUBLIC,
                video__status=Video.STATUS_READY,
            )
            .select_related('video', 'video__channel', 'identity')
        )
        if _can_use_fuzzy_search():
            _people_fuzzy = _postgres_fuzzy_filter(
                FaceIdentity.objects.exclude(name=''),
                q,
                ('name',),
            )
            _people_qs = _people_qs.filter(
                Q(identity__name__icontains=q)
                | Q(identity_id__in=_people_fuzzy.values('id'))
            )
        else:
            _people_qs = _people_qs.filter(identity__name__icontains=q)

        for df in _people_qs.order_by('identity_id', 'video_id', 'timestamp')[:300]:
            iid = df.identity_id
            if iid not in seen_people:
                seen_people[iid] = {'identity': df.identity, 'videos': {}, 'photos': {}}
            vid_id = str(df.video_id)
            if vid_id not in seen_people[iid]['videos']:
                seen_people[iid]['videos'][vid_id] = {'video': df.video, 'moments': [], '_seen_ts': set()}
            _pv = seen_people[iid]['videos'][vid_id]
            _ts = round(df.timestamp)
            if _ts not in _pv['_seen_ts'] and len(_pv['moments']) < 6:
                _pv['_seen_ts'].add(_ts)
                _pv['moments'].append({
                    'timestamp':     df.timestamp,
                    'time_fmt':      _fmt_seconds_display(df.timestamp),
                    'face_crop_url': df.crop_url,
                })

        _people_photo_qs = (
            DetectedFace.objects
            .filter(
                photo__visibility=Photo.VISIBILITY_PUBLIC,
                photo__status=Photo.STATUS_READY,
            )
            .select_related('photo', 'photo__channel', 'identity')
        )
        if _can_use_fuzzy_search():
            _people_fuzzy = _postgres_fuzzy_filter(
                FaceIdentity.objects.exclude(name=''),
                q,
                ('name',),
            )
            _people_photo_qs = _people_photo_qs.filter(
                Q(identity__name__icontains=q)
                | Q(identity_id__in=_people_fuzzy.values('id'))
            )
        else:
            _people_photo_qs = _people_photo_qs.filter(identity__name__icontains=q)

        for df in _people_photo_qs.order_by('identity_id', 'photo_id', 'id')[:300]:
            iid = df.identity_id
            if iid not in seen_people:
                seen_people[iid] = {'identity': df.identity, 'videos': {}, 'photos': {}}
            photo_id = str(df.photo_id)
            if photo_id not in seen_people[iid]['photos']:
                seen_people[iid]['photos'][photo_id] = {'photo': df.photo, 'moments': []}
            _pp = seen_people[iid]['photos'][photo_id]
            if len(_pp['moments']) < 6:
                _pp['moments'].append({
                    'face_crop_url': df.crop_url,
                })
        # Flatten, strip dedup sets
        for iid, data in seen_people.items():
            for _pv in data['videos'].values():
                _pv.pop('_seen_ts', None)
            data['videos'] = list(data['videos'].values())
            data['photos'] = list(data['photos'].values())
        people_matches = list(seen_people.values())

        # ── Combined voice signal: add speech moments for matched identities ──
        # For every FaceIdentity match that has a linked SpeakerIdentity, fetch
        # their VideoSegments and add them as 'voice_videos' on the people result.
        # Example: search "Kashish" → face detections + transcript moments where
        # her linked speaker spoke, even in videos where her face wasn't detected.
        _matched_face_ids = list(seen_people.keys())
        if _matched_face_ids:
            # speakers linked to matched face identities
            _linked_speakers = list(
                SpeakerIdentity.objects
                .filter(face_identity_id__in=_matched_face_ids)
                .values('id', 'face_identity_id', 'name')
            )
            if _linked_speakers:
                _spk_to_face = {s['id']: s['face_identity_id'] for s in _linked_speakers}
                _spk_ids     = list(_spk_to_face.keys())
                _voice_segs  = (
                    VideoSegment.objects
                    .filter(
                        speaker_identity_id__in=_spk_ids,
                        video__visibility=Video.VISIBILITY_PUBLIC,
                        video__status=Video.STATUS_READY,
                    )
                    .select_related('video', 'video__channel')
                    .order_by('speaker_identity_id', 'video_id', 'start_seconds')[:400]
                )
                # group voice segments by face_identity_id → video_id
                from collections import defaultdict as _vdd
                _voice_vid_map = _vdd(lambda: _vdd(list))
                for seg in _voice_segs:
                    fid = _spk_to_face[seg.speaker_identity_id]
                    _voice_vid_map[fid][str(seg.video_id)].append(seg)

                for iid, data in seen_people.items():
                    vid_voice = _voice_vid_map.get(iid, {})
                    voice_videos = []
                    for vid_id_str, segs in vid_voice.items():
                        # Skip if this video is already in face results (avoid duplication)
                        already = any(str(pv['video'].id) == vid_id_str for pv in data['videos'])
                        voice_videos.append({
                            'video':     segs[0].video,
                            'moments':   [
                                {
                                    'timestamp': s.start_seconds,
                                    'time_fmt':  _fmt_seconds_display(s.start_seconds),
                                    'text':      (s.text or '')[:120],
                                    'is_voice':  True,
                                }
                                for s in segs[:6]
                            ],
                            'already_in_face': already,
                        })
                    data['voice_videos'] = voice_videos

        frame_matches = scene_matches  # backwards compat alias

    # ── Channel and playlist search ───────────────────────────────────────────
    channel_matches  = []
    playlist_matches = []
    if q:
        if _can_use_fuzzy_search():
            channel_matches = list(
                _postgres_fuzzy_filter(Channel.objects, q, ('name',))
                .select_related('owner')[:20]
            )
            playlist_matches = list(
                _postgres_fuzzy_filter(Playlist.objects.filter(is_public=True), q, ('title',))
                .select_related('owner')[:20]
            )
        else:
            channel_matches = list(
                Channel.objects
                .filter(name__icontains=q)
                .select_related('owner')
                .order_by('name')[:20]
            )
            playlist_matches = list(
                Playlist.objects
                .filter(title__icontains=q, is_public=True)
                .select_related('owner')
                .order_by('title')[:20]
            )

    # ── Photo search ─────────────────────────────────────────────────────────
    photo_matches = []
    if q:
        _photo_qs = Photo.objects.filter(
            visibility=Photo.VISIBILITY_PUBLIC, status=Photo.STATUS_READY,
        ).select_related('channel')

        # People-aware photo search: include photos where a detected face identity matches the query.
        _photo_people_ids = Photo.objects.filter(
            visibility=Photo.VISIBILITY_PUBLIC,
            status=Photo.STATUS_READY,
            detected_faces__identity__name__icontains=q,
        ).values('pk')
        if _can_use_postgres_fts():
            from django.contrib.postgres.search import SearchQuery as _SQP, SearchVector as _SVP
            _sq_p = _SQP(q, config='english', search_type='plain')
            # FTS on short user-entered fields (stemmed, precise)
            _fts_title_qs  = _photo_qs.annotate(
                _s=_SVP('title', 'description', 'tags', config='english')
            ).filter(_s=_sq_p).distinct()
            # FTS on AI-generated fields — handles "animals"→"animals" correctly via stemming
            _fts_labels_qs = _photo_qs.filter(
                labels__search=_SQP(q, config='english', search_type='plain')
            )
            _fts_scene_qs  = _photo_qs.filter(
                scene_description__search=_SQP(q, config='english', search_type='plain')
            )
            # FTS on denormalized face name cache ("Soham, Alice, Bob")
            _fts_face_qs   = _photo_qs.filter(
                face_names__search=_SQP(q, config='english', search_type='plain')
            )
            # Fuzzy on short fields only.
            # face_names = "Soham, Alice" — short CSV, typos useful ("Sohm"→"Soham")
            # labels = comma-separated object names ("dog, cat") — short, typos useful ("elefant"→"elephant")
            # scene_description = long AI paragraph — FTS handles it; fuzzy is noisy there.
            # ocr_text = exact printed text — icontains only (no FTS, no fuzzy); stemming would
            #            cause false positives on unrelated words. "cow" must literally appear as
            #            a contiguous substring of at least one word in the OCR text.
            _fuzzy_qs = _postgres_fuzzy_filter(_photo_qs, q, ('title', 'tags', 'labels', 'face_names'))
            _photo_qs = _photo_qs.filter(
                Q(pk__in=_fts_title_qs.values('pk'))
                | Q(pk__in=_fts_labels_qs.values('pk'))
                | Q(pk__in=_fts_scene_qs.values('pk'))
                | Q(pk__in=_fts_face_qs.values('pk'))
                | Q(pk__in=_fuzzy_qs.values('pk'))
                | Q(pk__in=_photo_people_ids)
                | Q(face_names__icontains=q)   # direct substring match for names
                | Q(ocr_text__icontains=q)     # exact substring: "cow" in "cowboy" ✓; not in "conference" ✗
            ).distinct()
        else:
            _photo_qs = _photo_qs.filter(
                _q_icontains_all_terms('title', q)
                | _q_icontains_all_terms('tags', q)
                | _q_icontains_all_terms('labels', q)
                | _q_icontains_all_terms('scene_description', q)
                | _q_icontains_all_terms('ocr_text', q)
                | Q(face_names__icontains=q)
                | Q(pk__in=_photo_people_ids)
            ).distinct()

        # CLIP semantic search on photos
        if use_semantic and getattr(settings, 'CLIP_ENABLED', True):
            try:
                import torch as _torch
                from pgvector.django import CosineDistance as _CD
                if _clip_model_cache is None:
                    with _clip_cache_lock:
                        if _clip_model_cache is None:
                            from transformers import CLIPProcessor, CLIPModel
                            _clip_proc_cache  = CLIPProcessor.from_pretrained('openai/clip-vit-base-patch32')
                            _clip_model_cache = CLIPModel.from_pretrained('openai/clip-vit-base-patch32')
                            _clip_model_cache.eval()
                _txt_inputs = _clip_proc_cache(text=[q], return_tensors='pt', padding=True)
                with _torch.no_grad():
                    _tf = _clip_model_cache.get_text_features(**_txt_inputs)
                    _tf = _tf / _tf.norm(dim=-1, keepdim=True)
                _tv = _tf[0].tolist()
                # Photos use a stricter threshold than videos: CLIP does word→concept
                # associations (e.g. "cow" → Bali via Hindu context) that are too loose
                # for photo library search. 0.28 keeps genuine visual matches while
                # filtering out cultural/conceptual leaps.
                _threshold = getattr(settings, 'CLIP_PHOTO_SIMILARITY_THRESHOLD', 0.28)
                _clip_photos = (
                    Photo.objects
                    .filter(visibility=Photo.VISIBILITY_PUBLIC, status=Photo.STATUS_READY)
                    .exclude(clip_embedding=None)
                    .annotate(_dist=_CD('clip_embedding', _tv))
                    .filter(_dist__lte=1.0 - _threshold)
                    .order_by('_dist')
                    .select_related('channel')[:50]
                )
                seen_ids = {str(p.id) for p in _photo_qs[:200]}
                for p in _clip_photos:
                    if str(p.id) not in seen_ids:
                        seen_ids.add(str(p.id))
                        _photo_qs = list(_photo_qs) + [p]  # type: ignore[assignment]
            except Exception:
                pass

        photo_matches = list(_photo_qs[:60]) if not isinstance(_photo_qs, list) else _photo_qs[:60]

    return render(request, 'videos/player.html', {
        'videos':              videos_page,
        'feed_total_count':    feed_total_count,
        'has_more_videos':     has_more_videos,
        'subscribed_channels': subscribed_channels,
        'categories':          categories,
        'unread_count':        unread_count,
        'current_q':           q,
        'current_cat':         cat_slug,
        'current_duration':    duration_filter,
        'current_date':        date_filter,
        'current_sort':        sort,
        'segment_matches':     segment_matches,
        'chapter_matches':     chapter_matches,
        'scene_matches':       scene_matches,
        'people_matches':      people_matches,
        'channel_matches':     channel_matches,
        'playlist_matches':    playlist_matches,
        'photo_matches':       photo_matches,
        'frame_matches':       scene_matches,   # keep for any legacy template refs
        'use_semantic':        use_semantic,
        'feed_filter_params': {
            'channel': request.GET.get('channel', ''),
            'q': q,
            'category': cat_slug,
            'duration': duration_filter,
            'date': date_filter,
            'sort': sort,
        },
    })


def watch_page(request, video_id):
    try:
        video = Video.objects.select_related('channel').get(id=video_id)
    except Video.DoesNotExist:
        raise Http404

    if not video.is_visible_to(request.user):
        return render(request, 'videos/watch_restricted.html', {'video': video}, status=403)

    # ?t=<seconds> — from in-video search clicks; overrides watch-history resume
    seek_to = None
    t_param = request.GET.get('t', '').strip()
    if t_param:
        try:
            seek_to = max(0.0, float(t_param))
        except ValueError:
            pass

    # Playlist context — if ?playlist=<uuid> is passed, drive Up Next from the playlist
    playlist_obj  = None
    playlist_next = None
    playlist_prev = None
    playlist_pos  = None
    playlist_total = None
    playlist_id_param = request.GET.get('playlist', '').strip()
    if playlist_id_param:
        try:
            import uuid as _uuid
            _pid = _uuid.UUID(playlist_id_param)
            playlist_obj = Playlist.objects.get(id=_pid)
            _items = list(playlist_obj.items.select_related('video__channel').order_by('order', 'added_at'))
            _idx = next((i for i, it in enumerate(_items) if str(it.video_id) == str(video_id)), None)
            if _idx is not None:
                playlist_pos   = _idx + 1
                playlist_total = len(_items)
                if _idx + 1 < len(_items):
                    playlist_next = _items[_idx + 1].video
                if _idx > 0:
                    playlist_prev = _items[_idx - 1].video
                # Sidebar shows remaining playlist videos instead of generic related
                related = [it.video for it in _items if str(it.video_id) != str(video_id)][:10]
        except (ValueError, Playlist.DoesNotExist):
            playlist_id_param = ''

    if not playlist_obj:
        related = Video.objects.filter(
            visibility=Video.VISIBILITY_PUBLIC, status=Video.STATUS_READY
        ).exclude(id=video_id).select_related('channel')[:10]

    is_owner = _is_video_owner(request.user, video)

    user_liked = (
        request.user.is_authenticated
        and VideoLike.objects.filter(user=request.user, video=video).exists()
    )

    user_saved = (
        request.user.is_authenticated
        and SavedVideo.objects.filter(user=request.user, video=video).exists()
    )

    watch_progress = None
    if request.user.is_authenticated:
        wh = WatchHistory.objects.filter(user=request.user, video=video).first()
        if wh:
            watch_progress = wh.progress_seconds

    # Top-level comments only; replies fetched inline via serializer
    comments = []
    if video.comments_enabled:
        comments = video.comments.filter(parent__isnull=True).select_related('user').prefetch_related(
            'replies__user', 'comment_likes', 'replies__comment_likes'
        )

    chapters     = video.chapters.all()
    end_screens  = video.end_screens.select_related('target_video').all()
    subtitles    = video.subtitles.all()
    audio_tracks = video.audio_tracks.all()

    # Playlists for "Save to Playlist" modal
    user_playlists = []
    if request.user.is_authenticated:
        user_playlists = Playlist.objects.filter(owner=request.user).order_by('title')

    unread_count = 0
    if request.user.is_authenticated:
        unread_count = Notification.objects.filter(recipient=request.user, is_read=False).count()

    return render(request, 'videos/watch.html', {
        'video':           video,
        'related':         related,
        'is_owner':        is_owner,
        'user_liked':      user_liked,
        'user_saved':      user_saved,
        'watch_progress':  watch_progress,
        'comments':        comments,
        'chapters':        chapters,
        'end_screens':     end_screens,
        'subtitles':       subtitles,
        'audio_tracks':    audio_tracks,
        'user_playlists':  user_playlists,
        'unread_count':    unread_count,
        'seek_to':         seek_to,
        'all_categories':  Category.objects.all().order_by('name'),
        'frame_interval':  getattr(settings, 'FRAME_INTERVAL_SECONDS', 5),
        # Playlist queue context
        'playlist_obj':    playlist_obj,
        'playlist_id':     playlist_id_param,
        'playlist_next':   playlist_next,
        'playlist_prev':   playlist_prev,
        'playlist_pos':    playlist_pos,
        'playlist_total':  playlist_total,
    })


@xframe_options_exempt
def embed_page(request, video_id):
    try:
        video = Video.objects.get(id=video_id)
    except Video.DoesNotExist:
        raise Http404

    response = render(request, 'videos/embed.html', {'video': video})
    if 'X-Frame-Options' in response:
        del response['X-Frame-Options']

    allow_origins = getattr(settings, 'EMBED_ALLOW_ORIGINS', '*')
    origins = ' '.join(o.strip() for o in allow_origins.split(',') if o.strip())
    response['Content-Security-Policy'] = f"frame-ancestors {origins};"
    return response


def channel_page(request, slug):
    channel = get_object_or_404(Channel, slug=slug)

    is_owner = _is_channel_owner(request.user, channel)

    is_subscribed = (
        request.user.is_authenticated
        and ChannelSubscription.objects.filter(user=request.user, channel=channel).exists()
    )

    _video_fields = (
        'id', 'title', 'duration', 'views_count', 'status',
        'visibility', 'thumbnail', 'created_at', 'channel_id', 'category_id',
    )
    if is_owner:
        videos = list(Video.objects.filter(channel=channel)
                      .select_related('category').only(*_video_fields))
    else:
        videos = list(Video.objects.filter(channel=channel, visibility=Video.VISIBILITY_PUBLIC)
                      .select_related('category').only(*_video_fields))

    # Photos for this channel
    _photo_fields = ('id', 'title', 'thumbnail', 'views_count', 'width', 'height', 'status', 'created_at')
    _photo_qs = Photo.objects.filter(channel=channel)
    if not is_owner:
        _photo_qs = _photo_qs.filter(
            visibility=Photo.VISIBILITY_PUBLIC, status=Photo.STATUS_READY
        )
    photo_count  = _photo_qs.count()
    photos       = list(_photo_qs.only(*_photo_fields).order_by('-created_at')[:48]) if photo_count else []
    photo_has_more = photo_count > 48

    # Cache subscriber count — changes rarely, no need to COUNT(*) every page load
    _sub_cache_key = f'fs:sub_count_{channel.pk}'
    subscriber_count = cache.get(_sub_cache_key)
    if subscriber_count is None:
        subscriber_count = channel.subscribers.count()
        cache.set(_sub_cache_key, subscriber_count, 60 * 5)  # 5 min

    links = channel.links.all()

    unread_count = 0
    if request.user.is_authenticated:
        unread_count = Notification.objects.filter(recipient=request.user, is_read=False).count()

    return render(request, 'videos/channel.html', {
        'channel':          channel,
        'videos':           videos,
        'photos':           photos,
        'photo_count':      photo_count,
        'photo_has_more':   photo_has_more,
        'links':            links,
        'is_owner':         is_owner,
        'is_subscribed':    is_subscribed,
        'subscriber_count': subscriber_count,
        'unread_count':     unread_count,
    })


@editor_required
def analytics_page(request):
    from django.utils import timezone
    import datetime
    import json

    all_channels = list(
        _user_channels(request.user).annotate(
            subscribers_total=Count('subscribers', distinct=True)
        )
    )
    request._owned_channels_cache = all_channels
    if not all_channels:
        return render(request, 'videos/analytics.html', {
            'all_channels': [], 'channel': None, 'videos': [],
        })

    # Channel selector — ?channel=<uuid> or omit for combined view
    selected_id = request.GET.get('channel', '').strip()
    if selected_id and selected_id != 'all':
        channel = next((c for c in all_channels if str(c.id) == selected_id), None)
        if not channel:
            channel = all_channels[0]
            selected_id = str(channel.id)
    else:
        channel = None          # "All Channels" combined view
        selected_id = 'all'

    # Videos + base queryset
    if channel:
        videos = (Video.objects
                  .filter(channel=channel)
                  .select_related('category', 'channel')
                  .annotate(
                      likes_total=Count('likes', distinct=True),
                      comments_total=Count('comments', distinct=True),
                  )
                  .order_by('-created_at'))
        watch_q = {'video__channel': channel}
        total_subscribers = channel.subscribers_total
    else:
        videos = (Video.objects
                  .filter(channel__in=all_channels)
                  .select_related('category', 'channel')
                  .annotate(
                      likes_total=Count('likes', distinct=True),
                      comments_total=Count('comments', distinct=True),
                  )
                  .order_by('-created_at'))
        watch_q = {'video__channel__in': all_channels}
        total_subscribers = sum(c.subscribers_total for c in all_channels)

    videos = list(videos)
    total_views    = sum(v.views_count    for v in videos)
    total_likes    = sum(v.likes_total    for v in videos)
    total_comments = sum(v.comments_total for v in videos)

    # Watch time chart — last 30 days
    thirty_days_ago = (timezone.now() - datetime.timedelta(days=30)).date()
    watch_entries = (
        WatchTimeEntry.objects
        .filter(date__gte=thirty_days_ago, **watch_q)
        .values('date')
        .annotate(total=Sum('total_seconds'))
        .order_by('date')
    )
    chart_labels = [str(e['date']) for e in watch_entries]
    chart_data   = [round(e['total'] / 60, 1) for e in watch_entries]

    return render(request, 'videos/analytics.html', {
        'all_channels':      all_channels,
        'channel':           channel,         # None = "all" view
        'selected_id':       selected_id,
        'videos':            videos,
        'total_views':       total_views,
        'total_likes':       total_likes,
        'total_comments':    total_comments,
        'total_subscribers': total_subscribers,
        'chart_labels':      json.dumps(chart_labels),
        'chart_data':        json.dumps(chart_data),
    })


def trending_page(request):
    from django.utils import timezone
    import datetime
    week_ago = timezone.now() - datetime.timedelta(days=7)
    videos = Video.objects.filter(
        visibility=Video.VISIBILITY_PUBLIC,
        status=Video.STATUS_READY,
        created_at__gte=week_ago,
    ).select_related('channel', 'category').order_by('-views_count')[:10]

    return render(request, 'videos/trending.html', {'videos': videos})


def category_page(request, slug):
    category = get_object_or_404(Category, slug=slug)
    videos_qs = Video.objects.filter(
        category=category,
        visibility=Video.VISIBILITY_PUBLIC,
        status=Video.STATUS_READY,
    ).select_related('channel').order_by('-created_at').only(
        'id', 'title', 'duration', 'views_count', 'status', 'created_at', 'channel_id',
    )
    _peek = list(videos_qs[:25])
    has_more_videos = len(_peek) > 24
    videos = _peek[:24]

    return render(request, 'videos/category.html', {
        'category': category,
        'videos': videos,
        'has_more_videos': has_more_videos,
    })


@login_required
def saved_page(request):
    saved = (
        SavedVideo.objects
        .filter(user=request.user)
        .select_related('video__channel')
        .only('saved_at', 'video__id', 'video__title', 'video__duration',
              'video__views_count', 'video__status', 'video__thumbnail',
              'video__created_at', 'video__channel__id', 'video__channel__name',
              'video__channel__slug')
        .order_by('-saved_at')[:200]
    )

    return render(request, 'videos/saved.html', {'saved': saved})


@login_required
def history_page(request):
    history = (
        WatchHistory.objects
        .filter(user=request.user)
        .select_related('video__channel')
        .only('watched_at', 'progress_seconds', 'completed',
              'video__id', 'video__title', 'video__duration',
              'video__views_count', 'video__status', 'video__thumbnail',
              'video__created_at', 'video__channel__id', 'video__channel__name',
              'video__channel__slug')
        .order_by('-watched_at')[:200]
    )

    return render(request, 'videos/history.html', {'history': history})


@login_required
def playlists_page(request):
    playlists = list(
        Playlist.objects
        .filter(owner=request.user)
        .annotate(items_total=Count('items'))
        .order_by('-updated_at')
    )

    # Precompute thumbnails in ONE query instead of N×2 via the property.
    # Fetch all first thumbnail-having items across all playlists, ordered so the
    # first hit per playlist_id is the item with the lowest order/added_at.
    pl_ids = [p.id for p in playlists]
    _thumb_items = (
        PlaylistItem.objects
        .filter(playlist_id__in=pl_ids, video__thumbnail__isnull=False)
        .select_related('video')
        .only('playlist_id', 'order', 'added_at', 'video__thumbnail')
        .order_by('playlist_id', 'order', 'added_at')
    )
    _thumb_map = {}
    for item in _thumb_items:
        if item.playlist_id not in _thumb_map:
            _thumb_map[item.playlist_id] = item.video.thumbnail_url
    for pl in playlists:
        pl.thumb_url = _thumb_map.get(pl.id)

    return render(request, 'videos/playlists.html', {'playlists': playlists})


def playlist_detail_page(request, playlist_id):
    playlist = get_object_or_404(Playlist, id=playlist_id)

    # Only owner can see private playlists
    if not playlist.is_public and (
        not request.user.is_authenticated or playlist.owner != request.user
    ):
        raise Http404

    is_owner = request.user.is_authenticated and playlist.owner == request.user
    items = playlist.items.select_related('video__channel').order_by('order', 'added_at')

    return render(request, 'videos/playlist_detail.html', {
        'playlist': playlist,
        'items': items,
        'is_owner': is_owner,
    })


@login_required
def notifications_page(request):
    notifications = Notification.objects.filter(
        recipient=request.user
    ).select_related('sender').order_by('-created_at')[:50]

    # Mark all as read
    Notification.objects.filter(recipient=request.user, is_read=False).update(is_read=True)

    return render(request, 'videos/notifications.html', {
        'notifications': notifications,
        'unread_count': 0,
    })


@superuser_required
def user_management_page(request):
    users = User.objects.prefetch_related('channels', 'profile').order_by('-date_joined')
    return render(request, 'videos/user_management.html', {'users': users})


@superuser_required
def admin_commands_page(request):
    """Superadmin UI for running Django management commands."""
    channels   = list(Channel.objects.values('name', 'slug').order_by('name'))
    categories = list(Category.objects.values('name', 'slug').order_by('name'))
    users      = list(User.objects.values('id', 'username').order_by('username'))
    videos     = list(
        Video.objects.values('id', 'title', 'status')
        .order_by('-created_at')[:400]
    )
    return render(request, 'videos/admin_commands.html', {
        'channels':   channels,
        'categories': categories,
        'users':      users,
        'videos':     videos,
    })


@api_view(['POST'])
def admin_commands_run(request):
    """
    Execute a management command and return its stdout/stderr output.
    Superadmin only.
    """
    from django.core.management import call_command
    import io

    profile = getattr(request.user, 'profile', None)
    if not profile or profile.role != 'superadmin':
        return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)

    cmd  = request.data.get('command', '').strip()
    args = request.data.get('args', {})

    # Whitelist of safe management commands
    ALLOWED = {
        'assign_role', 'reanalyse_videos', 'rename_auto_identities',
        'ingest_videos', 'regenerate_captions', 'propagate_identities',
        'auto_confirm_similar', 'patch_master_playlists', 'fill_blip_descriptions',
        'run_diarization',
    }
    if cmd not in ALLOWED:
        return Response({'error': f'Command not allowed: {cmd}'}, status=status.HTTP_400_BAD_REQUEST)

    # Capture stdout + stderr
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    try:
        # Build clean kwargs — strip empty strings/None
        kwargs = {k: v for k, v in args.items() if v not in ('', None)}
        call_command(cmd, stdout=stdout_buf, stderr=stderr_buf, **kwargs)
        output = stdout_buf.getvalue() or '(no output)'
        errors = stderr_buf.getvalue()
        return Response({'ok': True, 'output': output, 'errors': errors})
    except Exception as exc:
        return Response({
            'ok': False,
            'output': stdout_buf.getvalue(),
            'errors': stderr_buf.getvalue(),
            'exception': str(exc),
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@editor_required
def categories_manage_page(request):
    cats = Category.objects.annotate(video_count=Count('videos')).order_by('name')
    return render(request, 'videos/categories_manage.html', {'categories': cats})


@editor_required
def channels_manage_page(request):
    channels = _user_channels(request.user).select_related('owner').prefetch_related('videos', 'links', 'editors')
    return render(request, 'videos/channels_manage.html', {'channels': channels})


# ─── Health ──────────────────────────────────────────────────────────────────

@api_view(['GET'])
def health_check(request):
    return Response({'status': 'ok', 'service': 'ClipLens API'})


# ─── Channels API ────────────────────────────────────────────────────────────

@api_view(['GET', 'POST'])
def channel_list(request):
    if request.method == 'GET':
        return Response(ChannelSerializer(Channel.objects.all(), many=True).data)

    # POST — editor creates a new channel
    if not request.user.is_authenticated:
        return Response({'error': 'Authentication required.'}, status=status.HTTP_401_UNAUTHORIZED)
    if not _is_editor(request.user):
        return Response({'error': 'Only editors can create channels.'}, status=status.HTTP_403_FORBIDDEN)

    data = request.data.copy() if hasattr(request.data, 'copy') else dict(request.data)
    name = data.get('name', '').strip()
    if not name:
        return Response({'name': ['Channel name is required.']}, status=status.HTTP_400_BAD_REQUEST)

    if Channel.objects.filter(name__iexact=name).exists():
        return Response({'name': [f'A channel named "{name}" already exists.']}, status=status.HTTP_400_BAD_REQUEST)

    # Auto-generate slug if not provided
    provided_slug = data.get('slug', '').strip()
    slug = _make_unique_slug(provided_slug or name)
    if provided_slug and Channel.objects.filter(slug=provided_slug).exists():
        return Response({'slug': ['This slug is already taken.']}, status=status.HTTP_400_BAD_REQUEST)

    channel = Channel.objects.create(
        owner=request.user,
        name=name,
        slug=slug,
        description=data.get('description', ''),
    )
    return Response(ChannelSerializer(channel).data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PATCH', 'DELETE'])
def channel_detail(request, slug):
    channel = get_object_or_404(Channel, slug=slug)
    return _channel_detail_response(request, channel)


@api_view(['GET', 'PATCH', 'DELETE'])
def channel_detail_by_id(request, channel_id):
    channel = get_object_or_404(Channel, pk=channel_id)
    return _channel_detail_response(request, channel)


def _channel_detail_response(request, channel):
    if request.method == 'GET':
        is_owner = _is_channel_owner(request.user, channel)
        data = ChannelSerializer(channel).data
        qs = channel.videos.all() if is_owner else channel.videos.filter(visibility=Video.VISIBILITY_PUBLIC)
        data['videos'] = VideoListSerializer(qs, many=True).data
        return Response(data)

    if not request.user.is_authenticated:
        return Response({'error': 'Authentication required.'}, status=status.HTTP_401_UNAUTHORIZED)

    if not _is_channel_owner(request.user, channel):
        return Response({'error': 'You do not own this channel.'}, status=status.HTTP_403_FORBIDDEN)

    if request.method == 'PATCH':
        serializer = ChannelSerializer(channel, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    channel.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


# ─── Channel Editors API ──────────────────────────────────────────────────────

@api_view(['GET', 'POST'])
@api_login_required
def channel_editors(request, channel_id):
    """
    GET  /api/channels/<id>/editors/  — list co-editors (owner only)
    POST /api/channels/<id>/editors/  — add a co-editor by username (owner only)
    """
    channel = get_object_or_404(Channel, pk=channel_id)

    # Only the primary owner can manage editors
    if channel.owner_id != request.user.pk and not _is_superadmin(request.user):
        return Response({'error': 'Only the channel owner can manage editors.'}, status=status.HTTP_403_FORBIDDEN)

    if request.method == 'GET':
        data = [
            {'id': u.id, 'username': u.username, 'email': u.email}
            for u in channel.editors.all()
        ]
        return Response(data)

    # POST — add editor by username
    username = request.data.get('username', '').strip()
    if not username:
        return Response({'error': 'username is required.'}, status=status.HTTP_400_BAD_REQUEST)

    target = User.objects.filter(username__iexact=username).first()
    if not target:
        return Response({'error': f'User "{username}" not found.'}, status=status.HTTP_404_NOT_FOUND)

    if target == request.user:
        return Response({'error': 'You are already the owner of this channel.'}, status=status.HTTP_400_BAD_REQUEST)

    # Ensure target has at least editor role
    try:
        if not target.profile.is_editor:
            return Response(
                {'error': f'{username} is a viewer — assign them the editor role first.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
    except Exception:
        return Response({'error': f'{username} has no role profile.'}, status=status.HTTP_400_BAD_REQUEST)

    channel.editors.add(target)
    return Response({'id': target.id, 'username': target.username, 'email': target.email}, status=status.HTTP_201_CREATED)


@api_view(['DELETE'])
@api_login_required
def channel_editor_remove(request, channel_id, user_id):
    """DELETE /api/channels/<id>/editors/<user_id>/ — remove a co-editor (owner only)"""
    channel = get_object_or_404(Channel, pk=channel_id)

    if channel.owner_id != request.user.pk and not _is_superadmin(request.user):
        return Response({'error': 'Only the channel owner can manage editors.'}, status=status.HTTP_403_FORBIDDEN)

    target = get_object_or_404(User, pk=user_id)
    channel.editors.remove(target)
    return Response(status=status.HTTP_204_NO_CONTENT)


# ─── Channel Links API ────────────────────────────────────────────────────────

@api_view(['GET', 'POST'])
def channel_links(request, slug):
    """
    GET  /api/channels/<slug>/links/ — list links (public)
    POST /api/channels/<slug>/links/ — add link (owner only)
    """
    channel = get_object_or_404(Channel, slug=slug)

    if request.method == 'GET':
        return Response(ChannelLinkSerializer(channel.links.all(), many=True).data)

    if not request.user.is_authenticated:
        return Response({'error': 'Authentication required.'}, status=status.HTTP_401_UNAUTHORIZED)

    if not _is_channel_owner(request.user, channel):
        return Response({'error': 'You do not own this channel.'}, status=status.HTTP_403_FORBIDDEN)

    serializer = ChannelLinkSerializer(data=request.data)
    if serializer.is_valid():
        serializer.save(channel=channel)
        return Response(serializer.data, status=status.HTTP_201_CREATED)
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@api_view(['PATCH', 'DELETE'])
@api_login_required
def channel_link_detail(request, link_id):
    """PATCH/DELETE /api/channel-links/<id>/ — owner only"""
    link = get_object_or_404(ChannelLink, id=link_id)

    if not _is_channel_owner(request.user, link.channel):
        return Response({'error': 'You do not own this channel.'}, status=status.HTTP_403_FORBIDDEN)

    if request.method == 'PATCH':
        serializer = ChannelLinkSerializer(link, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    link.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


# ─── Categories API ──────────────────────────────────────────────────────────

@api_view(['GET', 'POST'])
def category_list(request):
    if request.method == 'GET':
        cats = Category.objects.annotate(video_count=Count('videos')).all()
        return Response([
            {**CategorySerializer(c).data, 'video_count': c.video_count}
            for c in cats
        ])

    if not request.user.is_authenticated or not _is_editor(request.user):
        return Response({'error': 'Editor access required.'}, status=status.HTTP_403_FORBIDDEN)

    serializer = CategorySerializer(data=request.data)
    if serializer.is_valid():
        serializer.save()
        cache.delete('all_categories')
        return Response(serializer.data, status=status.HTTP_201_CREATED)
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@api_view(['PATCH', 'DELETE'])
def category_detail(request, category_id):
    """PATCH /api/categories/<id>/ — rename; DELETE — remove (only if no videos assigned)."""
    if not request.user.is_authenticated or not _is_editor(request.user):
        return Response({'error': 'Superuser access required.'}, status=status.HTTP_403_FORBIDDEN)
    try:
        cat = Category.objects.get(pk=category_id)
    except Category.DoesNotExist:
        return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)

    if request.method == 'DELETE':
        video_count = cat.videos.count()
        if video_count > 0:
            return Response(
                {'error': f'Cannot delete: {video_count} video(s) are assigned to this category. Reassign them first.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        cat.delete()
        cache.delete('all_categories')
        return Response(status=status.HTTP_204_NO_CONTENT)

    # PATCH — rename
    serializer = CategorySerializer(cat, data=request.data, partial=True)
    if serializer.is_valid():
        serializer.save()
        cache.delete('all_categories')
        return Response(serializer.data)
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# ─── Videos API ──────────────────────────────────────────────────────────────

@api_view(['GET'])
def video_list(request):
    videos = Video.objects.filter(
        visibility=Video.VISIBILITY_PUBLIC, status=Video.STATUS_READY,
    ).select_related('channel')

    if request.user.is_authenticated:
        own_channel = _user_channel(request.user)
        if own_channel:
            videos = Video.objects.filter(
                Q(visibility=Video.VISIBILITY_PUBLIC, status=Video.STATUS_READY) |
                Q(channel=own_channel)
            ).distinct().select_related('channel')

    # ── Filters (mirrors player_page) ──
    ch_slug = request.query_params.get('channel', '').strip()
    if ch_slug:
        videos = videos.filter(channel__slug=ch_slug)

    cat_slug = request.query_params.get('category', '').strip()
    if cat_slug:
        videos = videos.filter(category__slug=cat_slug)

    search = request.query_params.get('q', '').strip()
    if search:
        videos = videos.filter(
            Q(title__icontains=search) |
            Q(tags__icontains=search) |
            Q(channel__name__icontains=search)
        ).distinct()

    status_filter = request.query_params.get('status')
    if status_filter:
        videos = videos.filter(status=status_filter)

    duration_filter = request.query_params.get('duration', '').strip()
    if duration_filter == 'short':
        videos = videos.filter(duration__lt=240)
    elif duration_filter == 'medium':
        videos = videos.filter(duration__gte=240, duration__lt=1200)
    elif duration_filter == 'long':
        videos = videos.filter(duration__gte=1200)

    date_filter = request.query_params.get('date', '').strip()
    if date_filter:
        from django.utils import timezone as _tz
        import datetime as _dt
        _now = _tz.now()
        if date_filter == 'today':
            videos = videos.filter(created_at__date=_now.date())
        elif date_filter == 'week':
            videos = videos.filter(created_at__gte=_now - _dt.timedelta(days=7))
        elif date_filter == 'month':
            videos = videos.filter(created_at__gte=_now - _dt.timedelta(days=30))
        elif date_filter == 'year':
            videos = videos.filter(created_at__gte=_now - _dt.timedelta(days=365))

    sort = request.query_params.get('sort', '').strip()
    if sort == 'views':
        videos = videos.order_by('-views_count')
    elif sort == 'oldest':
        videos = videos.order_by('created_at')
    else:
        videos = videos.order_by('-created_at')

    # ── Pagination ──
    try:
        page      = max(1, int(request.query_params.get('page', 1)))
        page_size = min(48, max(1, int(request.query_params.get('page_size', 24))))
    except (ValueError, TypeError):
        page, page_size = 1, 24

    offset   = (page - 1) * page_size
    # Fetch one extra to determine has_more without COUNT(*)
    _slice   = list(videos.only(
        'id', 'title', 'duration', 'views_count', 'status',
        'created_at', 'channel_id',
    )[offset: offset + page_size + 1])
    has_more = len(_slice) > page_size
    results  = _slice[:page_size]

    return Response({
        'page':     page,
        'has_more': has_more,
        'results':  VideoFeedSerializer(results, many=True).data,
    })


@api_view(['POST'])
@api_login_required
@parser_classes([MultiPartParser, FormParser])
def video_upload(request):
    if not _is_editor(request.user):
        return Response({'error': 'You do not have upload access.'}, status=status.HTTP_403_FORBIDDEN)

    # If a specific channel_id was supplied, verify the user can manage it (owner OR co-editor)
    channel_id = request.data.get('channel_id')
    if channel_id:
        channel = _user_channels(request.user).filter(pk=channel_id).first()
        if not channel:
            return Response({'error': 'Channel not found or you do not have access.'}, status=status.HTTP_400_BAD_REQUEST)
    else:
        channel = _user_channel(request.user)

    if not channel:
        return Response(
            {'error': 'You do not have a channel. Please create one first.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    serializer = VideoUploadSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    video_file = serializer.validated_data.pop('video_file')
    thumbnail_file = serializer.validated_data.pop('thumbnail_file', None)

    video = Video(**serializer.validated_data)
    video.channel = channel
    video.uploaded_by = request.user.username
    video.original_filename = video_file.name
    video.file_size = video_file.size
    video.original_file = video_file
    if thumbnail_file:
        video.thumbnail = thumbnail_file
    video.save()

    # Notify subscribers
    _notify_subscribers_new_video(video)

    _dispatch_process_video(video.id)

    return Response({
        'id': str(video.id),
        'title': video.title,
        'status': video.status,
        'message': 'Video uploaded. HLS processing started in background.',
    }, status=status.HTTP_201_CREATED)


def _notify_subscribers_new_video(video):
    """Create Notification records for all channel subscribers."""
    if not video.channel:
        return
    subs = ChannelSubscription.objects.filter(channel=video.channel).select_related('user')
    notifications = [
        Notification(
            recipient=sub.user,
            sender=video.channel.owner,
            notification_type=Notification.TYPE_NEW_VIDEO,
            video=video,
            message=f'{video.channel.name} uploaded "{video.title}"',
            link=video.watch_url,
        )
        for sub in subs
        if sub.user != video.channel.owner
    ]
    if notifications:
        Notification.objects.bulk_create(notifications)


@api_view(['GET', 'PATCH', 'DELETE'])
def video_detail(request, video_id):
    try:
        video = Video.objects.get(id=video_id)
    except Video.DoesNotExist:
        return Response({'error': 'Video not found'}, status=status.HTTP_404_NOT_FOUND)

    if request.method == 'GET':
        data = VideoDetailSerializer(video, context={'request': request}).data
        data['embed_url'] = f"{settings.SITE_URL}/embed/{video_id}/"
        data['iframe_code'] = (
            f'<iframe src="{settings.SITE_URL}/embed/{video_id}/" '
            f'width="100%" style="aspect-ratio:16/9;border:none;" allowfullscreen></iframe>'
        )
        return Response(data)

    if not request.user.is_authenticated:
        return Response({'error': 'Authentication required.'}, status=status.HTTP_401_UNAUTHORIZED)

    if not _is_video_owner(request.user, video):
        return Response({'error': 'You do not own this video.'}, status=status.HTTP_403_FORBIDDEN)

    if request.method == 'PATCH':
        serializer = VideoDetailSerializer(video, data=request.data, partial=True, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    # DELETE
    if video.hls_path:
        hls_dir = Path(settings.MEDIA_ROOT) / 'hls' / str(video.id)
        if hls_dir.exists():
            shutil.rmtree(hls_dir)
    if video.original_file:
        orig_path = Path(settings.MEDIA_ROOT) / video.original_file.name
        if orig_path.exists():
            orig_path.unlink()
    video.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(['POST'])
@api_login_required
@parser_classes([MultiPartParser, FormParser])
def video_update_thumbnail(request, video_id):
    try:
        video = Video.objects.get(id=video_id)
    except Video.DoesNotExist:
        return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)

    is_owner = _is_video_owner(request.user, video)
    if not is_owner:
        return Response({'error': 'You do not own this video.'}, status=status.HTTP_403_FORBIDDEN)

    thumbnail = request.FILES.get('thumbnail')
    if not thumbnail:
        return Response({'error': 'thumbnail file required'}, status=status.HTTP_400_BAD_REQUEST)

    video.thumbnail = thumbnail
    video.save(update_fields=['thumbnail'])
    return Response({'thumbnail_url': video.thumbnail_url})


@api_view(['POST'])
def record_view(request, video_id):
    session_key = f'viewed_{video_id}'
    try:
        video = Video.objects.get(id=video_id)
    except Video.DoesNotExist:
        return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)

    if not request.session.get(session_key):
        video.views_count += 1
        video.save(update_fields=['views_count'])
        request.session[session_key] = True

    return Response({'views': video.views_count})


@api_view(['GET'])
def video_status(request, video_id):
    try:
        video = Video.objects.get(id=video_id)
    except Video.DoesNotExist:
        return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)

    return Response({
        'id': str(video.id),
        'status': video.status,
        'hls_url': video.hls_url,
        'processing_error': video.processing_error,
    })


@api_view(['POST'])
@api_login_required
def reprocess_video(request, video_id):
    try:
        video = Video.objects.get(id=video_id)
    except Video.DoesNotExist:
        return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)

    is_owner = _is_video_owner(request.user, video)
    if not is_owner:
        return Response({'error': 'You do not own this video.'}, status=status.HTTP_403_FORBIDDEN)

    if video.status == Video.STATUS_PROCESSING:
        return Response({'error': 'Already processing'}, status=status.HTTP_400_BAD_REQUEST)

    _dispatch_process_video(video.id)
    return Response({'message': 'Reprocessing started', 'id': str(video.id)})


@api_view(['GET'])
def stream_playlist(request, video_id):
    try:
        video = Video.objects.get(id=video_id)
    except Video.DoesNotExist:
        raise Http404

    if not video.hls_path or video.status != Video.STATUS_READY:
        return Response({'error': 'Stream not ready'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

    playlist_path = Path(settings.MEDIA_ROOT) / video.hls_path
    if not playlist_path.exists():
        raise Http404

    response = FileResponse(open(playlist_path, 'rb'), content_type='application/vnd.apple.mpegurl')
    response['Access-Control-Allow-Origin'] = '*'
    return response


# ─── Subscriptions API ───────────────────────────────────────────────────────

@api_view(['POST'])
@api_login_required
def toggle_subscribe(request, slug):
    channel = get_object_or_404(Channel, slug=slug)

    if _is_channel_owner(request.user, channel):
        return Response({'error': 'You cannot subscribe to your own channel.'}, status=status.HTTP_400_BAD_REQUEST)

    sub, created = ChannelSubscription.objects.get_or_create(user=request.user, channel=channel)
    if not created:
        sub.delete()
        subscribed = False
    else:
        subscribed = True
        # Notify channel owner
        if channel.owner:
            Notification.objects.create(
                recipient=channel.owner,
                sender=request.user,
                notification_type=Notification.TYPE_SUBSCRIBE,
                message=f'{request.user.username} subscribed to your channel',
                link=f'/channel/{channel.slug}/',
            )

    cache.delete(f'subs_{request.user.pk}')

    return Response({
        'subscribed': subscribed,
        'subscribers': channel.subscribers.count(),
    })


# ─── Likes API ───────────────────────────────────────────────────────────────

@api_view(['POST'])
@api_login_required
def toggle_like(request, video_id):
    try:
        video = Video.objects.get(id=video_id)
    except Video.DoesNotExist:
        return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)

    like, created = VideoLike.objects.get_or_create(user=request.user, video=video)
    if not created:
        like.delete()
        liked = False
    else:
        liked = True
        # Notify video owner
        if video.channel and video.channel.owner and video.channel.owner != request.user:
            Notification.objects.get_or_create(
                recipient=video.channel.owner,
                sender=request.user,
                notification_type=Notification.TYPE_COMMENT,  # reuse as engagement
                video=video,
                defaults={
                    'message': f'{request.user.username} liked your video "{video.title}"',
                    'link': video.watch_url,
                }
            )

    return Response({'liked': liked, 'likes': video.likes_count})


# ─── Save / Bookmark API ─────────────────────────────────────────────────────

@api_view(['POST'])
@api_login_required
def toggle_save(request, video_id):
    """POST /api/videos/<id>/save/ — toggle saved/bookmarked"""
    try:
        video = Video.objects.get(id=video_id)
    except Video.DoesNotExist:
        return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)

    save, created = SavedVideo.objects.get_or_create(user=request.user, video=video)
    if not created:
        save.delete()
        saved = False
    else:
        saved = True

    return Response({'saved': saved})


# ─── Watch History API ───────────────────────────────────────────────────────

@api_view(['DELETE'])
@api_login_required
def delete_watch_history(request, video_id):
    """DELETE /api/videos/<id>/history/ — remove this video from the user's watch history"""
    WatchHistory.objects.filter(user=request.user, video_id=video_id).delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


# ─── Watch Progress API ──────────────────────────────────────────────────────

@api_view(['POST'])
@api_login_required
def update_watch_progress(request, video_id):
    """
    POST /api/videos/<id>/progress/
    Body: { seconds: float, completed: bool }
    Updates WatchHistory and records WatchTimeEntry for analytics.
    """
    try:
        video = Video.objects.get(id=video_id)
    except Video.DoesNotExist:
        return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)

    try:
        seconds = float(request.data.get('seconds', 0))
        completed = bool(request.data.get('completed', False))
    except Exception:
        # Body may be empty (e.g. browser beforeunload fires fetch with no body)
        return Response({'ok': True})

    wh, created = WatchHistory.objects.get_or_create(user=request.user, video=video)
    prev_seconds = 0 if created else wh.progress_seconds
    wh.progress_seconds = seconds
    wh.completed = completed
    wh.save(update_fields=['progress_seconds', 'completed', 'watched_at'])

    # Aggregate watch time per day
    from django.utils import timezone
    today = timezone.now().date()
    delta = max(0, seconds - prev_seconds)
    if delta > 0:
        entry, __ = WatchTimeEntry.objects.get_or_create(video=video, date=today)
        WatchTimeEntry.objects.filter(pk=entry.pk).update(
            total_seconds=entry.total_seconds + int(delta)
        )

    return Response({'ok': True, 'progress': seconds})


# ─── Comments API ────────────────────────────────────────────────────────────

@api_view(['GET', 'POST'])
def comment_list_create(request, video_id):
    try:
        video = Video.objects.get(id=video_id)
    except Video.DoesNotExist:
        return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)

    if request.method == 'GET':
        # Return only top-level comments; replies embedded inside
        comments = video.comments.filter(parent__isnull=True).select_related('user').prefetch_related(
            'replies__user', 'comment_likes', 'replies__comment_likes'
        )
        return Response(CommentSerializer(comments, many=True, context={'request': request}).data)

    if not request.user.is_authenticated:
        return Response({'error': 'Authentication required.'}, status=status.HTTP_401_UNAUTHORIZED)

    if not video.comments_enabled:
        return Response({'error': 'Comments are disabled for this video.'}, status=status.HTTP_403_FORBIDDEN)

    serializer = CommentSerializer(data=request.data, context={'request': request})
    if serializer.is_valid():
        parent_id = request.data.get('parent')
        parent = None
        if parent_id:
            try:
                parent = Comment.objects.get(id=parent_id, video=video, parent__isnull=True)
            except Comment.DoesNotExist:
                return Response({'error': 'Invalid parent comment.'}, status=status.HTTP_400_BAD_REQUEST)

        comment = serializer.save(user=request.user, video=video, parent=parent)

        # Notifications
        _handle_comment_notifications(comment, video, request.user)

        return Response(CommentSerializer(comment, context={'request': request}).data, status=status.HTTP_201_CREATED)
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


def _handle_comment_notifications(comment, video, actor):
    """Create notifications for mentions, replies, and video owner comments."""
    # Notify video owner (if not the commenter)
    if video.channel and video.channel.owner and video.channel.owner != actor:
        Notification.objects.create(
            recipient=video.channel.owner,
            sender=actor,
            notification_type=Notification.TYPE_COMMENT,
            video=video,
            comment=comment,
            message=f'{actor.username} commented on your video "{video.title}"',
            link=video.watch_url,
        )

    # Notify parent comment author on reply
    if comment.parent and comment.parent.user != actor:
        Notification.objects.create(
            recipient=comment.parent.user,
            sender=actor,
            notification_type=Notification.TYPE_REPLY,
            video=video,
            comment=comment,
            message=f'{actor.username} replied to your comment',
            link=video.watch_url,
        )

    # Notify @mentions
    for username in comment.get_mentions():
        try:
            mentioned_user = User.objects.get(username=username)
        except User.DoesNotExist:
            continue
        if mentioned_user == actor:
            continue
        Notification.objects.create(
            recipient=mentioned_user,
            sender=actor,
            notification_type=Notification.TYPE_MENTION,
            video=video,
            comment=comment,
            message=f'{actor.username} mentioned you in a comment',
            link=video.watch_url,
        )


@api_view(['DELETE'])
@api_login_required
def comment_delete(request, comment_id):
    try:
        comment = Comment.objects.select_related('video__channel').get(id=comment_id)
    except Comment.DoesNotExist:
        return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)

    is_comment_author = comment.user == request.user
    is_video_owner = (
        comment.video.channel
        and _is_video_owner(request.user, comment.video)
    )

    if not (is_comment_author or is_video_owner):
        return Response({'error': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)

    comment.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(['POST'])
@api_login_required
def toggle_comment_like(request, comment_id):
    """POST /api/comments/<id>/like/ — toggle like on comment"""
    try:
        comment = Comment.objects.get(id=comment_id)
    except Comment.DoesNotExist:
        return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)

    cl, created = CommentLike.objects.get_or_create(user=request.user, comment=comment)
    if not created:
        cl.delete()
        liked = False
    else:
        liked = True

    return Response({'liked': liked, 'likes': comment.likes_count})


@api_view(['POST'])
@api_login_required
def pin_comment(request, comment_id):
    """POST /api/comments/<id>/pin/ — toggle pin (video owner only)"""
    try:
        comment = Comment.objects.select_related('video__channel').get(id=comment_id)
    except Comment.DoesNotExist:
        return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)

    is_video_owner = (
        comment.video.channel
        and _is_video_owner(request.user, comment.video)
    )
    if not is_video_owner:
        return Response({'error': 'Only the video owner can pin comments.'}, status=status.HTTP_403_FORBIDDEN)

    # Unpin all others first
    Comment.objects.filter(video=comment.video, is_pinned=True).update(is_pinned=False)

    if not comment.is_pinned:
        comment.is_pinned = True
        comment.save(update_fields=['is_pinned'])
        pinned = True
    else:
        # Was already pinned — just unpinned above
        pinned = False

    return Response({'pinned': pinned})


# ─── Chapters API ────────────────────────────────────────────────────────────

@api_view(['GET', 'POST'])
def chapter_list_create(request, video_id):
    try:
        video = Video.objects.get(id=video_id)
    except Video.DoesNotExist:
        return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)

    if request.method == 'GET':
        return Response(VideoChapterSerializer(video.chapters.all(), many=True).data)

    if not request.user.is_authenticated:
        return Response({'error': 'Authentication required.'}, status=status.HTTP_401_UNAUTHORIZED)

    is_owner = _is_video_owner(request.user, video)
    if not is_owner:
        return Response({'error': 'You do not own this video.'}, status=status.HTTP_403_FORBIDDEN)

    serializer = VideoChapterSerializer(data=request.data)
    if serializer.is_valid():
        serializer.save(video=video)
        return Response(serializer.data, status=status.HTTP_201_CREATED)
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@api_view(['PATCH', 'DELETE'])
@api_login_required
def chapter_detail(request, chapter_id):
    try:
        chapter = VideoChapter.objects.select_related('video__channel').get(id=chapter_id)
    except VideoChapter.DoesNotExist:
        return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)

    is_owner = (
        chapter.video.channel
        and _is_video_owner(request.user, chapter.video)
    )
    if not is_owner:
        return Response({'error': 'You do not own this video.'}, status=status.HTTP_403_FORBIDDEN)

    if request.method == 'PATCH':
        serializer = VideoChapterSerializer(chapter, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    chapter.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


# ─── Playlists API ───────────────────────────────────────────────────────────

@api_view(['GET', 'POST'])
@api_login_required
def playlist_list_create(request):
    """
    GET  /api/playlists/        — list my playlists
    POST /api/playlists/        — create a playlist
    """
    if request.method == 'GET':
        playlists = Playlist.objects.filter(owner=request.user)
        return Response(PlaylistListSerializer(playlists, many=True).data)

    serializer = PlaylistListSerializer(data=request.data)
    if serializer.is_valid():
        serializer.save(owner=request.user)
        return Response(serializer.data, status=status.HTTP_201_CREATED)
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@api_view(['GET', 'PATCH', 'DELETE'])
def playlist_detail_api(request, playlist_id):
    """
    GET    /api/playlists/<id>/  — detail with items (respects visibility)
    PATCH  /api/playlists/<id>/  — owner only
    DELETE /api/playlists/<id>/  — owner only
    """
    playlist = get_object_or_404(Playlist, id=playlist_id)

    if request.method == 'GET':
        if not playlist.is_public and (
            not request.user.is_authenticated or playlist.owner != request.user
        ):
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)
        return Response(PlaylistSerializer(playlist).data)

    if not request.user.is_authenticated:
        return Response({'error': 'Authentication required.'}, status=status.HTTP_401_UNAUTHORIZED)

    if playlist.owner != request.user:
        return Response({'error': 'You do not own this playlist.'}, status=status.HTTP_403_FORBIDDEN)

    if request.method == 'PATCH':
        serializer = PlaylistListSerializer(playlist, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    playlist.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(['POST', 'DELETE'])
@api_login_required
def playlist_video(request, playlist_id, video_id):
    """
    POST   /api/playlists/<id>/videos/<vid>/ — add video to playlist
    DELETE /api/playlists/<id>/videos/<vid>/ — remove video from playlist
    """
    playlist = get_object_or_404(Playlist, id=playlist_id)
    if playlist.owner != request.user:
        return Response({'error': 'You do not own this playlist.'}, status=status.HTTP_403_FORBIDDEN)

    video = get_object_or_404(Video, id=video_id)

    if request.method == 'POST':
        order = playlist.items.count()
        item, created = PlaylistItem.objects.get_or_create(
            playlist=playlist, video=video, defaults={'order': order}
        )
        if not created:
            return Response({'error': 'Video already in playlist.'}, status=status.HTTP_400_BAD_REQUEST)
        return Response(PlaylistItemSerializer(item).data, status=status.HTTP_201_CREATED)

    PlaylistItem.objects.filter(playlist=playlist, video=video).delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


# ─── Subtitles API ───────────────────────────────────────────────────────────

@api_view(['GET'])
def subtitle_list(request, video_id):
    """GET /api/videos/<id>/subtitles/ — list all subtitle tracks."""
    video = get_object_or_404(Video, id=video_id)
    qs = video.subtitles.all()
    return Response(SubtitleSerializer(qs, many=True).data)


@api_view(['POST'])
@api_login_required
def subtitle_upload(request, video_id):
    """POST /api/videos/<id>/subtitles/ — upload a subtitle file (owner only)."""
    video = get_object_or_404(Video, id=video_id)
    is_owner = _is_video_owner(request.user, video)
    if not is_owner:
        return Response({'error': 'Only the channel owner can upload subtitles.'}, status=status.HTTP_403_FORBIDDEN)

    language       = request.data.get('language', 'en').strip()
    language_label = request.data.get('language_label', language).strip()
    sub_file       = request.FILES.get('file')
    if not sub_file:
        return Response({'error': 'No file provided.'}, status=status.HTTP_400_BAD_REQUEST)

    ext = sub_file.name.rsplit('.', 1)[-1].lower()
    fmt = Subtitle.FORMAT_VTT if ext == 'vtt' else Subtitle.FORMAT_SRT

    # Delete existing manual subtitle for this language if it exists
    Subtitle.objects.filter(video=video, language=language, is_auto_generated=False).delete()

    subtitle = Subtitle(
        video=video,
        language=language,
        language_label=language_label or language,
        format=fmt,
        is_auto_generated=False,
    )
    subtitle.file = sub_file
    subtitle.save()

    # Kick off segment indexing in the background so this subtitle is
    # searchable via in-video speech search (works for VTT and SRT uploads).
    try:
        from .tasks import reindex_segments_task
        reindex_segments_task.apply_async(args=[subtitle.id], queue='default')
    except Exception:
        pass  # non-critical — subtitle still saved, search index rebuilt on next regenerate

    return Response(SubtitleSerializer(subtitle).data, status=status.HTTP_201_CREATED)


@api_view(['DELETE'])
@api_login_required
def subtitle_delete(request, video_id, subtitle_id):
    """DELETE /api/videos/<id>/subtitles/<sub_id>/ — owner only."""
    video    = get_object_or_404(Video, id=video_id)
    subtitle = get_object_or_404(Subtitle, id=subtitle_id, video=video)
    is_owner = _is_video_owner(request.user, video)
    if not is_owner:
        return Response({'error': 'Forbidden.'}, status=status.HTTP_403_FORBIDDEN)
    subtitle.file.delete(save=False)
    subtitle.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(['POST'])
@api_login_required
def subtitle_regenerate(request, video_id):
    """POST /api/videos/<id>/subtitles/regenerate/ — re-run Whisper (owner only)."""
    video    = get_object_or_404(Video, id=video_id)
    is_owner = _is_video_owner(request.user, video)
    if not is_owner:
        return Response({'error': 'Forbidden.'}, status=status.HTTP_403_FORBIDDEN)
    language = request.data.get('language', 'en')
    # Delete existing auto-caption AND stale speech segments so the task starts clean
    Subtitle.objects.filter(video=video, language=language, is_auto_generated=True).delete()
    VideoSegment.objects.filter(video=video).delete()
    try:
        from .tasks import generate_captions_task
        generate_captions_task.apply_async(args=[str(video_id), language], queue='captions')
        return Response({'message': f'Caption generation started for language: {language}'})
    except Exception as exc:
        return Response({'error': f'Could not dispatch task: {exc}'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)


# ─── Subtitle editor ──────────────────────────────────────────────────────────

def _parse_subtitle_cues(content: str, fmt: str) -> list:
    """
    Parse WebVTT or SRT content into a list of cue dicts:
      {id, start, end, text}
    where start/end are strings in 'HH:MM:SS.mmm' format.
    """
    import re as _re
    content = content.replace('\r\n', '\n').replace('\r', '\n')
    cues = []

    if fmt == 'vtt':
        # Skip everything up to and including the WEBVTT header line
        body = _re.sub(r'^.*?WEBVTT[^\n]*\n', '', content, count=1, flags=_re.DOTALL)
        # Each cue is separated by a blank line
        blocks = [b.strip() for b in _re.split(r'\n{2,}', body) if b.strip()]
        ts_pat = _re.compile(
            r'^(?P<start>\d+:\d{2}:\d{2}[.,]\d{3}|\d+:\d{2}[.,]\d{3})'
            r'\s*-->\s*'
            r'(?P<end>\d+:\d{2}:\d{2}[.,]\d{3}|\d+:\d{2}[.,]\d{3})'
        )
        for block in blocks:
            lines = block.split('\n')
            # Optional cue identifier on first line
            if lines and '-->' not in lines[0]:
                cue_id = lines[0].strip()
                lines = lines[1:]
            else:
                cue_id = str(len(cues) + 1)
            if not lines:
                continue
            m = ts_pat.match(lines[0])
            if not m:
                continue
            start, end = m.group('start'), m.group('end')
            # Strip cue settings (position, align etc.) from timestamp line
            text = '\n'.join(lines[1:]).strip()
            # Normalise MM:SS.mmm → HH:MM:SS.mmm
            def _norm(t):
                t = t.replace(',', '.')
                parts = t.split(':')
                if len(parts) == 2:
                    t = f'00:{t}'
                return t
            cues.append({'id': cue_id, 'start': _norm(start), 'end': _norm(end), 'text': text})

    else:  # srt
        ts_pat = _re.compile(
            r'^(?P<start>\d+:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(?P<end>\d+:\d{2}:\d{2}[,\.]\d{3})'
        )
        for block in _re.split(r'\n{2,}', content):
            lines = [l for l in block.split('\n') if l.strip()]
            if not lines:
                continue
            # Skip the cue number line
            start_idx = 0
            if lines[0].strip().isdigit():
                start_idx = 1
            if start_idx >= len(lines):
                continue
            m = ts_pat.match(lines[start_idx])
            if not m:
                continue
            start = m.group('start').replace(',', '.')
            end   = m.group('end').replace(',', '.')
            text  = '\n'.join(lines[start_idx + 1:]).strip()
            cues.append({'id': str(len(cues) + 1), 'start': start, 'end': end, 'text': text})

    return cues


def _build_subtitle_content(cues: list, fmt: str) -> str:
    """Rebuild WebVTT or SRT content from cue list."""
    if fmt == 'vtt':
        lines = ['WEBVTT', '']
        for i, c in enumerate(cues, 1):
            lines += [str(i), f"{c['start']} --> {c['end']}", c['text'], '']
        return '\n'.join(lines)
    else:  # srt
        lines = []
        for i, c in enumerate(cues, 1):
            start = c['start'].replace('.', ',')
            end   = c['end'].replace('.', ',')
            lines += [str(i), f"{start} --> {end}", c['text'], '']
        return '\n'.join(lines)


@login_required
def transcript_editor_page(request, video_id):
    """
    GET /videos/<id>/transcript/
    Dedicated transcript + speaker assignment editor.
    Shows all VideoSegments in order with their text and speaker label.
    Editors can reassign speakers inline without leaving the page.
    """
    video = get_object_or_404(Video, id=video_id)
    if not _is_video_owner(request.user, video):
        from django.core.exceptions import PermissionDenied
        raise PermissionDenied

    # All segments ordered by time
    segments = (
        VideoSegment.objects
        .filter(video=video)
        .select_related('speaker_identity')
        .order_by('start_seconds')
    )

    # Speakers visible in this video (for the reassign dropdown)
    video_speakers = list(
        SpeakerIdentity.objects
        .filter(segments__video=video)
        .distinct()
        .order_by('name')
    )

    # Also include ALL global speakers so user can assign a segment to a speaker
    # who hasn't been detected in this video yet (e.g. after a partial diarization)
    user_channels = _user_channels(request.user)
    user_channel_ids = list(user_channels.values_list('id', flat=True))
    all_speakers = list(
        SpeakerIdentity.objects
        .filter(segments__video__channel_id__in=user_channel_ids)
        .distinct()
        .order_by('name')
    )

    has_diarization = any(s.speaker_label for s in segments)

    return render(request, 'videos/transcript_editor.html', {
        'video':           video,
        'segments':        segments,
        'video_speakers':  video_speakers,
        'all_speakers':    all_speakers,
        'has_diarization': has_diarization,
        'unread_count':    _unread_count(request),
    })


@login_required
def subtitle_editor_page(request, video_id, subtitle_id):
    """Web editor for a single subtitle track — accessible to channel editors."""
    video    = get_object_or_404(Video, id=video_id)
    subtitle = get_object_or_404(Subtitle, id=subtitle_id, video=video)
    if not _is_video_owner(request.user, video):
        from django.core.exceptions import PermissionDenied
        raise PermissionDenied
    return render(request, 'videos/subtitle_editor.html', {
        'video':    video,
        'subtitle': subtitle,
    })


@api_view(['GET', 'POST'])
@api_login_required
def subtitle_cues(request, video_id, subtitle_id):
    """
    GET  /api/videos/<id>/subtitles/<sub_id>/cues/ — return parsed cues as JSON.
    POST /api/videos/<id>/subtitles/<sub_id>/cues/ — save edited cues back to file.
    """
    video    = get_object_or_404(Video, id=video_id)
    subtitle = get_object_or_404(Subtitle, id=subtitle_id, video=video)
    if not _is_video_owner(request.user, video):
        return Response({'error': 'Forbidden.'}, status=status.HTTP_403_FORBIDDEN)

    if request.method == 'GET':
        try:
            raw = subtitle.file.read().decode('utf-8')
        except Exception as exc:
            return Response({'error': f'Could not read file: {exc}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        cues = _parse_subtitle_cues(raw, subtitle.format)
        return Response({'cues': cues, 'format': subtitle.format, 'language': subtitle.language_label})

    # POST — save edited cues
    cues = request.data.get('cues')
    if not isinstance(cues, list):
        return Response({'error': 'cues must be a list.'}, status=status.HTTP_400_BAD_REQUEST)
    content = _build_subtitle_content(cues, subtitle.format)
    from django.core.files.base import ContentFile
    old_name = subtitle.file.name.split('/')[-1]
    subtitle.file.delete(save=False)
    subtitle.file.save(old_name, ContentFile(content.encode('utf-8')), save=True)
    return Response({'ok': True, 'cue_count': len(cues)})


# ─── Audio Tracks API ─────────────────────────────────────────────────────────

@api_view(['GET'])
def audio_track_list(request, video_id):
    """GET /api/videos/<id>/audio-tracks/ — list audio tracks."""
    video = get_object_or_404(Video, id=video_id)
    qs = video.audio_tracks.all()
    return Response(AudioTrackSerializer(qs, many=True).data)


@api_view(['POST'])
@api_login_required
def audio_track_extract(request, video_id):
    """POST /api/videos/<id>/audio-tracks/extract/ — trigger extraction (owner only)."""
    video    = get_object_or_404(Video, id=video_id)
    is_owner = _is_video_owner(request.user, video)
    if not is_owner:
        return Response({'error': 'Forbidden.'}, status=status.HTTP_403_FORBIDDEN)
    try:
        from .tasks import extract_audio_tracks_task
        extract_audio_tracks_task.apply_async(args=[str(video_id)], queue='processing')
        return Response({'message': 'Audio track extraction started.'})
    except Exception as exc:
        return Response({'error': f'Could not dispatch task: {exc}'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)


# ─── Video Frames API ────────────────────────────────────────────────────────

@api_view(['GET'])
def video_frames_list(request, video_id):
    """GET /api/videos/<id>/frames/ — list all VideoFrame records for a video."""
    video = get_object_or_404(Video, id=video_id)
    qs = video.frames.all()
    return Response(VideoFrameSerializer(qs, many=True).data)


@api_view(['POST'])
@api_login_required
def video_frames_analyze(request, video_id):
    """
    POST /api/videos/<id>/frames/analyze/ — (re-)trigger YOLOv8 frame analysis.
    Owner only. Deletes existing VideoFrame rows then re-queues the task.
    """
    video    = get_object_or_404(Video, id=video_id)
    is_owner = _is_video_owner(request.user, video)
    if not is_owner:
        return Response({'error': 'Forbidden.'}, status=status.HTTP_403_FORBIDDEN)

    VideoFrame.objects.filter(video=video).delete()
    try:
        from .tasks import analyze_video_frames_task
        analyze_video_frames_task.apply_async(args=[str(video_id)], queue='processing')
        return Response({'message': 'Visual analysis started.'})
    except Exception as exc:
        return Response({'error': f'Could not dispatch task: {exc}'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)


@api_view(['POST'])
@api_login_required
def run_diarization(request, video_id):
    """
    POST /api/videos/<id>/diarize/
    Triggers speaker diarization via pyannote.audio.
    Requires HF_TOKEN to be set in server settings.
    """
    video = get_object_or_404(Video, id=video_id)
    if not _is_video_owner(request.user, video):
        return Response({'error': 'Forbidden.'}, status=status.HTTP_403_FORBIDDEN)

    hf_token = getattr(settings, 'HF_TOKEN', '')
    if not hf_token:
        return Response(
            {'error': 'HF_TOKEN is not configured on this server. Add it to .env and restart Celery.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    segment_count = VideoSegment.objects.filter(video=video).count()
    if segment_count == 0:
        return Response(
            {'error': 'No transcript segments found. Run Whisper auto-generate first.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        from .tasks import run_diarization_task
        task = run_diarization_task.apply_async(args=[str(video_id)], queue='captions')
        return Response({'task_id': task.id, 'segment_count': segment_count})
    except Exception as exc:
        return Response({'error': f'Could not dispatch task: {exc}'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)


# ─── Face Recognition API ────────────────────────────────────────────────────

@api_view(['GET'])
def video_faces_list(request, video_id):
    """
    GET /api/videos/<id>/faces/
    Returns all FaceIdentity objects that appear in this video,
    each with their appearances (timestamps) and a thumbnail.
    Public endpoint — identity names are always shown.
    """
    video = get_object_or_404(Video, id=video_id)
    # Find all identities that have at least one DetectedFace in this video
    identity_ids = (
        DetectedFace.objects
        .filter(video=video)
        .values_list('identity_id', flat=True)
        .distinct()
    )
    # Tagged (user-named) first, then auto-named / “untagged” placeholders.
    identities = FaceIdentity.objects.filter(id__in=identity_ids).order_by(
        'is_auto_named', 'name', 'id'
    )
    serializer = FaceIdentitySerializer(
        identities, many=True, context={'video_id': video_id}
    )
    return Response(serializer.data)


@api_view(['PATCH'])
@api_login_required
def face_identity_tag(request, identity_id):
    """
    PATCH /api/faces/<id>/tag/
    Body: {"name": "John Doe"}
    Renames a FaceIdentity. Owner of any video this identity appears in can tag it.
    """
    identity = get_object_or_404(FaceIdentity, id=identity_id)

    user_channels = _user_channels(request.user)
    if not user_channels.exists():
        return Response({'error': 'No channel.'}, status=status.HTTP_403_FORBIDDEN)

    appears_in_own_video = DetectedFace.objects.filter(
        identity=identity,
        video__channel__in=user_channels,
    ).exists()
    if not appears_in_own_video:
        return Response({'error': 'Forbidden.'}, status=status.HTTP_403_FORBIDDEN)

    name = request.data.get('name', '').strip()
    if not name:
        return Response({'error': 'name is required.'}, status=status.HTTP_400_BAD_REQUEST)

    identity.name = name
    identity.is_auto_named = False
    identity.save(update_fields=['name', 'is_auto_named'])
    return Response(FaceIdentitySerializer(identity, context={}).data)


@api_view(['DELETE'])
@api_login_required
def face_identity_remove_from_video(request, video_id, identity_id):
    """
    DELETE /api/videos/<video_id>/faces/<identity_id>/remove/
    Removes all DetectedFace rows for this identity in this video only.
    If the identity has no faces left anywhere, it is also deleted.
    Owner of the video only.
    """
    video    = get_object_or_404(Video, id=video_id)
    identity = get_object_or_404(FaceIdentity, id=identity_id)
    is_owner = _is_video_owner(request.user, video)
    if not is_owner:
        return Response({'error': 'Forbidden.'}, status=status.HTTP_403_FORBIDDEN)

    DetectedFace.objects.filter(video=video, identity=identity).delete()

    # Clean up orphaned identity (no faces left anywhere)
    if not DetectedFace.objects.filter(identity=identity).exists():
        identity.delete()

    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(['POST'])
@api_login_required
def face_identity_merge(request, identity_id):
    """
    POST /api/faces/<id>/merge/
    Body: {"into": <target_identity_id>}
    Merges this identity into another (same person tagged twice).
    All DetectedFace rows re-pointed; source identity deleted.
    """
    source = get_object_or_404(FaceIdentity, id=identity_id)
    target_id = request.data.get('into')
    if not target_id:
        return Response({'error': '"into" identity id required.'}, status=status.HTTP_400_BAD_REQUEST)
    target = get_object_or_404(FaceIdentity, id=target_id)
    if source.pk == target.pk:
        return Response({'error': 'Cannot merge an identity into itself.'}, status=status.HTTP_400_BAD_REQUEST)

    user_channels = _user_channels(request.user)
    if not user_channels.exists():
        return Response({'error': 'No channel.'}, status=status.HTTP_403_FORBIDDEN)
    for ident in (source, target):
        if not DetectedFace.objects.filter(identity=ident).filter(
            Q(video__channel__in=user_channels) | Q(photo__channel__in=user_channels)
        ).exists():
            return Response({'error': 'Forbidden.'}, status=status.HTTP_403_FORBIDDEN)

    DetectedFace.objects.filter(identity=source).update(identity=target)
    source.delete()
    return Response(FaceIdentitySerializer(target, context={}).data)


# ─── Face Management Frontend Pages ──────────────────────────────────────────

@login_required
def faces_page(request):
    """
    GET /faces/
    Lists FaceIdentity rows that appear in the current user's channel videos.
    Each user only sees identities from their own content.
    Orphaned identities (no DetectedFace rows at all) are shown separately.
    """
    user_channels = _user_channels(request.user)
    has_channel   = user_channels.exists()

    from django.db.models import Max

    if has_channel:
        # Channel IDs as a list so we can reuse in multiple Q() filters efficiently
        user_channel_ids = list(user_channels.values_list('id', flat=True))
        ch_q = Q(
            Q(faces__video__channel_id__in=user_channel_ids) |
            Q(faces__photo__channel_id__in=user_channel_ids)
        )

        q = request.GET.get('q', '').strip()
        # Single query: annotate all per-identity counts in SQL — no Python loop queries.
        # All counts are restricted to faces WHERE crop_path != '' so they match exactly
        # what the person detail page shows (which also filters to saved crop images only).
        has_crop = ~Q(faces__crop_path='')
        identities = (
            FaceIdentity.objects
            .filter(ch_q)
            .distinct()
            .annotate(
                latest_video_upload=Max('faces__video__created_at'),
                total=Count('faces', filter=ch_q & has_crop),
                confirmed=Count('faces', filter=ch_q & has_crop & Q(faces__status=DetectedFace.STATUS_CONFIRMED)),
                rejected=Count('faces', filter=ch_q & has_crop & Q(faces__status=DetectedFace.STATUS_REJECTED)),
                video_count=Count('faces__video', filter=Q(faces__video__channel_id__in=user_channel_ids), distinct=True),
                photo_count=Count('faces__photo', filter=Q(faces__photo__channel_id__in=user_channel_ids), distinct=True),
            )
            .order_by('-latest_video_upload')
        )
        if q:
            identities = identities.filter(name__icontains=q)
    else:
        q = ''
        identities = FaceIdentity.objects.none()

    identity_data = []

    for ident in identities:
        identity_data.append({
            'identity':   ident,
            'total':      ident.total,
            'confirmed':  ident.confirmed,
            'rejected':   ident.rejected,
            'unreviewed': max(0, ident.total - ident.confirmed - ident.rejected),
            'video_count': ident.video_count,
            'photo_count': getattr(ident, 'photo_count', 0),
            'is_photo_only': False,
        })

    orphaned_data = list({'identity': ident} for ident in FaceIdentity.objects.filter(faces__isnull=True))

    # Global counts (always reflect full search results, ignoring tab filter)
    total_named  = sum(1 for r in identity_data if not r['identity'].is_auto_named)
    total_auto   = sum(1 for r in identity_data if r['identity'].is_auto_named)
    total_count  = total_named + total_auto

    # Apply tab filter before pagination
    active_filter = request.GET.get('filter', 'all')
    if active_filter == 'named':
        filtered_data = [r for r in identity_data if not r['identity'].is_auto_named]
    elif active_filter == 'auto':
        filtered_data = [r for r in identity_data if r['identity'].is_auto_named]
    else:
        active_filter = 'all'
        filtered_data = identity_data

    try:
        page_size = int(request.GET.get('page_size', 25))
    except (ValueError, TypeError):
        page_size = 25
    if page_size not in (25, 50, 75, 100):
        page_size = 25

    from django.core.paginator import Paginator
    paginator = Paginator(filtered_data, page_size)
    page_obj  = paginator.get_page(request.GET.get('page', 1))

    return render(request, 'videos/faces.html', {
        'identity_data': page_obj,
        'page_obj':      page_obj,
        'total_count':   total_count,
        'total_named':   total_named,
        'total_auto':    total_auto,
        'active_filter': active_filter,
        'page_size':     page_size,
        'search_query':  q,
        'orphaned_data': orphaned_data,
        'has_channel':   has_channel,
        'unread_count':  _unread_count(request),
    })


@login_required
def face_identity_page(request, identity_id):
    """
    GET /faces/<id>/?page=N
    Shows face crops for this identity grouped by video, paginated (5 videos/page).
    """
    from django.core.paginator import Paginator

    identity      = get_object_or_404(FaceIdentity, id=identity_id)
    user_channels = _user_channels(request.user)
    user_channel_ids = list(user_channels.values_list('id', flat=True))

    if not user_channel_ids:
        can_edit = False
        visible_faces = (
            DetectedFace.objects
            .filter(identity=identity)
            .exclude(crop_path='')
            .select_related('video', 'photo')
            .order_by('video_id', 'photo_id', 'timestamp')
        )
    else:
        own_face_q = Q(video__channel_id__in=user_channel_ids) | Q(photo__channel_id__in=user_channel_ids)
        appears_in_mine = DetectedFace.objects.filter(identity=identity).filter(own_face_q).exists()
        if not appears_in_mine:
            from django.core.exceptions import PermissionDenied
            raise PermissionDenied
        can_edit = True
        visible_faces = (
            DetectedFace.objects
            .filter(identity=identity)
            .filter(own_face_q)
            .exclude(crop_path='')
            .select_related('video', 'photo')
            .order_by('video_id', 'photo_id', 'timestamp')
        )

    total_faces_count = visible_faces.count()

    grouped = {}
    for f in visible_faces:
        if f.video_id:
            key = f'v:{f.video_id}'
            if key not in grouped:
                grouped[key] = {'kind': 'video', 'video': f.video, 'photo': None, 'faces': []}
            grouped[key]['faces'].append(f)
        elif f.photo_id:
            key = f'p:{f.photo_id}'
            if key not in grouped:
                grouped[key] = {'kind': 'photo', 'video': None, 'photo': f.photo, 'faces': []}
            grouped[key]['faces'].append(f)

    all_groups = list(grouped.values())

    # ── Full timestamps for timeline (ALL detections, not just cropped ones) ──
    # Uses one bulk query across all videos on this page to avoid N+1.
    from collections import defaultdict as _ts_dd
    video_ids_in_groups = [g['video'].id for g in all_groups if g['kind'] == 'video']
    if video_ids_in_groups:
        _all_ts = (
            DetectedFace.objects
            .filter(identity=identity, video_id__in=video_ids_in_groups)
            .values_list('video_id', 'timestamp')
            .order_by('video_id', 'timestamp')
        )
        _ts_map = _ts_dd(list)
        for _vid_id, _ts in _all_ts:
            _ts_map[str(_vid_id)].append(_ts)
        for g in all_groups:
            if g['kind'] == 'video':
                g['all_timestamps'] = _ts_map.get(str(g['video'].id), [])
            else:
                g['all_timestamps'] = []
    else:
        for g in all_groups:
            g['all_timestamps'] = []

    # ── Co-identity stacked avatars per video group ────────────────────────────
    # For each video, fetch the OTHER named identities that also appear in it.
    # Used to show stacked avatar circles on the video card (like GitHub contributors).
    # One bulk query — no N+1.
    if video_ids_in_groups:
        _co_rows = (
            DetectedFace.objects
            .filter(video_id__in=video_ids_in_groups)
            .exclude(identity=identity)
            .exclude(identity__isnull=True)
            .filter(identity__is_auto_named=False)
            .values('video_id', 'identity_id', 'identity__name')
            .annotate(appearances=Count('id'))
            .order_by('video_id', '-appearances', 'identity__name')
        )
        from collections import defaultdict as _co_dd
        _co_vid_map = _co_dd(list)
        for row in _co_rows:
            vid_id = str(row['video_id'])
            iid = row['identity_id']
            # Ordered by appearances desc per video, then name for stable ties.
            _co_vid_map[vid_id].append(iid)

        # Batch fetch FaceIdentity objects for thumbnail URLs
        all_co_ids = {iid for ids in _co_vid_map.values() for iid in ids}
        _co_identity_map = {fi.pk: fi for fi in FaceIdentity.objects.filter(pk__in=all_co_ids)}

        for g in all_groups:
            if g['kind'] == 'video':
                vid_id = str(g['video'].id)
                g['co_identities'] = [
                    _co_identity_map[iid]
                    for iid in _co_vid_map.get(vid_id, [])[:5]
                    if iid in _co_identity_map
                ]
            else:
                g['co_identities'] = []
    else:
        for g in all_groups:
            g['co_identities'] = []

    paginator = Paginator(all_groups, 5)
    page_obj  = paginator.get_page(request.GET.get('page', 1))

    if user_channel_ids:
        mergeable_ids = (
            DetectedFace.objects
            .filter(Q(video__channel_id__in=user_channel_ids) | Q(photo__channel_id__in=user_channel_ids))
            .exclude(identity_id=identity.pk)
            .values_list('identity_id', flat=True)
            .distinct()
        )
        all_identities = FaceIdentity.objects.filter(id__in=mergeable_ids).order_by('name')
    else:
        all_identities = FaceIdentity.objects.none()

    can_delete = False
    if can_edit:
        total_all = DetectedFace.objects.filter(identity=identity).count()
        my_faces_cnt = (
            DetectedFace.objects
            .filter(identity=identity)
            .filter(Q(video__channel_id__in=user_channel_ids) | Q(photo__channel_id__in=user_channel_ids))
            .count()
        )
        can_delete = (total_all == my_faces_cnt)

    # ── Co-appearances ────────────────────────────────────────────────────────
    # Find identities who appear in the same videos or photos as this identity,
    # ranked by total shared sources.
    from collections import defaultdict as _dd

    my_video_ids = (
        DetectedFace.objects.filter(identity=identity, video__isnull=False)
        .values_list('video_id', flat=True).distinct()
    )
    my_photo_ids = (
        DetectedFace.objects.filter(identity=identity, photo__isnull=False)
        .values_list('photo_id', flat=True).distinct()
    )

    _co = _dd(lambda: {'video_count': 0, 'photo_count': 0})
    # Only user-tagged identities (not Person #N). Counts / top-N are computed in that set
    # so ordering stays correct. Tagging someone adds them here on the next page load — no cache.
    _tagged_co_q = (
        Q(identity__is_auto_named=False)
        & ~Q(identity=identity)
        & Q(identity__isnull=False)
    )
    for row in (DetectedFace.objects
                .filter(video_id__in=my_video_ids)
                .filter(_tagged_co_q)
                .values('identity_id')
                .annotate(cnt=Count('video_id', distinct=True))):
        _co[row['identity_id']]['video_count'] = row['cnt']
    for row in (DetectedFace.objects
                .filter(photo_id__in=my_photo_ids)
                .filter(_tagged_co_q)
                .values('identity_id')
                .annotate(cnt=Count('photo_id', distinct=True))):
        _co[row['identity_id']]['photo_count'] = row['cnt']

    _sorted_co = sorted(_co.items(),
                        key=lambda x: x[1]['video_count'] + x[1]['photo_count'],
                        reverse=True)[:20]
    _co_id_map = {i.pk: i for i in FaceIdentity.objects.filter(
        pk__in=[pk for pk, _ in _sorted_co],
    )}
    co_appearances = [
        {
            'identity':    _co_id_map[pk],
            'video_count': counts['video_count'],
            'photo_count': counts['photo_count'],
            'total':       counts['video_count'] + counts['photo_count'],
        }
        for pk, counts in _sorted_co if pk in _co_id_map
    ]

    # Build back URL — only trust paths that start with /faces/ to prevent open redirect
    raw_back = request.GET.get('from', '')
    from django.urls import reverse as _reverse
    back_url = raw_back if raw_back.startswith('/faces') else _reverse('faces')

    _total_video_count = sum(1 for g in all_groups if g['kind'] == 'video')
    _total_photo_count = sum(1 for g in all_groups if g['kind'] == 'photo')
    _has_audio         = identity.speaker_identities.exists()

    return render(request, 'videos/face_identity.html', {
        'identity':          identity,
        'page_obj':          page_obj,
        'video_groups':      page_obj.object_list,
        'total_faces_count': total_faces_count,
        'total_video_count': _total_video_count,
        'total_photo_count': _total_photo_count,
        'has_audio':         _has_audio,
        'photos':            [],
        'all_identities':    all_identities,
        'can_edit':          can_edit,
        'can_delete':        can_delete,
        'back_url':          back_url,
        'co_appearances':       co_appearances,
        'frame_interval':       getattr(settings, 'FRAME_INTERVAL_SECONDS', 5),
        'unread_count':         _unread_count(request),
    })


def _unread_count(request):
    if not request.user.is_authenticated:
        return 0
    return Notification.objects.filter(recipient=request.user, is_read=False).count()


# ─── Face Management API ──────────────────────────────────────────────────────

@api_view(['POST'])
@api_login_required
def face_set_status(request, face_id):
    """
    POST /api/faces/crops/<id>/status/
    Body: {"status": "confirmed" | "rejected" | "unreviewed"}
    Only the owner of the source video/photo channel can set its status.
    """
    face = get_object_or_404(DetectedFace, id=face_id)
    user_channels = _user_channels(request.user)
    owner_channel_id = face.video.channel_id if face.video_id else (face.photo.channel_id if face.photo_id else None)
    if not user_channels.exists() or owner_channel_id is None or not user_channels.filter(pk=owner_channel_id).exists():
        return Response({'error': 'Forbidden — this crop belongs to another channel.'}, status=403)

    new_status = request.data.get('status', '').strip()
    valid = {DetectedFace.STATUS_CONFIRMED, DetectedFace.STATUS_REJECTED, DetectedFace.STATUS_UNREVIEWED}
    if new_status not in valid:
        return Response({'error': f'status must be one of: {", ".join(valid)}'}, status=400)

    face.status = new_status
    face.save(update_fields=['status'])
    return Response({'id': face.pk, 'status': face.status})


@api_view(['POST'])
@api_login_required
def face_set_thumbnail(request, face_id):
    """
    POST /api/faces/crops/<id>/set-thumbnail/
    Sets the crop as the thumbnail for its FaceIdentity.
    Only the owner of the source video/photo channel can do this.
    """
    face = get_object_or_404(DetectedFace, id=face_id)
    user_channels = _user_channels(request.user)
    owner_channel_id = face.video.channel_id if face.video_id else (face.photo.channel_id if face.photo_id else None)
    if not user_channels.exists() or owner_channel_id is None or not user_channels.filter(pk=owner_channel_id).exists():
        return Response({'error': 'Forbidden.'}, status=403)
    if not face.crop_path:
        return Response({'error': 'This crop has no image.'}, status=400)

    identity = face.identity
    if not identity:
        return Response({'error': 'Crop has no associated identity.'}, status=400)

    identity.thumbnail = face.crop_path
    identity.save(update_fields=['thumbnail'])
    return Response({'thumbnail_url': identity.thumbnail_url})


@api_view(['POST', 'PATCH'])
@api_login_required
def face_identity_rename(request, identity_id):
    """
    POST/PATCH /api/faces/<id>/rename/
    Body: {"name": "John Doe"}
    Any user who has this identity in at least one of their videos/photos can rename it.
    The rename is global — all channels that share this identity see the new name.
    """
    identity      = get_object_or_404(FaceIdentity, id=identity_id)
    user_channels = _user_channels(request.user)
    if not user_channels.exists():
        return Response({'error': 'No channel.'}, status=403)
    if not DetectedFace.objects.filter(identity=identity).filter(
        Q(video__channel__in=user_channels) | Q(photo__channel__in=user_channels)
    ).exists():
        return Response({'error': 'Forbidden — this identity does not appear in your content.'}, status=403)

    name = request.data.get('name', '').strip()
    if not name:
        return Response({'error': 'name is required.'}, status=400)
    old_name = identity.name
    identity.name = name
    identity.is_auto_named = False
    identity.save(update_fields=['name', 'is_auto_named'])

    # Cascade rename to any linked SpeakerIdentity that shared the old name.
    # Only update speakers whose name still matches the old face name (i.e. they
    # were auto-synced from this face, not independently named by the user).
    synced = (
        SpeakerIdentity.objects
        .filter(face_identity=identity, name=old_name)
        .update(name=name, is_auto_named=False)
    )

    return Response({'id': identity.pk, 'name': identity.name, 'speakers_synced': synced})


@api_view(['POST'])
@api_login_required
def faces_cleanup_orphans(request):
    """
    POST /api/faces/cleanup-orphans/
    Deletes all FaceIdentity rows that have no DetectedFace rows attached.
    These are ghost identities left over from re-analysis runs.
    """
    result = FaceIdentity.objects.filter(faces__isnull=True).delete()
    deleted_count = result[1].get('videos.FaceIdentity', 0)
    return Response({'deleted': deleted_count})


@api_view(['GET'])
@api_login_required
def face_identity_list(request):
    """
    GET /api/faces/list/
    Returns all FaceIdentity rows visible to the current user, with crop
    and video counts for disambiguation in the merge dropdown.
    """
    user_channels = _user_channels(request.user)
    if not user_channels.exists():
        return Response([])

    identity_ids = (
        DetectedFace.objects
        .filter(Q(video__channel__in=user_channels) | Q(photo__channel__in=user_channels))
        .values_list('identity_id', flat=True)
        .distinct()
    )
    identities = FaceIdentity.objects.filter(id__in=identity_ids).order_by('name')

    results = []
    for ident in identities:
        own_faces = DetectedFace.objects.filter(identity=ident).filter(
            Q(video__channel__in=user_channels) | Q(photo__channel__in=user_channels)
        )
        crop_count  = own_faces.count()
        video_count = own_faces.values('video_id').distinct().count()
        photo_count = own_faces.values('photo_id').distinct().count()
        results.append({
            'id':          ident.pk,
            'name':        ident.name,
            'is_auto':     ident.is_auto_named,
            'crops':       crop_count,
            'videos':      video_count,
            'photos':      photo_count,
            'thumbnail':   ident.thumbnail_url,
        })

    return Response(results)


@api_view(['DELETE'])
@api_login_required
def face_identity_delete(request, identity_id):
    """
    DELETE /api/faces/<id>/delete/
    Allowed only if ALL DetectedFace rows for this identity belong to the
    requesting user's channel (i.e. no other channel shares this identity).
    If other channels share it, use face_identity_remove_from_video instead.
    """
    identity      = get_object_or_404(FaceIdentity, id=identity_id)
    user_channels = _user_channels(request.user)
    if not user_channels.exists():
        return Response({'error': 'No channel.'}, status=403)

    total_faces = DetectedFace.objects.filter(identity=identity).count()
    my_faces    = DetectedFace.objects.filter(identity=identity).filter(
        Q(video__channel__in=user_channels) | Q(photo__channel__in=user_channels)
    ).count()

    if my_faces == 0:
        return Response({'error': 'Forbidden — this identity does not appear in your content.'}, status=403)
    if total_faces != my_faces:
        # Shared across channels — only remove from this user's videos
        DetectedFace.objects.filter(identity=identity).filter(
            Q(video__channel__in=user_channels) | Q(photo__channel__in=user_channels)
        ).delete()
        return Response({'partial': True, 'message': 'Removed from your content. Identity kept as it appears in other channels.'}, status=200)

    # Fully owned — delete entirely
    DetectedFace.objects.filter(identity=identity).delete()
    identity.delete()
    return Response(status=204)


@api_view(['GET'])
@api_login_required
def face_identity_audio_tab(request, identity_id):
    """
    GET /api/faces/<id>/audio/
    Returns videos where this FaceIdentity has a linked SpeakerIdentity that
    appears in the video's segments. Used for the Audio tab on the face detail page.
    Load-on-request — not included in the initial page render.
    """
    identity      = get_object_or_404(FaceIdentity, id=identity_id)
    user_channels = _user_channels(request.user)
    if not user_channels.exists():
        return Response([])

    user_channel_ids = list(user_channels.values_list('id', flat=True))

    # All SpeakerIdentity rows manually linked to this face
    speaker_identities = SpeakerIdentity.objects.filter(face_identity=identity)
    if not speaker_identities.exists():
        return Response({'linked_speakers': [], 'videos': []})

    # Videos in user's channels that have segments from those speakers
    video_qs = (
        Video.objects
        .filter(
            segments__speaker_identity__in=speaker_identities,
            channel_id__in=user_channel_ids,
        )
        .distinct()
        .annotate(
            seg_count=Count(
                'segments',
                filter=Q(segments__speaker_identity__in=speaker_identities),
            )
        )
        .order_by('-created_at')
        .only('id', 'title', 'thumbnail', 'duration')
    )

    speakers_data = [
        {'id': s.pk, 'name': s.name, 'role': s.role}
        for s in speaker_identities
    ]
    videos_data = [
        {
            'id':          str(v.id),
            'title':       v.title,
            'thumbnail':   v.thumbnail.url if v.thumbnail else None,
            'duration':    v.duration,
            'seg_count':   v.seg_count,
            'watch_url':   f'/watch/{v.id}/',
        }
        for v in video_qs
    ]
    return Response({'linked_speakers': speakers_data, 'videos': videos_data})


@api_view(['POST'])
@api_login_required
def segment_set_speaker(request, segment_id):
    """
    POST /api/segments/<id>/set-speaker/
    Body: {"speaker_identity_id": 4}  — or null to clear
    Lets the user correct which speaker pyannote assigned to a segment.
    Only the channel owner/editor can call this.
    """
    seg = get_object_or_404(VideoSegment, id=segment_id)
    user_channels = _user_channels(request.user)
    if not user_channels.filter(id=seg.video.channel_id).exists():
        return Response({'error': 'Forbidden.'}, status=403)

    si_id = request.data.get('speaker_identity_id')
    if si_id is None:
        seg.speaker_identity = None
        seg.speaker_label    = ''
        seg.save(update_fields=['speaker_identity', 'speaker_label'])
        return Response({'ok': True, 'speaker_identity_id': None})

    si = get_object_or_404(SpeakerIdentity, id=si_id)
    seg.speaker_identity = si
    seg.speaker_label    = si.name   # keep label in sync for display
    seg.save(update_fields=['speaker_identity', 'speaker_label'])
    return Response({'ok': True, 'speaker_identity_id': si.pk, 'speaker_name': si.name})


# ─── End Screens API ─────────────────────────────────────────────────────────

@api_view(['GET', 'POST'])
def end_screen_list_create(request, video_id):
    """
    GET  /api/videos/<id>/end-screens/ — list (public)
    POST /api/videos/<id>/end-screens/ — create (owner only)
    """
    video = get_object_or_404(Video, id=video_id)

    if request.method == 'GET':
        qs = video.end_screens.select_related('target_video').all()
        return Response(EndScreenSerializer(qs, many=True).data)

    # POST — owner only
    if not request.user.is_authenticated:
        return Response({'error': 'Authentication required.'}, status=status.HTTP_401_UNAUTHORIZED)
    is_owner = _is_video_owner(request.user, video)
    if not is_owner:
        return Response({'error': 'Only the channel owner can add end screens.'}, status=status.HTTP_403_FORBIDDEN)

    serializer = EndScreenSerializer(data=request.data)
    if serializer.is_valid():
        serializer.save(video=video)
        return Response(serializer.data, status=status.HTTP_201_CREATED)
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@api_view(['DELETE'])
@api_login_required
def end_screen_detail(request, video_id, end_screen_id):
    """DELETE /api/videos/<id>/end-screens/<es_id>/ — owner only"""
    video = get_object_or_404(Video, id=video_id)
    es    = get_object_or_404(EndScreen, id=end_screen_id, video=video)

    is_owner = _is_video_owner(request.user, video)
    if not is_owner:
        return Response({'error': 'Only the channel owner can delete end screens.'}, status=status.HTTP_403_FORBIDDEN)

    es.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


# ─── Notifications API ───────────────────────────────────────────────────────

@api_view(['GET'])
@api_login_required
def notification_list(request):
    """GET /api/notifications/ — list unread notifications"""
    notifications = Notification.objects.filter(
        recipient=request.user
    ).select_related('sender').order_by('-created_at')[:50]
    cache.delete(f'unread_{request.user.pk}')
    return Response(NotificationSerializer(notifications, many=True).data)


@api_view(['POST'])
@api_login_required
def mark_notifications_read(request):
    """POST /api/notifications/read/ — mark all as read"""
    Notification.objects.filter(recipient=request.user, is_read=False).update(is_read=True)
    cache.delete(f'unread_{request.user.pk}')
    return Response({'ok': True})


# ─── User Management API (superuser only) ────────────────────────────────────

@api_view(['GET'])
def user_management_api(request):
    """GET /api/admin/users/ — superuser only"""
    if not request.user.is_authenticated or not request.user.is_superuser:
        return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)

    users = User.objects.prefetch_related('channels', 'profile').order_by('-date_joined')
    data = []
    for u in users:
        first_ch = u.channels.first()
        try:
            role = u.profile.role
        except Exception:
            role = UserProfile.ROLE_SUPERADMIN if u.is_superuser else UserProfile.ROLE_VIEWER
        data.append({
            'id': u.id,
            'username': u.username,
            'email': u.email,
            'is_active': u.is_active,
            'is_staff': u.is_staff,
            'is_superuser': u.is_superuser,
            'role': role,
            'date_joined': u.date_joined,
            'channel': {
                'name': first_ch.name,
                'slug': first_ch.slug,
                'video_count': first_ch.videos.count(),
            } if first_ch else None,
        })
    return Response(data)


@api_view(['PATCH'])
def user_management_toggle(request, user_id):
    """PATCH /api/admin/users/<id>/ — update active/staff/role (superadmin only)"""
    if not request.user.is_authenticated or not _is_superadmin(request.user):
        return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)

    target = get_object_or_404(User, id=user_id)

    if 'is_active' in request.data:
        target.is_active = bool(request.data['is_active'])
    if 'is_staff' in request.data:
        target.is_staff = bool(request.data['is_staff'])
    target.save(update_fields=['is_active', 'is_staff'])

    if 'role' in request.data:
        new_role = request.data['role']
        if new_role not in (UserProfile.ROLE_SUPERADMIN, UserProfile.ROLE_EDITOR, UserProfile.ROLE_VIEWER):
            return Response({'error': 'Invalid role.'}, status=status.HTTP_400_BAD_REQUEST)
        profile, _ = UserProfile.objects.get_or_create(user=target, defaults={'role': UserProfile.ROLE_VIEWER})
        profile.role = new_role
        profile.save(update_fields=['role'])

    try:
        role = target.profile.role
    except Exception:
        role = UserProfile.ROLE_VIEWER

    return Response({'id': target.id, 'is_active': target.is_active, 'is_staff': target.is_staff, 'role': role})


# ═══════════════════════════════════════════════════════════════════════════════
# Photo / Digital Asset Management
# ═══════════════════════════════════════════════════════════════════════════════

@login_required
def photo_duplicates_page(request):
    """
    Show all potential-duplicate photos grouped by their original.
    Editors can keep one and delete the rest.
    """
    if not _is_editor(request.user):
        from django.contrib.auth.views import redirect_to_login
        return redirect_to_login(request.get_full_path())

    # All photos marked as duplicates, with their originals pre-fetched
    dupes_qs = (
        Photo.objects
        .filter(is_potential_duplicate=True)
        .select_related('duplicate_of', 'channel')
        .order_by('duplicate_of_id', 'created_at')
    )

    groups    = {}  # original_id → {'original': Photo, 'duplicates': [...]}
    orphaned  = []  # is_potential_duplicate=True but duplicate_of was deleted

    for photo in dupes_qs:
        if photo.duplicate_of_id is None:
            orphaned.append(photo)
        else:
            key = str(photo.duplicate_of_id)
            if key not in groups:
                groups[key] = {'original': photo.duplicate_of, 'duplicates': []}
            groups[key]['duplicates'].append(photo)

    return render(request, 'videos/photo_duplicates.html', {
        'groups':   list(groups.values()),
        'orphaned': orphaned,
        'total_groups': len(groups),
        'total_dupes':  dupes_qs.count(),
    })


@login_required
def photo_library_page(request):
    """Gallery / library view — analogous to player_page for videos."""
    tab = request.GET.get('tab', 'photos')   # 'photos' | 'archive'

    base_qs = Photo.objects.filter(visibility=Photo.VISIBILITY_PUBLIC, status=Photo.STATUS_READY)

    if tab == 'archive':
        photos = base_qs.filter(is_archived=True)
    else:
        # Main library: hide archived photos
        photos = base_qs.filter(is_archived=False)

    q = request.GET.get('q', '').strip()
    if q:
        # People-aware search: OR in photos where a detected identity name matches q.
        people_photo_ids = Photo.objects.filter(
            visibility=Photo.VISIBILITY_PUBLIC,
            status=Photo.STATUS_READY,
            detected_faces__identity__name__icontains=q,
        ).values('pk')

        if _can_use_postgres_fts():
            from django.contrib.postgres.search import SearchQuery, SearchVector
            _sq = SearchQuery(q, config='english', search_type='plain')
            # FTS on short user-entered fields
            _fts_qs    = photos.annotate(
                _search=SearchVector('title', 'description', 'tags', config='english')
            ).filter(_search=_sq).distinct()
            # FTS on AI-generated fields — stemmed, no noise
            _fts_labels_qs = photos.filter(
                labels__search=SearchQuery(q, config='english', search_type='plain')
            )
            _fts_scene_qs  = photos.filter(
                scene_description__search=SearchQuery(q, config='english', search_type='plain')
            )
            # FTS on face names cache ("Soham, Alice")
            _fts_face_qs   = photos.filter(
                face_names__search=SearchQuery(q, config='english', search_type='plain')
            )
            # Fuzzy ONLY on short user-controlled fields — long AI text fields are too noisy
            # face_names = "Soham, Alice" — short CSV, fuzzy gives typo tolerance on names
            # ocr_text = exact printed text — icontains only; stemming adds false positives
            _fuzzy_qs = _postgres_fuzzy_filter(photos, q, ('title', 'tags', 'face_names'))
            photos = photos.filter(
                Q(pk__in=_fts_qs.values('pk'))
                | Q(pk__in=_fts_labels_qs.values('pk'))
                | Q(pk__in=_fts_scene_qs.values('pk'))
                | Q(pk__in=_fts_face_qs.values('pk'))
                | Q(pk__in=_fuzzy_qs.values('pk'))
                | Q(pk__in=people_photo_ids)
                | Q(face_names__icontains=q)   # direct name substring match
                | Q(ocr_text__icontains=q)     # exact substring: "cow" in "cowboy" ✓; not in "conference" ✗
            ).distinct()
        else:
            photos = photos.filter(
                _q_icontains_all_terms('title', q)
                | _q_icontains_all_terms('description', q)
                | _q_icontains_all_terms('tags', q)
                | _q_icontains_all_terms('labels', q)
                | _q_icontains_all_terms('scene_description', q)
                | _q_icontains_all_terms('ocr_text', q)
                | Q(face_names__icontains=q)
                | Q(pk__in=people_photo_ids)
            ).distinct()

    cat_slug = request.GET.get('category', '').strip()
    if cat_slug:
        photos = photos.filter(category__slug=cat_slug)

    sort = request.GET.get('sort', '').strip()
    if sort == 'views':
        photos = photos.order_by('-views_count')
    elif sort == 'oldest':
        photos = photos.order_by('created_at')
    else:
        photos = photos.order_by('-created_at')

    categories = cache.get('all_categories')
    if categories is None:
        categories = list(Category.objects.all())
        cache.set('all_categories', categories, getattr(django_settings, 'CACHE_TTL_CATEGORIES', 3600))

    PAGE = 36
    photos = photos.select_related('channel')
    _peek = list(photos[:PAGE + 1])
    has_more = len(_peek) > PAGE
    photos_page = _peek[:PAGE]

    return render(request, 'videos/photo_library.html', {
        'photos':       photos_page,
        'has_more':     has_more,
        'categories':   categories,
        'current_q':    q,
        'current_cat':  cat_slug,
        'current_sort': sort,
        'current_tab':  tab,
    })


@editor_required
def photo_upload_page(request):
    """Upload form page for photos."""
    channels   = list(request.user.channels.all())
    categories = list(Category.objects.all())
    return render(request, 'videos/photo_upload.html', {
        'channels':   channels,
        'categories': categories,
    })


def photo_detail_page(request, photo_id):
    """Single photo detail / viewer page."""
    try:
        photo = Photo.objects.select_related('channel', 'category').get(id=photo_id)
    except Photo.DoesNotExist:
        raise Http404

    if photo.visibility == Photo.VISIBILITY_PRIVATE and not request.user.is_authenticated:
        from django.contrib.auth.views import redirect_to_login
        return redirect_to_login(request.get_full_path())

    # Record a view (fire-and-forget style, no race-condition safety needed)
    Photo.objects.filter(id=photo_id).update(views_count=F('views_count') + 1)

    photo_tags = [t.strip() for t in (photo.tags or '').split(',') if t.strip()]

    return render(request, 'videos/photo_detail.html', {
        'photo': photo,
        'photo_tags': photo_tags,
    })


# ── Photo API endpoints ───────────────────────────────────────────────────────

@api_view(['GET'])
def photo_list(request):
    """Paginated photo list — supports cursor pagination for TB-scale datasets.

    ?cursor=<ISO-timestamp>  — return items older than this timestamp (efficient, no OFFSET)
    ?page=<n>                — legacy offset pagination (falls back when cursor absent)
    ?channel=<slug>          — filter by channel slug
    """
    show_archived = request.query_params.get('archived', '0') == '1'
    photos = Photo.objects.filter(
        visibility=Photo.VISIBILITY_PUBLIC, status=Photo.STATUS_READY,
        is_archived=show_archived,          # main library hides archived; archive tab shows only archived
    ).select_related('channel').defer('clip_embedding', 'description', 'uploaded_by')

    q = request.query_params.get('q', '').strip()
    if q:
        photos = photos.filter(
            Q(title__icontains=q) | Q(tags__icontains=q)
            | Q(labels__icontains=q) | Q(scene_description__icontains=q)
        ).distinct()

    cat_slug = request.query_params.get('category', '').strip()
    if cat_slug:
        photos = photos.filter(category__slug=cat_slug)

    ch_slug = request.query_params.get('channel', '').strip()
    if ch_slug:
        photos = photos.filter(channel__slug=ch_slug)

    face = request.query_params.get('face', '').strip()
    if face:
        photos = photos.filter(face_names__icontains=face)

    sort = request.query_params.get('sort', '').strip()
    use_cursor = bool(request.query_params.get('cursor', '').strip()) or (
        not request.query_params.get('page') and sort not in ('views', 'oldest')
    )

    try:
        page_size = min(72, max(1, int(request.query_params.get('page_size', 36))))
    except (ValueError, TypeError):
        page_size = 36

    if sort == 'views':
        photos = photos.order_by('-views_count')
        use_cursor = False  # cursor by views not supported; fall back to offset
    elif sort == 'oldest':
        photos = photos.order_by('created_at')
        use_cursor = False
    else:
        photos = photos.order_by('-created_at')

    next_cursor = None

    if use_cursor:
        # Cursor pagination: avoid OFFSET scans on large datasets
        raw_cursor = request.query_params.get('cursor', '').strip()
        if raw_cursor:
            from django.utils.dateparse import parse_datetime
            cursor_dt = parse_datetime(raw_cursor)
            if cursor_dt:
                photos = photos.filter(created_at__lt=cursor_dt)

        _slice   = list(photos[:page_size + 1])
        has_more = len(_slice) > page_size
        items    = _slice[:page_size]
        if has_more and items:
            next_cursor = items[-1].created_at.isoformat()
        page = None
    else:
        # Legacy offset pagination
        try:
            page = max(1, int(request.query_params.get('page', 1)))
        except (ValueError, TypeError):
            page = 1
        offset   = (page - 1) * page_size
        _slice   = list(photos[offset: offset + page_size + 1])
        has_more = len(_slice) > page_size
        items    = _slice[:page_size]

    def _serialize(p):
        return {
            'id':                str(p.id),
            'title':             p.title,
            'thumbnail_url':     p.thumbnail_url,
            'width':             p.width,
            'height':            p.height,
            'labels':            p.labels,
            'scene_description': p.scene_description,
            'views_count':       p.views_count,
            'is_archived':       p.is_archived,
            'is_potential_duplicate': p.is_potential_duplicate,
            'created_at':        p.created_at.isoformat(),
            'photo_url':         p.photo_url,
            'channel':           {'name': p.channel.name} if p.channel else None,
        }

    return Response({
        'results':     [_serialize(p) for p in items],
        'has_more':    has_more,
        'page':        page,
        'next_cursor': next_cursor,
    })


@api_view(['POST'])
@parser_classes([MultiPartParser, FormParser])
def photo_upload(request):
    """Upload a new photo, queue AI analysis task."""
    if not _is_editor(request.user):
        return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)
    from . import tasks as _tasks

    file_obj = request.FILES.get('file')
    if not file_obj:
        return Response({'error': 'No file provided.'}, status=status.HTTP_400_BAD_REQUEST)

    # Basic content-type check
    ct = getattr(file_obj, 'content_type', '')
    if not ct.startswith('image/'):
        return Response({'error': 'Only image files are accepted.'}, status=status.HTTP_400_BAD_REQUEST)

    title = request.data.get('title', '').strip() or file_obj.name.rsplit('.', 1)[0]

    channel_id = request.data.get('channel_id', '').strip()
    channel = None
    if channel_id:
        try:
            channel = Channel.objects.get(id=channel_id)
        except Channel.DoesNotExist:
            pass

    category_id = request.data.get('category_id', '').strip()
    category = None
    if category_id:
        try:
            category = Category.objects.get(id=category_id)
        except Category.DoesNotExist:
            pass

    photo = Photo.objects.create(
        title=title,
        description=request.data.get('description', ''),
        tags=request.data.get('tags', ''),
        channel=channel,
        category=category,
        file=file_obj,
        file_size=file_obj.size,
        visibility=request.data.get('visibility', Photo.VISIBILITY_PUBLIC),
        uploaded_by=request.user.username,
        status=Photo.STATUS_PENDING,
    )

    _tasks.analyze_photo_task.apply_async(
        args=[str(photo.id)],
        queue='processing',
    )

    return Response({
        'id':     str(photo.id),
        'title':  photo.title,
        'status': photo.status,
        'photo_url': photo.photo_url,
    }, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PATCH', 'DELETE'])
def photo_detail_api(request, photo_id):
    """Retrieve, update metadata, or delete a photo."""
    try:
        photo = Photo.objects.select_related('channel', 'category').get(id=photo_id)
    except Photo.DoesNotExist:
        raise Http404

    if request.method == 'GET':
        return Response({
            'id':               str(photo.id),
            'title':            photo.title,
            'description':      photo.description,
            'tags':             photo.tags,
            'labels':           photo.labels,
            'labels_list':      photo.labels_list,
            'face_count':       photo.face_count,
            'face_names':       photo.face_names,
            'scene_description': photo.scene_description,
            'width':            photo.width,
            'height':           photo.height,
            'file_size':        photo.file_size,
            'views_count':      photo.views_count,
            'status':           photo.status,
            'processing_error': photo.processing_error,
            'visibility':       photo.visibility,
            'is_archived':      photo.is_archived,
            'uploaded_by':      photo.uploaded_by,
            'created_at':       photo.created_at.isoformat(),
            'thumbnail_url':    photo.thumbnail_url,
            'file_url':         photo.file.url if photo.file else None,
            'photo_url':        photo.photo_url,
            'channel': {'id': str(photo.channel.id), 'name': photo.channel.name} if photo.channel else None,
            'category': {'id': photo.category.id, 'name': photo.category.name} if photo.category else None,
        })

    if request.method == 'PATCH':
        if not _is_editor(request.user):
            return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)
        updatable = ['title', 'description', 'tags', 'visibility']
        for field in updatable:
            if field in request.data:
                setattr(photo, field, request.data[field])
        photo.save(update_fields=[f for f in updatable if f in request.data])
        return Response({'ok': True})

    if request.method == 'DELETE':
        if not _is_editor(request.user):
            return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)
        # Clean up files
        for field in (photo.file, photo.thumbnail):
            if field:
                try:
                    import os as _os
                    _os.remove(field.path)
                except Exception:
                    pass
        photo.delete()
        return Response({'ok': True})


@api_view(['POST'])
@api_login_required
def photo_toggle_archive(request, photo_id):
    """POST /api/photos/<id>/archive/ — toggle archived state."""
    try:
        photo = Photo.objects.get(id=photo_id)
    except Photo.DoesNotExist:
        raise Http404
    photo.is_archived = not photo.is_archived
    photo.save(update_fields=['is_archived'])
    return Response({'is_archived': photo.is_archived})


@api_view(['GET'])
def photo_status(request, photo_id):
    """Poll processing status — used by the upload page."""
    try:
        photo = Photo.objects.get(id=photo_id)
    except Photo.DoesNotExist:
        raise Http404
    return Response({
        'status':           photo.status,
        'processing_error': photo.processing_error,
        'thumbnail_url':    photo.thumbnail_url,
        'labels':           photo.labels,
        'scene_description': photo.scene_description,
    })


# ── Bulk photo operations ─────────────────────────────────────────────────────

@api_view(['GET'])
def search_suggest(request):
    """
    Fast typeahead suggestions for the search bar.
    Returns up to 10 items drawn from video titles, channel names, face names, and labels.
    """
    q = request.GET.get('q', '').strip()
    if len(q) < 2:
        return Response([])

    suggestions = []
    seen = set()

    def _add(text, kind):
        key = text.lower()
        if key not in seen:
            seen.add(key)
            suggestions.append({'text': text, 'type': kind})

    # Video titles
    for title in (
        Video.objects
        .filter(title__icontains=q, visibility=Video.VISIBILITY_PUBLIC, status=Video.STATUS_READY)
        .values_list('title', flat=True)[:6]
    ):
        _add(title, 'video')

    # Channel names
    for name in Channel.objects.filter(name__icontains=q).values_list('name', flat=True)[:3]:
        _add(name, 'channel')

    # Face / person names
    for name in FaceIdentity.objects.filter(name__icontains=q).values_list('name', flat=True)[:3]:
        _add(name, 'person')

    # Photo titles (if under limit)
    if len(suggestions) < 10:
        for title in (
            Photo.objects
            .filter(title__icontains=q, visibility=Photo.VISIBILITY_PUBLIC, status=Photo.STATUS_READY)
            .values_list('title', flat=True)[:4]
        ):
            _add(title, 'photo')

    return Response(suggestions[:10])


@api_view(['POST'])
def photo_bulk(request):
    """Bulk operations on photos.

    Body: { "photo_ids": ["uuid", ...], "action": "delete"|"add_to_album"|"set_visibility",
            "album_id": "<uuid>",  # for add_to_album
            "visibility": "public"|"private" }  # for set_visibility
    """
    if not _is_editor(request.user):
        return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)

    photo_ids = request.data.get('photo_ids', [])
    action    = request.data.get('action', '')
    if not photo_ids or not action:
        return Response({'error': 'photo_ids and action are required.'}, status=status.HTTP_400_BAD_REQUEST)

    photos = Photo.objects.filter(id__in=photo_ids)
    count  = photos.count()

    if action == 'delete':
        for p in photos:
            for field in (p.file, p.thumbnail):
                if field:
                    try:
                        import os as _os; _os.remove(field.path)
                    except Exception:
                        pass
            p.delete()
        return Response({'ok': True, 'deleted': count})

    if action == 'set_visibility':
        vis = request.data.get('visibility', '')
        if vis not in (Photo.VISIBILITY_PUBLIC, Photo.VISIBILITY_PRIVATE):
            return Response({'error': 'Invalid visibility.'}, status=status.HTTP_400_BAD_REQUEST)
        photos.update(visibility=vis)
        return Response({'ok': True, 'updated': count})

    if action == 'add_to_album':
        album_id = request.data.get('album_id')
        if not album_id:
            return Response({'error': 'album_id required.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            album = Album.objects.get(id=album_id, owner=request.user)
        except Album.DoesNotExist:
            return Response({'error': 'Album not found.'}, status=status.HTTP_404_NOT_FOUND)
        added = 0
        for p in photos:
            _, created = AlbumPhoto.objects.get_or_create(album=album, photo=p)
            if created:
                added += 1
        Album.objects.filter(id=album.id).update(photo_count=AlbumPhoto.objects.filter(album=album).count())
        return Response({'ok': True, 'added': added})

    if action == 'remove_from_album':
        album_id = request.data.get('album_id')
        if not album_id:
            return Response({'error': 'album_id required.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            album = Album.objects.get(id=album_id, owner=request.user)
        except Album.DoesNotExist:
            return Response({'error': 'Album not found.'}, status=status.HTTP_404_NOT_FOUND)
        removed = AlbumPhoto.objects.filter(album=album, photo_id__in=photo_ids).delete()[0]
        Album.objects.filter(id=album.id).update(photo_count=AlbumPhoto.objects.filter(album=album).count())
        return Response({'ok': True, 'removed': removed})

    if action == 'clear_duplicate_flag':
        # Used by the duplicate management page to clear the flag without deleting
        photos.update(is_potential_duplicate=False, duplicate_of=None)
        return Response({'ok': True, 'cleared': count})

    if action == 'archive':
        photos.update(is_archived=True)
        return Response({'ok': True, 'archived': count})

    if action == 'unarchive':
        photos.update(is_archived=False)
        return Response({'ok': True, 'unarchived': count})

    return Response({'error': f'Unknown action: {action}'}, status=status.HTTP_400_BAD_REQUEST)


# ── Album page views ──────────────────────────────────────────────────────────

@login_required
def albums_page(request):
    """Album library — all albums owned by the current user."""
    albums = (
        Album.objects.filter(owner=request.user)
        .select_related('cover_photo')
        .order_by('-updated_at')
    )
    return render(request, 'videos/albums.html', {
        'albums': albums,
    })


def album_detail_page(request, album_id):
    """View a single album and its photos."""
    try:
        album = Album.objects.select_related('owner', 'cover_photo').get(id=album_id)
    except Album.DoesNotExist:
        raise Http404

    # Access check
    if not album.is_public:
        if not request.user.is_authenticated or album.owner != request.user:
            from django.contrib.auth.views import redirect_to_login
            return redirect_to_login(request.get_full_path())

    # For smart albums, dynamically resolve photos; for regular albums use AlbumPhoto order
    if album.is_smart and album.smart_filter:
        photos = _resolve_smart_album(album.smart_filter)
    else:
        photos = (
            Photo.objects
            .filter(album_photos__album=album)
            .order_by('album_photos__order', 'album_photos__added_at')
            .defer('clip_embedding', 'description')
        )

    return render(request, 'videos/album_detail.html', {
        'album':  album,
        'photos': photos,
    })


def album_shared_page(request, share_token):
    """Public share link view."""
    try:
        album = Album.objects.select_related('owner', 'cover_photo').get(share_token=share_token)
    except Album.DoesNotExist:
        raise Http404

    if album.is_smart and album.smart_filter:
        photos = _resolve_smart_album(album.smart_filter)
    else:
        photos = (
            Photo.objects
            .filter(album_photos__album=album, visibility=Photo.VISIBILITY_PUBLIC)
            .order_by('album_photos__order', 'album_photos__added_at')
            .defer('clip_embedding', 'description')
        )

    return render(request, 'videos/album_detail.html', {
        'album':  album,
        'photos': photos,
        'is_shared_view': True,
    })


def _resolve_smart_album(smart_filter):
    """Return a Photo queryset matching the smart_filter dict."""
    qs = Photo.objects.filter(
        visibility=Photo.VISIBILITY_PUBLIC, status=Photo.STATUS_READY,
    ).defer('clip_embedding', 'description').order_by('-created_at')

    ftype = smart_filter.get('type', '')
    value = smart_filter.get('value', '')

    if ftype == 'label' and value:
        qs = qs.filter(labels__icontains=value)
    elif ftype == 'face' and value:
        qs = qs.filter(face_names__icontains=value)
    elif ftype == 'channel' and value:
        qs = qs.filter(channel__slug=value)
    elif ftype == 'date_range':
        start = smart_filter.get('start')
        end   = smart_filter.get('end')
        if start:
            qs = qs.filter(created_at__date__gte=start)
        if end:
            qs = qs.filter(created_at__date__lte=end)
    return qs


# ── Album API endpoints ───────────────────────────────────────────────────────

@api_view(['GET', 'POST'])
def album_list(request):
    """List user albums or create a new one."""
    if not request.user.is_authenticated:
        return Response({'error': 'Login required.'}, status=status.HTTP_401_UNAUTHORIZED)

    if request.method == 'GET':
        albums = (Album.objects.filter(owner=request.user)
                  .select_related('cover_photo')
                  .order_by('-updated_at'))
        def _ser(a):
            return {
                'id':          str(a.id),
                'title':       a.title,
                'description': a.description,
                'is_smart':    a.is_smart,
                'smart_filter':a.smart_filter,
                'is_public':   a.is_public,
                'photo_count': a.photo_count,
                'cover_url':   a.cover_url,
                'share_url':   a.share_url,
                'updated_at':  a.updated_at.isoformat(),
            }
        return Response([_ser(a) for a in albums])

    # POST — create
    title = (request.data.get('title') or '').strip()
    if not title:
        return Response({'error': 'Title is required.'}, status=status.HTTP_400_BAD_REQUEST)
    is_smart     = bool(request.data.get('is_smart', False))
    smart_filter = request.data.get('smart_filter') or None
    album = Album.objects.create(
        owner=request.user,
        title=title,
        description=request.data.get('description', ''),
        is_smart=is_smart,
        smart_filter=smart_filter,
        is_public=bool(request.data.get('is_public', True)),
    )
    return Response({'id': str(album.id), 'title': album.title, 'share_url': album.share_url},
                    status=status.HTTP_201_CREATED)


@api_view(['GET', 'PATCH', 'DELETE'])
def album_detail(request, album_id):
    """Retrieve, update, or delete an album."""
    try:
        album = Album.objects.get(id=album_id, owner=request.user)
    except Album.DoesNotExist:
        raise Http404

    if request.method == 'GET':
        photos_qs = (
            Photo.objects.filter(album_photos__album=album)
            .defer('clip_embedding', 'description')
            .order_by('album_photos__order', 'album_photos__added_at')
        ) if not album.is_smart else _resolve_smart_album(album.smart_filter or {})
        return Response({
            'id':           str(album.id),
            'title':        album.title,
            'description':  album.description,
            'is_smart':     album.is_smart,
            'smart_filter': album.smart_filter,
            'is_public':    album.is_public,
            'photo_count':  album.photo_count,
            'share_url':    album.share_url,
            'photos': [{'id': str(p.id), 'title': p.title, 'thumbnail_url': p.thumbnail_url,
                        'photo_url': p.photo_url} for p in photos_qs[:200]],
        })

    if request.method == 'PATCH':
        updatable = ['title', 'description', 'is_public', 'is_smart', 'smart_filter']
        for f in updatable:
            if f in request.data:
                setattr(album, f, request.data[f])
        album.save()
        return Response({'ok': True})

    if request.method == 'DELETE':
        album.delete()
        return Response({'ok': True})


@api_view(['POST', 'DELETE'])
def album_photos(request, album_id):
    """Add or remove individual photos from a manual album."""
    try:
        album = Album.objects.get(id=album_id, owner=request.user)
    except Album.DoesNotExist:
        raise Http404
    if album.is_smart:
        return Response({'error': 'Cannot manually modify a smart album.'}, status=status.HTTP_400_BAD_REQUEST)

    photo_id = request.data.get('photo_id')
    if not photo_id:
        return Response({'error': 'photo_id required.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        photo = Photo.objects.get(id=photo_id)
    except Photo.DoesNotExist:
        return Response({'error': 'Photo not found.'}, status=status.HTTP_404_NOT_FOUND)

    if request.method == 'POST':
        _, created = AlbumPhoto.objects.get_or_create(album=album, photo=photo)
        Album.objects.filter(id=album.id).update(
            photo_count=AlbumPhoto.objects.filter(album=album).count(),
            cover_photo=album.cover_photo or photo,
        )
        return Response({'ok': True, 'added': created})

    if request.method == 'DELETE':
        AlbumPhoto.objects.filter(album=album, photo=photo).delete()
        Album.objects.filter(id=album.id).update(
            photo_count=AlbumPhoto.objects.filter(album=album).count()
        )
        return Response({'ok': True})


# ─── Speaker Identity Pages ───────────────────────────────────────────────────

@login_required
def speakers_page(request):
    """
    GET /speakers/
    Lists SpeakerIdentity rows that have segments in the current user's channel videos.
    """
    user_channels = list(_user_channels(request.user))
    request._owned_channels_cache = user_channels
    has_channel   = bool(user_channels)

    q            = request.GET.get('q', '').strip()
    active_filter = request.GET.get('filter', 'all')

    if has_channel:
        user_channel_ids = [ch.id for ch in user_channels]
        # Speakers visible to this user = those whose segments appear in their videos
        base_qs = (
            SpeakerIdentity.objects
            .filter(segments__video__channel_id__in=user_channel_ids)
            .select_related('face_identity')
            .distinct()
            .annotate(
                video_count=Count(
                    'segments__video',
                    filter=Q(segments__video__channel_id__in=user_channel_ids),
                    distinct=True,
                ),
                segment_count=Count(
                    'segments',
                    filter=Q(segments__video__channel_id__in=user_channel_ids),
                ),
            )
            .order_by('name')
        )
        if q:
            base_qs = base_qs.filter(name__icontains=q)
        totals = base_qs.aggregate(
            total_all=Count('id'),
            total_named=Count('id', filter=Q(is_auto_named=False)),
            total_auto=Count('id', filter=Q(is_auto_named=True)),
            total_narrator=Count('id', filter=Q(role=SpeakerIdentity.ROLE_NARRATOR)),
            total_background=Count('id', filter=Q(role=SpeakerIdentity.ROLE_BACKGROUND)),
        )
        qs = base_qs
        if active_filter == 'named':
            qs = qs.filter(is_auto_named=False)
        elif active_filter == 'auto':
            qs = qs.filter(is_auto_named=True)
        elif active_filter == 'narrator':
            qs = qs.filter(role=SpeakerIdentity.ROLE_NARRATOR)
        elif active_filter == 'background':
            qs = qs.filter(role=SpeakerIdentity.ROLE_BACKGROUND)
    else:
        qs = SpeakerIdentity.objects.none()
        totals = {
            'total_all': 0,
            'total_named': 0,
            'total_auto': 0,
            'total_narrator': 0,
            'total_background': 0,
        }

    total_all        = totals['total_all']
    total_named      = totals['total_named']
    total_auto       = totals['total_auto']
    total_narrator   = totals['total_narrator']
    total_background = totals['total_background']

    # Re-evaluate after filter (for pagination — qs already filtered above)
    from django.core.paginator import Paginator
    paginator = Paginator(list(qs), 30)
    page_obj  = paginator.get_page(request.GET.get('page', 1))

    return render(request, 'videos/speakers.html', {
        'speakers':       page_obj,
        'page_obj':       page_obj,
        'total_all':      total_all,
        'total_named':    total_named,
        'total_auto':     total_auto,
        'total_narrator': total_narrator,
        'total_background': total_background,
        'active_filter':  active_filter,
        'search_query':   q,
        'has_channel':    has_channel,
    })


@login_required
def speaker_identity_page(request, speaker_id):
    """
    GET /speakers/<id>/
    Detail page for a SpeakerIdentity: per-video segment count, timeline, rename/merge/delete UI.
    Optional GET q= (2+ chars): full-text search over transcript segments for this speaker only
    (same Postgres FTS / icontains strategy as main player speech search).
    """
    speaker       = get_object_or_404(SpeakerIdentity.objects.select_related('face_identity'), id=speaker_id)
    user_channels = list(_user_channels(request.user))
    request._owned_channels_cache = user_channels

    # Gather videos this speaker appears in (current user's channels only)
    user_channel_ids = [ch.id for ch in user_channels]
    phrase_q         = (request.GET.get('q') or '').strip()
    active_phrase    = len(phrase_q) >= 2

    _speaker_seg_base = VideoSegment.objects.filter(
        speaker_identity=speaker,
        video__channel_id__in=user_channel_ids,
    )

    all_video_count = 0
    if user_channel_ids:
        all_video_count = (
            Video.objects
            .filter(segments__speaker_identity=speaker, channel_id__in=user_channel_ids)
            .distinct()
            .count()
        )

    videos_qs = (
        Video.objects
        .filter(
            segments__speaker_identity=speaker,
            channel_id__in=user_channel_ids,
        )
        .distinct()
        .order_by('-created_at')
        .only('id', 'title', 'thumbnail', 'duration', 'views_count', 'channel_id')
    )

    # Per-video segment list and timeline data
    video_groups = []
    if active_phrase:
        _cap_total    = 400
        _cap_per_vid  = 50
        seg_qs        = _speaker_seg_base.select_related('video')
        if _can_use_postgres_fts():
            from django.contrib.postgres.search import SearchQuery as _SQ
            seg_qs = seg_qs.filter(
                text__search=_SQ(phrase_q, config='english', search_type='plain')
            )
        else:
            seg_qs = seg_qs.filter(_q_icontains_all_terms('text', phrase_q))
        seg_qs = seg_qs.order_by('video_id', 'start_seconds')[:_cap_total]

        from collections import OrderedDict
        by_video = OrderedDict()
        for seg in seg_qs:
            vid = seg.video
            eid = vid.id
            if eid not in by_video:
                by_video[eid] = {'video': vid, 'segments': []}
            bucket = by_video[eid]['segments']
            if len(bucket) >= _cap_per_vid:
                continue
            bucket.append({
                'start_seconds':    seg.start_seconds,
                'end_seconds':      seg.end_seconds,
                'text':             seg.text,
                'highlighted_text': _highlight_query(seg.text or '', phrase_q),
            })
        video_groups = sorted(by_video.values(), key=lambda g: g['video'].created_at, reverse=True)
    else:
        for vid in videos_qs:
            segs = (
                VideoSegment.objects
                .filter(video=vid, speaker_identity=speaker)
                .order_by('start_seconds')
                .values('start_seconds', 'end_seconds', 'text')
            )
            video_groups.append({
                'video':    vid,
                'segments': list(segs),
            })

    # Face identity list for link-face dropdown
    face_identities = []
    if user_channel_ids:
        fi_ids = (
            DetectedFace.objects
            .filter(Q(video__channel_id__in=user_channel_ids) | Q(photo__channel_id__in=user_channel_ids))
            .values_list('identity_id', flat=True)
            .distinct()
        )
        face_identities = (
            FaceIdentity.objects
            .filter(id__in=fi_ids, is_auto_named=False)
            .order_by('name')
            .only('id', 'name', 'is_auto_named')
        )

    # All speakers for merge dropdown (exclude self)
    all_speakers = (
        SpeakerIdentity.objects
        .filter(segments__video__channel_id__in=user_channel_ids)
        .distinct()
        .exclude(id=speaker_id)
        .order_by('name')
    )

    return render(request, 'videos/speaker_identity.html', {
        'speaker':          speaker,
        'video_groups':     video_groups,
        'face_identities':  face_identities,
        'all_speakers':     all_speakers,
        'unread_count':     _unread_count(request),
        'phrase_query':     phrase_q,
        'active_phrase':    active_phrase,
        'all_video_count':  all_video_count,
    })


# ─── Speaker Identity API ─────────────────────────────────────────────────────

@api_view(['POST', 'PATCH'])
@api_login_required
def speaker_rename(request, speaker_id):
    """
    POST /api/speakers/<id>/rename/
    Body: {"name": "Alice"}
    """
    speaker = get_object_or_404(SpeakerIdentity, id=speaker_id)
    name    = request.data.get('name', '').strip()
    if not name:
        return Response({'error': 'name is required.'}, status=400)
    speaker.name          = name
    speaker.is_auto_named = False
    speaker.save(update_fields=['name', 'is_auto_named'])
    return Response({'id': speaker.pk, 'name': speaker.name})


@api_view(['POST'])
@api_login_required
def speaker_set_role(request, speaker_id):
    """
    POST /api/speakers/<id>/set-role/
    Body: {"role": "narrator"} — one of speaker/narrator/background
    """
    speaker = get_object_or_404(SpeakerIdentity, id=speaker_id)
    role    = request.data.get('role', '').strip()
    valid   = {SpeakerIdentity.ROLE_SPEAKER, SpeakerIdentity.ROLE_NARRATOR, SpeakerIdentity.ROLE_BACKGROUND}
    if role not in valid:
        return Response({'error': f'role must be one of {sorted(valid)}.'}, status=400)
    speaker.role = role
    speaker.save(update_fields=['role'])
    return Response({'id': speaker.pk, 'role': speaker.role})


@api_view(['POST'])
@api_login_required
def speaker_link_face(request, speaker_id):
    """
    POST /api/speakers/<id>/link-face/
    Body: {"face_identity_id": 42}  or  {"face_identity_id": null} to unlink
    """
    speaker = get_object_or_404(SpeakerIdentity, id=speaker_id)
    fi_id   = request.data.get('face_identity_id')
    if fi_id is None:
        speaker.face_identity = None
        speaker.save(update_fields=['face_identity'])
        return Response({'id': speaker.pk, 'face_identity_id': None})
    fi = get_object_or_404(FaceIdentity, id=fi_id)
    speaker.face_identity = fi
    update_fields = ['face_identity']
    # If the speaker is still auto-named, adopt the face identity's name
    renamed = False
    if speaker.is_auto_named and not fi.is_auto_named:
        speaker.name          = fi.name
        speaker.is_auto_named = False
        update_fields += ['name', 'is_auto_named']
        renamed = True
    speaker.save(update_fields=update_fields)
    return Response({
        'id':               speaker.pk,
        'face_identity_id': fi.pk,
        'face_identity_name': fi.name,
        'renamed':          renamed,
        'new_name':         speaker.name,
    })


@api_view(['POST'])
@api_login_required
def speaker_merge(request, speaker_id):
    """
    POST /api/speakers/<id>/merge/
    Body: {"into_id": 7}
    Moves all segments from speaker_id → into_id, then deletes speaker_id.
    """
    speaker = get_object_or_404(SpeakerIdentity, id=speaker_id)
    into_id = request.data.get('into_id')
    if not into_id:
        return Response({'error': 'into_id is required.'}, status=400)
    if int(into_id) == speaker_id:
        return Response({'error': 'Cannot merge a speaker into itself.'}, status=400)
    target = get_object_or_404(SpeakerIdentity, id=into_id)
    moved  = VideoSegment.objects.filter(speaker_identity=speaker).update(speaker_identity=target)

    # Recalculate embedding: weighted average by segment count so the identity
    # with more data contributes proportionally more to the merged embedding.
    if speaker.speaker_embedding is not None or target.speaker_embedding is not None:
        import numpy as np
        src_count = moved                               # segments being merged in
        tgt_count = VideoSegment.objects.filter(speaker_identity=target).count()
        total     = src_count + tgt_count
        if total > 0 and speaker.speaker_embedding is not None and target.speaker_embedding is not None:
            src_emb = np.array(speaker.speaker_embedding)
            tgt_emb = np.array(target.speaker_embedding)
            merged  = (src_emb * src_count + tgt_emb * tgt_count) / total
            norm    = np.linalg.norm(merged)
            if norm > 0:
                target.speaker_embedding = (merged / norm).tolist()
        elif target.speaker_embedding is None and speaker.speaker_embedding is not None:
            target.speaker_embedding = speaker.speaker_embedding
        target.save(update_fields=['speaker_embedding'])

    speaker.delete()
    return Response({'ok': True, 'moved_segments': moved, 'into': {'id': target.pk, 'name': target.name}})


@api_view(['DELETE'])
@api_login_required
def speaker_delete(request, speaker_id):
    """
    DELETE /api/speakers/<id>/delete/
    Unlinks segments (sets speaker_identity=NULL) then deletes the identity.
    """
    speaker = get_object_or_404(SpeakerIdentity, id=speaker_id)
    VideoSegment.objects.filter(speaker_identity=speaker).update(speaker_identity=None)
    speaker.delete()
    return Response(status=204)


@api_view(['GET'])
@api_login_required
def speaker_list_api(request):
    """
    GET /api/speakers/list/
    Returns all SpeakerIdentity rows visible to the current user (for merge dropdown).
    """
    user_channels = _user_channels(request.user)
    if not user_channels.exists():
        return Response([])
    user_channel_ids = list(user_channels.values_list('id', flat=True))
    speakers = (
        SpeakerIdentity.objects
        .filter(segments__video__channel_id__in=user_channel_ids)
        .distinct()
        .order_by('name')
    )
    return Response([
        {'id': s.pk, 'name': s.name, 'role': s.role, 'is_auto': s.is_auto_named}
        for s in speakers
    ])


@api_view(['GET'])
def video_speakers_list(request, video_id):
    """
    GET /api/videos/<id>/speakers/
    Returns SpeakerIdentity rows that appear in this video's segments.
    Public endpoint (no login required) — used by watch page.
    """
    video = get_object_or_404(Video, id=video_id)
    speakers = (
        SpeakerIdentity.objects
        .filter(segments__video=video)
        .distinct()
        .annotate(
            segment_count=Count('segments', filter=Q(segments__video=video))
        )
        .order_by('name')
        .select_related('face_identity')
    )
    data = []
    for s in speakers:
        data.append({
            'id':            s.pk,
            'name':          s.name,
            'role':          s.role,
            'is_auto':       s.is_auto_named,
            'segment_count': s.segment_count,
            'face_name':     s.face_identity.name if s.face_identity else None,
            'face_thumbnail': s.face_identity.thumbnail_url if s.face_identity else None,
        })
    return Response(data)
