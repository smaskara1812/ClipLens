import uuid
import re
from django.db import models
from django.contrib.auth.models import User
from django.db.models.signals import post_delete
from django.dispatch import receiver
from pgvector.django import VectorField


# ────────────────────────────────────────────────────────────────────────
# Soft-delete managers — shared by Video and Photo
#
# Default manager (`objects`) hides soft-deleted rows (deleted_at IS NOT NULL),
# so every existing query across views, searches, counts, and reverse FK
# lookups automatically skips trash.
#
# `all_objects` returns every row (including soft-deleted) for the
# deleted-media admin page, restore/purge endpoints, and storage scans.
# ────────────────────────────────────────────────────────────────────────
class _AliveQuerySet(models.QuerySet):
    def alive(self):
        return self.filter(deleted_at__isnull=True)

    def deleted(self):
        return self.filter(deleted_at__isnull=False)


class AliveManager(models.Manager):
    """Default manager — excludes soft-deleted rows."""
    def get_queryset(self):
        return _AliveQuerySet(self.model, using=self._db).filter(deleted_at__isnull=True)


class AllObjectsManager(models.Manager):
    """Manager that returns everything, including soft-deleted rows."""
    def get_queryset(self):
        return _AliveQuerySet(self.model, using=self._db)

    def alive(self):
        return self.get_queryset().alive()

    def deleted(self):
        return self.get_queryset().deleted()


class UserProfile(models.Model):
    """Extends Django's User with a platform role."""
    ROLE_SUPERADMIN = 'superadmin'
    ROLE_EDITOR     = 'editor'
    ROLE_VIEWER     = 'viewer'
    ROLE_CHOICES    = [
        (ROLE_SUPERADMIN, 'Super Admin'),
        (ROLE_EDITOR,     'Editor'),
        (ROLE_VIEWER,     'Viewer'),
    ]
    user       = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    role       = models.CharField(max_length=20, choices=ROLE_CHOICES, default=ROLE_VIEWER)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.user.username} ({self.role})'

    @property
    def is_editor(self):
        return self.role in (self.ROLE_EDITOR, self.ROLE_SUPERADMIN)

    @property
    def is_superadmin(self):
        return self.role == self.ROLE_SUPERADMIN


class Channel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='channels'
    )
    editors = models.ManyToManyField(
        User,
        related_name='editable_channels',
        blank=True,
        verbose_name='Additional Editors',
    )
    name = models.CharField(max_length=150, unique=True)
    slug = models.SlugField(max_length=150, unique=True)
    description = models.TextField(blank=True)
    avatar = models.ImageField(upload_to='channels/avatars/', blank=True, null=True)
    banner = models.ImageField(upload_to='channels/banners/', blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    @property
    def avatar_url(self):
        return self.avatar.url if self.avatar else None

    @property
    def banner_url(self):
        return self.banner.url if self.banner else None

    @property
    def subscriber_count(self):
        return self.subscribers.count()


class ChannelLink(models.Model):
    """Social / website links shown on the channel page."""
    channel = models.ForeignKey(Channel, on_delete=models.CASCADE, related_name='links')
    label   = models.CharField(max_length=100)
    url     = models.URLField()
    order   = models.IntegerField(default=0)

    class Meta:
        ordering = ['order']

    def __str__(self):
        return f'{self.channel.name} — {self.label}'


class Category(models.Model):
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=100, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = 'categories'
        ordering = ['name']

    def __str__(self):
        return self.name


class NamedPlace(models.Model):
    """A physical location (plant, office, site) that photos can be tagged with."""
    name          = models.CharField(max_length=150)
    slug          = models.SlugField(max_length=150, unique=True)
    latitude      = models.FloatField()
    longitude     = models.FloatField()
    radius_meters = models.PositiveIntegerField(default=500, help_text='Auto-assign radius in metres')
    color         = models.CharField(max_length=7, default='#3b82f6')  # hex for map circle
    description   = models.TextField(blank=True)
    created_by    = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True)
    created_at    = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class Video(models.Model):
    STATUS_PENDING    = 'pending'
    STATUS_PROCESSING = 'processing'
    STATUS_READY      = 'ready'
    STATUS_FAILED     = 'failed'

    STATUS_CHOICES = [
        (STATUS_PENDING,    'Pending'),
        (STATUS_PROCESSING, 'Processing'),
        (STATUS_READY,      'Ready'),
        (STATUS_FAILED,     'Failed'),
    ]

    VISIBILITY_PUBLIC      = 'public'
    VISIBILITY_PRIVATE     = 'private'
    VISIBILITY_SUBSCRIBERS = 'subscribers_only'

    VISIBILITY_CHOICES = [
        (VISIBILITY_PUBLIC,      'Public'),
        (VISIBILITY_PRIVATE,     'Private (URL only)'),
        (VISIBILITY_SUBSCRIBERS, 'Subscribers Only'),
    ]

    id      = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title   = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    channel  = models.ForeignKey(Channel, on_delete=models.SET_NULL, null=True, blank=True, related_name='videos')
    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True, blank=True, related_name='videos')
    tags     = models.CharField(max_length=500, blank=True, help_text='Comma-separated tags')

    original_file     = models.FileField(upload_to='originals/', blank=True)
    original_filename = models.CharField(max_length=255, blank=True)
    hls_path          = models.CharField(max_length=500, blank=True)
    thumbnail         = models.ImageField(upload_to='thumbnails/', blank=True, null=True)

    duration   = models.FloatField(null=True, blank=True)
    file_size  = models.BigIntegerField(null=True, blank=True,
                                        help_text='Size of the original uploaded file in bytes')
    upscaled_size = models.BigIntegerField(null=True, blank=True,
                                           help_text='Total size of all upscaled MP4 files in media/upscaled/<id>/ in bytes')
    resolution = models.CharField(max_length=20, blank=True)
    available_qualities = models.CharField(max_length=100, blank=True, default='')

    status           = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    processing_error = models.TextField(blank=True)

    # Visibility: public / private / subscribers_only
    visibility       = models.CharField(max_length=20, choices=VISIBILITY_CHOICES, default=VISIBILITY_PUBLIC)
    comments_enabled = models.BooleanField(default=True)
    views_count      = models.PositiveBigIntegerField(default=0)

    # Seek / scrub thumbnail sprite (generated post-processing)
    seek_sprite = models.CharField(max_length=500, blank=True)

    # Location
    latitude    = models.FloatField(null=True, blank=True, db_index=True)
    longitude   = models.FloatField(null=True, blank=True, db_index=True)
    named_place = models.ForeignKey(
        'NamedPlace', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='videos',
    )

    uploaded_by = models.CharField(max_length=100, blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)

    # ── Soft-delete (trash) ──
    deleted_at      = models.DateTimeField(null=True, blank=True, db_index=True,
                                           help_text='If set, this video is in Trash and hidden everywhere')
    deleted_by      = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                        related_name='deleted_videos')
    deleted_reason  = models.CharField(max_length=255, blank=True)

    # Default manager hides soft-deleted rows; all_objects sees everything.
    objects     = AliveManager()
    all_objects = AllObjectsManager()

    class Meta:
        ordering = ['-created_at']
        # IMPORTANT: keep the default manager that hides deleted rows as
        # the base manager too, so reverse relations (channel.videos.all(),
        # named_place.videos.all(), etc.) also skip trash.
        base_manager_name = 'objects'
        default_manager_name = 'objects'
        indexes = [
            models.Index(fields=['status', 'visibility'], name='video_status_vis'),
            models.Index(fields=['channel', 'status', 'visibility'], name='video_ch_status_vis'),
            models.Index(fields=['category', 'status', 'visibility'], name='video_cat_status_vis'),
            models.Index(fields=['created_at'], name='video_created_at'),
            models.Index(fields=['views_count'], name='video_views'),
            models.Index(fields=['duration'], name='video_duration'),
        ]

    def __str__(self):
        return self.title

    # ── Soft-delete helpers ──
    @property
    def is_deleted(self):
        return self.deleted_at is not None

    def soft_delete(self, *, user=None, reason=''):
        """Mark as trashed — hides from everywhere, keeps files on disk."""
        from django.utils import timezone
        self.deleted_at     = timezone.now()
        self.deleted_by     = user if (user and getattr(user, 'pk', None)) else None
        self.deleted_reason = (reason or '')[:255]
        self.save(update_fields=['deleted_at', 'deleted_by', 'deleted_reason', 'updated_at'])

    def restore(self):
        """Revert soft-delete — video becomes visible again."""
        # Use all_objects so the UPDATE isn't filtered by AliveManager (base_manager_name).
        Video.all_objects.filter(pk=self.pk).update(
            deleted_at=None,
            deleted_by=None,
            deleted_reason='',
        )
        self.deleted_at     = None
        self.deleted_by     = None
        self.deleted_reason = ''

    # ── backward-compat property ──────────────────────────────────────────────
    @property
    def is_public(self):
        return self.visibility == self.VISIBILITY_PUBLIC

    @property
    def hls_url(self):
        return f'/media/{self.hls_path}' if self.hls_path else None

    @property
    def qualities_list(self):
        if not self.available_qualities:
            return []
        return [q.strip() for q in self.available_qualities.split(',') if q.strip()]

    @property
    def thumbnail_url(self):
        return self.thumbnail.url if self.thumbnail else None

    @property
    def watch_url(self):
        return f'/watch/{self.id}/'

    @property
    def likes_count(self):
        return self.likes.count()

    def is_visible_to(self, user):
        """Check whether `user` (may be AnonymousUser) can watch this video."""
        if self.visibility == self.VISIBILITY_PUBLIC:
            return True
        if self.visibility == self.VISIBILITY_PRIVATE:
            return True  # accessible via direct URL regardless of auth
        if self.visibility == self.VISIBILITY_SUBSCRIBERS:
            if not user or not user.is_authenticated:
                return False
            if self.channel and hasattr(user, 'channel') and user.channel == self.channel:
                return True  # owner
            return ChannelSubscription.objects.filter(user=user, channel=self.channel).exists()
        return False


