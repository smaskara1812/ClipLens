# ClipStream — Product Specification

## Visual Intelligence & Asset Management Platform

**Version:** 2.0 (post-fork from LMS branch)
**Focus:** Media ingestion, AI processing, semantic search, and digital asset management

---

## 1. Vision

ClipStream is a self-hosted media intelligence platform. Its primary value is not playback — it is the ability to **ingest videos/photos and images, automatically extract structured knowledge from them, and make that knowledge instantly searchable** via natural language, object labels, face identities, speaker identities, visual semantics, and **named geographic places** (where GPS metadata exists).

---

## 2. Core Capabilities

### 2.1 Video Ingestion Pipeline


| Stage                | Technology                   | Output                                                |
| -------------------- | ---------------------------- | ----------------------------------------------------- |
| Transcoding          | FFmpeg → HLS (multi-quality) | Adaptive bitrate stream                               |
| Frame extraction     | FFmpeg `fps=1/N`             | JPEG frames at configurable interval                  |
| Object detection     | YOLOv8n                      | Per-frame label set (comma-separated)                 |
| Scene captioning     | BLIP or Florence-2           | Per-frame natural language description                |
| CLIP encoding        | CLIP ViT-B/32                | Per-frame 512-d embedding (pgvector)                  |
| Face detection       | InsightFace buffalo_l        | ArcFace 512-d embeddings, bounding boxes, crop images |
| Face clustering      | Greedy cosine clustering     | Automatic identity grouping across all frames         |
| Speech transcription | faster-whisper               | WebVTT subtitles + searchable transcript segments     |


All stages run asynchronously via **Celery** (`processing` and `captions` queues backed by Redis).

### 2.2 Photo / Image Ingestion Pipeline


| Stage            | Technology         | Output                                                    |
| ---------------- | ------------------ | --------------------------------------------------------- |
| Upload           | Django ImageField  | Stored in `photos/originals/`                             |
| Thumbnail        | Pillow             | 480×480 JPEG stored in `photos/thumbnails/`               |
| Object detection | YOLOv8n            | `Photo.labels`                                            |
| Scene captioning | BLIP or Florence-2 | `Photo.scene_description`                                 |
| CLIP encoding    | CLIP ViT-B/32      | `Photo.clip_embedding` (pgvector)                         |
| Face detection   | InsightFace        | `Photo.face_count`, `Photo.face_names`, identity matching |


No HLS, no Whisper — photos need neither.

### 2.3 Search System

ClipStream implements a **four-layer search stack** over every piece of processed content:

#### Layer 1 — Keyword / FTS

- Postgres full-text search (`to_tsvector / to_tsquery`) with GIN expression indexes
- Covers: video title/description/tags, frame labels, frame descriptions, transcript text, photo title/description/tags/labels/scene_description, channel names, playlist titles, face identity names
- Stemming, stop-word removal, English lexeme matching via the `english` text-search configuration

#### Layer 2 — Fuzzy (pg_trgm)

- Trigram similarity ≥ 0.22 threshold (configurable)
- Catches: typos, partial words, different word forms
- GIN trigram indexes on all text fields (no full-table scan)
- Combined with FTS via SQL `OR pk IN (...)`

#### Layer 3 — Semantic / CLIP (visual meaning)

- Triggered by `?semantic=1` in the query
- Query text is encoded via CLIP's text encoder to a 512-d vector
- pgvector HNSW cosine ANN search returns the top-N visually matching frames and photos
- Threshold configurable (`CLIP_SIMILARITY_THRESHOLD`, default 0.20)
- Caps: 250 video frames, 50 photos per query

#### Layer 4 — Structural (faces, channels, playlists, places)

- Face identity name search across all DetectedFace records, grouped by person then video
- Channel name and playlist title fuzzy search
- Named place name/description search → **Places** tab and place detail URLs (`/places/<slug>/`)
- Results grouped into tabbed UI: All / Videos / People / Scenes / Speech / Channels / Playlists / Photos / **Places** (named locations; each row links to a place detail page so similarly named sites stay distinguishable)

#### Filter propagation

All four filter parameters (channel, category, duration, date) apply uniformly to the main video grid **and** to every AI sub-search (speech, scenes, CLIP). This means a user can search "person walking" within a specific channel and duration range and the semantic results will be scoped accordingly.

### 2.4 Digital Asset Management (DAM)

Photos are first-class assets alongside videos:

- **Library** (`/photos/`) — infinite-scroll grid, filter by category/sort, keyword search
- **Upload** (`/photos/upload/`) — drag-and-drop with live preview, post-upload polling for processing status
- **Detail** (`/photos/<id>/`) — full image viewer with AI metadata sidebar; polls for processing completion
- **Search integration** — photos appear in the Photos tab of the main search results

### 2.5 Named places, geolocation & media map

