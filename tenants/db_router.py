"""
TenantDatabaseRouter
────────────────────
All models in the 'tenants' app go to the 'control' database.
Every other model goes to the current tenant's database (resolved from
a thread-local set by TenantMiddleware or by Celery task setup).

Thread-local usage:
    from tenants.db_router import set_db, get_db, clear_db

    set_db('freestream_orga')  # call this in middleware / tasks
    get_db()                   # returns current db alias
    clear_db()                 # reset at end of request
"""

import threading

_state = threading.local()

# ── Public helpers called by middleware and Celery tasks ───────────────────────

def set_db(alias: str) -> None:
    """Set the tenant DB alias for this thread."""
    _state.db_alias = alias


def get_db() -> str | None:
    """Return the active tenant DB alias for this thread, or None."""
    return getattr(_state, 'db_alias', None)


def clear_db() -> None:
    """Reset tenant DB alias (called at end of each request)."""
    _state.db_alias = None


# ── Router ─────────────────────────────────────────────────────────────────────

class TenantDatabaseRouter:
    """
    Routing rules:
      - tenants app models → 'control' DB always
      - Everything else   → active tenant DB alias (from thread-local)
                            Falls back to 'default' if none set.
    """

    CONTROL_APPS = {'tenants', 'contenttypes', 'auth', 'sessions',
                    'admin', 'celery_results', 'django_celery_results'}

    def _is_control(self, model) -> bool:
        return (model is not None and
                getattr(model, '_meta', None) is not None and
                model._meta.app_label == 'tenants')

    def _tenant_db(self) -> str:
        alias = get_db()
        return alias if alias else 'default'

    # ── Read ──────────────────────────────────────────────────────────────────

    def db_for_read(self, model, **hints):
        if self._is_control(model):
            return 'control'
        return self._tenant_db()

    # ── Write ─────────────────────────────────────────────────────────────────

    def db_for_write(self, model, **hints):
        if self._is_control(model):
            return 'control'
        return self._tenant_db()

    # ── Migrations ────────────────────────────────────────────────────────────

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if app_label == 'tenants':
            # Tenant models only migrate to the control DB
            return db == 'control'
        if db == 'control':
            # Nothing else goes into the control DB
            return False
        # All other apps can migrate into any tenant DB (or default)
        return True

    # ── Relations ─────────────────────────────────────────────────────────────

    def allow_relation(self, obj1, obj2, **hints):
        # Allow relations within the same DB; block cross-DB relations
        db1 = 'control' if self._is_control(type(obj1)) else self._tenant_db()
        db2 = 'control' if self._is_control(type(obj2)) else self._tenant_db()
        return db1 == db2