# ── Engagement ────────────────────────────────────────────────────────────────

class VideoLike(models.Model):
    user  = models.ForeignKey(User, on_delete=models.CASCADE, related_name='video_likes')
    video = models.ForeignKey(Video, on_delete=models.CASCADE, related_name='likes')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'video')


class Comment(models.Model):
    video  = models.ForeignKey(Video, on_delete=models.CASCADE, related_name='comments')
    user   = models.ForeignKey(User, on_delete=models.CASCADE, related_name='comments')
    parent = models.ForeignKey(
        'self', on_delete=models.CASCADE, null=True, blank=True, related_name='replies'
    )
    text             = models.TextField()
    is_pinned        = models.BooleanField(default=False)
    timestamp_seconds = models.FloatField(null=True, blank=True)  # linked video timestamp
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-is_pinned', '-created_at']

    def __str__(self):
        return f'{self.user.username} on {self.video.title}'

    @property
    def likes_count(self):
        return self.comment_likes.count()

    @property
    def reply_count(self):
        return self.replies.count()

    def get_mentions(self):
        """Return list of mentioned usernames (@username) found in text."""
        return re.findall(r'@(\w+)', self.text)


class CommentLike(models.Model):
    user    = models.ForeignKey(User, on_delete=models.CASCADE, related_name='comment_likes')
    comment = models.ForeignKey(Comment, on_delete=models.CASCADE, related_name='comment_likes')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'comment')


# ── Subscriptions ─────────────────────────────────────────────────────────────

class ChannelSubscription(models.Model):
    user    = models.ForeignKey(User, on_delete=models.CASCADE, related_name='subscriptions')
    channel = models.ForeignKey(Channel, on_delete=models.CASCADE, related_name='subscribers')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'channel')

    def __str__(self):
        return f'{self.user.username} → {self.channel.name}'


# ── Playlists ─────────────────────────────────────────────────────────────────

class Playlist(models.Model):
    id          = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner       = models.ForeignKey(User, on_delete=models.CASCADE, related_name='playlists')
    title       = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    is_public   = models.BooleanField(default=True)
    created_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return self.title

    @property
    def video_count(self):
        return self.items.count()

    @property
    def thumbnail_url(self):
        first = self.items.select_related('video').filter(
            video__thumbnail__isnull=False
        ).first()
        return first.video.thumbnail_url if first else None


class PlaylistItem(models.Model):
    playlist   = models.ForeignKey(Playlist, on_delete=models.CASCADE, related_name='items')
    video      = models.ForeignKey(Video, on_delete=models.CASCADE, related_name='playlist_items')
    order      = models.IntegerField(default=0)
    added_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('playlist', 'video')
        ordering        = ['order', 'added_at']


# ── Watch History & Saved ─────────────────────────────────────────────────────

class WatchHistory(models.Model):
    user             = models.ForeignKey(User, on_delete=models.CASCADE, related_name='watch_history')
    video            = models.ForeignKey(Video, on_delete=models.CASCADE, related_name='watch_histories')
    progress_seconds = models.FloatField(default=0)
    completed        = models.BooleanField(default=False)
    watched_at       = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('user', 'video')
        ordering        = ['-watched_at']
        indexes = [
            models.Index(fields=['user', 'watched_at'], name='watch_user_time'),
        ]

    def __str__(self):
        return f'{self.user.username} watched {self.video.title}'


