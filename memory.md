# ClipStream — Project Memory

> Reference file for Claude and developers. Paste this at the start of any new session to avoid re-reading large files.
> Last updated: 2026-04-01

---

## Stack

| Layer | Tech |
|-------|------|
| Backend | Django 4.2, Django REST Framework |
| Auth | Django session auth + `CsrfExemptSessionAuthentication` (no CSRF on XHR) |
| DB | MySQL (default) or MSSQL — toggled via `.env` |
| Task Queue | Celery 5.6.3 + Redis broker (`redis://localhost:6379/0`) |
| Task Results | `django-celery-results` → stores in `celery_taskmeta` DB table |
| Video Encoding | FFmpeg → HLS (multi-quality adaptive bitrate) |
| Speech-to-Text | `faster-whisper` (local, CPU or CUDA) |
| Static Files | WhiteNoise |
| Frontend | Plain HTML/CSS/JS templates (no React/Vue), Hls.js for HLS playback |

---

## Project Structure

```
ClipStream/
├── ClipStream/           # Django project config
│   ├── settings.py
│   ├── urls.py
│   ├── celery.py         # Celery app definition
│   └── __init__.py       # imports celery_app so it loads with Django
├── videos/               # Main app
│   ├── models.py
│   ├── views.py
│   ├── urls.py
│   ├── serializers.py
│   ├── tasks.py          # Celery tasks
│   ├── services.py       # Video processing (FFmpeg HLS pipeline)
│   ├── admin.py
│   ├── authentication.py # CsrfExemptSessionAuthentication
│   ├── middleware.py      # HLSHeadersMiddleware (CORS headers for .m3u8/.ts)
│   ├── context_processors.py  # injects SITE_URL into all templates
│   ├── templatetags/
│   │   └── video_filters.py   # duration_fmt, format_duration
│   ├── migrations/
│   │   ├── 0001_initial.py
│   │   ├── 0002_channel_...
│   │   ├── 0003_add_available_qualities.py
│   │   ├── 0004_user_channel_likes_comments_chapters.py
│   │   ├── 0005_add_channel_subscription.py
│   │   ├── 0006_phase4_features.py
│   │   ├── 0007_phase5_celery_captions_audio.py  # Subtitle, AudioTrack
│   │   └── 0008_add_video_segments.py            # VideoSegment
│   └── templates/
│       ├── registration/
│       │   ├── login.html
│       │   └── register.html
│       └── videos/
│           ├── base.html
│           ├── player.html          # home/browse page
│           ├── watch.html           # video player page
│           ├── embed.html           # embeddable player
│           ├── upload.html
│           ├── channel.html
│           ├── category.html
│           ├── analytics.html
│           ├── channels_manage.html
│           ├── trending.html
│           ├── saved.html
│           ├── history.html
│           ├── playlists.html
│           ├── playlist_detail.html
│           ├── notifications.html
│           ├── setup_channel.html
│           ├── user_management.html
│           └── watch_restricted.html
├── media/                # uploaded files (gitignored)
│   ├── originals/        # raw uploaded videos
│   ├── hls/              # HLS output: hls/<video_id>/master.m3u8
│   │   └── <video_id>/audio/track<N>/playlist.m3u8
│   ├── thumbnails/
│   ├── channels/avatars/ + banners/
│   └── subtitles/        # .vtt files
├── staticfiles/
├── .env                  # not committed
├── .env.example
├── requirements.txt
├── manage.py
└── start.sh
```

---

## Models

### Channel
```
id (UUID PK), owner (OneToOne→User), name, slug, description,
avatar (ImageField), banner (ImageField), created_at
Properties: avatar_url, banner_url, subscriber_count
related_names: links (ChannelLink), subscribers (ChannelSubscription), videos (Video)
```

### ChannelLink
```
id, channel (FK→Channel), label, url (URLField), order (int)
```

### Category
```
id, name, slug, created_at
```

### Video
```
id (UUID PK), title, description, channel (FK→Channel), category (FK→Category),
tags (comma-separated CharField), original_file, original_filename, hls_path,
thumbnail (ImageField), duration (float), file_size, resolution,
available_qualities (comma-separated, e.g. "1080,720,480"),
status: pending|processing|ready|failed, processing_error,
visibility: public|private|subscribers_only,
comments_enabled (bool), views_count, uploaded_by, created_at, updated_at
Properties: is_public, hls_url, qualities_list, thumbnail_url, watch_url, likes_count
Method: is_visible_to(user)
```