- **`NamedPlace` model** — Curated locations (name, unique slug, lat/lng, radius in metres, optional description, map colour). Slugs are generated from the name and deduplicated automatically when names collide.
- **Photos and videos** — Both support optional `latitude`, `longitude`, and optional `named_place` foreign key. Bulk tools can auto-assign items whose coordinates fall inside a place’s radius.
- **Named Places admin** (`/named-places/`, editors and superadmins) — Leaflet map with inline address search (geocoding via **OpenStreetMap Nominatim** when the browser calls that API), create/edit places, and table with photo and video counts per place.
- **Media Map** (`/media/map/`) — Unified map of geotagged **photos and videos**; statistics distinguish total markers vs distinct named places. **Map** vs **Grouped** view: grouped sections show media per named place with “View more” to the place page. Map tile theme can be toggled light/dark independently of the global site theme (stored in `localStorage`).
- **Place detail** (`/places/<slug>/`) — Browse all photos and videos linked to one named place, with counts and a small map.
- **Search suggestions** — Matching named places appear in autocomplete with short disambiguating text (description snippet or rounded coordinates) when names are similar.
- **Admin chrome** — Users, Categories, Named Places, Commands, and Storage share the same top tab navigation. **Superadmins** see all tabs; **editors** only see **Categories** and **Named Places** (no links to superadmin-only routes).

### 2.6 Face Recognition & Identity Management

- InsightFace `buffalo_l` model (ArcFace) runs per frame and per photo
- Detected faces are clustered per-video using greedy cosine similarity (threshold: 0.35 for within-video clustering)
- New clusters are matched against all existing `FaceIdentity` records (named: 0.45 threshold; auto-named: 0.50 threshold)
- Unmatched clusters create a new `FaceIdentity` with name `Person N`
- Editors can rename, merge, and tag identities via the People page
- Running-average embeddings are maintained per identity as the system sees more faces

### 2.7 Speaker Identity & Voice Recognition (Diarization)

- Speaker diarization uses **pyannote.audio** (`pyannote/speaker-diarization-3.1`) to label who spoke when
- Each Whisper transcript segment (`VideoSegment`) can be tagged with:
  - `speaker_label` — a stable global label like `SPEAKER_02`
  - `speaker_identity` — a resolved `SpeakerIdentity` row (name, role, optional face link)
- Cross-video “same voice” matching uses a **256-d WeSpeaker** embedding (`pyannote/wespeaker-voxceleb-resnet34-LM`) with cosine similarity (threshold `SPEAKER_MATCH_THRESHOLD`, default `0.75`)
- Speakers have a dedicated UI: `/speakers/` list + `/speakers/<id>/` detail (rename, role, link face, merge, delete)

**Implementation details:** see `documentation files/SPEAKER_IDENTITY.md`.

---

## 3. Data Models (Processing-relevant)

### 3.1 Video

```
id (UUID) | title | channel | category | tags
original_file | hls_path | thumbnail
duration | file_size | resolution | available_qualities
status (pending/processing/ready/failed)
visibility (public/private/subscribers_only)
latitude | longitude | named_place (FK → NamedPlace, optional)
```

### 3.2 VideoFrame (one per sampled frame)

```
video (FK) | timestamp (seconds)
labels       — YOLO class labels, comma-separated, FTS+trgm indexed
description  — BLIP/Florence-2 caption, FTS+trgm indexed
clip_embedding — vector(512), HNSW indexed
face_count | face_names
```

### 3.3 VideoSegment (one per Whisper segment)

```
video (FK) | start_seconds | end_seconds
text  — transcript text, FTS+trgm indexed
speaker_label — diarization label (e.g. SPEAKER_02)
speaker_identity — FK to SpeakerIdentity (resolved identity after diarization)
```

### 3.4 DetectedFace (one per face detected in a frame)

```
video (FK) | frame (FK) | identity (FK)
timestamp | bbox (JSON) | embedding (JSON 512-d)
confidence | crop_path | status (unreviewed/confirmed/rejected)
```

### 3.5 FaceIdentity

```
name | is_auto_named | ref_embedding (JSON running average) | thumbnail
```

### 3.6 Photo

```
id (UUID) | title | description | channel | category | tags
file | thumbnail | width | height | file_size
labels       — YOLO class labels, FTS+trgm indexed
scene_description — BLIP/Florence-2 caption, FTS+trgm indexed
clip_embedding — vector(512), HNSW indexed
face_count | face_names
latitude | longitude | named_place (FK → NamedPlace, optional)
status (pending/processing/ready/failed)
visibility (public/private)
```

### 3.7 NamedPlace

```
name | slug (unique) | latitude | longitude
radius_meters | color (hex) | description (optional)
created_by (FK User) | created_at
```

### 3.8 SpeakerIdentity

```
name | is_auto_named | role (speaker/narrator/background)
speaker_embedding — vector(256) for cross-video voice matching
face_identity (optional FK) — manual bridge between voice and face identity
```

---

## 4. Processing Architecture