class SavedVideo(models.Model):
    user     = models.ForeignKey(User, on_delete=models.CASCADE, related_name='saved_videos')
    video    = models.ForeignKey(Video, on_delete=models.CASCADE, related_name='saves')
    saved_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'video')
        ordering        = ['-saved_at']
        indexes = [
            models.Index(fields=['user', 'saved_at'], name='saved_user_time'),
        ]


# ── Analytics ─────────────────────────────────────────────────────────────────

class WatchTimeEntry(models.Model):
    """Daily aggregate of watch-time seconds per video."""
    video         = models.ForeignKey(Video, on_delete=models.CASCADE, related_name='watch_time_entries')
    date          = models.DateField()
    total_seconds = models.BigIntegerField(default=0)

    class Meta:
        unique_together = ('video', 'date')
        ordering        = ['date']


# ── Chapters ─────────────────────────────────────────────────────────────────

class VideoChapter(models.Model):
    video     = models.ForeignKey(Video, on_delete=models.CASCADE, related_name='chapters')
    title     = models.CharField(max_length=255)
    timestamp = models.FloatField(help_text='Start time in seconds')

    class Meta:
        ordering = ['timestamp']

    def __str__(self):
        return f'{self.video.title} — {self.title} @ {self.timestamp}s'


# ── Video Moments ─────────────────────────────────────────────────────────────

class MomentCategory(models.Model):
    """
    A category that can be applied to VideoMoment.
    System categories (is_system=True) ship with the app and cannot be deleted.
    Custom categories (is_system=False) are created by editors/superadmins and are global.
    """
    SYSTEM_CATEGORIES = [
        # (key, name, icon, color, sort_order)
        ('important',   'Important',   '🔴', '#ef4444', 10),
        ('celebration', 'Celebration', '🎉', '#f97316', 20),
        ('watch_again', 'Watch Again', '⭐', '#eab308', 30),
        ('highlight',   'Highlight',   '✅', '#22c55e', 40),
        ('note',        'Note',        '📝', '#3b82f6', 50),
        ('question',    'Question',    '❓', '#a855f7', 60),
    ]

    key        = models.CharField(max_length=50, unique=True)
    name       = models.CharField(max_length=50)
    icon       = models.CharField(max_length=8, default='🏷️')
    color      = models.CharField(max_length=7, default='#6b7280')
    is_system  = models.BooleanField(default=False)
    created_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True)
    sort_order = models.PositiveIntegerField(default=100)

    class Meta:
        ordering = ['sort_order', 'name']

    def __str__(self):
        return self.name

    @classmethod
    def ensure_system_categories(cls):
        """Idempotent — create system categories if they don't exist yet."""
        for key, name, icon, color, order in cls.SYSTEM_CATEGORIES:
            cls.objects.get_or_create(
                key=key,
                defaults=dict(name=name, icon=icon, color=color, is_system=True, sort_order=order),
            )


class VideoMoment(models.Model):
    """
    User-tagged moment in a video.

    Visibility rules:
      private — only the creator can see it (viewers and editors alike)
      team    — visible to all editors and superadmins; viewers cannot see it
    """
    VISIBILITY_PRIVATE = 'private'
    VISIBILITY_TEAM    = 'team'
    VISIBILITY_CHOICES = [
        ('private', 'Private'),
        ('team',    'Team'),
    ]

    CATEGORY_NONE        = ''
    CATEGORY_IMPORTANT   = 'important'    # red
    CATEGORY_CELEBRATION = 'celebration'  # orange
    CATEGORY_WATCH_AGAIN = 'watch_again'  # yellow
    CATEGORY_HIGHLIGHT   = 'highlight'    # green
    CATEGORY_NOTE        = 'note'         # blue
    CATEGORY_QUESTION    = 'question'     # purple
    CATEGORY_CHOICES = [
        ('',           'None'),
        ('important',   'Important'),
        ('celebration', 'Celebration'),
        ('watch_again', 'Watch Again'),
        ('highlight',   'Highlight'),
        ('note',        'Note'),
        ('question',    'Question'),
    ]
    # Maps category → hex colour used on seek bar + UI
    CATEGORY_COLOURS = {
        '':            '#6b7280',  # grey  (no category)
        'important':   '#ef4444',  # red
        'celebration': '#f97316',  # orange
        'watch_again': '#eab308',  # yellow
        'highlight':   '#22c55e',  # green
        'note':        '#3b82f6',  # blue
        'question':    '#a855f7',  # purple
    }

    video         = models.ForeignKey(Video, on_delete=models.CASCADE, related_name='moments')
    created_by    = models.ForeignKey(
        'auth.User', on_delete=models.SET_NULL, null=True, related_name='video_moments'
    )
    timestamp     = models.FloatField(help_text='Start time in seconds')
    end_timestamp = models.FloatField(null=True, blank=True, help_text='End time in seconds (optional)')
    title         = models.CharField(max_length=200)
    description   = models.TextField(blank=True)
    visibility    = models.CharField(max_length=10, choices=VISIBILITY_CHOICES, default=VISIBILITY_PRIVATE)
    # Stores the MomentCategory.key — may be a system key or a custom 'c_<id>' key
    category      = models.CharField(max_length=50, blank=True, default='')
    created_at    = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['timestamp']
        indexes = [
            models.Index(fields=['video', 'visibility']),
            models.Index(fields=['video', 'created_by']),
        ]

    def __str__(self):
        return f'{self.video.title} — {self.title} @ {self.timestamp}s [{self.visibility}]'


# ── End Screens ───────────────────────────────────────────────────────────────

class EndScreen(models.Model):
    video                    = models.ForeignKey(Video, on_delete=models.CASCADE, related_name='end_screens')
    target_video             = models.ForeignKey(
        Video, on_delete=models.SET_NULL, null=True, blank=True, related_name='end_screen_targets'
    )
    target_url               = models.URLField(blank=True)
    label                    = models.CharField(max_length=100)
    start_seconds_before_end = models.FloatField(default=20)

    def __str__(self):
        return f'EndScreen on {self.video.title} → {self.label}'


# ── Video Segments (speech index) ────────────────────────────────────────────

class VideoSegment(models.Model):
    """
    One transcript segment produced by Whisper.
    Enables full-text in-video search: find the exact second a phrase was spoken.
    """
    video         = models.ForeignKey(Video, on_delete=models.CASCADE, related_name='segments')
    start_seconds = models.FloatField()
    end_seconds   = models.FloatField()
    text          = models.TextField()
    speaker_label    = models.CharField(max_length=50, blank=True,
                                        help_text='Raw pyannote speaker label, e.g. SPEAKER_02')
    speaker_identity = models.ForeignKey(
        'SpeakerIdentity', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='segments',
        help_text='Resolved SpeakerIdentity — set after diarization',
    )

    class Meta:
        ordering = ['start_seconds']
        indexes  = [
            models.Index(fields=['video', 'start_seconds']),
            models.Index(fields=['speaker_identity', 'video', 'start_seconds'], name='videoseg_spk_vid_start'),
            models.Index(fields=['speaker_identity'], name='videoseg_speaker'),
        ]

    def __str__(self):
        return f'{self.video.title} @ {self.start_seconds:.1f}s: {self.text[:60]}'


