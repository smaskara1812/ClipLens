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
#   6. Celery beat                 (periodic: health sweep, grace purges)
#   7. Flower task monitor         → http://localhost:5556
#
# Prerequisites (must be running before ./start.sh):
#   • PostgreSQL   — brew services start postgresql@15  (or your version)
#   • Redis        — brew services start redis
#   • nginx        — sudo brew services start nginx
#                    (proxies *.cliplens.local → :8000; config in
#                    deploy/nginx/cliplens-local.conf)
#
# NOTE: After adding a new Celery task to videos/tasks.py or tenants/tasks.py
#       you MUST restart this script — Celery only registers tasks at startup.
#       If you see "Received unregistered task of type ..." in worker logs,
#       that's the signal: Ctrl-C and re-run.
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

# Ensure nginx upload temp dir exists (nginx uses this to buffer large uploads;
# path is permanent but the dir may need to be recreated after OS reinstall)
mkdir -p "$HOME/.nginx_tmp/client_body"

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

# Count tasks by scanning the task modules — gives a quick sanity number
# at startup. If you add a task and the number doesn't go up, your import
# is broken; if it goes up but worker says "unregistered", restart this script.
TASK_COUNT=$(python -c "
import django, os
os.environ.setdefault('DJANGO_SETTINGS_MODULE','cliplens.settings')
django.setup()
from videos import tasks as _vt
from tenants import tasks as _tt
import inspect
n = sum(1 for m in (_vt, _tt) for v in vars(m).values()
        if hasattr(v, 'delay') and hasattr(v, 'name'))
print(n)
" 2>/dev/null || echo "?")
echo "  ✓ ${TASK_COUNT} Celery tasks registered"
echo ""

# ── 1. Django dev server ─────────────────────────────────────────────────────
echo "  [1/6] Django dev server       → http://localhost:8000"
python manage.py runserver &
DJANGO_PID=$!

# ── macOS fork-safety fix ─────────────────────────────────────────────────────
# On macOS, forked child processes that use Objective-C runtime (PyTorch, OpenCV,
# InsightFace, etc.) crash with SIGSEGV unless this env var is set.
export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES

# ── 2. Main Celery worker ────────────────────────────────────────────────────
#   Handles: video processing (HLS encode), frame analysis (YOLO / BLIP /
#            CLIP / InsightFace), seek thumbnails, Whisper captions,
#            Ollama AI summary, livestream recording pipeline.
#
#   --pool=solo: runs tasks in the main process (no fork). This avoids
#   SIGSEGV crashes on macOS where PyTorch/onnxruntime/OpenCV are not
#   fork-safe. Tasks run serially but models stay loaded between tasks.
echo "  [2/6] Celery main worker      (processing, captions, default)"
celery -A cliplens worker -l info \
  -Q processing,captions,default \
  -n main@%h \
  --pool=solo &
CELERY_PID=$!

# ── 3. Audio worker ───────────────────────────────────────────────────────────
#   Handles: PANNs CNN14 audio event detection + FFmpeg silence detection.
#   --pool=solo: PANNs (onnxruntime) also crashes under fork on macOS.
echo "  [3/6] Celery audio worker     (${AUDIO_QUEUE})"
celery -A cliplens worker -l info \
  -Q "${AUDIO_QUEUE}" \
  -n audio@%h \
  --pool=solo &
AUDIO_CELERY_PID=$!

# ── 4. Translation worker ─────────────────────────────────────────────────────
#   Handles: NLLB-200 subtitle translation (~2.4 GB model, CPU/GPU).
#   Concurrency=1 — one instance is enough and avoids OOM.
echo "  [4/6] Celery translation      (translation)"
celery -A cliplens worker -l info \
  -Q translation \
  -n translation@%h \
  --pool=solo &
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

# ── 6. Celery beat ────────────────────────────────────────────────────────────
#   Scheduler for periodic tasks: media-relocation grace purges (daily 03:17),
#   automated health sweep (hourly :07). Without beat, none of these fire.
echo "  [6/7] Celery beat             (scheduler)"
celery -A cliplens beat -l info \
  --schedule /tmp/cliplens-celerybeat-schedule &
BEAT_PID=$!

# ── 7. Flower ─────────────────────────────────────────────────────────────────
echo "  [7/7] Flower monitor          → http://localhost:5556"
celery -A cliplens flower \
  --port=5556 \
  --basic_auth="${FLOWER_BASIC_AUTH}" &
FLOWER_PID=$!

# ── 7. Stripe CLI webhook tunnel (optional) ──────────────────────────────────
# Forwards real Stripe test events to localhost so checkout completions
# create StorageAddon / AICreditPack rows in the control DB.
# Only starts if:
#   • the `stripe` CLI is installed (brew install stripe/stripe-cli/stripe)
#   • STRIPE_SECRET_KEY is set in .env (otherwise mock-purchase path is used)
STRIPE_PID=""
if [ -n "$STRIPE_SECRET_KEY" ]; then
  if command -v stripe &>/dev/null; then
    echo "  [+]  Stripe webhook tunnel    → forwarding to /api/stripe/webhook/"
    # --skip-verify lets the tunnel start even if you haven't accepted the host key
    stripe listen --forward-to localhost:8000/api/stripe/webhook/ \
      --skip-verify > /tmp/stripe-listen.log 2>&1 &
    STRIPE_PID=$!
    # Wait a moment then surface the signing secret it printed
    sleep 2
    if grep -q "webhook signing secret" /tmp/stripe-listen.log; then
      echo "        $(grep "webhook signing secret" /tmp/stripe-listen.log | tail -1)"
      echo "        (set this as STRIPE_WEBHOOK_SECRET in .env if not already)"
    fi
  else
    echo "  [!]  STRIPE_SECRET_KEY is set but 'stripe' CLI not found."
    echo "       Install:  brew install stripe/stripe-cli/stripe"
    echo "       Then:     stripe login"
  fi
fi

# ── Ready ─────────────────────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  All services started."
echo ""
echo "  App       → http://localhost:8000"
echo "  Flower    → http://localhost:5556  (${FLOWER_BASIC_AUTH%%:*} / ${FLOWER_BASIC_AUTH##*:})"
echo "  API test  → open api_tester.html in your browser"
if [ -n "$STRIPE_PID" ]; then
  echo "  Stripe    → tunnel active (log: /tmp/stripe-listen.log)"
fi
echo ""
echo "  Press Ctrl-C to stop everything."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# ── Cleanup ───────────────────────────────────────────────────────────────────
trap "
  echo ''
  echo 'Stopping all services...'
  kill \$DJANGO_PID \$CELERY_PID \$AUDIO_CELERY_PID \$TRANSLATION_CELERY_PID \$LIVE_CELERY_PID \$BEAT_PID \$FLOWER_PID \$STRIPE_PID 2>/dev/null
  echo 'Done.'
  exit 0
" INT TERM

wait