### VideoLike
```
user (FK→User), video (FK→Video), created_at  [unique: user+video]
```

### Comment
```
id, video (FK→Video), user (FK→User), parent (self FK nullable),
text, is_pinned, timestamp_seconds (float nullable), created_at, updated_at
Properties: likes_count, reply_count
Method: get_mentions() → list of @usernames
```

### CommentLike
```
user (FK→User), comment (FK→Comment), created_at  [unique: user+comment]
```

### ChannelSubscription
```
user (FK→User), channel (FK→Channel), created_at  [unique: user+channel]
```

### Playlist
```
id (UUID PK), owner (FK→User), title, description, is_public, created_at, updated_at
Properties: video_count, thumbnail_url
related_name: items (PlaylistItem)
```

### PlaylistItem
```
playlist (FK→Playlist), video (FK→Video), order (int), added_at
[unique: playlist+video, ordered by order+added_at]
```

### WatchHistory
```
user (FK→User), video (FK→Video), progress_seconds (float), completed (bool), watched_at
[unique: user+video]
```

### SavedVideo
```
user (FK→User), video (FK→Video), saved_at  [unique: user+video]
```

### WatchTimeEntry
```
video (FK→Video), date (DateField), total_seconds (BigInt)
[unique: video+date] — daily aggregate for analytics
```

### VideoChapter
```
video (FK→Video), title, timestamp (float seconds)
[ordered by timestamp]
```

### EndScreen
```
video (FK→Video), target_video (FK→Video nullable), target_url (URLField),
label, start_seconds_before_end (float, default=20)
```

### VideoFrame  ← visual / object-detection index
```
video (FK→Video), timestamp (float seconds), labels (comma-sep, e.g. "car, person, dog"),
face_count (int, proxy person count), face_names (blank until Phase B), description (blank until Phase C)
[ordered by timestamp, indexed on video+timestamp]
Created by analyze_video_frames_task (YOLOv8n); bulk_create(batch_size=200)
Property: labels_list → list of strings
```

### VideoSegment  ← speech index for in-video search
```
video (FK→Video), start_seconds (float), end_seconds (float), text (TextField)
[ordered by start_seconds, indexed on video+start_seconds]
Created by Whisper transcription task; bulk_create(batch_size=500)
```

### Subtitle
```
video (FK→Video), language (BCP-47, e.g. "en"), language_label (e.g. "English"),
format: vtt|srt, file (FileField, upload_to='subtitles/'), is_auto_generated,
created_at
[unique: video+language+is_auto_generated]
Property: url → file.url
```

### AudioTrack
```
video (FK→Video), label, language, track_index (int, stream index in source),
hls_path (relative path to audio-only HLS playlist), is_default, created_at
```

### Notification
```
recipient (FK→User), actor (FK→User nullable), video (FK→Video nullable),
comment (FK→Comment nullable), notification_type, is_read, created_at
Types: comment, reply, mention, like, subscription, system
```

---

## URL Routes

### Frontend Pages
```
GET /                         → player_page        (name='player')
GET /upload/                  → upload_page         (name='upload')
GET /analytics/               → analytics_page      (name='analytics')
GET /channels/                → channels_manage_page (name='channels_manage')
GET /trending/                → trending_page       (name='trending')
GET /saved/                   → saved_page          (name='saved')
GET /history/                 → history_page        (name='history')
GET /playlists/               → playlists_page      (name='playlists')
GET /notifications/           → notifications_page  (name='notifications')
GET /admin-panel/             → user_management_page (name='user_management')
GET /register/                → register_page       (name='register')
GET /setup-channel/           → setup_channel_page  (name='setup_channel')
GET /watch/<uuid:video_id>/   → watch_page          (name='watch')
GET /embed/<uuid:video_id>/   → embed_page          (name='embed')
GET /channel/<slug>/          → channel_page        (name='channel')
GET /category/<slug>/         → category_page       (name='category')
GET /playlist/<uuid>/         → playlist_detail_page (name='playlist_detail')
```