# ── Video Frames (visual / object-detection index) ───────────────────────────

class VideoFrame(models.Model):
    """
    One sampled frame from a video, annotated with YOLO object-detection labels.
    Enables visual search: find the exact second an object / scene appears.
    """
    video       = models.ForeignKey(Video, on_delete=models.CASCADE, related_name='frames')
    timestamp   = models.FloatField(help_text='Seconds into the video')
    labels      = models.TextField(blank=True,
                                   help_text='Comma-separated YOLO class labels, e.g. "person, car, dog"')
    face_count  = models.IntegerField(default=0,
                                      help_text='Number of "person" detections in this frame')
    face_names  = models.TextField(blank=True,
                                   help_text='Comma-separated identified names (Phase B InsightFace)')
    description     = models.TextField(blank=True,
                                       help_text='Natural language scene description (Phase C)')
    clip_embedding  = VectorField(dimensions=512, null=True, blank=True,
                                  help_text='CLIP image embedding (512-dim float vector)')

    class Meta:
        ordering = ['timestamp']
        indexes  = [models.Index(fields=['video', 'timestamp'])]

    def __str__(self):
        return f'{self.video.title} @ {self.timestamp:.1f}s: {self.labels[:60]}'

    @property
    def labels_list(self):
        if not self.labels:
            return []
        return [l.strip() for l in self.labels.split(',') if l.strip()]


# ── Face Recognition (Phase 2) ───────────────────────────────────────────────

class FaceIdentity(models.Model):
    """
    A named person that can appear across many videos.
    Created automatically (name='Person N') during frame analysis,
    and renamed by the channel owner via the API.
    """
    name         = models.CharField(max_length=100)
    is_auto_named= models.BooleanField(default=True,
                                       help_text='True until owner gives this identity a real name')
    ref_embedding= models.TextField(blank=True,
                                    help_text='JSON float array — running average of all face embeddings')
    thumbnail    = models.CharField(max_length=500, blank=True,
                                    help_text='Relative path to a representative face-crop JPEG')
    created_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']
        indexes = [
            models.Index(fields=['name'], name='faceidentity_name'),
            models.Index(fields=['is_auto_named'], name='faceidentity_auto'),
        ]

    def __str__(self):
        return self.name

    @property
    def thumbnail_url(self):
        return f'/media/{self.thumbnail}' if self.thumbnail else None


class DetectedFace(models.Model):
    """
    One face detected in a video frame or a photo via InsightFace.
    Many faces can share one FaceIdentity.
    """
    STATUS_UNREVIEWED = 'unreviewed'
    STATUS_CONFIRMED  = 'confirmed'
    STATUS_REJECTED   = 'rejected'
    STATUS_CHOICES    = [
        (STATUS_UNREVIEWED, 'Unreviewed'),
        (STATUS_CONFIRMED,  'Confirmed'),
        (STATUS_REJECTED,   'Rejected'),
    ]

    video     = models.ForeignKey(Video,        on_delete=models.CASCADE,
                                  related_name='detected_faces', null=True, blank=True)
    photo     = models.ForeignKey('Photo',      on_delete=models.CASCADE,
                                  related_name='detected_faces', null=True, blank=True)
    frame     = models.ForeignKey('VideoFrame', on_delete=models.CASCADE,
                                  related_name='faces', null=True, blank=True)
    identity  = models.ForeignKey(FaceIdentity, on_delete=models.SET_NULL,
                                  null=True, blank=True, related_name='faces')
    timestamp = models.FloatField(help_text='Seconds into the video')
    bbox      = models.TextField(help_text='JSON [x1,y1,x2,y2] pixel coords')
    embedding = models.TextField(blank=True,
                                 help_text='JSON float[512] — InsightFace ArcFace embedding')
    confidence= models.FloatField(default=0.0,
                                  help_text='InsightFace detection score (0–1)')
    crop_path = models.CharField(max_length=500, blank=True,
                                 help_text='Relative path to cropped face JPEG in media/')
    status    = models.CharField(max_length=20, choices=STATUS_CHOICES,
                                 default=STATUS_UNREVIEWED,
                                 help_text='Manual review status: unreviewed/confirmed/rejected')

    class Meta:
        ordering = ['timestamp']
        indexes  = [
            models.Index(fields=['video', 'timestamp']),
            models.Index(fields=['photo', 'timestamp']),
            models.Index(fields=['identity']),
            models.Index(fields=['video', 'identity', 'timestamp'], name='detface_vid_ident_ts'),
            # Composite indexes for faces_page GROUP BY + conditional COUNTs
            models.Index(fields=['identity', 'status'], name='detface_identity_status'),
            models.Index(fields=['identity', 'video'],  name='detface_identity_video'),
            models.Index(fields=['identity', 'photo'],  name='detface_identity_photo'),
        ]

    def __str__(self):
        name = self.identity.name if self.identity_id else 'Unknown'
        if self.video_id:
            return f'{self.video.title} @ {self.timestamp:.1f}s — {name}'
        if self.photo_id:
            return f'{self.photo.title} — {name}'
        return f'Unknown source — {name}'

    @property
    def crop_url(self):
        return f'/media/{self.crop_path}' if self.crop_path else None


# ── Speaker Identity ─────────────────────────────────────────────────────────

class SpeakerIdentity(models.Model):
    """
    A named speaker that can appear across many videos.
    Created automatically during diarization (name='Speaker N'),
    and managed by the channel owner via the Speakers UI.
    """
    ROLE_SPEAKER    = 'speaker'
    ROLE_NARRATOR   = 'narrator'
    ROLE_BACKGROUND = 'background'
    ROLE_CHOICES = [
        (ROLE_SPEAKER,    'Speaker'),
        (ROLE_NARRATOR,   'Narrator'),
        (ROLE_BACKGROUND, 'Background'),
    ]

    name          = models.CharField(max_length=100)
    is_auto_named = models.BooleanField(default=True,
                                        help_text='True until owner gives this speaker a real name')
    role          = models.CharField(max_length=20, choices=ROLE_CHOICES, default=ROLE_SPEAKER)
    # Optional manual link to a FaceIdentity (user asserts "this voice = this person")
    face_identity = models.ForeignKey(
        'FaceIdentity', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='speaker_identities',
        help_text='Manually linked face identity — set by user to cross-reference voice + face',
    )
    # 256-dim wespeaker embedding — mean of all speaker segments across all videos.
    # Used for cross-video speaker matching (cosine similarity ≥ SPEAKER_MATCH_THRESHOLD).
    speaker_embedding = VectorField(
        dimensions=256, null=True, blank=True,
        help_text='Mean wespeaker-voxceleb-resnet34-LM embedding for cross-video matching',
    )
    created_at    = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']
        indexes  = [
            models.Index(fields=['name'],          name='speakerid_name'),
            models.Index(fields=['is_auto_named'], name='speakerid_auto'),
            models.Index(fields=['role'],          name='speakerid_role'),
        ]

    def __str__(self):
        return f'{self.name} ({self.role})'


