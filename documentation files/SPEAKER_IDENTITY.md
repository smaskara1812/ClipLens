# Speaker Identity & Voice Recognition

This document describes **ClipLens**’s speaker diarization, per-segment speaker assignment, cross-video speaker matching (voice embeddings), and integration with face identities. It is separate from the main architecture doc so product and engineering can reference one place for this subsystem.

---

## 1. Product / PRD-aligned features

These are the user-visible capabilities the implementation delivers today.

| Capability | Description |
|------------|-------------|
| **Who spoke when** | After captions exist, editors can run **speaker diarization** so each transcript segment (`VideoSegment`) gets a diarization label and resolves to a **`SpeakerIdentity`**. |
| **Cross-video “same voice”** | New speakers are matched to existing **`SpeakerIdentity`** rows using a **256-d WeSpeaker embedding** and **cosine similarity** (threshold configurable via `SPEAKER_MATCH_THRESHOLD`, default `0.75`). |
| **Speaker library** | **`/speakers/`** lists speakers that appear in the signed-in user’s channel videos, with search and filters (all / named / auto / narrator / background). |
| **Speaker detail** | **`/speakers/<id>/`** shows per-video transcript snippets, optional **phrase search** over that speaker’s segments only, and actions to rename, set role, link face, merge, or delete. |
| **Watch page integration** | Public **`GET /api/videos/<id>/speakers/`** drives a collapsible **Speakers** panel on the watch page; owners can trigger **`POST /api/videos/<id>/diarize/`** from subtitle settings when `HF_TOKEN` is configured. |
| **Transcript editor** | **`/watch/<uuid>/transcript/`** lists all segments with speaker assignments; editors reassign speakers via **`POST /api/segments/<id>/set-speaker/`**. |
| **Voice ↔ face linking** | **`SpeakerIdentity.face_identity`** optionally links a named voice to a **`FaceIdentity`**. Linking can **adopt the face’s name** if the speaker is still auto-named. |
| **Unified people search** | On the main search page, when a **face identity** matches the query, **linked speakers** contribute **`voice_videos`**: speech moments for that person even in videos where the face was not detected. |
| **Merge & maintain embeddings** | **Merge** moves all segments to a target identity, **recomputes** the stored embedding as a **segment-count–weighted** blend, then deletes the source identity. **Delete** clears `speaker_identity` on segments and removes the row. |
| **Face rename sync** | Renaming a **`FaceIdentity`** **cascades** to linked **`SpeakerIdentity`** rows that still had the **same name** as the old face name (keeps voice labels aligned when users name people from the face UI). |

**Out of scope / implicit limits (current behavior)**

- Diarization **requires prior Whisper segments**; it does not replace transcription.
- Cross-video matching runs **at diarization time**; correcting segments manually does not automatically re-derive the global embedding (merges and new diarization runs do update it in defined ways).
- Speaker embedding search uses **Postgres/pgvector cosine distance** over stored vectors; there is **no HNSW index** on `speaker_embedding` in migrations (scale is expected to be modest; very large identity tables may want an index later).

---

## 2. Architecture (high level)

```text
┌─────────────────────────────────────────────────────────────────────────┐
│                         Ingestion / captions queue                       │
└─────────────────────────────────────────────────────────────────────────┘
         │
         ▼
   faster-whisper ──► VideoSegment rows (text + timestamps)
         │
         │  POST /api/videos/<id>/diarize/  (owner, HF_TOKEN, segments > 0)
         ▼
   run_diarization_task (Celery, queue: captions)
         │
         ├─► FFmpeg: video/audio → 16 kHz mono WAV in MEDIA_ROOT/audio/<video_id>.wav
         │
         ├─► pyannote: speaker-diarization-3.1 → (start, end, local_label) turns
         │
         ├─► Label remap: global SPEAKER_XX across library (avoid collisions)
         │
         ├─► Overlap alignment: each VideoSegment ← dominant diarization speaker
         │
         ├─► WeSpeaker: pyannote/wespeaker-voxceleb-resnet34-LM
         │       → per-label mean L2-normalised 256-d embedding
         │
         └─► SpeakerIdentity resolution:
               1) embedding match (cosine ≥ threshold) → update rolling mean embedding
               2) else same global label already linked elsewhere (re-run safety)
               3) else create SpeakerIdentity (+ optional role heuristic)
                     → bulk_update segments.speaker_identity
```

