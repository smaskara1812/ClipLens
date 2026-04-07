#!/bin/bash
# ClipLens Backend — development server
cd "$(dirname "$0")"
source venv/bin/activate
python manage.py runserver
# Start Celery workers
celery -A cliplens worker -l info -Q processing,captions,default