class SpeakerFaceSuggestion(models.Model):
    """
    Pending suggestion that a speaker voice matches a face identity.
    Created after diarization from temporal overlap; user confirms or rejects.
    """
    STATUS_PENDING = 'pending'
    STATUS_ACCEPTED = 'accepted'
    STATUS_REJECTED = 'rejected'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_ACCEPTED, 'Accepted'),
        (STATUS_REJECTED, 'Rejected'),
    ]

    speaker_identity = models.ForeignKey(
        'SpeakerIdentity',
        on_delete=models.CASCADE,
        related_name='face_suggestions',
    )
    face_identity = models.ForeignKey(
        'FaceIdentity',
        on_delete=models.CASCADE,
        related_name='speaker_suggestions',
    )
    video = models.ForeignKey(
        'Video',
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name='speaker_face_suggestions',
        help_text='Source video where this co-occurrence was observed.',
    )
    score = models.FloatField(default=0.0, help_text='Higher = stronger evidence (overlap ratio vs speech).')
    overlap_seconds = models.FloatField(default=0.0, help_text='Estimated speech/face overlap in seconds.')
    evidence = models.JSONField(default=dict, blank=True, help_text='Heuristic metadata for review.')
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-score', '-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['speaker_identity', 'face_identity', 'video'],
                name='spk_face_suggestion_unique_triplet',
            ),
        ]
        indexes = [
            models.Index(fields=['speaker_identity', 'status'], name='spkfacesugg_spk_status'),
            models.Index(fields=['face_identity', 'status'], name='spkfacesugg_face_status'),
            models.Index(fields=['video'], name='spkfacesugg_video'),
        ]

    def __str__(self):
        return f'{self.speaker_identity.name} → {self.face_identity.name} ({self.status})'


# ── Subtitles / Captions ─────────────────────────────────────────────────────

class Subtitle(models.Model):
    FORMAT_VTT = 'vtt'
    FORMAT_SRT = 'srt'
    FORMATS = [(FORMAT_VTT, 'WebVTT'), (FORMAT_SRT, 'SRT')]

    video            = models.ForeignKey(Video, on_delete=models.CASCADE, related_name='subtitles')
    language         = models.CharField(max_length=10, default='en',
                                        help_text='BCP-47 code, e.g. en, fr, es')
    language_label   = models.CharField(max_length=60, default='English')
    format           = models.CharField(max_length=3, choices=FORMATS, default=FORMAT_VTT)
    file             = models.FileField(upload_to='subtitles/')
    is_auto_generated= models.BooleanField(default=False)
    is_translation   = models.BooleanField(default=False,
                                           help_text='True if generated by NLLB translation, not direct transcription')
    source_language  = models.CharField(max_length=10, blank=True, default='',
                                        help_text='BCP-47 code of the language this was translated from')
    created_at       = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['language']
        unique_together = [('video', 'language', 'is_auto_generated', 'is_translation')]

    def __str__(self):
        tags = []
        if self.is_auto_generated: tags.append('auto')
        if self.is_translation: tags.append('translated')
        tag = ' [' + ', '.join(tags) + ']' if tags else ''
        return f'{self.video.title} — {self.language_label}{tag}'

    @property
    def url(self):
        return self.file.url if self.file else None


# ── Audio Events (non-speech audio tagging + silence) ────────────────────────

class AudioEvent(models.Model):
    """
    One non-speech audio span detected on a video.

    Populated by `videos.tasks.detect_audio_events_task`, which runs as a
    follow-up to speaker diarization.  Drives the third lane ("Audio events")
    on the video X-ray page.  Speech segments live in `VideoSegment`, not
    here — this table intentionally has no text/speaker fields.

    Sources:
        - 'panns'  → PANNs CNN14 multi-label tagger over sliding windows
        - 'ffmpeg' → FFmpeg `silencedetect` (used only for `label='silence'`)
    """
    LABEL_SILENCE  = 'silence'
    LABEL_APPLAUSE = 'applause'
    LABEL_LAUGHTER = 'laughter'
    LABEL_MUSIC    = 'music'
    LABEL_CHEERING = 'cheering'
    LABEL_CROWD    = 'crowd'
    LABEL_BOOING   = 'booing'
    LABEL_SPEECH   = 'speech'
    LABEL_CHOICES  = [
        (LABEL_SILENCE,  'Silence'),
        (LABEL_APPLAUSE, 'Applause'),
        (LABEL_LAUGHTER, 'Laughter'),
        (LABEL_MUSIC,    'Music'),
        (LABEL_CHEERING, 'Cheering'),
        (LABEL_CROWD,    'Crowd noise'),
        (LABEL_BOOING,   'Booing'),
        (LABEL_SPEECH,   'Speech'),
    ]

    SOURCE_PANNS  = 'panns'
    SOURCE_FFMPEG = 'ffmpeg'
    SOURCE_CHOICES = [
        (SOURCE_PANNS,  'PANNs'),
        (SOURCE_FFMPEG, 'FFmpeg silencedetect'),
    ]

    video         = models.ForeignKey(Video, on_delete=models.CASCADE, related_name='audio_events')
    start_seconds = models.FloatField()
    end_seconds   = models.FloatField()
    label         = models.CharField(max_length=32, choices=LABEL_CHOICES)
    confidence    = models.FloatField(default=0.0,
                                      help_text='Mean softmax probability for PANNs; 0.0 for FFmpeg-derived silence')
    source        = models.CharField(max_length=16, choices=SOURCE_CHOICES, default=SOURCE_PANNS)
    created_at    = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['start_seconds']
        indexes  = [
            models.Index(fields=['video', 'start_seconds']),
            models.Index(fields=['video', 'label']),
        ]

    def __str__(self):
        return f'{self.video.title} [{self.label} {self.start_seconds:.1f}–{self.end_seconds:.1f}s]'


# ── Audio Tracks ──────────────────────────────────────────────────────────────

class AudioTrack(models.Model):
    video      = models.ForeignKey(Video, on_delete=models.CASCADE, related_name='audio_tracks')
    label      = models.CharField(max_length=100, help_text='e.g. English, Director\'s Commentary')
    language   = models.CharField(max_length=10, default='en')
    track_index= models.IntegerField(default=0, help_text='Stream index in the source file')
    hls_path   = models.CharField(max_length=500, blank=True,
                                  help_text='Relative path to the HLS audio-only playlist')
    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-is_default', 'track_index']

    def __str__(self):
        return f'{self.video.title} — {self.label}'


