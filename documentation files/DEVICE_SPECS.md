# ClipStream — Hardware Specifications

> **Development (Sufficient)** — comfortable daily use with all AI models loaded.
> **Production (Minimum)** — stable operation under real workloads; scale up for higher concurrency.

---

## 1. Development Workstation

These specs let you run Django, Celery, PostgreSQL, Redis, and all AI inference pipelines
(YOLO, BLIP/Florence-2, CLIP, InsightFace) simultaneously without bottlenecks.


| Component   | Spec Required                                                 | Notes                                                                     |
| ----------- | ------------------------------------------------------------- | ------------------------------------------------------------------------- |
| **CPU**     | AMD Ryzen 9 7900X / Intel Core i9-13900K (16+ physical cores) | Celery uses multiprocessing; more cores = more parallel video/photo tasks |
| **GPU**     | NVIDIA RTX 4080 16 GB VRAM                                    | See VRAM breakdown below. 16 GB is the comfortable floor for one worker   |
| **RAM**     | 32 GB DDR5                                                    | OS + Postgres shared_buffers + Django + model weights that spill to RAM   |
| **Storage** | 2 TB NVMe SSD (PCIe 4.0)                                      | HuggingFace cache alone can reach 20 GB; media files grow quickly         |
| **OS**      | Ubuntu 22.04 LTS (preferred) or Windows 11 with WSL2          | Native CUDA support, no WSL VRAM overhead                                 |
| **Network** | Gigabit Ethernet or Wi-Fi 6                                   | Needed when streaming HLS during dev testing                              |
| **Python**  | 3.11 (via pyenv or system)                                    | Required for all current dependencies                                     |


### VRAM Budget (single Celery worker, all models loaded)


| Model                           | Approx. VRAM |
| ------------------------------- | ------------ |
| YOLOv8-large                    | ~2 GB        |
| BLIP-large (or Florence-2-base) | ~4 GB        |
| CLIP ViT-B/32                   | ~1 GB        |
| InsightFace buffalo_l           | ~2–3 GB      |
| **Total (worst case)**          | **~9–10 GB** |


A 16 GB card (RTX 4080 / RTX 3090) gives comfortable headroom.

---

## 2. Production Server

### 2a. Single-Server Deployment (all-in-one)

Suitable for a team of up to ~50 concurrent users with moderate upload volume.


| Component         | Minimum Spec                                                                      | Recommended                                                      |
| ----------------- | --------------------------------------------------------------------------------- | ---------------------------------------------------------------- |
| **CPU**           | 16 cores / 32 threads (AMD EPYC or Intel Xeon)                                    | 32 cores for heavier concurrent processing                       |
| **GPU**           | NVIDIA RTX A4000 16 GB                                                            | NVIDIA A5000 24 GB for 2 concurrent AI workers                   |
| **RAM**           | 64 GB ECC DDR4                                                                    | 128 GB if pgvector HNSW index exceeds 1M vector                  |
| **OS Storage**    | 250 GB NVMe SSD (OS + code + model cache)                                         | 250 GB NVMe                                                      |
| **Media Storage** | 4 TB NVMe or NAS (RAID-10) — or S3/MinIO (depends on media size 3X size of media) | Object storage recommended for scale; local NVMe for low latency |
| **OS**            | Ubuntu 22.04 LTS                                                                  | Ubuntu 22.04 LTS                                                 |
| **Network**       | 100 Mbps dedicated                                                                | 100 Mbps if no multiple concurrent streaming is required         |


### 2b. Distributed Deployment (recommended for production scale)

Split services across dedicated nodes for independent scaling.

#### Web / API Node

Handles Django, Gunicorn/Nginx — no GPU needed.


| Component | Minimum    |
| --------- | ---------- |
| CPU       | 8 cores    |
| RAM       | 16 GB      |
| Storage   | 100 GB SSD |
| GPU       | None       |


#### AI Processing Worker Node

Runs Celery tasks: YOLO, BLIP/Florence-2, CLIP, InsightFace.


| Component | Minimum            | Notes                                                     |
| --------- | ------------------ | --------------------------------------------------------- |
| CPU       | 8 cores            | Each Celery worker is a separate process                  |
| GPU       | NVIDIA A4000 16 GB | One worker per GPU; two workers need 2× GPU or 24 GB card |
| RAM       | 32 GB              | Model loading can spike RAM before moving weights to VRAM |
| Storage   | 500 GB NVMe        | Model cache + temp video frames                           |