```
Upload (HTTP multipart)
        │
        ▼
Django view creates Video/Photo record (status=pending)
        │
        ▼
Celery task queued (queue='processing')
        │
        ├── process_video_task
        │       │── FFmpeg → HLS segments
        │       │── status = ready
        │       └── triggers →
        │               ├── generate_captions_task  (queue='captions')
        │               └── analyze_video_frames_task (queue='processing')
        │
        └── analyze_photo_task
                │── YOLO labels
                │── BLIP/Florence-2 scene description
                │── CLIP 512-d embedding
                │── InsightFace face detection + identity matching
                │── Pillow thumbnail generation
                └── status = ready
```

### Task queues


| Queue        | Tasks                                                          | Typical runtime |
| ------------ | -------------------------------------------------------------- | --------------- |
| `processing` | HLS encoding, frame analysis, photo analysis, audio extraction | 1–30 min        |
| `captions`   | faster-whisper transcription, speaker diarization (pyannote)    | 30s–10 min      |
| `default`    | segment re-indexing, misc                                      | <5s             |


---

## 5. Search Caps & Performance Characteristics


| Search type                 | Row cap                           | Index used         | Typical latency |
| --------------------------- | --------------------------------- | ------------------ | --------------- |
| Video FTS (title/desc/tags) | 100                               | GIN FTS expression | <10 ms          |
| Video fuzzy (trgm)          | 100                               | GIN trgm           | <20 ms          |
| Speech FTS + fuzzy          | 500 segments → per-video grouping | GIN FTS + trgm     | <30 ms          |
| YOLO frame labels           | 400 frames                        | GIN FTS + trgm     | <30 ms          |
| Scene description           | 400 frames                        | GIN FTS + trgm     | <30 ms          |
| CLIP semantic (video)       | 250 frames                        | HNSW ANN           | <50 ms          |
| CLIP semantic (photo)       | 50 photos                         | HNSW ANN           | <10 ms          |
| Face identity name          | 300 faces                         | B-tree + trgm      | <20 ms          |
| Photo FTS + fuzzy           | 60 photos                         | GIN FTS + trgm     | <15 ms          |


All caps apply after active filters (channel/category/duration/date) have narrowed the dataset.

---

## 6. AI Model Registry


| Model                             | Purpose                             | Disk    | RAM     |
| --------------------------------- | ----------------------------------- | ------- | ------- |
| YOLOv8n                           | Object detection                    | ~6 MB   | ~150 MB |
| BLIP (blip-image-captioning-base) | Scene captioning                    | ~990 MB | ~1.5 GB |
| Florence-2-base                   | Scene captioning (alternative)      | ~900 MB | ~2 GB   |
| CLIP ViT-B/32                     | Visual embeddings + semantic search | ~600 MB | ~1 GB   |
| faster-whisper (medium)           | Speech transcription                | ~1.5 GB | ~2 GB   |
| InsightFace buffalo_l             | Face detection + ArcFace embeddings | ~300 MB | ~600 MB |
| pyannote speaker-diarization-3.1  | Speaker diarization (who spoke when) | ~?     | ~?      |
| WeSpeaker ResNet34 (voxceleb)     | Speaker embeddings (256-d)          | ~?     | ~?      |


**Cache strategy:** CLIP model is loaded once per Django process (module-level cache with threading lock). All other models are loaded inside the Celery task and released when the task completes — Celery workers are typically long-lived so models may stay warm across tasks.

---

## 7. Settings Reference

```python
FRAME_INTERVAL_SECONDS        = 5       # sample 1 frame every N seconds of video
FRAME_ANALYSIS_ENABLED        = True
SCENE_DESCRIPTION_ENABLED     = True
SCENE_CAPTION_MODEL           = 'blip'  # or 'florence2'
CLIP_ENABLED                  = True
CLIP_SIMILARITY_THRESHOLD     = 0.20    # cosine similarity floor for CLIP matches
FACE_RECOGNITION_ENABLED      = True
YOLO_MODEL                    = 'yolov8n'
FUZZY_SEARCH_ENABLED          = True
FUZZY_SEARCH_SIMILARITY_THRESHOLD = 0.22
AUTO_CAPTION_ON_UPLOAD        = True
SPEAKER_MATCH_THRESHOLD       = 0.75
HF_TOKEN                      = ''   # required for pyannote diarization models
```

---

## 8. Future Work


| Feature                                     | Complexity | Notes                                                                              |
| ------------------------------------------- | ---------- | ---------------------------------------------------------------------------------- |
| API-based pagination for AI search results  | Medium     | Dedicated endpoints `/api/search/speech/`, `/api/search/scenes/` with offset/limit |
| SearchRank relevance ordering               | Low        | Django `SearchRank` annotation once video count is in the hundreds                 |
| Florence-2 upgrade as primary caption model | Low        | Swap `SCENE_CAPTION_MODEL='florence2'` after Python 3.10+ move                     |
| Shot-boundary detection                     | Medium     | Replace fixed-interval sampling with scene-change detection                        |
| Re-indexing management command              | Low        | `python manage.py reanalyse --model clip` to batch update embeddings               |
| Multi-modal search (image query)            | High       | Upload an image as query, embed it with CLIP, run HNSW search                      |
| Face re-identification across photos+videos | Medium     | Extend DetectedFace FK to accept Photo as source                                   |