# ── Photos (Digital Asset Management) ───────────────────────────────────────

class Photo(models.Model):
    STATUS_PENDING    = 'pending'
    STATUS_PROCESSING = 'processing'
    STATUS_READY      = 'ready'
    STATUS_FAILED     = 'failed'
    STATUS_CHOICES = [
        (STATUS_PENDING,    'Pending'),
        (STATUS_PROCESSING, 'Processing'),
        (STATUS_READY,      'Ready'),
        (STATUS_FAILED,     'Failed'),
    ]

    VISIBILITY_PUBLIC  = 'public'
    VISIBILITY_PRIVATE = 'private'
    VISIBILITY_CHOICES = [
        (VISIBILITY_PUBLIC,  'Public'),
        (VISIBILITY_PRIVATE, 'Private (URL only)'),
    ]

    id          = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title       = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    channel     = models.ForeignKey(Channel,  on_delete=models.SET_NULL, null=True, blank=True, related_name='photos')
    category    = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True, blank=True, related_name='photos')
    tags        = models.CharField(max_length=500, blank=True, help_text='Comma-separated tags')

    file        = models.ImageField(upload_to='photos/originals/')
    thumbnail   = models.ImageField(upload_to='photos/thumbnails/', blank=True, null=True)

    width       = models.IntegerField(null=True, blank=True)
    height      = models.IntegerField(null=True, blank=True)
    file_size   = models.BigIntegerField(null=True, blank=True)

    # AI analysis results
    labels            = models.TextField(blank=True, help_text='Comma-separated YOLO class labels')
    face_count        = models.IntegerField(default=0)
    face_names        = models.TextField(blank=True, help_text='Comma-separated identified names')
    scene_description = models.TextField(blank=True, help_text='BLIP/Florence-2 scene description')
    clip_embedding    = VectorField(dimensions=512, null=True, blank=True,
                                   help_text='CLIP image embedding (512-dim float vector)')

    # OCR — extracted text visible in the image
    ocr_text = models.TextField(blank=True, help_text='Text detected in image via OCR')

    # EXIF metadata — extracted from image file at upload time
    exif_data = models.JSONField(null=True, blank=True,
                                 help_text='Camera EXIF metadata: make, model, GPS, exposure settings, etc.')
    taken_at  = models.DateTimeField(null=True, blank=True, db_index=True,
                                     help_text='Date/time the photo was taken (from EXIF DateTimeOriginal)')

    # GPS coordinates — indexed for fast geo queries (populated from EXIF or set manually)
    latitude    = models.FloatField(null=True, blank=True, db_index=True)
    longitude   = models.FloatField(null=True, blank=True, db_index=True)
    named_place = models.ForeignKey('NamedPlace', on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name='photos')

    # Archive — hidden from main library without deleting
    is_archived = models.BooleanField(default=False, db_index=True,
                                      help_text='Archived photos are hidden from the main library but not deleted')

    # Duplicate detection — set by analyze_photo_task when cosine similarity > 0.97
    is_potential_duplicate = models.BooleanField(default=False, db_index=True)
    duplicate_of           = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.SET_NULL, related_name='duplicates'
    )

    status           = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    processing_error = models.TextField(blank=True)

    visibility  = models.CharField(max_length=20, choices=VISIBILITY_CHOICES, default=VISIBILITY_PUBLIC)
    views_count = models.PositiveBigIntegerField(default=0)
    uploaded_by = models.CharField(max_length=100, blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)

    # ── Soft-delete (trash) ──
    deleted_at      = models.DateTimeField(null=True, blank=True, db_index=True,
                                           help_text='If set, this photo is in Trash and hidden everywhere')
    deleted_by      = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                        related_name='deleted_photos')
    deleted_reason  = models.CharField(max_length=255, blank=True)

    # Default manager hides soft-deleted rows; all_objects sees everything.
    objects     = AliveManager()
    all_objects = AllObjectsManager()

    class Meta:
        ordering = ['-created_at']
        base_manager_name = 'objects'
        default_manager_name = 'objects'
        indexes = [
            models.Index(fields=['status', 'visibility'], name='photo_status_vis'),
            models.Index(fields=['channel', 'status', 'visibility'], name='photo_ch_status_vis'),
            models.Index(fields=['created_at'], name='photo_created_at'),
        ]

    def __str__(self):
        return self.title

    # ── Soft-delete helpers ──
    @property
    def is_deleted(self):
        return self.deleted_at is not None

    def soft_delete(self, *, user=None, reason=''):
        from django.utils import timezone
        self.deleted_at     = timezone.now()
        self.deleted_by     = user if (user and getattr(user, 'pk', None)) else None
        self.deleted_reason = (reason or '')[:255]
        self.save(update_fields=['deleted_at', 'deleted_by', 'deleted_reason', 'updated_at'])

    def restore(self):
        # Use all_objects so the UPDATE isn't filtered by AliveManager (base_manager_name).
        Photo.all_objects.filter(pk=self.pk).update(
            deleted_at=None,
            deleted_by=None,
            deleted_reason='',
        )
        self.deleted_at     = None
        self.deleted_by     = None
        self.deleted_reason = ''

    @property
    def thumbnail_url(self):
        if self.thumbnail:
            return self.thumbnail.url
        return self.file.url if self.file else None

    @property
    def labels_list(self):
        if not self.labels:
            return []
        return [lbl.strip() for lbl in self.labels.split(',') if lbl.strip()]

    @property
    def face_names_list(self):
        if not self.face_names:
            return []
        return [n.strip() for n in self.face_names.split(',') if n.strip()]

    @property
    def photo_url(self):
        return f'/photos/{self.id}/'


@receiver(post_delete, sender='videos.Video')
def _cleanup_video_files_on_delete(sender, instance, **kwargs):
    """
    Guarantee physical-file cleanup whenever a Video row is deleted — regardless of
    whether the deletion came from the custom API view, Django admin, or any management
    command.  The view-level cleanup is kept for symmetry but this is the safety net.
    """
    import shutil as _shutil
    from django.conf import settings as _settings
    from pathlib import Path as _Path

    media = _Path(_settings.MEDIA_ROOT)

    # 1. HLS segments directory
    if instance.hls_path:
        hls_dir = media / 'hls' / str(instance.id)
        if hls_dir.exists():
            try:
                _shutil.rmtree(hls_dir)
            except Exception:
                pass

    # 2. Original uploaded file
    if instance.original_file:
        try:
            orig = media / instance.original_file.name
            if orig.exists():
                orig.unlink()
        except Exception:
            pass

    # 3. Upscaled MP4 files (media/upscaled/<video_id>/)
    upscale_dir = media / 'upscaled' / str(instance.id)
    if upscale_dir.exists():
        try:
            _shutil.rmtree(upscale_dir)
        except Exception:
            pass

    # 4. Thumbnail
    if instance.thumbnail:
        try:
            thumb = _Path(instance.thumbnail.path)
            if thumb.exists():
                thumb.unlink()
        except Exception:
            pass

    # 5. Seek sprite
    if instance.seek_sprite:
        seek_path = media / instance.seek_sprite
        if seek_path.exists():
            try:
                seek_path.unlink()
            except Exception:
                pass