### API Endpoints
```
GET  /api/health/

# Channels
GET  /api/channels/
GET  /api/channels/<uuid>/
GET/PATCH /api/channels/<slug>/
POST /api/channels/<slug>/subscribe/       → toggle subscribe
GET/POST  /api/channels/<slug>/links/
PATCH/DELETE /api/channel-links/<int>/

# Categories
GET /api/categories/

# Videos
GET  /api/videos/                          → list (supports ?q=, ?sort=, ?duration=, ?date=, ?category=)
POST /api/videos/upload/
GET/PATCH/DELETE /api/videos/<uuid>/
POST /api/videos/<uuid>/view/
GET  /api/videos/<uuid>/status/
POST /api/videos/<uuid>/reprocess/
GET  /api/videos/<uuid>/stream/            → HLS stream/serve
POST /api/videos/<uuid>/thumbnail/
POST /api/videos/<uuid>/like/
POST /api/videos/<uuid>/save/
POST /api/videos/<uuid>/progress/
GET/POST  /api/videos/<uuid>/comments/
GET/POST  /api/videos/<uuid>/chapters/
GET/POST  /api/videos/<uuid>/end-screens/
DELETE /api/videos/<uuid>/end-screens/<int>/

# Subtitles
GET  /api/videos/<uuid>/subtitles/
POST /api/videos/<uuid>/subtitles/upload/  → multipart: file + language
POST /api/videos/<uuid>/subtitles/regenerate/
DELETE /api/videos/<uuid>/subtitles/<int>/

# Audio Tracks
GET  /api/videos/<uuid>/audio-tracks/
POST /api/videos/<uuid>/audio-tracks/extract/

# Comments
DELETE /api/comments/<int>/
POST   /api/comments/<int>/like/
POST   /api/comments/<int>/pin/

# Chapters
GET/PATCH/DELETE /api/chapters/<int>/

# Playlists
GET/POST /api/playlists/
GET/PATCH/DELETE /api/playlists/<uuid>/
POST/DELETE /api/playlists/<uuid>/videos/<uuid>/

# Notifications
GET  /api/notifications/
POST /api/notifications/read/

# Admin
GET  /api/admin/users/
POST /api/admin/users/<int>/
```

---

## Celery Tasks (`videos/tasks.py`)

### `process_video_task(video_id)`
- Queue: `processing`
- Calls `services.process_video(video_id)` → FFmpeg HLS encoding
- On success, chains `generate_captions_task` (if `AUTO_CAPTION_ON_UPLOAD=True`)
- Max retries: 2, delay: 30s

### `generate_captions_task(video_id, language='en')`
- Queue: `captions`
- Pipeline:
  1. `_has_audio_stream()` — ffprobe check; skip silently if no audio
  2. `_extract_audio_wav()` — FFmpeg → 16kHz mono WAV (avoids PyAV IndexError)
  3. `WhisperModel.transcribe(wav_file)` with VAD filter
  4. `_segments_to_vtt_and_index()` — builds WebVTT + bulk-creates VideoSegment rows
  5. Saves `Subtitle` record (file='subtitles/<id>_<lang>_auto.vtt', is_auto_generated=True)
  6. Cleans up temp WAV in `finally` block
- Skips if subtitle with same language+auto already exists
- Max retries: 1

### `analyze_video_frames_task(video_id)`
- Queue: `processing`
- Triggered automatically by `process_video_task` (if `FRAME_ANALYSIS_ENABLED=true`)
- Pipeline:
  1. FFmpeg extracts 1 JPEG frame every `FRAME_INTERVAL_SECONDS` seconds
  2. `ultralytics.YOLO(yolov8n.pt)` runs inference on each frame (conf≥0.40)
  3. Collected COCO class labels bulk-created as `VideoFrame` rows
  4. Temp frame dir cleaned up in `finally`
- Skips if `FRAME_ANALYSIS_ENABLED=false`
- Max retries: 1

### `reindex_segments_task(subtitle_id)`
- Queue: `default`
- Parses a saved Subtitle's `.vtt` file with `_parse_vtt_segments()`
- Deletes old VideoSegment rows for that video, bulk-creates new ones
- Called automatically by `subtitle_upload` (manual uploads) and as a fallback after any subtitle save
- Max retries: 1

### `extract_audio_tracks_task(video_id)`
- Queue: `processing`
- Probes audio streams with ffprobe; skips if only 1 stream
- Extracts each stream as an HLS audio-only playlist: `hls/<video_id>/audio/track<N>/playlist.m3u8`
- Creates `AudioTrack` records

