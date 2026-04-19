# ClipStream — Technical Architecture

## Database, Vector Storage, Full-Text Search & Indexing

---

## 1. Database Stack


| Layer             | Technology      | Version  |
| ----------------- | --------------- | -------- |
| Primary DB        | PostgreSQL      | 15+      |
| ORM               | Django 4.2      | —        |
| Vector extension  | pgvector        | 0.4+     |
| Trigram extension | pg_trgm         | built-in |
| Driver            | psycopg2-binary | 2.9.10   |


---

## 2. pgvector Extension

### What it is

pgvector is a Postgres extension that adds a native `vector(n)` column type and approximate nearest-neighbour (ANN) index methods (HNSW, IVFFlat) directly inside the database. It lets us do semantic similarity search entirely in SQL without a separate vector DB.

### Installation (one-time)

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

This runs inside migration `0017_pgvector_and_fts` (and is safe to repeat).

### Column type

```sql
ALTER TABLE videos_videoframe
    ADD COLUMN clip_embedding vector(512);
```

`vector(512)` stores a 512-dimensional float array as a compact binary blob (~2 KB per row). Django maps this to `VectorField(dimensions=512, null=True, blank=True)` via the `pgvector-django` package.

### How the column was migrated from the old TextField

Migration `0017` uses a `SeparateDatabaseAndState` block:

- **Database side**: `ALTER COLUMN clip_embedding TYPE vector(512) USING CASE WHEN trim(clip_embedding)='' OR clip_embedding IS NULL THEN NULL ELSE clip_embedding::vector(512) END`  — casts the old JSON-string representation directly in Postgres.
- **State side**: Django model is updated to `VectorField(dimensions=512)` so the ORM stays in sync.

---

## 3. HNSW Index (Approximate Nearest Neighbour)

### What is HNSW?

Hierarchical Navigable Small World (HNSW) is a graph-based ANN algorithm. Instead of scanning every row (O(n) full-table scan), it navigates a layered graph to find the k nearest vectors in O(log n).

### Indexes created


| Table               | Index name        | Column           | Operator            |
| ------------------- | ----------------- | ---------------- | ------------------- |
| `videos_videoframe` | `vf_clip_hnsw`    | `clip_embedding` | `vector_cosine_ops` |
| `videos_photo`      | `photo_clip_hnsw` | `clip_embedding` | `vector_cosine_ops` |


```sql
CREATE INDEX vf_clip_hnsw
    ON videos_videoframe
    USING hnsw (clip_embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
```

**Parameters:**

- `m = 16` — number of bi-directional links per node. Higher = better recall, more memory.
- `ef_construction = 64` — size of the dynamic candidate list during construction. Higher = better quality index, slower build.
- `vector_cosine_ops` — distance function: `1 - cosine_similarity`. Range [0, 2]; 0 = identical, 2 = opposite.

### How search works

```python
from pgvector.django import CosineDistance

frames = (
    VideoFrame.objects
    .exclude(clip_embedding=None)
    .annotate(_dist=CosineDistance('clip_embedding', query_vector))
    .filter(_dist__lte=0.80)          # threshold: similarity ≥ 0.20
    .order_by('_dist')
    [:250]
)
```

Django translates this to:

```sql
SELECT *, (clip_embedding <=> '[0.12, -0.04, ...]') AS _dist
FROM videos_videoframe
WHERE clip_embedding IS NOT NULL
  AND (clip_embedding <=> '[...]') <= 0.80
ORDER BY _dist
LIMIT 250;
```

The `<=>` operator triggers the HNSW index. Postgres returns approximate (not exact) nearest neighbours — fast enough for real-time search even with millions of rows.

---

## 4. CLIP Embeddings — Generation & Storage

### Model

- **openai/clip-vit-base-patch32** via HuggingFace Transformers
- Vision encoder output: 512-dimensional float vector
- Generates embeddings for both frames (video) and full images (photos)

### Generation pipeline (per frame / per photo)

```python
from transformers import CLIPProcessor, CLIPModel
import torch

processor = CLIPProcessor.from_pretrained('openai/clip-vit-base-patch32')
model     = CLIPModel.from_pretrained('openai/clip-vit-base-patch32')
model.eval()

pil_img = Image.open(path).convert('RGB')
inputs  = processor(images=pil_img, return_tensors='pt')

with torch.no_grad():
    features = model.get_image_features(**inputs)
    features = features / features.norm(dim=-1, keepdim=True)   # L2 normalise

embedding = features[0].tolist()   # list of 512 floats
```