@receiver(post_delete, sender='videos.Photo')
def _clear_orphaned_duplicate_flags(sender, instance, **kwargs):
    """
    When an "original" photo is deleted, Django SET_NULL clears duplicate_of on
    the photos that pointed to it — but is_potential_duplicate stays True.
    This signal cleans up those orphaned flags in one UPDATE.
    """
    sender.objects.filter(
        is_potential_duplicate=True,
        duplicate_of__isnull=True,
    ).update(is_potential_duplicate=False)


# ── Notifications ─────────────────────────────────────────────────────────────

class Notification(models.Model):
    TYPE_MENTION   = 'mention'
    TYPE_COMMENT   = 'comment'
    TYPE_REPLY     = 'reply'
    TYPE_SUBSCRIBE = 'subscribe'
    TYPE_NEW_VIDEO = 'new_video'

    TYPES = [
        (TYPE_MENTION,   'Mention'),
        (TYPE_COMMENT,   'Comment on your video'),
        (TYPE_REPLY,     'Reply to your comment'),
        (TYPE_SUBSCRIBE, 'New subscriber'),
        (TYPE_NEW_VIDEO, 'New video'),
    ]

    recipient         = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notifications')
    sender            = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='sent_notifications')
    notification_type = models.CharField(max_length=20, choices=TYPES)
    video             = models.ForeignKey(Video, on_delete=models.SET_NULL, null=True, blank=True)
    comment           = models.ForeignKey(Comment, on_delete=models.SET_NULL, null=True, blank=True)
    message           = models.CharField(max_length=255)
    link              = models.CharField(max_length=255)
    is_read           = models.BooleanField(default=False)
    created_at        = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['recipient', 'is_read', 'created_at'], name='notif_recipient'),
        ]

    def __str__(self):
        return f'→ {self.recipient.username}: {self.message}'


# ── Albums (Google Photos-style collections) ──────────────────────────────────

import secrets as _secrets


class Album(models.Model):
    """User-created or smart-generated photo collection."""

    id          = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner       = models.ForeignKey(User, on_delete=models.CASCADE, related_name='albums')
    channel     = models.ForeignKey(Channel, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name='albums')
    title       = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    cover_photo = models.ForeignKey('Photo', null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name='+')

    # Smart album — auto-populated from AI metadata
    is_smart     = models.BooleanField(default=False)
    smart_filter = models.JSONField(null=True, blank=True,
                                    help_text='e.g. {"label":"dog"} or {"face":"Soham"}')

    is_public   = models.BooleanField(default=True)
    share_token = models.CharField(max_length=64, unique=True, blank=True,
                                   help_text='Token for shareable public link')

    photo_count = models.PositiveIntegerField(default=0)   # cached; updated on add/remove
    created_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        indexes = [
            models.Index(fields=['owner', 'is_smart'], name='album_owner_smart'),
            models.Index(fields=['share_token'],        name='album_share_token'),
        ]

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        if not self.share_token:
            self.share_token = _secrets.token_urlsafe(32)
        super().save(*args, **kwargs)

    @property
    def share_url(self):
        return f'/albums/shared/{self.share_token}/'

    @property
    def cover_url(self):
        if self.cover_photo:
            return self.cover_photo.thumbnail_url
        return None


class AlbumPhoto(models.Model):
    """Through model preserving photo order within an album."""

    album    = models.ForeignKey(Album, on_delete=models.CASCADE, related_name='album_photos')
    photo    = models.ForeignKey(Photo, on_delete=models.CASCADE, related_name='album_photos')
    order    = models.PositiveIntegerField(default=0)
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering            = ['order', 'added_at']
        unique_together     = [('album', 'photo')]
        indexes = [
            models.Index(fields=['album', 'order'], name='albphoto_album_order'),
        ]


# ── Face Identity Nicknames ───────────────────────────────────────────────────