### Fallback Pattern (`views._dispatch_process_video`)
```python
try:
    process_video_task.apply_async(args=[video_id], queue='processing')
except Exception:
    threading.Thread(target=process_video, args=[video_id], daemon=True).start()
```

---

## Settings / Environment Variables

| Variable | Default | Notes |
|----------|---------|-------|
| `SECRET_KEY` | dev key | Change in production |
| `DEBUG` | `True` | |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | |
| `SITE_URL` | `http://localhost:8000` | Used in templates for absolute URLs |
| `USE_MYSQL` | `true` | |
| `USE_MSSQL` | `false` | Mutually exclusive with USE_MYSQL |
| `MYSQL_DB_NAME` | `video` | |
| `MYSQL_DB_USER` | `root` | |
| `MYSQL_DB_PASSWORD` | `` | |
| `MYSQL_DB_HOST` | `localhost` | |
| `MYSQL_DB_PORT` | `3306` | |
| `FFMPEG_PATH` | `ffmpeg` | |
| `FFPROBE_PATH` | `ffprobe` | |
| `HLS_MULTI_QUALITY` | `true` | Multi-rendition ABR vs single |
| `HLS_SEGMENT_DURATION` | `6` | Seconds per HLS segment |
| `HLS_QUALITIES` | `1080,720,480,360` | Heights to encode |
| `CELERY_BROKER_URL` | `redis://localhost:6379/0` | |
| `WHISPER_MODEL_SIZE` | `base` | tiny/base/small/medium/large-v2/large-v3 |
| `WHISPER_DEVICE` | `cpu` | `cuda` if GPU available |
| `WHISPER_COMPUTE_TYPE` | `int8` | |
| `AUTO_CAPTION_ON_UPLOAD` | `true` | |
| `EMBED_ALLOW_ORIGINS` | `*` | CSP for embed iframe |
| `FRAME_ANALYSIS_ENABLED` | `true` | Set false to skip YOLO during dev |
| `FRAME_INTERVAL_SECONDS` | `5` | Extract 1 frame every N seconds |
| `YOLO_MODEL` | `yolov8n` | nano/small/medium/large |

---

## Serializers (`videos/serializers.py`)

- `ChannelSerializer` — includes links list, subscriber_count
- `VideoListSerializer` — list view, includes thumbnail_url, channel name, duration_display
- `VideoDetailSerializer` — full detail, includes qualities_list, hls_url, watch_progress
- `VideoUploadSerializer` — validates file upload
- `CommentSerializer` — includes likes_count, reply_count, nested replies (3 levels)
- `CommentReplySerializer`
- `PlaylistSerializer` — includes items list
- `WatchHistorySerializer`
- `SavedVideoSerializer`
- `SubtitleSerializer` — fields: id, language, language_label, format, is_auto_generated (RO), url (RO), created_at (RO)
- `VideoFrameSerializer` — fields: id, timestamp, labels, labels_list (RO), face_count, face_names, description
- `AudioTrackSerializer` — fields: id, label, language, track_index, hls_path, is_default
- `EndScreenSerializer` — fields: id, label, target_video, target_video_title (RO), target_video_thumbnail (RO), target_url, start_seconds_before_end
- `NotificationSerializer`

---

## Template Filters (`videos/templatetags/video_filters.py`)

```
{% load video_filters %}
{{ video.duration|duration_fmt }}    → "3:45" or "1:02:30"
{{ video.duration|format_duration }} → same
```

---

## Key Architectural Decisions

### HLS Pipeline
1. Upload → `original_file` saved to `media/originals/`
2. `_dispatch_process_video(video_id)` called
3. FFmpeg encodes to HLS in `media/hls/<video_id>/`
   - Multi-quality: `master.m3u8` + `360p/`, `480p/`, `720p/`, `1080p/` renditions
   - Single quality: `playlist.m3u8` + `segment_XXXX.ts` files
4. `Video.hls_path` set to relative path; `Video.status` → `ready`
5. `VideoChapter` timestamps align with HLS segments

### Caption / Speech-Index Flow
1. `generate_captions_task` triggered after video processing
2. FFmpeg extracts 16kHz mono WAV (avoids PyAV IndexError on video files without audio)
3. `faster-whisper` transcribes WAV → segments (text, start_seconds, end_seconds)
4. `_segments_to_vtt_and_index()` consumes iterator ONCE:
   - Builds WebVTT string
   - Bulk-creates `VideoSegment` rows (deletes old ones first)
