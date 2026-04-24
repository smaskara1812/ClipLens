# Database performance & scale plan (PostgreSQL)

**Status:** planning — pre-production  
**Constraint:** **No user-visible or API behavior change** unless explicitly called out in an optional section. This document describes **internal** schema and operations changes that keep semantic search, CLIP, face flow, and all endpoints producing the same results (within normal floating-point limits).

**Related:** [PERFORMANCE.md](PERFORMANCE.md) (broader app performance guide; some DB details there may be outdated as this project moves from MySQL to PostgreSQL in practice—verify against actual migrations).

---

## 1. Why this plan exists

Workloads observed in development (`pg_stat_statements` / `EXPLAIN`):

- **Bulk `INSERT` into `videos_videoframe` with `clip_embedding`** is dominated by **HNSW index maintenance** on `vf_clip_hnsw`, not by the row data itself. A small synthetic insert (200 rows) spent ~**247 ms** with ~**170k buffer hits**; almost all of that is the vector index, not the heap insert.
- **HNSW index size** on a small table can be **hundreds of times larger** than the base table’s heap, because the graph is stored separately and each insert walks the graph.
- **Trigram and other indexes** may show `idx_scan = 0` in dev if features are not exercised; that does *not* prove they are unused in a future product build—dropping them requires evidence from **real** query traffic.

The goal is a structure that **scales to millions of rows and multi-TB media** (your stated 10 TB media target) without changing what the app *does*.

---

## 2. Principles (functionality preserved)

| Principle | Meaning |
|-----------|---------|
| **Equivalence** | Same inputs → same business outputs: search rankings, face clustering decisions (given same model outputs), API JSON shapes, permissions. |
| **Schema is not the product** | Moving a column to another table, or tuning Postgres, is allowed if every code path is updated to preserve behavior. |
| **Numerical prudence** | Changing `vector` → `halfvec` changes stored values slightly. That is a **separate, optional** phase; default plan keeps `vector(512)` in new tables to minimize drift. |
| **Evidence before drops** | Do not remove indexes (including trigram) until production-like workloads or integration tests show they are truly unused, or a feature is formally deprecated. |

---

## 3. Diagnosis summary (what is actually slow)

1. **Vector + HNSW on wide rows**  
   `VideoFrame` (and `Photo`) keep `clip_embedding` on the same heap row as labels, description, etc. Every insert/update that sets the embedding pays **HNSW + TOAST** cost. Non-search queries that read frames often **defer** the embedding, which is a sign the hot path should not be tied to the fat column.

2. **Index maintenance dominates bulk writes**  
   Reducing `batch_size` alone does not fix the root cause; the cost is **per-row index work**. Fewer rows per statement can even increase commit/WAL overhead.

3. **Deletes and bloat**  
   Many small `DELETE … WHERE id IN (…)` patterns increase round trips. Consolidating delete scope is a **behavior-neutral** win.

4. **Future scale**  
   At millions of `VideoFrame` / `DetectedFace` rows, **write amplification** from over-indexing and **vacuum** pressure become as important as single-query `SELECT` time.

---

## 4. Recommended plan (phased, behavior-neutral by default)

### Phase 1 — **Separate embedding storage for CLIP (VideoFrame, Photo)** — *high value*

**Change**

- Add **one-to-one** child tables, for example:
  - `VideoFrameClipEmbedding` / `VideoFrameEmbedding`: `frame_id` (PK, FK) + `clip_embedding` `vector(512)` (or `halfvec` only in optional Phase 1b).
  - `PhotoClipEmbedding` / `PhotoEmbedding`: `photo_id` (PK, FK) + `clip_embedding` same type.
- Remove `clip_embedding` from `videos_videoframe` and the photo table (after data copy and code updates).
- Recreate **HNSW** on the **child** table (same `vector_cosine_ops` / `halfvec_cosine_ops` as today), with an optional `WHERE … IS NOT NULL` only if the column is `NOT NULL` in the new table (typically every row in the child table *has* an embedding).

**Application updates (behavior-neutral)**

- **Writes:** Anywhere that sets `frame.clip_embedding` or `photo.clip_embedding`, switch to `get_or_create` / `update` on the child row.
- **Reads:** `CosineDistance('clip_embedding', …)` on `VideoFrame` becomes `CosineDistance('clip_embedding', …)` on a queryset joined to the child, or on the child’s queryset with `select_related('frame'|'photo'…')`, producing the same ordering and filter semantics.
- **List/detail views** that use `.defer('clip_embedding', …)`: can defer fields on the **parent** only; the child row is not loaded unless the view asks for it (same as today’s intent).
- **Management commands** (`run_clip`, seed scripts, tasks): point at the new models; outputs of CLIP and search stay the same.

**Benefits**

