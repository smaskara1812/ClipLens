"""
Celery application for FreeStream.

Start a worker with:
    celery -A freestream worker -l info

In development you can also use:
    celery -A freestream worker -l info --concurrency=2
"""
import os
from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'freestream.settings')

app = Celery('freestream')

# Read config from CELERY_* keys in Django settings
app.config_from_object('django.conf:settings', namespace='CELERY')

# Auto-discover tasks in all INSTALLED_APPS
app.autodiscover_tasks()
