# This makes `celery_app` available when Django starts so that shared_task
# decorators in apps can reference it without explicit imports.
from .celery import app as celery_app

__all__ = ('celery_app',)
