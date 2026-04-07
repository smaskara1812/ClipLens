from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User
from .models import (
    Video, Category, Channel, ChannelLink, ChannelSubscription,
    Comment, CommentLike, VideoLike, Playlist, PlaylistItem,
    WatchHistory, SavedVideo, WatchTimeEntry, VideoChapter,
    EndScreen, Notification, Subtitle, AudioTrack, VideoSegment, VideoFrame,
    FaceIdentity, DetectedFace, UserProfile,
)


# ── UserProfile inline — shows role on the User admin page ──────────────────

class UserProfileInline(admin.StackedInline):
    model = UserProfile
    can_delete = False
    verbose_name = 'ClipLens Role'
    verbose_name_plural = 'ClipLens Roles'
    fields = ('role',)


class UserAdmin(BaseUserAdmin):
    """Extends the default User admin with the role inline."""
    inlines = (UserProfileInline,)
    list_display = ('username', 'email', 'get_role', 'is_active', 'is_staff', 'date_joined')
    list_filter  = ('is_active', 'is_staff', 'is_superuser', 'profile__role')

    def get_role(self, obj):
        try:
            return obj.profile.get_role_display()
        except Exception:
            return '—'
    get_role.short_description = 'Role'


# Re-register User with the extended admin
admin.site.unregister(User)
admin.site.register(User, UserAdmin)


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display  = ('user', 'role', 'created_at')
    list_filter   = ('role',)
    search_fields = ('user__username', 'user__email')
    raw_id_fields = ('user',)


@admin.register(Channel)
class ChannelAdmin(admin.ModelAdmin):
    list_display  = ['name', 'slug', 'owner', 'editor_count', 'created_at']
    prepopulated_fields = {'slug': ('name',)}
    search_fields = ['name', 'description']
    filter_horizontal = ('editors',)

    def editor_count(self, obj):
        return obj.editors.count()
    editor_count.short_description = 'Co-editors'


@admin.register(ChannelLink)
class ChannelLinkAdmin(admin.ModelAdmin):
    list_display = ['channel', 'label', 'url', 'order']


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ['name', 'slug', 'created_at']
    prepopulated_fields = {'slug': ('name',)}


@admin.register(Video)
class VideoAdmin(admin.ModelAdmin):
    list_display = ['title', 'channel', 'status', 'visibility', 'category', 'duration', 'resolution', 'views_count', 'created_at']
    list_filter = ['status', 'visibility', 'category', 'channel']
    search_fields = ['title', 'description', 'tags', 'uploaded_by']
    readonly_fields = ['id', 'hls_path', 'duration', 'resolution', 'file_size', 'views_count', 'created_at', 'updated_at']
    list_per_page = 30


@admin.register(VideoChapter)
class VideoChapterAdmin(admin.ModelAdmin):
    list_display = ['video', 'title', 'timestamp']
    ordering = ['video', 'timestamp']


@admin.register(Comment)
class CommentAdmin(admin.ModelAdmin):
    list_display = ['user', 'video', 'is_pinned', 'created_at']
    list_filter = ['is_pinned']
    search_fields = ['text', 'user__username']


@admin.register(Playlist)
class PlaylistAdmin(admin.ModelAdmin):
    list_display = ['title', 'owner', 'is_public', 'created_at']
    search_fields = ['title', 'owner__username']


@admin.register(WatchHistory)
class WatchHistoryAdmin(admin.ModelAdmin):
    list_display = ['user', 'video', 'progress_seconds', 'completed', 'watched_at']


@admin.register(ChannelSubscription)
class ChannelSubscriptionAdmin(admin.ModelAdmin):
    list_display = ['user', 'channel', 'created_at']


@admin.register(EndScreen)
class EndScreenAdmin(admin.ModelAdmin):
    list_display = ['video', 'label', 'target_video', 'target_url', 'start_seconds_before_end']
    search_fields = ['video__title', 'label']
    raw_id_fields = ['video', 'target_video']


@admin.register(VideoSegment)
class VideoSegmentAdmin(admin.ModelAdmin):
    list_display  = ['video', 'start_seconds', 'end_seconds', 'text']
    search_fields = ['text', 'video__title']
    list_filter   = ['video']


@admin.register(Subtitle)
class SubtitleAdmin(admin.ModelAdmin):
    list_display = ['video', 'language', 'language_label', 'format', 'is_auto_generated', 'created_at']
    list_filter  = ['is_auto_generated', 'language', 'format']
    search_fields= ['video__title', 'language_label']


@admin.register(AudioTrack)
class AudioTrackAdmin(admin.ModelAdmin):
    list_display = ['video', 'label', 'language', 'track_index', 'is_default']


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ['recipient', 'notification_type', 'is_read', 'created_at']
    list_filter = ['notification_type', 'is_read']


@admin.register(VideoFrame)
class VideoFrameAdmin(admin.ModelAdmin):
    list_display    = ['video', 'timestamp', 'labels', 'face_count', 'face_names']
    search_fields   = ['labels', 'video__title', 'face_names', 'description']
    list_filter     = ['video']
    readonly_fields = ['timestamp', 'labels', 'face_count', 'face_names', 'description']


@admin.register(FaceIdentity)
class FaceIdentityAdmin(admin.ModelAdmin):
    list_display   = ['name', 'is_auto_named', 'face_count_display', 'created_at']
    search_fields  = ['name']
    list_filter    = ['is_auto_named']
    readonly_fields= ['ref_embedding', 'thumbnail', 'created_at']

    def face_count_display(self, obj):
        return obj.faces.count()
    face_count_display.short_description = 'Detected Faces'


@admin.register(DetectedFace)
class DetectedFaceAdmin(admin.ModelAdmin):
    list_display   = ['video', 'timestamp', 'identity', 'confidence']
    search_fields  = ['video__title', 'identity__name']
    list_filter    = ['identity', 'video']
    readonly_fields= ['timestamp', 'bbox', 'embedding', 'confidence', 'crop_path']
    raw_id_fields  = ['identity', 'video', 'frame']