**Integration with faces (orthogonal but linked)**

- **Face pipeline**: InsightFace → `FaceIdentity` + `DetectedFace` (visual).
- **Speaker pipeline**: pyannote + WeSpeaker → `SpeakerIdentity` + `VideoSegment.speaker_identity`.
- **Bridge**: user-set `SpeakerIdentity.face_identity` plus search-time joining for `voice_videos`.

---

## 3. Data model

### 3.1 `SpeakerIdentity` (`videos.models.SpeakerIdentity`)

| Field | Purpose |
|-------|---------|
| `name` | Display name; auto default like **“Speaker N”** from global label index. |
| `is_auto_named` | `True` until the user renames (or linking adopts a face name). |
| `role` | `speaker` \| `narrator` \| `background` — set manually or by **diarization heuristics** for new auto identities. |
| `face_identity` | Optional FK to **`FaceIdentity`** for voice ↔ face. |
| `speaker_embedding` | **pgvector 256-d** — voice embedding for cross-video matching (WeSpeaker). |

### 3.2 `VideoSegment` (speech rows)

| Field | Purpose |
|-------|---------|
| `text`, `start_seconds`, `end_seconds` | From Whisper. |
| `speaker_label` | String such as **`SPEAKER_02`** (global after remap). |
| `speaker_identity` | Resolved **`SpeakerIdentity`** after diarization (or manual assignment). |

---

## 4. Implementation details

### 4.1 Celery task: `run_diarization_task`

**Module:** `videos/tasks.py`  
**Queue:** `captions` (see `cliplens/settings.py` `CELERY_TASK_ROUTES`).

**Prerequisites**

- `HF_TOKEN` in environment (Hugging Face token with access to **pyannote/speaker-diarization-3.1**; users must accept model terms on Hugging Face).
- `pyannote.audio` installed (see `requirements.txt`, e.g. `pyannote.audio==3.3.2`).
- `VideoSegment` rows for the video.

**Steps (condensed)**

1. Load or extract **`media/audio/<video_id>.wav`** (16 kHz mono).
2. Run **`pyannote/speaker-diarization-3.1`**; normalize return type to an **Annotation** (`itertracks`).
3. **Remap** local `SPEAKER_00…` to **globally unique** `SPEAKER_XX` by scanning existing segment labels in other videos.
4. For each **Whisper segment**, pick the diarization label with **maximum time overlap**.
5. **Persist** `speaker_label` on all segments (`bulk_update`).
6. Compute **per-label statistics** (total duration, segment count) for **role heuristics**:
   - Very low total time or very short average segments → **`background`**
   - High average segment duration → **`narrator`**
   - Otherwise → **`speaker`**
7. For each diarization span ≥ **0.5 s**, crop audio and run **WeSpeaker** inference; **mean** embeddings, **L2-normalise** per global label.
8. For each global label, **resolve `SpeakerIdentity`**:
   - **Priority 1:** `CosineDistance` against existing rows with non-null `speaker_embedding`; if `dist < 1 - SPEAKER_MATCH_THRESHOLD`, treat as match and **update embedding** as normalised `(old + new) / 2`.
   - **Priority 2:** Another video already has this **`speaker_label`** tied to an identity (helps safe re-runs).
   - **Priority 3:** **Create** new `SpeakerIdentity`, apply heuristic role, store embedding if present.
9. **`bulk_update`** `speaker_identity` on segments.

**Device:** Uses the same `WHISPER_DEVICE` setting as Whisper (`cuda` if available when set).

