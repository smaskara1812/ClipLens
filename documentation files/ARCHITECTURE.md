# ClipStream — Architecture & Process Documentation

A self-hosted video platform with adaptive streaming, AI-powered search, face recognition, and speech transcription. Runs entirely locally — no cloud APIs, no data sent externally.

---

## Table of Contents

1. [Tech Stack](#tech-stack)
2. [AI Models — What, Why, How](#ai-models)
3. [Model Cache Locations](#model-cache-locations)
4. [Video Upload Pipeline — Full Flow](#video-upload-pipeline)
5. [Search Pipeline — How Queries Work](#search-pipeline)
6. [Database Models](#database-models)
7. [Configuration Reference](#configuration-reference)
8. [Directory Structure](#directory-structure)

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Django 4.2 + Django REST Framework |
| Async Tasks | Celery 5 + Redis |
| Database | MySQL (configurable) |
| Video Encoding | FFmpeg |
| Streaming | HLS (HTTP Live Streaming) |
| Frontend | Django server-rendered templates (no JS framework) |
| Python | 3.11 |

---

## AI Models

Five models run entirely locally. No API keys. No data leaves your machine.

---

### 1. YOLOv8 — Object Detection

**What it does:** Scans each video frame and identifies physical objects present.

**Output:** Labels like `person, laptop, chair, car, dog`

**Why this model:** YOLOv8 (You Only Look Once v8) by Ultralytics is the industry standard for real-time object detection. The `nano` variant (`yolov8n`) is lightweight enough to run on CPU quickly while still being accurate.

**How it's used:**
- Runs on every extracted frame
- Detected class names stored in `VideoFrame.labels` as a comma-separated string
- Powers label-based search: searching `"car"` finds every frame where a car was detected

**Configurable:**
```
YOLO_MODEL=yolov8n   # nano (fastest)
YOLO_MODEL=yolov8s   # small (more accurate, slower)
YOLO_MODEL=yolov8m   # medium
```

**Weights location:** `~/Desktop/ClipStream/yolov8n.pt` (downloaded on first run, ~6MB)

---

### 2. BLIP / Florence-2 — Scene Description

**What it does:** Looks at each frame holistically and writes a natural language description of the scene.

**Output:** `"Two people sitting at a conference table with laptops open"`

**Why these models:**
- **BLIP** (Bootstrapped Language-Image Pretraining, Salesforce) — fast, reliable, concise captions. Native transformers model, no compatibility issues. Good for quick descriptions.
- **Florence-2** (Microsoft) — richer, more detailed descriptions. Understands spatial relationships, actions, and context better. Slightly slower.

**How it's used:**
- Runs on every extracted frame after YOLO
- Description stored in `VideoFrame.description`
- Powers natural language search: searching `"people on stairs"` matches frames where the description contains those words (word-split OR logic — ignores common stop words)

**Configurable via `.env`:**
```
SCENE_CAPTION_MODEL=blip       # fast, concise (default)
SCENE_CAPTION_MODEL=florence2  # richer, more verbose
SCENE_DESCRIPTION_ENABLED=true/false
```

**Weights location:** `~/.cache/huggingface/hub/`
- BLIP: `models--Salesforce--blip-image-captioning-base` (~900MB)
- Florence-2: `models--microsoft--Florence-2-base` (~900MB)

---

### 3. InsightFace buffalo_l — Face Detection & Recognition

**What it does:** Two things in one model:
1. **Detection** — finds every face in a frame, returns bounding box coordinates
2. **Recognition** — generates a 512-dimensional ArcFace embedding (a unique numerical fingerprint) for each detected face

**Output:** For each frame, a list of faces with their position and embedding

**Why this model:** InsightFace's `buffalo_l` with ArcFace embeddings is the industry standard for open-source face recognition. It's what production security systems and phone face-unlock systems are based on. Outperforms alternatives like FaceNet and Dlib on most benchmarks.

**How it's used:**

*During analysis:*
1. Detects all faces in each frame
2. Generates ArcFace embedding per face
3. **Intra-video clustering** — groups faces by cosine similarity (threshold: 0.35). Faces that are similar enough become one identity (`Person 1`, `Person 2`, etc.)
4. **Cross-video matching** — compares new video's face clusters against existing named identities in the database (threshold: 0.45). If your tagged `"Soham"` from video 1 appears in video 2, it's automatically recognised and tagged

*Data stored:*
- `FaceIdentity` — one record per unique person (name, reference embedding, thumbnail)
- `DetectedFace` — one record per face detection (timestamp, bounding box, embedding, crop image, link to identity)

*On the watch page:*
- "People in this Video" panel shows all detected identities
- Displays: face thumbnail, name, N appearances, ~Xs screen time
- Timestamp chips to jump directly to any appearance
- Owner can: Tag (rename), Merge (combine two identities), Remove (delete from this video)

**Configurable:**
```
FACE_RECOGNITION_ENABLED=true/false
```

**Weights location:** `~/.insightface/models/buffalo_l/` (~500MB)
- `det_10g.onnx` — face detector
- `w600k_r50.onnx` — ArcFace recognition
- `1k3d68.onnx` — 3D landmark detector
- `2d106det.onnx` — 2D landmark detector
- `genderage.onnx` — gender/age estimator

---

### 4. Whisper (faster-whisper) — Speech Transcription

**What it does:** Transcribes spoken audio from the video into text, with timestamps for every sentence/phrase.

**Output:** WebVTT subtitle file + timestamped text segments in database

**Why this model:** OpenAI's Whisper is the best open-source speech recognition model available. Supports 99 languages, highly accurate even with background noise. `faster-whisper` is a CTranslate2-optimised version that runs 4x faster on CPU with the same accuracy.

**How it's used:**
1. Extracts audio from video to a temporary WAV file
2. Runs Whisper on the WAV
3. Saves `.vtt` subtitle file → displayed on video player as closed captions
4. Bulk-creates `VideoSegment` rows (one per spoken sentence) with start/end timestamps
5. Powers speech search: searching `"budget meeting"` finds every video/timestamp where those words were spoken

**Configurable:**
```
WHISPER_MODEL_SIZE=base    # tiny/base/small/medium/large-v2/large-v3
WHISPER_DEVICE=cpu         # cpu or cuda (if GPU available)
AUTO_CAPTION_ON_UPLOAD=true/false
```

**Weights location:** `~/.cache/huggingface/hub/` or `~/.cache/whisper/` depending on version (~150MB for base)

---

### 5. CLIP — Semantic Visual Search

**What it does:** Encodes each frame into a 512-dimensional vector that represents its visual meaning. At search time, encodes the text query into the same vector space and finds frames that match semantically.

**Output:** 512-dim embedding per frame stored in database

**Why this model:** CLIP (Contrastive Language-Image Pretraining, OpenAI) understands the conceptual relationship between images and text. Unlike BLIP/Florence (which need the exact words in the description), CLIP matches meaning. Searching `"celebration"` finds party/birthday frames even if BLIP described them as `"people raising their arms"`.

**How it's used:**

*During analysis:*
- Generates a normalised 512-dim image embedding per frame
- Stored as JSON in `VideoFrame.clip_embedding`

*During search:*
- CLIP text encoder converts the search query into a 512-dim vector
- Cosine similarity computed between query vector and all stored frame embeddings
- Frames above the similarity threshold (default: 0.20) are returned as `[visual match]` results
- CLIP model cached in Django process RAM after first search — subsequent searches are instant

**Configurable:**
```
CLIP_ENABLED=true/false
CLIP_SIMILARITY_THRESHOLD=0.20   # lower = more results, higher = stricter matches
```

**Weights location:** `~/.cache/huggingface/hub/models--openai--clip-vit-base-patch32` (~600MB)

---

## Model Cache Locations

All model weights are downloaded automatically on first use and cached locally.

| Model | Cache Location | Size | Delete Command |
|-------|---------------|------|----------------|
| YOLOv8n | `~/Desktop/ClipStream/yolov8n.pt` | ~6MB | `rm ~/Desktop/ClipStream/yolov8n.pt` |
| BLIP | `~/.cache/huggingface/hub/models--Salesforce--blip-image-captioning-base` | ~900MB | `rm -rf ~/.cache/huggingface/hub/models--Salesforce--blip-image-captioning-base` |
| Florence-2 | `~/.cache/huggingface/hub/models--microsoft--Florence-2-base` | ~900MB | `rm -rf ~/.cache/huggingface/hub/models--microsoft--Florence-2-base` |
| CLIP | `~/.cache/huggingface/hub/models--openai--clip-vit-base-patch32` | ~600MB | `rm -rf ~/.cache/huggingface/hub/models--openai--clip-vit-base-patch32` |
| InsightFace | `~/.insightface/models/buffalo_l/` | ~500MB | `rm -rf ~/.insightface` |
| Whisper | `~/.cache/huggingface/hub/` | ~150MB (base) | `rm -rf ~/.cache/huggingface/hub/models--Systran*` |

**To wipe everything at once:**
```bash
rm -rf ~/.cache/huggingface
rm -rf ~/.insightface
rm ~/Desktop/ClipStream/yolov8n.pt
```

**In-memory cache (RAM only, not disk):**
- CLIP model is cached in Django process memory after first search. Cleared on Django restart. Nothing written to disk.

---

## Video Upload Pipeline

```
USER UPLOADS VIDEO
        │
        ▼
┌─────────────────────────────────┐
│  Django saves file to disk      │
│  Video record created (DB)      │
│  Status: PROCESSING             │
└────────────┬────────────────────┘
             │ fires two Celery tasks
             ├──────────────────────────────────────────────┐
             ▼                                              ▼
┌─────────────────────────┐                ┌───────────────────────────────┐
│  process_video_task     │                │  generate_captions_task       │
│  Queue: processing      │                │  Queue: captions (5s delay)   │
│                         │                │                               │
│  FFmpeg encodes video   │                │  FFmpeg extracts audio → WAV  │
│  into HLS renditions:   │                │                               │
│  144p / 240p / 360p /   │                │  faster-whisper transcribes   │
│  480p / 720p / 1080p /  │                │  audio → timestamped segments │
│  1440p / 4K             │                │                               │
│  (skips above source    │                │  Saves .vtt subtitle file     │
│   resolution)           │                │                               │
│                         │                │  Bulk-creates VideoSegment    │
│  Writes master.m3u8     │                │  rows for speech search       │
│  Status → READY         │                │                               │
└────────────┬────────────┘                │  Deletes temp WAV             │
             │                             └───────────────────────────────┘
             │ fires after encoding
             ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  analyze_video_frames_task                                               │
│  Queue: default                                                          │
│                                                                          │
│  1. Extract 1 frame every 5s → media/frames/<video_id>/frame_0000.jpg   │
│                                                                          │
│  2. Load models (once per task):                                         │
│     ├── YOLOv8n                                                          │
│     ├── BLIP or Florence-2 (based on SCENE_CAPTION_MODEL)                │
│     ├── CLIP (openai/clip-vit-base-patch32)                              │
│     └── InsightFace buffalo_l                                            │
│                                                                          │
│  3. For EACH frame:                                                      │
│     ├── YOLOv8  → ["person", "laptop", "chair"] → VideoFrame.labels     │
│     ├── BLIP    → "Two people in a meeting room" → VideoFrame.description│
│     ├── CLIP    → [0.23, -0.11, ...] (512 floats) → VideoFrame.clip_embedding │
│     └── InsightFace → face bbox + ArcFace embedding per face detected    │
│                                                                          │
│  4. Bulk-save all VideoFrame rows to DB                                  │
│                                                                          │
│  5. Cluster face embeddings (cosine similarity, threshold 0.35)          │
│     → Groups similar faces across frames into FaceIdentity records       │
│     → Auto-names: "Person 1", "Person 2", etc.                          │
│                                                                          │
│  6. Cross-video face matching (threshold 0.45)                           │
│     → Compares clusters against existing named identities in DB          │
│     → If match found: assigns existing name automatically                │
│     → If no match: creates new FaceIdentity                              │
│                                                                          │
│  7. Save face crop images → media/faces/<video_id>/                      │
│     Bulk-create DetectedFace rows                                        │
└──────────────────────────────────────────────────────────────────────────┘
        │
        ▼
VIDEO FULLY PROCESSED
  ✓ Adaptive HLS streaming (all qualities)
  ✓ Closed captions (WebVTT)
  ✓ Object search (YOLO labels)
  ✓ Scene search (BLIP/Florence-2 descriptions)
  ✓ Semantic search (CLIP embeddings)
  ✓ Face recognition (InsightFace identities)
  ✓ Speech search (Whisper segments)
```

---

## Search Pipeline

When a user searches on the player/home page, five independent queries run and results are merged:

```
SEARCH QUERY: "celebration"
        │
        ├── 1. YOLO label search
        │      VideoFrame.labels ICONTAINS "celebration"
        │      → Usually nothing (YOLO detects objects, not events)
        │
        ├── 2. Scene description search (word-split OR)
        │      Split query into words, ignore stop words
        │      VideoFrame.description ICONTAINS any word
        │      → Finds frames described as "people celebrating" etc.
        │
        ├── 3. Speech search
        │      VideoSegment.text ICONTAINS "celebration"
        │      → Finds moments where word was spoken in dialogue
        │
        ├── 4. Face identity search
        │      FaceIdentity.name ICONTAINS "celebration"
        │      → Not relevant here (identity names are person names)
        │
        └── 5. CLIP semantic search
               Encode "celebration" → 512-dim text vector
               Cosine similarity vs all VideoFrame.clip_embedding vectors
               Return frames with similarity ≥ 0.20
               → Finds visually matching frames even if never described as "celebration"

All results merged by video → displayed as "moments" with timestamp chips
Click a chip → video seeks directly to that moment
```

---

## Database Models

| Model | Purpose |
|-------|---------|
| `Video` | Core video record — title, file, status, visibility, category |
| `Channel` | Creator channel — name, slug, avatar, banner |
| `VideoFrame` | One row per extracted frame — labels, description, CLIP embedding |
| `VideoSegment` | One row per Whisper speech segment — text, start/end seconds |
| `FaceIdentity` | One unique person — name, reference embedding, thumbnail |
| `DetectedFace` | One detected face — links frame + identity, stores bbox + crop |
| `Subtitle` | WebVTT file record linked to a video |
| `AudioTrack` | Extracted audio track record |
| `Playlist` / `PlaylistItem` | User-created playlists |
| `Comment` / `CommentLike` | Video comments |
| `VideoLike` | Like records |
| `WatchHistory` | Per-user watch history |
| `SavedVideo` | Saved/bookmarked videos |
| `WatchTimeEntry` | Watch time analytics |
| `Notification` | User notification records |
| `Category` | Video categories for grouping |
| `EndScreen` | End screen card overlays |
| `VideoChapter` | Chapter markers |

---

## Configuration Reference

All settings are controlled via `.env` in the project root. Never edit `settings.py` directly for environment-specific values.

```bash
# ── Core Django ──────────────────────────────────────────────────────────────
SECRET_KEY=your-secret-key
DEBUG=True
ALLOWED_HOSTS=localhost,127.0.0.1
SITE_URL=http://localhost:8000

# ── Database ─────────────────────────────────────────────────────────────────
USE_MYSQL=true
MYSQL_DB_NAME=videos
MYSQL_DB_USER=root
MYSQL_DB_PASSWORD=yourpassword
MYSQL_DB_HOST=localhost
MYSQL_DB_PORT=3306

# ── HLS Encoding ─────────────────────────────────────────────────────────────
HLS_MULTI_QUALITY=true
HLS_QUALITIES=2160,1440,1080,720,480,360,240,144  # skips above source resolution
HLS_SEGMENT_DURATION=6                              # seconds per .ts segment

# ── Frame Analysis ───────────────────────────────────────────────────────────
FRAME_ANALYSIS_ENABLED=true     # set false to skip during dev (much faster uploads)
FRAME_INTERVAL_SECONDS=5        # extract 1 frame every N seconds
YOLO_MODEL=yolov8n              # yolov8n / yolov8s / yolov8m (bigger = slower + better)

# ── Scene Description ────────────────────────────────────────────────────────
SCENE_DESCRIPTION_ENABLED=true
SCENE_CAPTION_MODEL=blip        # blip (fast, concise) or florence2 (richer, slower)

# ── CLIP Semantic Search ─────────────────────────────────────────────────────
CLIP_ENABLED=true
CLIP_SIMILARITY_THRESHOLD=0.20  # 0.15 = more results, 0.25 = stricter

# ── Face Recognition ─────────────────────────────────────────────────────────
FACE_RECOGNITION_ENABLED=true

# ── Speech Transcription ─────────────────────────────────────────────────────
AUTO_CAPTION_ON_UPLOAD=true
WHISPER_MODEL_SIZE=base         # tiny/base/small/medium/large-v2/large-v3
WHISPER_DEVICE=cpu              # cpu or cuda
WHISPER_COMPUTE_TYPE=int8

# ── Embedding ────────────────────────────────────────────────────────────────
EMBED_ALLOW_ORIGINS=*           # domains allowed to iframe your videos
```

---

## Directory Structure

```
ClipStream/
├── ClipStream/
│   ├── settings.py          # all settings (reads from .env)
│   ├── celery.py            # Celery app config + task routing
│   └── urls.py              # root URL conf
│
├── videos/
│   ├── models.py            # all DB models
│   ├── views.py             # page views + API endpoints
│   ├── tasks.py             # all Celery async tasks
│   ├── services.py          # FFmpeg HLS encoding logic
│   ├── serializers.py       # DRF serializers for API responses
│   ├── urls.py              # URL routing
│   ├── admin.py             # Django admin registrations
│   ├── migrations/          # DB schema history
│   └── templates/videos/    # all HTML templates
│
├── media/                   # all user-uploaded + generated files
│   ├── videos/              # original uploaded files
│   ├── hls/                 # encoded HLS segments + playlists
│   ├── frames/              # extracted video frames (JPEGs)
│   ├── faces/               # cropped face images
│   ├── subtitles/           # WebVTT caption files
│   └── channels/            # channel avatars + banners
│
├── .env                     # environment config (never commit this)
├── requirements.txt         # Python dependencies
├── manage.py
└── ARCHITECTURE.md          # this file
```

---

## Running the Project

```bash
# 1. Activate venv
source venv/bin/activate

# 2. Start Redis (required for Celery)
redis-server

# 3. Start Django
python manage.py runserver

# 4. Start Celery (separate terminal, venv activated)
celery -A ClipStream worker -l info -Q processing,captions,default
```

All five AI models load lazily — they download and cache on first use, then load from cache on subsequent runs.