**L2 normalisation** is critical: it ensures that cosine similarity equals dot product, which is what the `<=>` (cosine distance) operator computes.

### Storage

```python
# model field
clip_embedding = VectorField(dimensions=512, null=True, blank=True)

# write — pgvector accepts a plain Python list
frame.clip_embedding = embedding          # list[float]
frame.save(update_fields=['clip_embedding'])

# bulk write
VideoFrame.objects.bulk_create(frame_objects, batch_size=200)
```

### Query-time text embedding

For text queries the same model encodes the search string:

```python
inputs  = processor(text=[query], return_tensors='pt', padding=True)
with torch.no_grad():
    feat = model.get_text_features(**inputs)
    feat = feat / feat.norm(dim=-1, keepdim=True)
query_vector = feat[0].tolist()
```

CLIP's joint vision–language embedding space means a text query and a visually matching image end up close in the 512-d space.

### In-process cache

The CLIP model (~600 MB) is loaded once at the module level and reused for all search requests (protected by a `threading.Lock`):

```python
_clip_model_cache = None
_clip_proc_cache  = None
_clip_cache_lock  = threading.Lock()
```

---

## 5. Postgres Full-Text Search (FTS) GIN Indexes

### What are GIN indexes for FTS?

A GIN (Generalised Inverted iNdex) stores a map from each lexeme (normalised word) to the set of rows that contain it. `to_tsvector('english', text)` applies stemming (running → run), stop-word removal, and ranking weights.

### Indexes on `videos_videoframe`

```sql
-- YOLO object labels
CREATE INDEX vf_labels_fts ON videos_videoframe
    USING gin (to_tsvector('english', coalesce(labels, '')));

-- BLIP / Florence-2 scene descriptions
CREATE INDEX vf_desc_fts ON videos_videoframe
    USING gin (to_tsvector('english', coalesce(description, '')));
```

### Indexes on `videos_video`

```sql
-- Title + description + tags combined
CREATE INDEX v_title_fts ON videos_video
    USING gin (
        to_tsvector('english',
            coalesce(title, '') || ' ' ||
            coalesce(description, '') || ' ' ||
            coalesce(tags, ''))
    );
```

### Indexes on `videos_videosegment` (Whisper transcript)

```sql
CREATE INDEX vs_text_fts ON videos_videosegment
    USING gin (to_tsvector('english', coalesce(text, '')));
```

### Indexes on `videos_photo`

```sql
CREATE INDEX photo_title_desc_tags_fts ON videos_photo
    USING gin (to_tsvector('english',
        coalesce(title,'') || ' ' || coalesce(description,'') || ' ' || coalesce(tags,'')));

CREATE INDEX photo_scene_desc_fts ON videos_photo
    USING gin (to_tsvector('english', coalesce(scene_description, '')));

CREATE INDEX photo_labels_fts ON videos_photo
    USING gin (to_tsvector('english', coalesce(labels, '')));
```

### Django FTS lookup

```python
from django.contrib.postgres.search import SearchVector, SearchQuery

qs.annotate(_search=SearchVector('title', 'description', 'tags', config='english'))
  .filter(_search=SearchQuery(q, config='english', search_type='plain'))
```

Or directly on a GIN-indexed column (single field):

```python
qs.filter(labels__search=SearchQuery(q, config='english', search_type='plain'))
```

---

## 6. pg_trgm Fuzzy-Search GIN Indexes

### What is pg_trgm?

