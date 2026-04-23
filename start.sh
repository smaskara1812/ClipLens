#!/bin/bash
# ClipLens — development launcher
# Starts Django, Celery worker, and Flower monitor in parallel.
# Ctrl-C kills all three.
set -e
cd "$(dirname "$0")"
source venv/bin/activate

# Load .env safely — skips blank lines, comments, and lines with special chars
if [ -f .env ]; then
  while IFS= read -r line; do
    # Skip blank lines and comments
    [[ -z "$line" || "$line" == \#* ]] && continue
    # Only export lines that look like VALID_VAR=value
    if [[ "$line" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]]; then
      export "$line"
    fi
  done < .env
fi

# Default Flower credentials if not set in .env
export FLOWER_BASIC_AUTH="${FLOWER_BASIC_AUTH:-admin:password}"

echo "==> Starting Django dev server  (http://localhost:8000)"
python manage.py runserver &
DJANGO_PID=$!

echo "==> Starting Celery worker      (queues: processing, captions, default)"
celery -A cliplens worker -l info -Q processing,captions,default -n main@%h &
CELERY_PID=$!

# Dedicated worker for non-speech audio event detection (PANNs CNN14 +
# FFmpeg silencedetect).  Queue name is driven by AUDIO_EVENTS_QUEUE
# (default "audio").  Concurrency is pinned to 1 because PANNs inference
# is CPU-heavy and holds a model in RAM per process.
AUDIO_QUEUE="${AUDIO_EVENTS_QUEUE:-audio}"
echo "==> Starting Celery worker      (queue: ${AUDIO_QUEUE}, concurrency=1)"
celery -A cliplens worker -l info -Q "${AUDIO_QUEUE}" -n audio@%h --concurrency=1 &
AUDIO_CELERY_PID=$!

echo "==> Starting Flower monitor     (http://localhost:5556)"
celery -A cliplens flower \
  --port=5556 \
  --basic_auth="${FLOWER_BASIC_AUTH}" &
FLOWER_PID=$!

echo ""
echo "  Django  → http://localhost:8000"
echo "  Flower  → http://localhost:5556  (login: ${FLOWER_BASIC_AUTH})"
echo ""
echo "Press Ctrl-C to stop all processes."

# Kill all processes on Ctrl-C
trap "echo 'Stopping...'; kill $DJANGO_PID $CELERY_PID $AUDIO_CELERY_PID $FLOWER_PID 2>/dev/null; exit 0" INT TERM
wait
