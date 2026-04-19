# Upscaling — Implementation, Methods & Roadmap

> **Scope:** Manual upscaling of videos and photos inside the Freestream platform.  
> **Added in:** April 2026  
> **Entry points:** `videos/upscale.py`, `videos/tasks.py` (tasks `upscale_video_task`, `upscale_photo_task`), API routes `/api/videos/<id>/upscale/`, `/api/photos/<id>/upscale/`.

---

## Table of Contents

1. [What "upscaling" means](#1-what-upscaling-means)
2. [Algorithm overview — all known methods](#2-algorithm-overview--all-known-methods)
3. [What we implemented](#3-what-we-implemented)
4. [Preset ladder](#4-preset-ladder)
5. [Pipeline walkthrough](#5-pipeline-walkthrough)
6. [Advantages of the current approach](#6-advantages-of-the-current-approach)
7. [Limitations & trade-offs](#7-limitations--trade-offs)
8. [Comparison table](#8-comparison-table)
9. [Future upgrade paths](#9-future-upgrade-paths)
10. [Decision guide — when to use each tier](#10-decision-guide--when-to-use-each-tier)

---

## 1. What "upscaling" means

Upscaling (super-resolution) is the task of producing a higher-resolution output from a lower-resolution input.  The fundamental problem is that the extra pixels **do not exist in the source** — every algorithm has to invent them.  The methods differ in *how* they invent those pixels: pure mathematics, learned statistics, or neural synthesis.

The two axes that matter in practice are:

| Axis | Trade-off |
|---|---|
| **Fidelity** | Does the output look like what was actually there? |
| **Sharpness / perceptual quality** | Does it look crisp, even if some synthesised detail is not "real"? |

Classical methods (nearest-neighbour → Lanczos) maximise fidelity.  Neural methods (ESRGAN, BasicVSR) maximise perceived sharpness, sometimes at the cost of hallucinating fine detail.

---

## 2. Algorithm Overview — All Known Methods

### 2.1 Nearest-Neighbour
Each output pixel copies the value of its nearest input pixel with no blending.

- **Speed:** Instant (O(n)).
- **Quality:** Blocky, staircase edges.
- **Use case:** Retro pixel-art aesthetics only; never for real media upscaling.

### 2.2 Bilinear Interpolation
Blends the 4 nearest pixels with distance-weighted averages.

- **Speed:** Very fast — common in real-time GPU texture sampling.
- **Quality:** Smooth but visibly blurry. Loses edge crispness above ×1.5.
- **Use case:** Real-time previews, thumbnail generation.

### 2.3 Bicubic Interpolation
Fits a cubic polynomial through 16 surrounding pixels (4×4 neighbourhood). Produces smoother gradients and better-preserved edges than bilinear.

- **Speed:** Fast.
- **Quality:** Acceptable for moderate scale factors (up to ×2). Industry standard before Lanczos became accessible.
- **Use case:** Default in many older export pipelines. Still common as a "medium" quality mode.
- **Artifacts:** Slight ringing at sharp edges; over-sharpening at high scale.

### 2.4 Lanczos Resampling ← *current implementation*
A **sinc-based windowed filter**. Convolves the image with `sinc(x) * sinc(x/a)` where `a` is the number of lobes (typically 3, giving a 6×6 kernel). The sinc function is the ideal band-limited reconstruction filter from signal theory — Lanczos is the best practical approximation of it.

- **Speed:** Fast (same order as bicubic).
- **Quality:** The best classical algorithm. Sharp edges with minimal blurring, controlled ringing.
- **Use case:** Highest-quality classical upscaling in Photoshop, Lightroom, FFmpeg, ImageMagick, PIL.
- **Artifacts:** Slight ringing ("Gibbs phenomenon") visible at very high-contrast edges; invisible in most real-world content.

### 2.5 Edge-Directed Interpolation (NEDI, iNEDI, ICBI)
Classical methods that analyse local edge direction before interpolating along the edge rather than across it.

- **Speed:** Moderate (more compute than Lanczos).
- **Quality:** Notably sharper text and geometric edges than Lanczos; slightly worse on organic textures.
- **Availability:** Niche; requires custom implementation or ImageMagick `-filter EWA`.

### 2.6 Waifu2x (2015)
First widely-used neural upscaler. A small CNN trained on pairs of high-res anime images and artificially downscaled versions. Simultaneously denoises and upscales.

- **Speed:** GPU: fast. CPU: slow.
- **Quality:** Excellent for anime/illustration. Poor generalisation to photorealistic content.
- **Artifacts:** Smooth plastic-like surfaces; loses film grain and natural texture variation.
- **Availability:** Open source (`nagadomi/waifu2x`). Python wrapper: `waifu2x-ncnn-vulkan`.

### 2.7 ESRGAN / Real-ESRGAN (2018–2021)
GAN-based models trained on high-resolution photos paired with synthetically degraded versions (blur, JPEG compression, noise). Real-ESRGAN (2021) extended training to "real-world" degradations making it far more general.

- **Speed:** GPU required for practical use (~10–30s per 1080p frame batch for ×4).
- **Quality:** Dramatically sharper than any classical method. Reconstructs believable brick, fabric, foliage, and skin textures from blurry or compressed sources.
- **Artifacts:** Can hallucinate detail (smooth skin may gain artificial pores; plain walls may gain synthetic texture). Oversharpening on already-clean sources.
- **Availability:** Open source (`xinntao/Real-ESRGAN`). Python: `realesrgan` pip package. Weights: ~67 MB (`RealESRGAN_x4plus.pth`).
- **Variants:** `x4plus-anime` for illustrations, `x2plus` for smaller scale, `RealESRNet` (no GAN) for less aggressive sharpening.

### 2.8 SwinIR (2021)
Transformer-based super-resolution replacing CNN with Swin Transformer. Achieves higher PSNR/SSIM than ESRGAN (better fidelity metrics) while also being perceptually sharp.

- **Speed:** Slower than Real-ESRGAN; GPU strongly required.
- **Quality:** State-of-the-art in classical SR benchmarks (Set5, Set14, DIV2K). Less prone to GAN hallucination.
- **Availability:** Open source (`JingyunLiang/SwinIR`).

### 2.9 BasicVSR / BasicVSR++ (2021–2022) — Video-specific
Exploits **temporal information** across adjacent frames. Uses bidirectional propagation and optical flow to align frames before reconstruction. Because it can see what a pixel "should" look like across multiple frames, it reconstructs detail that single-image methods would have to hallucinate.

- **Speed:** Very slow without GPU; even with GPU, 2–5× slower per frame than Real-ESRGAN.
- **Quality:** Best open-source video super-resolution. Temporally coherent — avoids flickering artifacts.
- **Availability:** Open source (`ckkelvinchan/BasicVSR_PlusPlus`); part of MMagic/MMEditing framework.

### 2.10 RVRT (Recurrent Video Restoration Transformer, 2022)
Combines transformer architecture with temporal recurrence. Benchmark-leading across multiple video restoration tasks.

- **Speed:** GPU-only; higher memory requirements than BasicVSR.
- **Quality:** State-of-the-art video SR.
- **Availability:** Open source (`JingyunLiang/RVRT`).

### 2.11 Commercial / Proprietary Neural Upscalers
These are the reference-quality professional tools:

| Tool | Strengths | Notes |
|---|---|---|
| **Topaz Video AI** | Multiple specialised models (Artemis, Proteus, Iris, Gaia) for different content types. Best real-world results for consumer video. | Paid desktop software. ~$300 perpetual. |
| **Topaz Gigapixel AI** | Photo upscaling; near-lossless 6× on high-ISO photography. | Paid. |
| **Adobe Premiere AI upscale** | Integrated into NLE timeline. | Requires CC subscription. Cloud-assisted. |
| **DaVinci Resolve Super Scale** | GPU-accelerated 2×/4× with proprietary neural model. | Free in Studio tier with purchased DaVinci key. |
| **Apple Neural Engine (ProRes RAW/ProRes 4444)** | Real-time upscaling on M-series Macs using ANE. | Apple ecosystem only. |
| **NVIDIA RTX Video Super Resolution** | Driver-level, in-player upscaling on RTX cards. | Requires NVIDIA GPU + compatible player. |

---

## 3. What We Implemented

**Algorithm: Lanczos resampling** via:
- **PIL** (`Image.Resampling.LANCZOS`) for photos.
- **FFmpeg** (`scale=W:H:flags=lanczos`) for videos.

### Why Lanczos was chosen
1. **Zero additional dependencies.** PIL and FFmpeg are already required by the rest of the application.
2. **No GPU required.** The processing queue workers are CPU-only in the default self-hosted setup.
3. **No model weights to download or maintain.**
4. **Deterministic.** Identical input always produces identical output — no GAN randomness.
5. **Fast enough for asynchronous background tasks.** A 1080p → 4K video upscale of a 10-minute video takes roughly 5–15 minutes on a modern CPU core, which is acceptable as a queued job.
6. **Best classical quality.** For sources that are already in good condition (clean signal, low noise, no JPEG artifacting), Lanczos will preserve that quality faithfully. Neural methods would not add meaningful benefit and risk introducing hallucinated texture.

### Photo pipeline (`run_photo_upscale_pipeline`)
1. Read current dimensions from the `Photo` model (or probe via PIL if absent).
2. Validate the chosen preset long-edge is greater than the current long-edge.
3. Compute `(new_width, new_height)` preserving aspect ratio, both divisible by 2.
4. Open with `_open_any_photo()` (supports HEIC, PSD, RAW, TIFF, AVIF, all standard formats).
5. `PIL.Image.resize((tw, th), Image.Resampling.LANCZOS)`.
6. Save as JPEG (quality 92) / PNG / WebP according to original format; exotic formats fall back to JPEG.
7. Replace `photo.file` via Django's storage layer; delete the old file.
8. Update `photo.width`, `photo.height`, `photo.file_size`; clear thumbnail.
9. Dispatch `analyze_photo_task` — re-runs YOLO, BLIP/Florence, CLIP, InsightFace on the new file.

### Video pipeline (`run_video_upscale_pipeline`)
1. Read source dimensions with `get_video_metadata` (ffprobe).
2. Validate preset.
3. FFmpeg encode to a temporary `.mp4` with `scale=W:H:flags=lanczos`, `libx264`, `CRF 18`, `preset medium`.  Audio is `copy` first; if that fails (codec incompatibility), falls back to `aac 192k`.
4. Replace `video.original_file` via Django storage; delete the old file.
5. Call `process_video()` — full HLS pipeline: metadata, multi-quality encode, thumbnail.
6. Dispatch `generate_seek_thumbnails_task` for the new resolution.

---

## 4. Preset Ladder

Presets are defined in `videos/upscale.py::UPSCALE_PRESETS`. The **long edge** is used (i.e., the larger of width and height), so the same preset works correctly for both landscape and portrait content.

| Preset ID | Label | Long edge (px) | Typical use |
|---|---|---|---|
| `480p` | 480p (SD) | 854 | Archive quality, streaming on slow connections |
| `720p` | 720p (HD) | 1280 | Standard web delivery |
| `1080p` | 1080p (Full HD) | 1920 | Primary web/VOD standard |
| `1440p` | 1440p (QHD) | 2560 | High-resolution displays, future-proofing |
| `4k` | 4K UHD | 3840 | Large-screen, cinema, archival delivery |

A preset is only offered as **available** in the UI if its long edge is strictly greater than the current file's long edge (upscale only, not downscale).

---

## 5. Pipeline Walkthrough

```
User clicks "Upscale" in UI
    │
    ▼
GET /api/videos/<id>/upscale/presets/
    │  Reads current dimensions via ffprobe
    │  Returns preset list with available=true/false
    ▼
User selects preset → POST /api/videos/<id>/upscale/  { preset: "1080p" }
    │
    ├─ Validates ownership (channel owner or editor only)
    ├─ Validates: not already processing
    ├─ Validates: chosen preset > current long edge
    │
    ▼
_dispatch_upscale_video()
    │
    ├─ Celery available → upscale_video_task.apply_async(queue='processing')
    └─ Celery down → daemon thread fallback
    
    ─── In worker ───────────────────────────────────────────────────────
    run_video_upscale_pipeline(video_id, target_long_edge)
        │
        ├── ffprobe → get W×H
        ├── compute new dims (aspect-preserving, ÷2 aligned)
        ├── Video.status = 'processing'
        ├── FFmpeg: source.mp4 → tmp.mp4  (lanczos scale + x264 CRF 18)
        ├── Replace original_file in Django storage
        ├── process_video() → HLS encode + thumbnail
        └── generate_seek_thumbnails_task.apply_async()
    ─────────────────────────────────────────────────────────────────────
    
UI polls GET /api/videos/<id>/status/ every 4 s → reloads on 'ready'
```

Photos follow the same pattern, replacing ffprobe with PIL and HLS-encode with `analyze_photo_task`.

---

## 6. Advantages of the Current Approach

- **No extra dependencies.** Everything runs on what's already in `requirements.txt`.
- **CPU-only.** Works on any server — no CUDA, no GPU driver management.
- **Lossless fidelity guarantee.** Lanczos cannot hallucinate detail, so the output is always a faithful (if softer) representation of the source.
- **Predictable file size.** Output scales roughly with pixel count — no surprise bloat from neural model outputs.
- **Reversible via re-upload.** Since it replaces the original, a user can always upload the original again.
- **Stable under load.** Classical algorithms have no memory spikes or model loading overhead.
- **Best for clean sources.** If the original footage is already sharp and uncompressed, Lanczos is essentially lossless in quality terms.

---

## 7. Limitations & Trade-offs

| Limitation | Impact | Mitigation |
|---|---|---|
| **Cannot synthesise new detail.** Lanczos interpolates; it doesn't reconstruct. A soft or blurry source stays proportionally soft. | Significant when upscaling by ×2 or more. | Upgrade to Real-ESRGAN (see §9). |
| **Ringing artifacts** at very high-contrast edges (e.g. black text on white). | Mostly invisible in video content. Occasionally visible in scanned documents or graphics. | Post-process with unsharp mask or switch to ESRGAN for those assets. |
| **No temporal coherence for video.** Each frame is processed independently in the HLS encode. | Risk of slight frame-to-frame flicker at very high scale factors. | Use BasicVSR++ for video (see §9). |
| **Long upscale pipeline.** Video must be decoded, scaled, re-encoded, then re-HLS-encoded. | A 10-min 4K output may take 15–30 min on one CPU core. | GPU or distributed workers; or accept the wait given it's a background task. |
| **Replaces original.** There is no "undo" other than re-uploading the source. | Destructive operation. | Add a "keep original as backup" option (see §9). |
| **Audio codec edge case.** If source uses an unsupported passthrough codec, fallback re-encodes to AAC 192k. Perceptibly lossless for most content; audiophiles may notice. | Low likelihood. | Detect and warn before upscale; store audio track separately. |

---

## 8. Comparison Table

| Method | Sharpness | Fidelity | Artifacts | GPU needed | OSS | Extra deps | Notes |
|---|---|---|---|---|---|---|---|
| Nearest-neighbour | ✗ Blocky | High | Severe | No | — | None | Avoid entirely |
| Bilinear | ✗ Blurry | Medium | None | No | — | None | Previews only |
| Bicubic | ▲ OK | Medium | Low ringing | No | — | None | Decent fallback |
| **Lanczos (current)** | **▲ Good** | **High** | **Low ringing** | **No** | **Yes** | **None** | **Best classical** |
| Waifu2x | ✔ Sharp | Medium | Plastic texture | Preferred | Yes | ~200 MB | Anime/art only |
| Real-ESRGAN | ✔✔ Very sharp | Medium | Hallucination | Preferred | Yes | ~67 MB model | Best OSS general |
| SwinIR | ✔✔ Very sharp | High | Low | Yes | Yes | ~100 MB | Best PSNR/SSIM |
| BasicVSR++ | ✔✔✔ Excellent | High | Very low | Yes | Yes | ~200 MB | Best OSS video |
| Topaz Video AI | ✔✔✔✔ Near-perfect | High | Near-zero | Yes | No (paid) | Software licence | Pro standard |
| DaVinci Super Scale | ✔✔✔ Excellent | High | Very low | Yes | Freemium | DaVinci Studio | Real-time NLE |

---

## 9. Future Upgrade Paths

### 9.1 Real-ESRGAN for Photos (High Priority)

The most impactful near-term upgrade. Add as an optional "High Quality (AI)" upscale mode alongside the existing Lanczos option.

**What changes:**
- `videos/upscale.py`: add `_realesrgan_upscale_image()` path.
- UI: two-option toggle — *Standard (Lanczos)* / *AI Enhanced (Real-ESRGAN)*.
- Worker: download model weights on first use (or bundle during deploy).

**Rough implementation sketch:**
```python
def _realesrgan_upscale_image(src: Path, dst: Path, scale: int) -> None:
    from basicsr.archs.rrdbnet_arch import RRDBNet
    from realesrgan import RealESRGANer
    import cv2

    model = RRDBNet(
        num_in_ch=3, num_out_ch=3, num_feat=64,
        num_block=23, num_grow_ch=32, scale=scale,
    )
    upsampler = RealESRGANer(
        scale=scale,
        model_path=f'weights/RealESRGAN_x{scale}plus.pth',
        model=model,
        tile=512,           # tile-based processing for large images — avoids OOM
        tile_pad=10,
        pre_pad=0,
        half=False,         # set True if FP16 GPU available
    )
    img = cv2.imread(str(src), cv2.IMREAD_UNCHANGED)
    output, _ = upsampler.enhance(img, outscale=scale)
    cv2.imwrite(str(dst), output)
```

**Dependencies to add:** `realesrgan`, `basicsr`, `facexlib`, `gfpgan` (optional, for face enhancement).  
**Model weights:** `RealESRGAN_x4plus.pth` (~67 MB) — download once.  
**Estimated quality gain:** Very significant for blurry, compressed, or noisy sources. Minimal for already-clean originals.

---

### 9.2 BasicVSR++ for Video (Medium Priority)

Temporal super-resolution — each frame benefits from information in surrounding frames. Eliminates the frame-independence limitation of the current approach.

**What changes:**
- Add a new `upscale_video_task_neural` Celery task that iterates frames via BasicVSR++ before handing off to FFmpeg for HLS re-encode.
- Significantly higher GPU memory requirement (~8 GB VRAM for 1080p input tiles).
- Processing time: ~2–5 min per minute of 1080p content on an RTX 3090.

---

### 9.3 Preserve a Backup of the Original (Low Effort, High Value)

Currently the upscale **replaces** the original file. Adding a simple backup path would make the operation non-destructive.

**What changes in `run_video_upscale_pipeline`:**
```python
# Before replacing original_file:
backup_name = f"originals/backups/{video_id}_pre_upscale_{int(time.time())}.mp4"
shutil.copy(src, Path(settings.MEDIA_ROOT) / backup_name)
# Store in a new Video.original_backup_file field or just log the path.
```

A "Restore original" button in the Edit modal could then move the backup back.

---

### 9.4 Face Enhancement with GFPGAN (Optional Add-on)

For portrait photography, GFPGAN can run after Real-ESRGAN to specifically restore and enhance facial features.

```python
from gfpgan import GFPGANer

restorer = GFPGANer(
    model_path='weights/GFPGANv1.4.pth',
    upscale=upscale_factor,
    arch='clean',
    channel_multiplier=2,
)
_, _, output = restorer.enhance(img, has_aligned=False, only_center_face=False)
```

**Use case:** Event photography, portrait albums — where sharp faces matter more than background texture.

---

### 9.5 Batch / Album Upscaling (Quality of Life)

Currently upscaling must be triggered one item at a time. A batch endpoint and UI would let users select multiple photos (or an entire album) and queue them all.

**API:**
```
POST /api/photos/upscale/batch/
{ "photo_ids": ["uuid1", "uuid2", ...], "preset": "1080p", "method": "lanczos" }
```

**Worker:** Chain `upscale_photo_task` calls with `celery.group()` for parallelism.

---

### 9.6 Progress Reporting via WebSocket / SSE

Currently the UI polls the status endpoint every 3–4 seconds. Replacing with Server-Sent Events or Django Channels WebSocket would give real-time progress (e.g. "Encoding frame 1420/3600…").

---

## 10. Decision Guide — When to Use Each Tier

```
Is the source already high-quality (clean signal, not heavily compressed)?
    └─ YES → Lanczos is sufficient. Neural methods add minimal value and may
             introduce artifacts on clean inputs.
    └─ NO  → Use Real-ESRGAN (when available).

Is this a video with fast motion or many scene cuts?
    └─ YES → BasicVSR++ temporal coherence matters.
    └─ NO  → Real-ESRGAN frame-by-frame is fine.

Is this a portrait / face-forward photo?
    └─ YES → Real-ESRGAN + GFPGAN combination.
    └─ NO  → Real-ESRGAN alone.

Is a GPU available on the worker?
    └─ YES → Enable neural methods.
    └─ NO  → Stay on Lanczos; it remains the best practical CPU option.

What scale factor?
    └─ ×1.0–1.5 → Any classical method is fine.
    └─ ×1.5–2.0 → Lanczos is good. ESRGAN noticeably better on compressed sources.
    └─ ×2.0–4.0 → ESRGAN strongly preferred.
    └─ ×4.0+    → ESRGAN / BasicVSR++ only; classical methods produce visibly
                  blurry results.
```

---

*Last updated: April 2026*