#### Database Node (PostgreSQL + pgvector + Redis)


| Component | Minimum                         | Notes                                                                       |
| --------- | ------------------------------- | --------------------------------------------------------------------------- |
| CPU       | 8 cores                         | High clock speed matters more than count for query latency                  |
| RAM       | 32 GB                           | pgvector HNSW index is memory-mapped; index for 1M 512-dim vectors ≈ 2–4 GB |
| Storage   | 2 TB NVMe SSD (high IOPS ≥ 50k) | WAL writes + HNSW index build are I/O intensive                             |
| GPU       | None                            |                                                                             |


#### Media / Object Storage Node

Stores HLS segments, original videos, photo originals and thumbnails.


| Component | Minimum                                      |
| --------- | -------------------------------------------- |
| Storage   | 10 TB (RAID-10 or MinIO with erasure coding) |
| Network   | 10 Gbps to web node                          |
| RAM       | 8 GB                                         |
| CPU       | 4 cores                                      |


---

## 3. Cloud Instance Equivalents

If deploying on AWS / GCP / Azure:


| Role      | AWS                                                    | GCP                   | Azure                     |
| --------- | ------------------------------------------------------ | --------------------- | ------------------------- |
| Web/API   | `c6i.2xlarge` (8 vCPU, 16 GB)                          | `c2-standard-8`       | `Standard_F8s_v2`         |
| AI Worker | `g4dn.2xlarge` (T4 16 GB) or `g5.2xlarge` (A10G 24 GB) | `n1-standard-8` + T4  | `Standard_NC8as_T4_v3`    |
| Database  | `r6i.2xlarge` (8 vCPU, 64 GB)                          | `m2-ultramem-208`     | `Standard_E8s_v3`         |
| Storage   | S3 + EFS or EBS gp3                                    | GCS + Persistent Disk | Azure Blob + Managed Disk |


---

## 4. Software Stack Prerequisites


| Software             | Version                | Purpose                               |
| -------------------- | ---------------------- | ------------------------------------- |
| Python               | 3.11                   | Runtime                               |
| PostgreSQL           | 15+ with pgvector 0.7+ | Primary DB + vector search            |
| Redis                | 7+                     | Celery broker + Django cache          |
| FFmpeg               | 6+                     | HLS transcoding, frame extraction     |
| CUDA                 | 12.1+                  | GPU inference                         |
| cuDNN                | 9.x                    | Required by PyTorch                   |
| Nginx                | 1.24+                  | Reverse proxy + HLS static serving    |
| Gunicorn             | 21+                    | WSGI server                           |
| Supervisor / systemd | —                      | Process management for Celery workers |


---

## 5. Storage Growth Estimates


| Content Type                   | Per Item | Notes                 |
| ------------------------------ | -------- | --------------------- |
| Original video (1080p, 10 min) | ~500 MB  | Pre-transcode         |
| HLS segments (same video)      | ~300 MB  | Post-transcode        |
| Video frame thumbnails         | ~2–5 MB  | 1 frame per 5 seconds |
| CLIP embedding (video frame)   | 2 KB     | 512 × float32         |
| CLIP embedding (photo)         | 2 KB     | Same                  |
| Original photo (12 MP JPEG)    | ~4–8 MB  |                       |
| Photo thumbnail (480px)        | ~80 KB   |                       |
| Face embedding                 | 2 KB     | 512-dim float32       |


**Rule of thumb:** Budget 2–3× raw media size for all derived artefacts (HLS segments, thumbnails, embeddings, DB storage).

---

## 6. Key Bottlenecks to Monitor


| Bottleneck               | Symptom                          | Solution                                                        |
| ------------------------ | -------------------------------- | --------------------------------------------------------------- |
| GPU VRAM exhausted       | Celery task OOM-killed           | Reduce concurrent workers or upgrade GPU                        |
| Postgres RAM             | Slow HNSW ANN queries            | Increase `shared_buffers`, add RAM                              |
| Disk IOPS                | Slow HLS seek / frame extraction | Switch to NVMe; use object storage for media                    |
| Celery queue backlog     | Processing lag for uploads       | Add GPU worker nodes                                            |
| pgvector HNSW build time | Slow first ANN query             | Pre-build index during off-peak; set `hnsw.ef_construction=128` |


