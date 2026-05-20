from django.apps import AppConfig


class TenantsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'tenants'
    label = 'tenants'
    verbose_name = 'Multi-Tenancy'

    def ready(self):
        """Load all active tenant DB aliases into settings.DATABASES at startup."""
        from django.conf import settings
        if getattr(settings, 'MULTI_TENANT', False):
            from .provisioning import load_all_tenant_dbs
            load_all_tenant_dbs()
            from .celery_utils import connect_celery_signals
            connect_celery_signals()