class FaceIdentityNickname(models.Model):
    """
    Alternative names / aliases for a FaceIdentity.
    Multiple editors can add nicknames; all are searchable as equivalents of the primary name.
    """
    identity   = models.ForeignKey(FaceIdentity, on_delete=models.CASCADE, related_name='nicknames')
    nickname   = models.CharField(max_length=100)
    added_by   = models.ForeignKey(
        'auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('identity', 'nickname')
        ordering        = ['nickname']
        indexes = [
            models.Index(fields=['nickname'], name='facenick_nickname_idx'),
        ]

    def __str__(self):
        return f'{self.nickname} → {self.identity.name}'


# ── Events (combined photo + video folders) ───────────────────────────────────

class Event(models.Model):
    """
    A named collection grouping both photos and videos — typically imported from
    a single folder (e.g. 'Founded_Day_2015'). Links to an Album for photos and
    a Playlist for videos so each sub-collection is also browsable independently.
    """
    name        = models.CharField(max_length=255)
    slug        = models.SlugField(max_length=255, unique=True, blank=True)
    description = models.TextField(blank=True)
    event_date  = models.DateField(null=True, blank=True)
    tags        = models.TextField(blank=True, help_text='Comma-separated tags (includes folder name)')
    cover_photo = models.ForeignKey(
        Photo, null=True, blank=True, on_delete=models.SET_NULL, related_name='+'
    )
    # Sub-containers — either or both may be null if the folder had only one type
    album       = models.OneToOneField(
        Album, null=True, blank=True, on_delete=models.SET_NULL, related_name='event'
    )
    playlist    = models.OneToOneField(
        Playlist, null=True, blank=True, on_delete=models.SET_NULL, related_name='event'
    )
    created_by  = models.ForeignKey(
        'auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='events'
    )
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-event_date', '-created_at']
        indexes  = [
            models.Index(fields=['slug'],       name='event_slug_idx'),
            models.Index(fields=['event_date'], name='event_date_idx'),
        ]

    def __str__(self):
        return self.name

    @property
    def photo_count(self):
        return self.album.photo_count if self.album_id else 0

    @property
    def video_count(self):
        return self.playlist.items.count() if self.playlist_id else 0

    @property
    def cover_url(self):
        if self.cover_photo:
            return self.cover_photo.thumbnail_url
        if self.album and self.album.cover_photo:
            return self.album.cover_photo.thumbnail_url
        return None


class ActivityLog(models.Model):
    """Append-only audit trail for editorial & administrative actions.

    Views and searches are intentionally NOT logged. Snapshot-style: the
    target is referenced by type + id + a human label so log entries
    survive deletion of the target.
    """
    # --- High-level action taxonomy ---------------------------------
    ACTION_LOGIN          = 'login'
    ACTION_LOGOUT         = 'logout'
    ACTION_LOGIN_FAILED   = 'login_failed'
    ACTION_CREATE         = 'create'
    ACTION_UPDATE         = 'update'
    ACTION_DELETE         = 'delete'
    ACTION_UPLOAD         = 'upload'
    ACTION_ARCHIVE        = 'archive'
    ACTION_RESTORE        = 'restore'
    ACTION_PURGE          = 'purge'
    ACTION_BULK           = 'bulk'
    ACTION_RUN_COMMAND    = 'run_command'
    ACTION_ROLE_CHANGE    = 'role_change'
    ACTION_PERMISSION     = 'permission_change'
    ACTION_REGENERATE     = 'regenerate'
    ACTION_TAG            = 'tag'
    ACTION_MERGE          = 'merge'
    ACTION_RENAME         = 'rename'
    ACTION_OTHER          = 'other'
    ACTION_CHOICES = [
        (ACTION_LOGIN, 'Login'),
        (ACTION_LOGOUT, 'Logout'),
        (ACTION_LOGIN_FAILED, 'Login failed'),
        (ACTION_CREATE, 'Create'),
        (ACTION_UPDATE, 'Update'),
        (ACTION_DELETE, 'Delete'),
        (ACTION_UPLOAD, 'Upload'),
        (ACTION_ARCHIVE, 'Archive'),
        (ACTION_RESTORE, 'Restore'),
        (ACTION_PURGE, 'Permanent delete (purge)'),
        (ACTION_BULK, 'Bulk operation'),
        (ACTION_RUN_COMMAND, 'Run command'),
        (ACTION_ROLE_CHANGE, 'Role change'),
        (ACTION_PERMISSION, 'Permission change'),
        (ACTION_REGENERATE, 'Regenerate'),
        (ACTION_TAG, 'Tag'),
        (ACTION_MERGE, 'Merge'),
        (ACTION_RENAME, 'Rename'),
        (ACTION_OTHER, 'Other'),
    ]

    # --- Who ---
    actor           = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='activity_log_entries'
    )
    actor_username  = models.CharField(max_length=150, blank=True,
                                       help_text='Snapshot of username at time of action')

    # --- What ---
    action  = models.CharField(max_length=32, choices=ACTION_CHOICES, default=ACTION_OTHER)
    verb    = models.CharField(max_length=140, blank=True,
                               help_text='Short human-readable description, e.g. "uploaded video"')

    # --- Target (generic snapshot FK) ---
    target_type  = models.CharField(max_length=64, blank=True,
                                    help_text='Model name of the target, e.g. "Video", "Photo"')
    target_id    = models.CharField(max_length=64, blank=True,
                                    help_text='PK of the target at time of action')
    target_label = models.CharField(max_length=500, blank=True,
                                    help_text='Human-friendly identifier snapshot (title/name)')

    # --- Diff / extra payload ---
    changes  = models.JSONField(default=dict, blank=True,
                                help_text='{ field: {"before": ..., "after": ...}, ... }')
    metadata = models.JSONField(default=dict, blank=True,
                                help_text='Extra free-form context')

    # --- Request context ---
    ip_address     = models.GenericIPAddressField(null=True, blank=True)
    user_agent     = models.CharField(max_length=500, blank=True)
    request_path   = models.CharField(max_length=500, blank=True)
    request_method = models.CharField(max_length=10, blank=True)

    # --- When ---
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-timestamp']
        indexes  = [
            models.Index(fields=['-timestamp'],                 name='act_ts_idx'),
            models.Index(fields=['actor', '-timestamp'],        name='act_actor_ts_idx'),
            models.Index(fields=['target_type', 'target_id'],   name='act_target_idx'),
            models.Index(fields=['action', '-timestamp'],       name='act_action_ts_idx'),
        ]

    def __str__(self):
        who = self.actor_username or 'system'
        tgt = f' {self.target_type}#{self.target_id}' if self.target_type else ''
        return f'[{self.timestamp:%Y-%m-%d %H:%M}] {who} {self.action}{tgt}'


# ── Live Streaming ────────────────────────────────────────────────────────────

import secrets as _secrets

def _generate_stream_key():
    """Generate a short, URL-safe stream key like xK9m-p2Qr-7vNt-Lw3Y."""
    parts = [_secrets.token_urlsafe(4) for _ in range(4)]
    return '-'.join(parts)


class StreamKey(models.Model):
    """One stream key per channel. The key is what the streamer enters in OBS."""
    channel    = models.OneToOneField(Channel, on_delete=models.CASCADE, related_name='stream_key')
    key        = models.CharField(max_length=64, unique=True, default=_generate_stream_key)
    is_active  = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.channel.name} — {self.key}'

    def regenerate(self):
        self.key = _generate_stream_key()
        self.save(update_fields=['key'])


class LiveStream(models.Model):
    """Represents one live streaming session from start to finish."""
    STATUS_LIVE       = 'live'
    STATUS_ENDED      = 'ended'
    STATUS_PROCESSING = 'processing'
    STATUS_READY      = 'ready'
    STATUS_CHOICES = [
        (STATUS_LIVE,       'Live'),
        (STATUS_ENDED,      'Ended'),
        (STATUS_PROCESSING, 'Processing'),
        (STATUS_READY,      'Ready'),
    ]

    channel        = models.ForeignKey(Channel, on_delete=models.CASCADE, related_name='live_streams')
    stream_key     = models.ForeignKey(StreamKey, on_delete=models.SET_NULL, null=True)
    status         = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_LIVE)
    title          = models.CharField(max_length=200, blank=True, default='')
    started_at     = models.DateTimeField(auto_now_add=True)
    ended_at       = models.DateTimeField(null=True, blank=True)
    # Paths relative to MEDIA_ROOT
    hls_path       = models.CharField(max_length=500, blank=True, default='',
                                      help_text='Relative path to live.m3u8 inside MEDIA_ROOT')
    recording_path = models.CharField(max_length=500, blank=True, default='',
                                      help_text='Relative path to recording.mp4 inside MEDIA_ROOT')
    # Set once the VOD is fully processed
    video          = models.OneToOneField(Video, on_delete=models.SET_NULL,
                                          null=True, blank=True, related_name='from_live_stream')

    class Meta:
        ordering = ['-started_at']

    def __str__(self):
        return f'{self.channel.name} — {self.started_at:%Y-%m-%d %H:%M} ({self.status})'

    @property
    def duration_seconds(self):
        if self.ended_at and self.started_at:
            return int((self.ended_at - self.started_at).total_seconds())
        return None
