import uuid
import re
from django.db import models
from django.contrib.auth.models import User
from django.db.models.signals import post_delete
from django.dispatch import receiver
from pgvector.django import VectorField


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
    file_size  = models.BigIntegerField(null=True, blank=True)
    resolution = models.CharField(max_length=20, blank=True)
    available_qualities = models.CharField(max_length=100, blank=True, default='')

    status           = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    processing_error = models.TextField(blank=True)

    # Visibility: public / private / subscribers_only
    visibility       = models.CharField(max_length=20, choices=VISIBILITY_CHOICES, default=VISIBILITY_PUBLIC)
    comments_enabled = models.BooleanField(default=True)
    views_count      = models.PositiveBigIntegerField(default=0)

    uploaded_by = models.CharField(max_length=100, blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
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
    created_at       = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['language']
        unique_together = [('video', 'language', 'is_auto_generated')]

    def __str__(self):
        tag = ' [auto]' if self.is_auto_generated else ''
        return f'{self.video.title} — {self.language_label}{tag}'

    @property
    def url(self):
        return self.file.url if self.file else None


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

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', 'visibility'], name='photo_status_vis'),
            models.Index(fields=['channel', 'status', 'visibility'], name='photo_ch_status_vis'),
            models.Index(fields=['created_at'], name='photo_created_at'),
        ]

    def __str__(self):
        return self.title

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