`pg_trgm` breaks strings into overlapping 3-character n-grams (trigrams) and computes a **similarity score** [0, 1]. It handles typos, partial matches, and different word forms. GIN trigram indexes make `LIKE '%term%'` and similarity queries index-backed.

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;
```

### Indexes created (migration 0018 + 0019)


| Table                 | Index name              | Column              |
| --------------------- | ----------------------- | ------------------- |
| `videos_video`        | `v_title_trgm`          | `title`             |
| `videos_video`        | `v_desc_trgm`           | `description`       |
| `videos_video`        | `v_tags_trgm`           | `tags`              |
| `videos_videosegment` | `vs_text_trgm`          | `text`              |
| `videos_videoframe`   | `vf_labels_trgm`        | `labels`            |
| `videos_videoframe`   | `vf_desc_trgm`          | `description`       |
| `videos_channel`      | `ch_name_trgm`          | `name`              |
| `videos_playlist`     | `pl_title_trgm`         | `title`             |
| `videos_faceidentity` | `fi_name_trgm`          | `name`              |
| `videos_photo`        | `photo_title_trgm`      | `title`             |
| `videos_photo`        | `photo_labels_trgm`     | `labels`            |
| `videos_photo`        | `photo_scene_desc_trgm` | `scene_description` |


```sql
CREATE INDEX v_title_trgm ON videos_video USING gin (title gin_trgm_ops);
```

### Django fuzzy filter helper

```python
from django.contrib.postgres.search import TrigramSimilarity
from django.db.models.functions import Greatest

def _postgres_fuzzy_filter(qs, raw, fields):
    similarity_expr = Greatest(*[TrigramSimilarity(f, raw) for f in fields])
    return qs.annotate(_fuzzy_sim=similarity_expr).filter(_fuzzy_sim__gte=0.22)
```

Threshold `0.22` configured via `FUZZY_SEARCH_SIMILARITY_THRESHOLD` in settings.

### Search strategy (combined)

Every search runs **FTS first** (exact lexeme match, stemming) combined via `OR` with **fuzzy** (trigram similarity ≥ threshold), then deduplicates with `distinct()`:

```python
videos = videos.filter(
    Q(pk__in=fts_qs.values('pk')) | Q(pk__in=fuzzy_qs.values('pk'))
).distinct()
```

---

## 7. All B-Tree Indexes by Table

### `videos_video`


| Index                  | Fields                         | Purpose                 |
| ---------------------- | ------------------------------ | ----------------------- |
| `video_status_vis`     | `status, visibility`           | Feed query WHERE clause |
| `video_ch_status_vis`  | `channel, status, visibility`  | Channel page            |
| `video_cat_status_vis` | `category, status, visibility` | Category page           |
| `video_created_at`     | `created_at`                   | ORDER BY default        |
| `video_views`          | `views_count`                  | Sort by popularity      |
| `video_duration`       | `duration`                     | Duration filter         |


### `videos_videoframe`


| Index  | Fields             | Purpose                       |
| ------ | ------------------ | ----------------------------- |
| (auto) | `video, timestamp` | Time-ordered per-video access |


### `videos_detectedface`


| Index                     | Fields             | Purpose                               |
| ------------------------- | ------------------ | ------------------------------------- |
| (auto)                    | `video, timestamp` | Per-video face list                   |
| (auto)                    | `identity`         | Identity lookup                       |
| `detface_identity_status` | `identity, status` | People page confirmed/rejected counts |
| `detface_identity_video`  | `identity, video`  | Per-video face grouping               |


### `videos_faceidentity`


| Index               | Fields          | Purpose                  |
| ------------------- | --------------- | ------------------------ |
| `faceidentity_name` | `name`          | Name search              |
| `faceidentity_auto` | `is_auto_named` | Named/Unnamed tab filter |


### `videos_watchhistory`


| Index             | Fields             | Purpose                       |
| ----------------- | ------------------ | ----------------------------- |
| `watch_user_time` | `user, watched_at` | History page per user, sorted |


### `videos_savedvideo`


| Index             | Fields           | Purpose                     |
| ----------------- | ---------------- | --------------------------- |
| `saved_user_time` | `user, saved_at` | Saved page per user, sorted |


### `videos_notification`


| Index             | Fields                           | Purpose                          |
| ----------------- | -------------------------------- | -------------------------------- |
| `notif_recipient` | `recipient, is_read, created_at` | Unread count + notification list |


### `videos_photo`


| Index                 | Fields                        | Purpose           |
| --------------------- | ----------------------------- | ----------------- |
| `photo_status_vis`    | `status, visibility`          | Library feed      |
| `photo_ch_status_vis` | `channel, status, visibility` | Channel photo tab |
| `photo_created_at`    | `created_at`                  | Default sort      |


---

## 8. What Is & Isn't Stored as a Vector


| Data                         | Storage type              | Notes                                                                                                                                        |
| ---------------------------- | ------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| `VideoFrame.clip_embedding`  | `vector(512)` in Postgres | HNSW-indexed; null for frames with no CLIP run                                                                                               |
| `Photo.clip_embedding`       | `vector(512)` in Postgres | Same as above                                                                                                                                |
| `DetectedFace.embedding`     | `TextField` (JSON string) | ArcFace 512-d embedding; used for cosine similarity in Python during clustering — no SQL ANN search needed (clustering runs once per ingest) |
| `FaceIdentity.ref_embedding` | `TextField` (JSON string) | Running average of all face embeddings for this identity; also used in Python                                                                |
| `VideoFrame.labels`          | `TextField`               | Comma-separated YOLO class strings; FTS + trgm indexed                                                                                       |
| `VideoFrame.description`     | `TextField`               | BLIP/Florence-2 caption; FTS + trgm indexed                                                                                                  |
| `VideoSegment.text`          | `TextField`               | Whisper transcript; FTS + trgm indexed                                                                                                       |


**Why are face embeddings still JSON strings?**
Face clustering runs in-Python during ingest (scipy-style greedy merging) and only touches ~dozens of clusters per video. There's no need for SQL ANN on face data; the overhead of a pgvector column would not pay back. CLIP embeddings are the opposite — they need fast runtime search across potentially millions of frames, so pgvector HNSW is essential.

---

## 9. Search Request Flow

```
User types query q
         │
         ▼
