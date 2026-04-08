# ClipLens — LinkedIn & Resume Content

---

## LinkedIn "About" Project Description

### ClipLens: Self-Hosted Media Intelligence Platform

ClipLens is a self-hosted media intelligence platform I built from the ground up as my first major software project. The system allows users to search across large personal video and photo libraries using natural language queries — finding specific moments, objects, faces, or spoken words without uploading anything to a cloud service. It combines a Django 4.2 web application with a full asynchronous AI analysis pipeline powered by YOLO, InsightFace, OpenAI CLIP, BLIP/Florence-2, and Whisper, all orchestrated by Celery and backed by PostgreSQL with pgvector.

The core technical challenge was designing a search system that runs up to eight parallel query passes per request — spanning full-text search, trigram fuzzy matching, pgvector approximate nearest-neighbor semantic search, and face identity lookup — while returning results in under three seconds. I learned to tune search quality specifically for each data type: raising fuzzy thresholds for short structured strings like YOLO labels, disabling fuzzy entirely on long transcript text to prevent noise, and applying separate cosine similarity cutoffs for video frame embeddings versus photo embeddings. Each of these decisions came from observing real failure modes and iterating.

Beyond search, ClipLens includes an adaptive bitrate HLS video pipeline (FFmpeg, eight quality levels, HLS.js client), a photo digital asset management system with AI-powered duplicate detection, an in-browser subtitle editor, role-based access control, and a face identity management workflow with manual review and bulk propagation commands. Building this project taught me how to wire together a production-grade stack — async task queues, vector databases, AI model inference, and a full Django web layer — while solving the kinds of search quality and performance problems that only emerge at scale.

---

## Resume Bullet Points

**ClipLens — Self-Hosted Media Intelligence Platform** | Django, Python, Celery, PostgreSQL, pgvector, FFmpeg, YOLOv8, CLIP, Whisper

- Architected and built a full-stack self-hosted media search platform in Django 4.2, enabling natural language search across video and photo libraries using 8 parallel AI-powered query passes per request (FTS, trigram fuzzy, pgvector ANN, face identity lookup), returning results in under 3 seconds
- Designed and tuned a multimodal search engine with per-modality quality controls: raised pg_trgm fuzzy thresholds for structured YOLO label strings (0.50 vs 0.35 default), disabled fuzzy for long-form transcript text, and applied separate CLIP cosine cutoffs for video frames (0.24) and photos (0.28) to minimize false positives
- Built an asynchronous AI analysis pipeline using Celery (3 queues) orchestrating YOLOv8 object detection, InsightFace face recognition, BLIP/Florence-2 scene captioning, OpenAI CLIP embedding generation, and faster-whisper speech-to-text across ingested video and photo assets
- Implemented adaptive bitrate HLS video streaming using FFmpeg (8 quality levels) with HLS.js client-side playback, enabling smooth video delivery across varying network conditions on a local server
- Developed a face identity management system with InsightFace embedding-based clustering, manual review workflow, and bulk management commands (propagate_identities, auto_confirm_similar, rename_identities) to organize hundreds of detected faces across video and photo sources
- Integrated pgvector (HNSW index) and pg_trgm (GIN indexes) extensions into PostgreSQL to support sub-linear approximate nearest-neighbor semantic search and performant fuzzy text search at scale without query degradation
- Built a photo digital asset management module with AI deduplication (cosine similarity > 0.97 CLIP threshold), archive management, and an in-browser VTT/SRT subtitle editor backed by HLS.js for video playback
- Diagnosed and resolved multiple production-grade issues including N+1 query problems (fixed with select_related/prefetch_related), Celery result persistence (django-db backend), short-query fuzzy noise (length guard <= 3), and a full project rename (freestream to cliplens) at the module level