5. `Subtitle` record saved with `.vtt` file in `media/subtitles/`
6. Player: `<track>` elements added to `<video>` tag; CC picker button in controls

### In-Video Speech Search
- `player_page` view: when `?q=` present, queries `VideoSegment.objects.filter(text__icontains=q)`
- Groups by video (max 5 moments per video)
- Adds `time_fmt` (M:SS) to each moment
- Template shows "Spoken in N videos" section above results; clicking a moment → `/watch/<id>?t=<seconds>`
- `watch_page` reads `?t=` param → `seek_to` context → JS `video.currentTime = seek_to`

### Authentication
- `CsrfExemptSessionAuthentication`: session cookie → `request.user`, but no CSRF header required for XHR
- All page views require login via `@login_required` or redirect to `/login/`
- All API views check `request.user.is_authenticated` manually (no global permission class)

### End Screens
- `EndScreen` model: `start_seconds_before_end` triggers overlay display
- Overlay `<div id="endScreenOverlay">` must be **inside** `.video-wrap` (which has `position:relative`)
- Overlay uses `position:absolute; inset:0` to fill video
- JS shows overlay when `video.currentTime >= video.duration - start_seconds_before_end`

---

## Dev Commands

```bash
# Start Django dev server
cd /Users/sohammaskara/Desktop/ClipStream
source venv/bin/activate
python manage.py runserver

# Start Redis (required for Celery)
redis-server
# or via brew: brew services start redis

# Start Celery workers (3 queues)
celery -A ClipStream worker -Q processing,captions,default -l info

# Or run each queue separately:
celery -A ClipStream worker -Q processing -l info -c 2
celery -A ClipStream worker -Q captions -l info -c 1
celery -A ClipStream worker -Q default -l info

# Run migrations
python manage.py migrate

# Create superuser
python manage.py createsuperuser

# Collect static files
python manage.py collectstatic --noinput
```

### Convenience start script
```bash
bash start.sh
```

---

## Admin Panel

Access at `/django-admin/` with superuser credentials.

Registered models:
- Channel, ChannelLink, Category
- Video (list_display: title, channel, status, visibility, category, duration, resolution, views_count)
- VideoChapter
- Comment
- Playlist
- WatchHistory
- ChannelSubscription
- EndScreen (raw_id_fields: video, target_video)
- VideoSegment (search: text, video__title)
- Subtitle (filter: is_auto_generated, language, format)
- AudioTrack
- Notification (filter: notification_type, is_read)

---

## Common Gotchas / Known Issues

1. **`IndexError: tuple index out of range`** — happens if Whisper/PyAV tries to process a video with no audio stream. Fixed by using `_has_audio_stream()` check + `_extract_audio_wav()` pre-extraction.

2. **End screen not visible** — `#endScreenOverlay` must be INSIDE `.video-wrap` (which has `position:relative`). If placed outside, `position:absolute` has no anchor.

3. **TemplateSyntaxError: 'endif' tag** — template block tags must be balanced. Stray `{% endif %}` without a matching `{% if %}` crashes the template engine.

4. **Celery not picking up code changes** — must restart the worker after editing `tasks.py` or `services.py`.

5. **Redis not running** — `_dispatch_process_video()` fallback catches the connection error and uses a thread instead, so video processing still works, but no async task queue.

6. **`AUTO_CAPTION_ON_UPLOAD=false`** in `.env` to disable automatic Whisper transcription (saves CPU during development/testing).

7. **Subtitle `unique_together`** — `(video, language, is_auto_generated)`. A video can have both a manual and an auto-generated subtitle for the same language.

8. **Audio track extraction** — only runs if source video has ≥2 audio streams. Single-audio videos use the default HLS stream; no `AudioTrack` records are created.

9. **Session cookie** — expires after 8 hours. `SESSION_COOKIE_SECURE=True` in production (HTTPS required).

10. **CORS** — fully open (`CORS_ALLOW_ALL_ORIGINS=True`) for development. Restrict in production.

---

## Phase History

| Phase | Features |
|-------|----------|
| 1 | Basic video upload, HLS encoding, player |
| 2 | Channels, categories, comments, likes |
| 3 | Playlists, watch history, saved videos, subscriptions |
| 4 | Analytics, notifications, end screens, channel links, trending, embed |
| 5 | Celery/Redis async tasks, auto-captions (Whisper), multi-audio tracks, in-video speech search, search filters UI |
 