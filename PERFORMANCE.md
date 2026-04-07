# ClipStream — Performance & Scalability Guide

> Target: up to **2 TB** of video + metadata, serving hundreds of concurrent users.

---

## Table of Contents

1. [Current Architecture Overview](#1-current-architecture-overview)
2. [Optimizations Already Implemented](#2-optimizations-already-implemented)
   - [2.1 Database Indexes](#21-database-indexes)
   - [2.2 Redis Caching](#22-redis-caching)
   - [2.3 Feed Pagination & Infinite Scroll](#23-feed-pagination--infinite-scroll)
   - [2.4 Query Efficiency](#24-query-efficiency)
   - [2.5 AI/Search Query Bounds](#25-aisearch-query-bounds)
3. [Applying the Migration](#3-applying-the-migration)
4. [Configuration Reference](#4-configuration-reference)
5. [Future Optimizations — Roadmap](#5-future-optimizations--roadmap)
   - [Phase 1 — Near Term (no new services)](#phase-1--near-term-no-new-services)
   - [Phase 2 — Infrastructure (new services)](#phase-2--infrastructure-new-services)
   - [Phase 3 — Scale-out (dedicated infra)](#phase-3--scale-out-dedicated-infra)
6. [Elasticsearch Setup Guide](#6-elasticsearch-setup-guide)
7. [pgvector / Vector Search Guide](#7-pgvector--vector-search-guide)
8. [CDN Setup Guide](#8-cdn-setup-guide)
9. [Monitoring & Query Profiling](#9-monitoring--query-profiling)
10. [Scaling Checklist](#10-scaling-checklist)

---

## 1. Current Architecture Overview

```
Browser
  │
  ├── Django (Gunicorn/Uvicorn)
  │     ├── Videos app (views, serializers, models)
  │     ├── Celery tasks → HLS encoding, AI analysis, captions
  │     └── WhiteNoise → static files
  │
  ├── MySQL (primary datastore)
  ├── Redis (Celery broker DB 0 + Django cache DB 1)
  └── Media files → local disk (or S3-compatible)
```

**Key data volumes at 2 TB:**
- Videos: 10,000–50,000+ files at typical 50–200 MB each
- `VideoFrame` rows: ~500 frames/video × 50,000 videos = **25 million rows**
- `VideoSegment` (transcripts): ~200 rows/video × 50,000 = **10 million rows**
- `DetectedFace`: potentially **50–100 million rows**

---

## 2. Optimizations Already Implemented

### 2.1 Database Indexes

Migration `0015_add_db_indexes` adds 11 B-tree indexes. Run `python manage.py migrate` to apply.

| Table | Index fields | Purpose |
|-------|-------------|---------|
| `videos_video` | `(status, visibility)` | Main feed filter — covers the most common WHERE clause |
| `videos_video` | `(channel_id, status, visibility)` | Channel page filter |
| `videos_video` | `(category_id, status, visibility)` | Category page filter |
| `videos_video` | `(created_at)` | Default sort (newest first) |
| `videos_video` | `(views_count)` | Sort by popularity |
| `videos_video` | `(duration)` | Short/medium/long filter |
| `videos_faceidentity` | `(name)` | People search by name |
| `videos_faceidentity` | `(is_auto_named)` | Separate named vs auto identities |
| `videos_watchhistory` | `(user_id, watched_at)` | History page per-user sort |
| `videos_savedvideo` | `(user_id, saved_at)` | Saved page per-user sort |
| `videos_notification` | `(recipient_id, is_read, created_at)` | Unread count + notification list |

Pre-existing indexes (from earlier migrations):

| Table | Index |
|-------|-------|
| `videos_videosegment` | `(video_id, start_seconds)` |
| `videos_videoframe` | `(video_id, timestamp)` |
| `videos_detectedface` | `(video_id, timestamp)`, `(identity_id)` |

**What this prevents:** Without these indexes, every page load was a full table scan. At 50,000 videos a `SELECT WHERE status='ready' AND visibility='public' ORDER BY created_at DESC` with no index reads every row. With the composite index it becomes a single index range scan.

---

### 2.2 Redis Caching

Redis DB 1 is used for Django's cache (DB 0 is Celery's broker). Configured in `settings.py`.

| Cache key | TTL | Content | Invalidated when |
|-----------|-----|---------|-----------------|
| `fs:all_categories` | 1 hour | List of all Category objects | Category created / renamed / deleted |
| `fs:subs_{user_pk}` | 5 min | Subscribed Channel objects for user | User subscribes / unsubscribes |
| `fs:unread_{user_pk}` | 1 min | Unread notification count | Notifications marked read / new notification created |

**Impact:** Every page load previously hit the DB for categories (renders the filter bar) and unread notification count (renders the badge in the sidebar). With 100 concurrent users, that's 200+ redundant queries per second eliminated.

**To change TTLs**, edit `settings.py`:

```python
CACHE_TTL_CATEGORIES    = 60 * 60   # seconds — currently 1 hour
CACHE_TTL_SUBSCRIPTIONS = 60 * 5    # currently 5 minutes
CACHE_TTL_UNREAD_COUNT  = 60        # currently 1 minute
CACHE_TTL_CHANNEL_INFO  = 60 * 10   # currently 10 minutes (reserved for future use)
```

---

### 2.3 Feed Pagination & Infinite Scroll

**Before:** `player_page` loaded every matching video into memory, serialized it all, and sent it to the template. At 10,000 videos this was ~40 MB of data per page load.

**After:**
- Server renders the **first 24 videos** only (using `only()` + `select_related`)
- Template includes an `IntersectionObserver` sentinel at the bottom of the grid
- When the sentinel enters the viewport, JavaScript fetches `/api/videos/?page=2&...` and appends cards
- The API preserves all active filter state (channel, category, duration, date, sort, search query)
- Each API response uses the lightweight `VideoFeedSerializer` (no `description`, `hls_url`, `available_qualities` etc.)

**Page size:** 24 videos per batch (configurable via `page_size` API param, max 48).

**No `COUNT(*)` queries:** The feed uses a "fetch N+1, check if N+1 exists" pattern to determine `has_more` without an expensive count.

---

### 2.4 Query Efficiency

**`only()` on list views** — instead of `SELECT *`, list pages fetch only the columns needed for card rendering:

```python
videos.only(
    'id', 'title', 'duration', 'views_count', 'status',
    'created_at', 'channel_id',
)
```

This avoids loading `description` (can be kilobytes), `tags`, `hls_path`, `source_path`, `available_qualities`, `processing_error`, etc.

**`VideoFeedSerializer`** — a lightweight serializer used exclusively for feed/infinite scroll API responses. It omits `description`, `hls_url`, `hls_path`, `available_qualities`, `qualities_list`, `likes_count`, `comments_count`, `uploaded_by`.

**`select_related`** on all list views — eliminates N+1 queries for `channel` and `category` data on every video card.

---

### 2.5 AI/Search Query Bounds

**YOLO label search** — defers `clip_embedding` (up to ~8 KB of JSON per row) and `description`:
```python
.defer('clip_embedding', 'description')
```

**Scene description search** — defers `clip_embedding` and `labels`:
```python
.defer('clip_embedding', 'labels')
```

**CLIP semantic search** — hard-capped at **3,000 most-recent frames**. Before this cap, CLIP scanned every frame with an embedding — at 25 million rows this would take minutes. The cap prioritises recent content and keeps response time under ~2 seconds:
```python
.order_by('-video__created_at')[:3000]
```

**Speech search** — limited to 200 matching `VideoSegment` rows per query.

**Scene/YOLO/people matches** — capped at 20 moments per video (raised from 5, with a "Show more" collapse UI).

---

## 3. Applying the Migration

```bash
# Activate your virtualenv
source venv/bin/activate

# Apply the index migration (safe — only adds indexes, no schema changes)
python manage.py migrate videos 0015_add_db_indexes

# Verify
python manage.py showmigrations videos
```

> Adding indexes on large tables is a non-blocking DDL in MySQL 8+ (`ALGORITHM=INPLACE, LOCK=NONE`). It runs online — no downtime needed. On a table with millions of rows it may take several minutes. Monitor progress with:
>
> ```sql
> SELECT * FROM information_schema.INNODB_METRICS
> WHERE NAME LIKE '%alter%';
> ```

---

## 4. Configuration Reference

All toggleable in `.env` or `settings.py`:

```bash
# Redis
REDIS_URL=redis://127.0.0.1:6379/1           # cache DB (keep separate from Celery's /0)

# AI features (set false to skip on low-resource dev machines)
FRAME_ANALYSIS_ENABLED=true
FACE_RECOGNITION_ENABLED=true
CLIP_ENABLED=true
CLIP_SIMILARITY_THRESHOLD=0.20               # lower = more results, higher = stricter

# Whisper transcription
WHISPER_MODEL_SIZE=base                      # tiny/base/small/medium/large
WHISPER_DEVICE=cpu                           # cpu or cuda

# HLS quality ladder
HLS_MULTI_QUALITY=true                       # false = single quality (faster ingest)
```

---

## 5. Future Optimizations — Roadmap

### Phase 1 — Near Term (no new services)

#### 1a. MySQL FULLTEXT indexes for text search

The current `icontains` (i.e. `LIKE '%query%'`) on `VideoFrame.labels`, `VideoFrame.description`, and `VideoSegment.text` **cannot use B-tree indexes** — they require a full table scan regardless of indexing. At 25 million `VideoFrame` rows, each search takes several seconds.

**Fix:** Add MySQL FULLTEXT indexes and switch to `MATCH() AGAINST()` queries.

```python
# In a new migration:
from django.db import migrations

class Migration(migrations.Migration):
    operations = [
        migrations.RunSQL(
            "ALTER TABLE videos_videoframe ADD FULLTEXT INDEX vf_labels_ft (labels);",
            reverse_sql="ALTER TABLE videos_videoframe DROP INDEX vf_labels_ft;",
        ),
        migrations.RunSQL(
            "ALTER TABLE videos_videoframe ADD FULLTEXT INDEX vf_desc_ft (description);",
            reverse_sql="ALTER TABLE videos_videoframe DROP INDEX vf_desc_ft;",
        ),
        migrations.RunSQL(
            "ALTER TABLE videos_videosegment ADD FULLTEXT INDEX vs_text_ft (text);",
            reverse_sql="ALTER TABLE videos_videosegment DROP INDEX vs_text_ft;",
        ),
        migrations.RunSQL(
            "ALTER TABLE videos_video ADD FULLTEXT INDEX v_title_desc_ft (title, description, tags);",
            reverse_sql="ALTER TABLE videos_video DROP INDEX v_title_desc_ft;",
        ),
    ]
```

Then in `views.py`, replace `icontains` with raw FULLTEXT queries:

```python
from django.db.models import Q
from django.db import connection

# Instead of: VideoFrame.objects.filter(labels__icontains=q)
# Use:
VideoFrame.objects.filter(
    video__visibility='public', video__status='ready'
).extra(where=["MATCH(labels) AGAINST (%s IN BOOLEAN MODE)"], params=[q])
```

**Gains:** FULLTEXT search in MySQL is 100–1000× faster than `LIKE '%q%'` on large tables.

#### 1b. Deferred / background thumbnail generation

Ensure all thumbnails are pre-generated at upload time (already done via Celery). Do not generate on-the-fly in requests.

#### 1c. `select_related` on watch/history/saved pages

Ensure the history, saved, and playlist detail pages use:
```python
WatchHistory.objects.filter(user=request.user).select_related('video__channel').order_by('-watched_at')[:100]
```
Currently unbounded — add a `:100` or `:200` hard cap.

#### 1d. Paginate the People (faces) page server-side fully

The current faces page paginates in Python — move the window entirely to the DB:
```python
FaceIdentity.objects.filter(...).only('id', 'name', 'thumbnail_face_id', 'is_auto_named')
```

#### 1e. Cache channel pages

Channel pages load all channel videos on every request. Add a short cache:
```python
@cache_page(60 * 2)  # 2 minutes
def channel_page(request, slug): ...
```

---

### Phase 2 — Infrastructure (new services)

#### 2a. Elasticsearch for full-text search

**When to add:** When you have 100,000+ videos or 10M+ VideoFrame rows and text search feels slow even with FULLTEXT indexes. Elasticsearch provides relevance ranking, fuzzy matching, and faceted search that MySQL cannot.

**Stack:** Elasticsearch 8.x + `django-elasticsearch-dsl`

**What to index:**
- `Video`: title, description, tags, channel name
- `VideoSegment`: text, video_id, start_seconds
- `VideoFrame`: labels, description, video_id, timestamp

See [Section 6](#6-elasticsearch-setup-guide) for full setup instructions.

#### 2b. pgvector for CLIP semantic search

**When to add:** CLIP semantic search is currently a Python loop comparing text embeddings against up to 3,000 frame embeddings. At 25M frames this is unavoidable even with the cap.

The solution is a proper **vector index** (HNSW or IVFFlat) that finds approximate nearest neighbours in O(log n) instead of O(n).

Options:
- **pgvector** (if switching to PostgreSQL) — native Django support via `pgvector` Python package
- **Qdrant** — standalone vector database, self-hosted or cloud
- **Weaviate** — similar to Qdrant with multimodal support
- **Pinecone** — fully managed, serverless pricing

See [Section 7](#7-pgvector--vector-search-guide) for pgvector setup instructions.

#### 2c. CDN for media (thumbnails + HLS)

**When to add:** As soon as the server is under any meaningful load. Media delivery is the single biggest bandwidth cost and latency contributor.

**What to serve via CDN:**
- Thumbnails (`/media/thumbnails/`)
- HLS master playlists and segments (`/media/hls/`)
- Channel avatars and banners

See [Section 8](#8-cdn-setup-guide) for setup instructions.

#### 2d. django-redis with compression

Replace the built-in `RedisCache` with `django-redis` for:
- Per-key compression (reduces Redis memory for large cached objects)
- Connection pooling
- `delete_pattern()` for bulk cache invalidation

```bash
pip install django-redis
```

```python
CACHES = {
    'default': {
        'BACKEND': 'django_redis.cache.RedisCache',
        'LOCATION': 'redis://127.0.0.1:6379/1',
        'OPTIONS': {
            'CLIENT_CLASS': 'django_redis.client.DefaultClient',
            'COMPRESSOR': 'django_redis.compressors.zlib.ZlibCompressor',
            'IGNORE_EXCEPTIONS': True,   # degrade gracefully if Redis is down
        },
        'KEY_PREFIX': 'fs',
    }
}
```

---

### Phase 3 — Scale-out (dedicated infra)

#### 3a. Horizontal Celery workers

Celery already handles HLS encoding, AI analysis, caption generation. As video volume grows, add more workers:

```bash
# Separate queues for different task types
celery -A ClipStream worker -Q processing -c 2 --hostname=encoder@%h
celery -A ClipStream worker -Q frame_analysis -c 1 --hostname=ai@%h
celery -A ClipStream worker -Q captions -c 2 --hostname=captions@%h
```

#### 3b. MySQL read replica

Route all `SELECT` queries to a read replica using Django's database router:

```python
# settings.py
DATABASES = {
    'default': { ... },   # writes
    'replica': { ... },   # reads
}

# routers.py
class PrimaryReplicaRouter:
    def db_for_read(self, model, **hints):
        return 'replica'
    def db_for_write(self, model, **hints):
        return 'default'
    def allow_relation(self, obj1, obj2, **hints):
        return True
    def allow_migrate(self, db, app_label, **hints):
        return db == 'default'
```

#### 3c. Object storage for media (S3 / MinIO)

Replace local disk storage with S3-compatible object storage using `django-storages`:

```bash
pip install django-storages boto3
```

```python
# settings.py
DEFAULT_FILE_STORAGE = 'storages.backends.s3boto3.S3Boto3Storage'
AWS_STORAGE_BUCKET_NAME = 'ClipStream-media'
AWS_S3_REGION_NAME = 'us-east-1'
AWS_S3_CUSTOM_DOMAIN = 'cdn.yourdomain.com'  # CloudFront distribution
```

#### 3d. Gunicorn + async workers

For HLS playlist requests (high concurrency, fast I/O):

```bash
# Use gevent workers for I/O-bound requests
gunicorn ClipStream.wsgi:application \
    --worker-class gevent \
    --workers 4 \
    --worker-connections 1000 \
    --bind 0.0.0.0:8000
```

---

## 6. Elasticsearch Setup Guide

### Install

```bash
pip install django-elasticsearch-dsl elasticsearch==8.*
```

```python
# settings.py
INSTALLED_APPS += ['django_elasticsearch_dsl']

ELASTICSEARCH_DSL = {
    'default': {'hosts': 'http://localhost:9200'},
}
```

### Define documents

```python
# videos/documents.py
from django_elasticsearch_dsl import Document, fields
from django_elasticsearch_dsl.registries import registry
from .models import Video, VideoSegment, VideoFrame

@registry.register_document
class VideoDocument(Document):
    channel_name = fields.TextField()

    class Index:
        name = 'videos'
        settings = {'number_of_shards': 1, 'number_of_replicas': 0}

    class Django:
        model = Video
        fields = ['title', 'description', 'tags']

    def prepare_channel_name(self, instance):
        return instance.channel.name if instance.channel else ''


@registry.register_document
class SegmentDocument(Document):
    class Index:
        name = 'segments'
        settings = {'number_of_shards': 2, 'number_of_replicas': 0}

    class Django:
        model = VideoSegment
        fields = ['text', 'video', 'start_seconds', 'end_seconds']


@registry.register_document
class FrameDocument(Document):
    class Index:
        name = 'frames'
        settings = {'number_of_shards': 2, 'number_of_replicas': 0}

    class Django:
        model = VideoFrame
        fields = ['labels', 'description', 'video', 'timestamp']
```

### Build the index

```bash
# Initial index build (run once; re-run after bulk imports)
python manage.py search_index --rebuild
```

### Query in views

```python
from .documents import VideoDocument, SegmentDocument, FrameDocument
from elasticsearch_dsl import Q as ESQ

# Replace ORM icontains with ES query
results = VideoDocument.search().query(
    ESQ('multi_match', query=q, fields=['title^3', 'description', 'tags', 'channel_name'])
)
video_ids = [h.meta.id for h in results[:50]]
videos = Video.objects.filter(pk__in=video_ids)

# Speech search
seg_results = SegmentDocument.search().query(
    ESQ('match', text={'query': q, 'fuzziness': 'AUTO'})
)[:200]
```

### Keep index in sync

Signal receivers automatically update the index when models are saved/deleted via `django-elasticsearch-dsl`. For bulk operations (management commands), call:

```python
from .documents import VideoDocument
VideoDocument().update(video_instance)
```

---

## 7. pgvector / Vector Search Guide

> This requires switching from MySQL to **PostgreSQL**. If you're staying on MySQL, use Qdrant instead (see below).

### PostgreSQL + pgvector

```bash
# Install pgvector extension
psql -c "CREATE EXTENSION vector;"

pip install pgvector
```

```python
# models.py
from pgvector.django import VectorField

class VideoFrame(models.Model):
    # Replace the TextField clip_embedding with:
    clip_embedding_vec = VectorField(dimensions=512, null=True, blank=True)
    # Keep the old TextField for migration compatibility during transition
```

```python
# Migration
from pgvector.django import VectorField, IvfflatIndex

class Migration(migrations.Migration):
    operations = [
        migrations.AddField(
            model_name='videoframe',
            name='clip_embedding_vec',
            field=VectorField(dimensions=512, null=True, blank=True),
        ),
        migrations.AddIndex(
            model_name='videoframe',
            index=IvfflatIndex(
                fields=['clip_embedding_vec'],
                name='vf_clip_ivfflat',
                lists=100,  # sqrt(num_rows / 1000) is a good starting point
            ),
        ),
    ]
```

```python
# views.py — replace Python cosine loop with DB vector search
from pgvector.django import CosineDistance

frames = VideoFrame.objects.filter(
    video__visibility='public',
    video__status='ready',
    clip_embedding_vec__isnull=False,
).order_by(
    CosineDistance('clip_embedding_vec', _txt_vec)
)[:50]
```

### Qdrant (MySQL-compatible alternative)

```bash
# Run Qdrant
docker run -p 6333:6333 qdrant/qdrant

pip install qdrant-client
```

```python
# utils/vector_store.py
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct

client = QdrantClient(host='localhost', port=6333)
COLLECTION = 'clip_frames'

def ensure_collection():
    client.recreate_collection(
        collection_name=COLLECTION,
        vectors_config=VectorParams(size=512, distance=Distance.COSINE),
    )

def upsert_frame(frame_id: int, embedding: list[float], payload: dict):
    client.upsert(collection_name=COLLECTION, points=[
        PointStruct(id=frame_id, vector=embedding, payload=payload)
    ])

def search_frames(query_vec: list[float], top_k=50) -> list[int]:
    results = client.search(
        collection_name=COLLECTION,
        query_vector=query_vec,
        limit=top_k,
    )
    return [r.id for r in results]  # VideoFrame PKs
```

```python
# views.py — replace CLIP loop with Qdrant search
from utils.vector_store import search_frames

frame_ids = search_frames(_txt_vec, top_k=50)
frames = VideoFrame.objects.filter(pk__in=frame_ids).select_related('video', 'video__channel')
```

---

## 8. CDN Setup Guide

### Option A — Cloudflare (simplest, free tier available)

1. Point your domain's DNS to Cloudflare
2. In Cloudflare dashboard → **Caching** → **Cache Rules**:
   - Cache `/media/thumbnails/*` for 7 days
   - Cache `/media/hls/*.m3u8` for 30 seconds (playlists change during live encoding)
   - Cache `/media/hls/*.ts` (HLS segments) for 365 days (immutable once written)
3. In `settings.py`, set `MEDIA_URL` to your Cloudflare domain:
   ```python
   MEDIA_URL = 'https://cdn.yourdomain.com/media/'
   ```

### Option B — AWS CloudFront + S3

```python
# settings.py
DEFAULT_FILE_STORAGE = 'storages.backends.s3boto3.S3Boto3Storage'
AWS_STORAGE_BUCKET_NAME = 'ClipStream-media'
AWS_S3_CUSTOM_DOMAIN    = f'{AWS_STORAGE_BUCKET_NAME}.s3.amazonaws.com'
MEDIA_URL               = f'https://{AWS_CF_DISTRIBUTION_DOMAIN}/media/'

AWS_S3_OBJECT_PARAMETERS = {
    'thumbnails/': {'CacheControl': 'max-age=604800'},   # 7 days
    'hls/':        {'CacheControl': 'max-age=31536000'},  # 1 year (segments are immutable)
}
```

### HLS segment caching note

HLS `.ts` segments are **write-once** — once written by FFmpeg they never change. Set a very long cache TTL (1 year). Master playlists (`.m3u8`) change during multi-quality processing — keep their TTL short (30–60 seconds) until processing is complete.

---

## 9. Monitoring & Query Profiling

### Django Debug Toolbar (development)

```bash
pip install django-debug-toolbar
```

```python
# settings.py (dev only)
if DEBUG:
    INSTALLED_APPS += ['debug_toolbar']
    MIDDLEWARE.insert(0, 'debug_toolbar.middleware.DebugToolbarMiddleware')
    INTERNAL_IPS = ['127.0.0.1']
```

Shows exact SQL queries, query count, and timing for every request. Aim for **< 20 queries per page load**.

### Django Silk (production profiling)

```bash
pip install django-silk
```

Records query timings and slow queries in production without impacting users.

### MySQL slow query log

```sql
-- Enable in MySQL
SET GLOBAL slow_query_log = 'ON';
SET GLOBAL long_query_time = 0.1;   -- log queries > 100ms
SET GLOBAL slow_query_log_file = '/var/log/mysql/slow.log';
```

Then run `pt-query-digest /var/log/mysql/slow.log` (from Percona Toolkit) to find the worst offenders.

### Redis cache hit rate

```bash
redis-cli info stats | grep -E "keyspace_hits|keyspace_misses"
# Target: > 80% hit rate
```

---

## 10. Scaling Checklist

Use this checklist as data volume grows.

### At 5,000 videos / ~250 GB

- [x] Database indexes applied (`0015_add_db_indexes`)
- [x] Feed paginated (24 per page, infinite scroll)
- [x] Redis cache for categories, subscriptions, unread count
- [x] `only()` / `defer()` on list queries
- [x] CLIP search capped at 3,000 frames
- [ ] MySQL FULLTEXT indexes on `VideoFrame.labels`, `VideoFrame.description`, `VideoSegment.text`
- [ ] `django-debug-toolbar` used in dev to verify query count < 20 per page

### At 20,000 videos / ~1 TB

- [ ] CDN for thumbnails and HLS segments (Cloudflare or CloudFront)
- [ ] Object storage (S3/MinIO) for media files — local disk becomes a bottleneck
- [ ] Dedicated Celery workers per queue (`processing`, `frame_analysis`, `captions`)
- [ ] Elasticsearch for `VideoSegment` and `VideoFrame` text search
- [ ] Read replica for MySQL (route SELECT to replica)
- [ ] `django-redis` with connection pooling replacing built-in Redis cache backend

### At 50,000 videos / ~2 TB

- [ ] Vector database (Qdrant or pgvector) for CLIP semantic search
- [ ] Switch to PostgreSQL + pgvector (if vector search is critical)
- [ ] Horizontal Django instances behind a load balancer (nginx)
- [ ] Celery autoscaling or Kubernetes Jobs for encoding spikes
- [ ] Separate analytics DB or ClickHouse for `WatchTimeEntry` aggregations
- [ ] Consider pre-computing feed rankings (background task updates a `feed_score` column)

---

*Last updated: April 2026*
