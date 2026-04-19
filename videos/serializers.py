from rest_framework import serializers
from django.contrib.auth.models import User
from .models import (
    Video, Category, Channel, ChannelLink, Comment, CommentLike,
    VideoChapter, VideoLike, Playlist, PlaylistItem,
    WatchHistory, SavedVideo, Notification, EndScreen, Subtitle, AudioTrack, VideoFrame,
    FaceIdentity, DetectedFace,
)


# ── Channel ───────────────────────────────────────────────────────────────────

class ChannelLinkSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChannelLink
        fields = ['id', 'label', 'url', 'order']


class ChannelSerializer(serializers.ModelSerializer):
    avatar_url = serializers.ReadOnlyField()
    banner_url = serializers.ReadOnlyField()
    subscriber_count = serializers.ReadOnlyField()
    video_count = serializers.SerializerMethodField()
    links = ChannelLinkSerializer(many=True, read_only=True)

    class Meta:
        model = Channel
        fields = [
            'id', 'name', 'slug', 'description',
            'avatar_url', 'banner_url',
            'subscriber_count', 'video_count',
            'links', 'created_at',
        ]

    def get_video_count(self, obj):
        return obj.videos.count()


# ── Category ──────────────────────────────────────────────────────────────────

class CategorySerializer(serializers.ModelSerializer):
    slug = serializers.SlugField(read_only=True)

    class Meta:
        model = Category
        fields = ['id', 'name', 'slug', 'created_at']

    def _unique_slug(self, name, exclude_pk=None):
        from django.utils.text import slugify
        base = slugify(name)
        slug = base
        counter = 1
        qs = Category.objects.filter(slug=slug)
        if exclude_pk:
            qs = qs.exclude(pk=exclude_pk)
        while qs.exists():
            slug = f'{base}-{counter}'
            counter += 1
            qs = Category.objects.filter(slug=slug)
            if exclude_pk:
                qs = qs.exclude(pk=exclude_pk)
        return slug

    def create(self, validated_data):
        slug = self._unique_slug(validated_data['name'])
        return Category.objects.create(name=validated_data['name'], slug=slug)

    def update(self, instance, validated_data):
        if 'name' in validated_data:
            instance.name = validated_data['name']
            instance.slug = self._unique_slug(validated_data['name'], exclude_pk=instance.pk)
        instance.save()
        return instance


# ── Video ─────────────────────────────────────────────────────────────────────

class VideoListSerializer(serializers.ModelSerializer):
    hls_url = serializers.ReadOnlyField()
    thumbnail_url = serializers.ReadOnlyField()
    watch_url = serializers.ReadOnlyField()
    qualities_list = serializers.ReadOnlyField()
    likes_count = serializers.ReadOnlyField()
    category_name = serializers.CharField(source='category.name', read_only=True)
    channel_name = serializers.CharField(source='channel.name', read_only=True)
    channel_slug = serializers.CharField(source='channel.slug', read_only=True)
    channel_avatar_url = serializers.SerializerMethodField()
    comments_count = serializers.SerializerMethodField()

    class Meta:
        model = Video
        fields = [
            'id', 'title', 'description', 'category', 'category_name',
            'channel', 'channel_name', 'channel_slug', 'channel_avatar_url',
            'tags', 'thumbnail_url', 'duration', 'resolution',
            'status', 'visibility', 'is_public', 'views_count',
            'likes_count', 'comments_count',
            'hls_url', 'watch_url', 'available_qualities', 'qualities_list',
            'uploaded_by', 'created_at',
        ]

    def get_comments_count(self, obj):
        return obj.comments.count()

    def get_channel_avatar_url(self, obj):
        if obj.channel:
            return obj.channel.avatar_url
        return None


class VideoFeedSerializer(serializers.ModelSerializer):
    """Lightweight serializer for paginated feed / infinite scroll."""
    thumbnail_url       = serializers.ReadOnlyField()
    watch_url           = serializers.ReadOnlyField()
    channel_name        = serializers.CharField(source='channel.name',       read_only=True, default='')
    channel_slug        = serializers.CharField(source='channel.slug',       read_only=True, default='')
    channel_avatar_url  = serializers.SerializerMethodField()

    class Meta:
        model  = Video
        fields = [
            'id', 'title', 'thumbnail_url', 'watch_url',
            'duration', 'views_count', 'status', 'created_at',
            'channel_name', 'channel_slug', 'channel_avatar_url',
        ]

    def get_channel_avatar_url(self, obj):
        if obj.channel:
            return obj.channel.avatar_url
        return None