### 4.2 HTTP APIs (Django REST)

| Method | Path | Role |
|--------|------|------|
| `POST` | `/api/videos/<uuid>/diarize/` | Owner; enqueues diarization; requires `HF_TOKEN` and segments. |
| `GET` | `/api/videos/<uuid>/speakers/` | **Public**; JSON for watch-page panel (names, roles, counts, linked face thumb). |
| `POST` | `/api/segments/<id>/set-speaker/` | Authenticated channel access; body `speaker_identity_id` or `null` (clears label+identity). |
| `POST` | `/api/speakers/<id>/rename/` | Sets `name`, clears `is_auto_named`. |
| `POST` | `/api/speakers/<id>/set-role/` | `speaker` / `narrator` / `background`. |
| `POST` | `/api/speakers/<id>/link-face/` | Set or clear `face_identity_id`; may rename speaker from face. |
| `POST` | `/api/speakers/<id>/merge/` | Body `into_id`; moves segments; **weighted merge** of embeddings; deletes source. |
| `DELETE` | `/api/speakers/<id>/delete/` | Nulls `speaker_identity` on segments; deletes identity. |
| `GET` | `/api/speakers/list/` | All speakers visible to user (merge UI). |

**Views module:** `videos/views.py` (speaker section near file end; diarization trigger mid-file).

### 4.3 Pages & templates

| URL | View | Template (approx.) |
|-----|------|---------------------|
| `/speakers/` | `speakers_page` | `videos/speakers.html` |
| `/speakers/<id>/` | `speaker_identity_page` | `videos/speaker_identity.html` |
| `/watch/<uuid>/transcript/` | `transcript_editor_page` | `videos/transcript_editor.html` |

**Watch page:** `videos/templates/videos/watch.html` — speakers panel, `runDiarization()` → `POST .../diarize/`.

### 4.4 Search: `voice_videos` augmentation

In **`player_page`** (`videos/views.py`), after face identity grouping for a query, the code loads **`SpeakerIdentity`** rows whose **`face_identity_id`** is in the matched face set, then loads up to **400** **`VideoSegment`** rows and attaches **`voice_videos`** per identity so the People tab can show speech where the linked voice appears, including videos without face hits for that query.

---

## 5. Configuration

| Setting / env | Default | Meaning |
|---------------|---------|---------|
| `SPEAKER_MATCH_THRESHOLD` | `0.75` | Min cosine similarity to treat a new embedding as an existing speaker. |
| `HF_TOKEN` | `''` | Hugging Face token for pyannote + WeSpeaker weights. |
| `WHISPER_DEVICE` | `cpu` | Also used for moving the pyannote pipeline to CUDA when `cuda` + GPU. |
| `HF_HUB_OFFLINE` | `0` | When `1`, skips some hub checks after models are cached. |

**Worker:** Diarization runs on the **`captions`** Celery worker (alongside caption-related tasks).

---

## 6. Dependencies & compliance

- **License / terms:** pyannote models on Hugging Face require **accepting model conditions** on the hub (documented in code comments and `requirements.txt`).
- **Storage:** Cached model weights live under the Hugging Face cache (same as other HF models); WeSpeaker weights are pulled as part of the pyannote ecosystem.

---

## 7. File reference (quick index)

| Area | Location |
|------|----------|
| Models | `videos/models.py` — `SpeakerIdentity`, `VideoSegment` |
| Task | `videos/tasks.py` — `run_diarization_task` |
| Routes | `videos/urls.py` — `/speakers/`, APIs listed above |
| Settings | `cliplens/settings.py` — `SPEAKER_MATCH_THRESHOLD`, `HF_TOKEN`, task route |
| Migrations | `videos/migrations/0027_add_speaker_identity.py`, `0028_add_speaker_embedding.py` |

---

*Last reviewed against codebase structure as of the migration adding `speaker_embedding` and diarization task implementation in `videos/tasks.py`.*