- **Cheaper non-embedding frame/photo work:** Inserts/updates of labels, description, YOLO results no longer touch the HNSW structure when embeddings are absent or updated separately.
- **Smaller, clearer hot path for search:** HNSW only covers rows that actually have CLIP vectors (child table).
- **Clearer path to ops tuning:** Reindex, `CONCURRENTLY`, and monitoring apply to a small, dedicated table.

**Risk / mitigation**

- **Migration effort** in pre-prod: acceptable; in prod later: use batched backfill, `CREATE INDEX CONCURRENTLY`, and brief maintenance windows for cutover.
- **Test coverage:** Add regression tests for CLIP search ordering and a few known queries (same threshold, same top-K).

---

### Phase 1b (optional) — **halfvec(512) for CLIP** — *storage & CPU, “almost identical” results*

**Change:** Store CLIP in `halfvec(512)` instead of `vector(512)`.

**Functional impact:** Cosine orderings are **nearly** always the same; edge cases can differ at rank boundaries. This is *not* strictly identical to `float32` in all cases.

**When to do it:** After Phase 1 is stable, behind a feature flag or A/B, if you accept negligible ranking drift for half the index size and faster HNSW maintenance.

**Benefit:** Roughly **~2×** smaller stored vectors; faster inserts/updates; less memory for the HNSW build.

---

### Phase 2 — **Query and delete hygiene** — *medium value, no feature change*

| Change | Required work | Benefit |
|--------|----------------|--------|
| Replace repeated `DELETE … IN ($1..$100)` loops with a single `filter(...).delete()` or one SQL `DELETE` per job | Find call sites in tasks / re-analysis | Fewer round trips, less lock churn |
| **Partitioning** | Not yet; revisit when `WatchHistory` (or similar append-only fact tables) hits millions of rows | Time-based partitions allow cheap retention (`DROP PARTITION`) without changing app semantics if queries always bound by time |
| **Connection pooling (PgBouncer, transaction mode)** | Infra + Django `CONN_MAX_AGE` | Stabilizes many short Celery workers against Postgres |
| **Postgres: `max_wal_size`, `checkpoint_timeout`, `maintenance_work_mem`, `track_io_timing`** | Config only | Smoother bulk loads and easier diagnosis |

---

### Phase 3 — **Index audit (conservative)** — *do not over-drop*

| Action | How |
|--------|-----|
| **Unused indexes (production evidence)** | Use `pg_stat_user_indexes` when the app is exercised end-to-end; consider `pg_stat_statements` for which queries use `%`, `gin`, or vector ops. |
| **Remove only when** | `idx_scan` stays 0 over a representative window *and* no migration/feature depends on the index, **or** a duplicate index is proven redundant with the same column order and predicate. |
| **Trigram (GIN) indexes** on title/description/labels | Keep until full-text or fuzzy search behavior is either implemented with another mechanism or formally removed; dropping early risks **changing** search behavior when you enable a feature. |

**Benefit:** Less write amplification and smaller backups, without surprise behavior changes.

---

### Phase 4 — **Not in “no behavior change” scope (future, explicit opt-in)**

These **do** change implementation details and sometimes semantics; track as separate product/tech decisions:

- **Moving `DetectedFace.embedding` from JSON `TextField` to `vector` / `halfvec`:** Same *idea* of similarity, but you must prove parity with the current Python/numpy path (tolerance thresholds, clustering). Not required for the CLIP split.
- **Soft deletes** instead of `DELETE`:** Changes queries (filter `is_deleted`, retention jobs). Not equivalent unless all reads are updated.
- **Async ingestion queue** (Kafka, etc.): Operationally good; not a database-only change.

---

## 5. What this plan does **not** claim

- It does not replace the need for **media tier** planning (10 TB of objects, CDN, object storage, transcoding) — that lives beside Postgres tuning.
- It does not guarantee `idx_scan = 0` in dev means an index is useless in production.
- It does not recommend shrinking Django `batch_size` blindly; tune after measuring WAL and HNSW cost **after** Phase 1.

---

## 6. Success criteria (non-functional)

- **P95/P99** time for `VideoFrame` bulk insert (with embeddings) drops materially vs baseline at the same row count (target: **3–10×** after Phase 1, environment-dependent).
- **CLIP and photo semantic search** return the same result sets for a fixed test corpus (or within agreed float tolerance if Phase 1b is used).
- **No new N+1** query issues on list pages (use `select_related` / `Prefetch` as needed).

---

## 7. Suggested order of execution

1. **Phase 1** (VideoFrame + Photo embedding tables + HNSW on children + full regression tests).  
2. **Phase 2** (deletes, pooling, Postgres config) in parallel with staging tests.  
3. **Phase 3** (index audit with real traffic) after a beta or internal dogfood period.  
4. **Phase 1b** (halfvec) only if you accept tiny ranking differences or validate with a golden set.  
5. **Phase 4** only with explicit spec sign-off.

---

## 8. Document history

| Date | Note |
|------|------|
| 2026-04-23 | Initial plan: pre-production, behavior-neutral constraint, scale target aligned with multi-TB media roadmap. |
