#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# ClipLens — development launcher
#
# Starts all required services:
#   1. Django dev server           → http://localhost:8000
#   2. Celery main worker          (queues: processing, captions, default)
#   3. Celery audio worker         (queue: audio  — PANNs, silence detection)
#   4. Celery translation worker   (queue: translation — NLLB-200)
#   5. Celery live worker          (queue: live — FFmpeg live stream tasks)
#   6. Flower task monitor         → http://localhost:5556
#
# Prerequisites (must be running before ./start.sh):
#   • PostgreSQL   — brew services start postgresql@15  (or your version)
#   • Redis        — brew services start redis
#
# Ctrl-C kills all processes cleanly.
# ═══════════════════════════════════════════════════════════════════════════

set -e
cd "$(dirname "$0")"
source venv/bin/activate

# ── Load .env ───────────────────────────────────────────────────────────────
if [ -f .env ]; then
  while IFS= read -r line; do
    [[ -z "$line" || "$line" == \#* ]] && continue
    if [[ "$line" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]]; then
      export "$line"
    fi
  done < .env
fi

# ── Defaults ────────────────────────────────────────────────────────────────
export FLOWER_BASIC_AUTH="${FLOWER_BASIC_AUTH:-admin:password}"
AUDIO_QUEUE="${AUDIO_EVENTS_QUEUE:-audio}"

# ── Preflight checks ────────────────────────────────────────────────────────
echo ""
echo "ClipLens — starting dev environment"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if ! redis-cli ping &>/dev/null; then
  echo "  ✗ Redis is not running."
  echo "    Start it:  brew services start redis"
  exit 1
fi
echo "  ✓ Redis"

if ! python manage.py check --database default &>/dev/null; then
  echo "  ✗ PostgreSQL connection failed."
  echo "    Start it:  brew services start postgresql@15"
  exit 1
fi
echo "  ✓ PostgreSQL"
echo ""

# ── 1. Django dev server ─────────────────────────────────────────────────────
echo "  [1/6] Django dev server       → http://localhost:8000"
python manage.py runserver &
DJANGO_PID=$!

# ── 2. Main Celery worker ────────────────────────────────────────────────────
#   Handles: video processing (HLS encode), frame analysis (YOLO / BLIP /
#            CLIP / InsightFace), seek thumbnails, Whisper captions,
#            Ollama AI summary, livestream recording pipeline.
echo "  [2/6] Celery main worker      (processing, captions, default)"
celery -A cliplens worker -l info \
  -Q processing,captions,default \
  -n main@%h \
  --concurrency=2 &
CELERY_PID=$!

# ── 3. Audio worker ───────────────────────────────────────────────────────────
#   Handles: PANNs CNN14 audio event detection + FFmpeg silence detection.
#   Concurrency=1 — PANNs holds ~150 MB in RAM per process.
echo "  [3/6] Celery audio worker     (${AUDIO_QUEUE})"
celery -A cliplens worker -l info \
  -Q "${AUDIO_QUEUE}" \
  -n audio@%h \
  --concurrency=1 &
AUDIO_CELERY_PID=$!

# ── 4. Translation worker ─────────────────────────────────────────────────────
#   Handles: NLLB-200 subtitle translation (~2.4 GB model, CPU/GPU).
#   Concurrency=1 — one instance is enough and avoids OOM.
echo "  [4/6] Celery translation      (translation)"
celery -A cliplens worker -l info \
  -Q translation \
  -n translation@%h \
  --concurrency=1 &
TRANSLATION_CELERY_PID=$!

# ── 5. Live worker ────────────────────────────────────────────────────────────
#   Handles: run_live_ffmpeg — blocks for the entire stream duration.
#   Kept separate so live streams never starve other tasks.
echo "  [5/6] Celery live worker      (live)"
celery -A cliplens worker -l info \
  -Q live \
  -n live@%h \
  --concurrency=2 &
LIVE_CELERY_PID=$!

# ── 6. Flower ─────────────────────────────────────────────────────────────────
echo "  [6/6] Flower monitor          → http://localhost:5556"
celery -A cliplens flower \
  --port=5556 \
  --basic_auth="${FLOWER_BASIC_AUTH}" &
FLOWER_PID=$!

# ── Ready ─────────────────────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  All services started."
echo ""
echo "  App       → http://localhost:8000"
echo "  Flower    → http://localhost:5556  (${FLOWER_BASIC_AUTH%%:*} / ${FLOWER_BASIC_AUTH##*:})"
echo "  API test  → open api_tester.html in your browser"
echo ""
echo "  Press Ctrl-C to stop everything."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# ── Cleanup ───────────────────────────────────────────────────────────────────
trap "
  echo ''
  echo 'Stopping all services...'
  kill \$DJANGO_PID \$CELERY_PID \$AUDIO_CELERY_PID \$TRANSLATION_CELERY_PID \$LIVE_CELERY_PID \$FLOWER_PID 2>/dev/null
  echo 'Done.'
  exit 0
" INT TERM

wait