class VideoDetailSerializer(serializers.ModelSerializer):
    hls_url = serializers.ReadOnlyField()
    thumbnail_url = serializers.ReadOnlyField()
    watch_url = serializers.ReadOnlyField()
    qualities_list = serializers.ReadOnlyField()
    likes_count = serializers.ReadOnlyField()
    category_name = serializers.CharField(source='category.name', read_only=True)
    channel_name = serializers.CharField(source='channel.name', read_only=True)
    channel_slug = serializers.CharField(source='channel.slug', read_only=True)
    comments_count = serializers.SerializerMethodField()
    user_liked = serializers.SerializerMethodField()
    user_saved = serializers.SerializerMethodField()
    watch_progress = serializers.SerializerMethodField()

    class Meta:
        model = Video
        fields = [
            'id', 'title', 'description', 'category', 'category_name',
            'channel', 'channel_name', 'channel_slug',
            'tags', 'hls_url', 'hls_path', 'thumbnail_url', 'watch_url',
            'duration', 'file_size', 'resolution',
            'available_qualities', 'qualities_list',
            'status', 'processing_error', 'visibility', 'is_public',
            'comments_enabled', 'views_count', 'likes_count',
            'comments_count', 'user_liked', 'user_saved', 'watch_progress',
            'uploaded_by', 'created_at', 'updated_at',
        ]

    def get_comments_count(self, obj):
        return obj.comments.count()

    def get_user_liked(self, obj):
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            return obj.likes.filter(user=request.user).exists()
        return False

    def get_user_saved(self, obj):
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            return obj.saves.filter(user=request.user).exists()
        return False

    def get_watch_progress(self, obj):
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            entry = obj.watch_histories.filter(user=request.user).first()
            if entry:
                return {'seconds': entry.progress_seconds, 'completed': entry.completed}
        return None


class VideoUploadSerializer(serializers.ModelSerializer):
    video_file = serializers.FileField(write_only=True)
    thumbnail_file = serializers.ImageField(write_only=True, required=False)

    class Meta:
        model = Video
        fields = [
            'title', 'description', 'category',
            'tags', 'visibility', 'video_file', 'thumbnail_file',
        ]

    def validate_video_file(self, value):
        allowed = ['.mp4', '.avi', '.mov', '.mkv', '.wmv', '.flv', '.webm', '.m4v']
        ext = '.' + value.name.rsplit('.', 1)[-1].lower()
        if ext not in allowed:
            raise serializers.ValidationError(f'Unsupported format. Allowed: {", ".join(allowed)}')
        return value


# ── Comments ──────────────────────────────────────────────────────────────────

class CommentSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source='user.username', read_only=True)
    likes_count = serializers.ReadOnlyField()
    reply_count = serializers.ReadOnlyField()
    user_liked = serializers.SerializerMethodField()
    replies = serializers.SerializerMethodField()

    class Meta:
        model = Comment
        fields = [
            'id', 'username', 'text',
            'parent', 'is_pinned', 'timestamp_seconds',
            'likes_count', 'reply_count', 'user_liked',
            'replies', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'username', 'is_pinned', 'created_at', 'updated_at']

    def get_user_liked(self, obj):
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            return obj.comment_likes.filter(user=request.user).exists()
        return False

    def get_replies(self, obj):
        # Only serialize replies for top-level comments (parent=None)
        if obj.parent_id is not None:
            return []
        qs = obj.replies.select_related('user').all()
        return CommentReplySerializer(qs, many=True, context=self.context).data


class CommentReplySerializer(serializers.ModelSerializer):
    """Lightweight serializer for nested replies (no further nesting)."""
    username = serializers.CharField(source='user.username', read_only=True)
    likes_count = serializers.ReadOnlyField()
    user_liked = serializers.SerializerMethodField()

    class Meta:
        model = Comment
        fields = [
            'id', 'username', 'text', 'parent',
            'timestamp_seconds', 'likes_count', 'user_liked',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'username', 'created_at', 'updated_at']

    def get_user_liked(self, obj):
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            return obj.comment_likes.filter(user=request.user).exists()
        return False


# ── Chapters ──────────────────────────────────────────────────────────────────

class VideoChapterSerializer(serializers.ModelSerializer):
    class Meta:
        model = VideoChapter
        fields = ['id', 'title', 'timestamp']


# ── Playlists ─────────────────────────────────────────────────────────────────

class PlaylistItemSerializer(serializers.ModelSerializer):
    video_title = serializers.CharField(source='video.title', read_only=True)
    video_thumbnail = serializers.CharField(source='video.thumbnail_url', read_only=True)
    video_duration = serializers.FloatField(source='video.duration', read_only=True)
    video_watch_url = serializers.CharField(source='video.watch_url', read_only=True)

    class Meta:
        model = PlaylistItem
        fields = ['id', 'video', 'video_title', 'video_thumbnail', 'video_duration', 'video_watch_url', 'order', 'added_at']
        read_only_fields = ['id', 'added_at']


class PlaylistSerializer(serializers.ModelSerializer):
    video_count = serializers.ReadOnlyField()
    thumbnail_url = serializers.ReadOnlyField()
    owner_username = serializers.CharField(source='owner.username', read_only=True)
    items = PlaylistItemSerializer(many=True, read_only=True)

    class Meta:
        model = Playlist
        fields = [
            'id', 'title', 'description', 'is_public',
            'owner_username', 'video_count', 'thumbnail_url',
            'items', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'owner_username', 'created_at', 'updated_at']


class PlaylistListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for listing playlists (no items)."""
    video_count = serializers.ReadOnlyField()
    thumbnail_url = serializers.ReadOnlyField()
    owner_username = serializers.CharField(source='owner.username', read_only=True)

    class Meta:
        model = Playlist
        fields = [
            'id', 'title', 'description', 'is_public',
            'owner_username', 'video_count', 'thumbnail_url',
            'created_at', 'updated_at',
        ]


# ── Watch History & Saved ─────────────────────────────────────────────────────

class WatchHistorySerializer(serializers.ModelSerializer):
    video_title = serializers.CharField(source='video.title', read_only=True)
    video_thumbnail = serializers.CharField(source='video.thumbnail_url', read_only=True)
    video_duration = serializers.FloatField(source='video.duration', read_only=True)
    video_watch_url = serializers.CharField(source='video.watch_url', read_only=True)
    channel_name = serializers.CharField(source='video.channel.name', read_only=True)

    class Meta:
        model = WatchHistory
        fields = [
            'id', 'video', 'video_title', 'video_thumbnail',
            'video_duration', 'video_watch_url', 'channel_name',
            'progress_seconds', 'completed', 'watched_at',
        ]


class SavedVideoSerializer(serializers.ModelSerializer):
    video_title = serializers.CharField(source='video.title', read_only=True)
    video_thumbnail = serializers.CharField(source='video.thumbnail_url', read_only=True)
    video_duration = serializers.FloatField(source='video.duration', read_only=True)
    video_watch_url = serializers.CharField(source='video.watch_url', read_only=True)
    channel_name = serializers.CharField(source='video.channel.name', read_only=True)

    class Meta:
        model = SavedVideo
        fields = [
            'id', 'video', 'video_title', 'video_thumbnail',
            'video_duration', 'video_watch_url', 'channel_name', 'saved_at',
        ]


# ── Subtitles ────────────────────────────────────────────────────────────────

class SubtitleSerializer(serializers.ModelSerializer):
    url = serializers.ReadOnlyField()

    class Meta:
        model  = Subtitle
        fields = ['id', 'language', 'language_label', 'format', 'is_auto_generated', 'url', 'created_at']
        read_only_fields = ['id', 'is_auto_generated', 'created_at']


# ── Audio Tracks ──────────────────────────────────────────────────────────────

class AudioTrackSerializer(serializers.ModelSerializer):
    class Meta:
        model  = AudioTrack
        fields = ['id', 'label', 'language', 'track_index', 'hls_path', 'is_default']


# ── End Screens ───────────────────────────────────────────────────────────────

class EndScreenSerializer(serializers.ModelSerializer):
    target_video_title     = serializers.CharField(source='target_video.title',         read_only=True)
    target_video_thumbnail = serializers.CharField(source='target_video.thumbnail_url', read_only=True)

    class Meta:
        model  = EndScreen
        fields = [
            'id', 'label', 'target_video', 'target_video_title',
            'target_video_thumbnail', 'target_url', 'start_seconds_before_end',
        ]


# ── VideoFrame ────────────────────────────────────────────────────────────────

class VideoFrameSerializer(serializers.ModelSerializer):
    labels_list = serializers.ReadOnlyField()

    class Meta:
        model  = VideoFrame
        fields = ['id', 'timestamp', 'labels', 'labels_list', 'face_count', 'face_names', 'description']


# ── Face Recognition ─────────────────────────────────────────────────────────

class DetectedFaceSerializer(serializers.ModelSerializer):
    crop_url     = serializers.ReadOnlyField()
    identity_name= serializers.CharField(source='identity.name', read_only=True, allow_null=True)

    class Meta:
        model  = DetectedFace
        fields = ['id', 'timestamp', 'confidence', 'crop_url', 'identity', 'identity_name']


class FaceIdentitySerializer(serializers.ModelSerializer):
    thumbnail_url = serializers.ReadOnlyField()
    face_count    = serializers.SerializerMethodField()
    appearances   = serializers.SerializerMethodField()

    class Meta:
        model  = FaceIdentity
        fields = ['id', 'name', 'is_auto_named', 'thumbnail_url', 'face_count', 'appearances', 'created_at']

    def get_face_count(self, obj):
        # scoped to the video in context if provided
        qs = obj.faces.all()
        vid = self.context.get('video_id')
        if vid:
            qs = qs.filter(video_id=vid)
        return qs.exclude(status=DetectedFace.STATUS_REJECTED).count()

    def get_appearances(self, obj):
        """Sorted list of timestamps where this identity appears (scoped to video)."""
        qs = obj.faces.all()
        vid = self.context.get('video_id')
        if vid:
            qs = qs.filter(video_id=vid)
        return sorted(set(qs.exclude(status=DetectedFace.STATUS_REJECTED).values_list('timestamp', flat=True)))


# ── Notifications ─────────────────────────────────────────────────────────────

class NotificationSerializer(serializers.ModelSerializer):
    sender_username = serializers.CharField(source='sender.username', read_only=True)

    class Meta:
        model = Notification
        fields = [
            'id', 'sender_username', 'notification_type',
            'message', 'link', 'is_read', 'created_at',
        ]