player_page() view
         │
         ├─► Video title/desc/tags  ── FTS (GIN)  ╮
         │                        -─ fuzzy (trgm) ╯  → main grid
         │
         ├─► VideoSegment.text  ── FTS + fuzzy → speech tab
         │   (cap: 500 segments, filters applied)
         │
         ├─► VideoFrame.labels    ── FTS + fuzzy ╮
         │   VideoFrame.description ── FTS+fuzzy ╯ → scenes tab
         │   (cap: 400 frames each, filters applied)
         │
         ├─► CLIP semantic  ── HNSW <=> query_vec → scenes tab
         │   (cap: 250 frames, filters applied, semantic=1 only)
         │
         ├─► DetectedFace.identity.name ── icontains + fuzzy → people tab
         │
         ├─► Photo.title/tags/labels/scene_description ── FTS + fuzzy → photos tab
         │   Photo.clip_embedding ── HNSW (semantic=1) → photos tab
         │
         ├─► NamedPlace.name / description ── icontains → places tab + suggest API
         │
         └─► Channel.name, Playlist.title ── fuzzy → channels/playlists tab
```

---

## 10. Key Settings

```python
# settings.py
FRAME_ANALYSIS_ENABLED    = True          # enable YOLO + BLIP + CLIP + InsightFace
SCENE_DESCRIPTION_ENABLED = True          # enable BLIP / Florence-2 captioning
SCENE_CAPTION_MODEL       = 'blip'        # 'blip' | 'florence2'
CLIP_ENABLED              = True          # enable CLIP embeddings
CLIP_SIMILARITY_THRESHOLD = 0.20          # cosine similarity floor (0–1)
FACE_RECOGNITION_ENABLED  = True          # enable InsightFace
FRAME_INTERVAL_SECONDS    = 5             # sample one frame every N seconds
FUZZY_SEARCH_ENABLED      = True          # enable pg_trgm fuzzy search
FUZZY_SEARCH_SIMILARITY_THRESHOLD = 0.22  # trigram similarity threshold (0–1)
YOLO_MODEL                = 'yolov8n'    # YOLO model name (without .pt)
```

---

## 11. Geolocation & named places (application layer)

Location features are mostly **relational and HTTP**, not extra vector indexes:

- **`NamedPlace`** — one row per curated site (`slug` unique; `latitude`, `longitude`, `radius_meters`).
- **`Video` / `Photo`** — optional `latitude`, `longitude`, optional `named_place_id` FK; B-tree indexes on coordinates support map queries and proximity assignment.
- **Map UI** — Leaflet on `/media/map/` and `/named-places/`; marker payloads from `/api/media/map-markers/` (combined media) and place CRUD via `/api/named-places/`.
- **Search** — `NamedPlace` name/description matches feed the main search **Places** tab and `/api/search/suggest/` (deduped by slug with human-readable disambiguation).

