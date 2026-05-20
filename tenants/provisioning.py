"""
Tenant Provisioning
────────────────────
Called when the platform owner creates a new organisation via the control plane.
Everything happens automatically — no manual DB work needed.

Steps:
  1. INSERT Tenant row into freestream_control
  2. CREATE DATABASE <db_name> in PostgreSQL (psycopg2 autocommit)
  3. Enable pgvector + pg_trgm extensions in the new DB
  4. Register the new DB alias in Django's DATABASES dict at runtime
  5. Run Django migrations against the new DB
  6. Create media folder structure
  7. Create the first superadmin user for the org
  8. (Optional) seed starter data

Returns a dict with status / error message.
"""

import os
import logging
from pathlib import Path

import psycopg2
from psycopg2 import sql as pg_sql
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.auth.hashers import make_password

logger = logging.getLogger(__name__)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _pg_control_conn():
    """
    Return a psycopg2 connection to the PostgreSQL server (connecting to
    the 'postgres' maintenance DB so we can CREATE DATABASE).
    Uses the same credentials as the control DB.
    """
    ctrl = settings.DATABASES['control']
    return psycopg2.connect(
        dbname='postgres',          # maintenance DB for CREATE DATABASE
        user=ctrl['USER'],
        password=ctrl['PASSWORD'],
        host=ctrl['HOST'],
        port=ctrl['PORT'],
    )


def _register_db_alias(db_name: str) -> str:
    """
    Add a new entry to Django's DATABASES dict so the ORM can use it
    immediately (without restarting the server).  Returns the alias used.
    The alias IS the db_name (e.g. "freestream_orga").

    We include ALL keys that Django's DatabaseWrapper.check_settings() and
    ensure_defaults() expect so the connection initialises cleanly without
    going through the normal settings-loading path.
    """
    if db_name not in settings.DATABASES:
        ctrl = settings.DATABASES['control']
        settings.DATABASES[db_name] = {
            'ENGINE':  'django.db.backends.postgresql',
            'NAME':    db_name,
            'USER':    ctrl['USER'],
            'PASSWORD': ctrl['PASSWORD'],
            'HOST':    ctrl['HOST'],
            'PORT':    ctrl['PORT'],
            # Required by Django's DatabaseWrapper.check_settings()
            'TIME_ZONE':        None,
            'ATOMIC_REQUESTS':  False,
            'AUTOCOMMIT':       True,
            'CONN_MAX_AGE':     0,
            'CONN_HEALTH_CHECKS': False,
            'OPTIONS':  {'connect_timeout': 5},
            'TEST': {
                'CHARSET': None,
                'COLLATION': None,
                'MIGRATE': True,
                'MIRROR': None,
                'NAME': None,
            },
        }
        # Also close any stale connection reference Django may have cached
        from django.db import connections
        connections.close_all()
        logger.info("Registered new DB alias: %s", db_name)
    return db_name


def _create_postgres_db(db_name: str) -> None:
    """CREATE DATABASE db_name — skips if it already exists."""
    conn = _pg_control_conn()
    conn.autocommit = True
    with conn.cursor() as cur:
        # Check existence first
        cur.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (db_name,)
        )
        if cur.fetchone():
            logger.info("DB %s already exists — skipping CREATE", db_name)
        else:
            cur.execute(
                pg_sql.SQL("CREATE DATABASE {}").format(pg_sql.Identifier(db_name))
            )
            logger.info("Created PostgreSQL database: %s", db_name)
    conn.close()


def _enable_extensions(db_name: str) -> None:
    """Enable pgvector and pg_trgm in the newly created DB."""
    ctrl = settings.DATABASES['control']
    conn = psycopg2.connect(
        dbname=db_name,
        user=ctrl['USER'],
        password=ctrl['PASSWORD'],
        host=ctrl['HOST'],
        port=ctrl['PORT'],
    )
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")
        cur.execute("CREATE EXTENSION IF NOT EXISTS unaccent;")
    conn.close()
    logger.info("Enabled extensions in %s", db_name)


def _run_migrations(db_alias: str) -> None:
    """
    Run Django migrations for the new tenant DB.

    We must set the thread-local to the new alias BEFORE running migrations so
    that any RunPython data-migrations that call ORM methods without an explicit
    .using() get routed to the correct (new, empty) DB — not to 'default'.

    We use MigrationExecutor directly so we can exclude the 'tenants' app
    (which belongs in the control DB, not in tenant DBs).
    """
    from django.db import connections
    from django.db.migrations.executor import MigrationExecutor
    from django.db.migrations.loader import MigrationLoader
    from .db_router import set_db, clear_db

    # Point the router at the new DB for the duration of this migration run
    set_db(db_alias)
    try:
        connection = connections[db_alias]

        loader = MigrationLoader(connection, ignore_no_migrations=True)
        targets = [
            (app_label, name)
            for (app_label, name) in loader.graph.leaf_nodes()
            if app_label != 'tenants'
        ]

        executor = MigrationExecutor(connection)
        plan = executor.migration_plan(targets)

        if plan:
            executor.migrate(targets, plan=plan, fake=False, fake_initial=False)
            logger.info("Applied %d migrations to %s", len(plan), db_alias)
        else:
            logger.info("No pending migrations for %s", db_alias)

        connection.close()
    finally:
        clear_db()


def _create_media_folder(media_folder: str) -> None:
    """Create the tenant media directory tree."""
    base = Path(settings.MEDIA_ROOT)
    for subdir in ['videos', 'photos', 'thumbnails', 'face_crops', 'captions']:
        path = base / media_folder / subdir
        path.mkdir(parents=True, exist_ok=True)
    logger.info("Created media folders under %s", media_folder)


def _create_admin_user(db_alias: str, email: str, password: str, username: str) -> User:
    """Create the first superadmin user in the tenant DB."""
    user = User(
        username=username,
        email=email,
        is_staff=True,
        is_superuser=False,  # Django superuser != our superadmin role
        password=make_password(password),
    )
    user.save(using=db_alias)

    # Create the UserProfile with superadmin role
    from videos.models import UserProfile
    # UserProfile has related_name='profile' — create it in the tenant DB
    profile = UserProfile(user=user, role='superadmin')
    profile.save(using=db_alias)
    logger.info("Created UserProfile (superadmin) for '%s' in %s", user.username, db_alias)

    logger.info("Created admin user '%s' in %s", username, db_alias)
    return user


# ── Main entry point ───────────────────────────────────────────────────────────

def provision_tenant(
    *,
    slug: str,
    name: str,
    plan_id: int,
    admin_email: str,
    admin_username: str,
    admin_password: str,
) -> dict:
    """
    Full provisioning flow for a new tenant.

    Returns:
        {'success': True, 'tenant_id': <pk>}
        or
        {'success': False, 'error': '<message>'}
    """
    from .models import Tenant, Plan

    slug = slug.lower().strip()
    db_name = f"freestream_{slug.replace('-', '_')}"
    media_folder = f"tenants/{slug}/"

    try:
        plan = Plan.objects.using('control').get(pk=plan_id)
    except Plan.DoesNotExist:
        return {'success': False, 'error': f"Plan {plan_id} not found."}

    # ── 1. Create Tenant record (will be rolled back on failure) ───────────────
    tenant = Tenant(
        slug=slug,
        name=name,
        db_name=db_name,
        media_folder=media_folder,
        plan=plan,
        admin_email=admin_email,
        is_active=False,  # mark active only after all steps succeed
    )
    try:
        tenant.save(using='control')
    except Exception as exc:
        return {'success': False, 'error': f"Could not create tenant record: {exc}"}

    try:
        # ── 2. Create PostgreSQL database ──────────────────────────────────────
        _create_postgres_db(db_name)

        # ── 3. Enable extensions ───────────────────────────────────────────────
        _enable_extensions(db_name)

        # ── 4. Register in Django DATABASES ───────────────────────────────────
        _register_db_alias(db_name)

        # ── 5. Run migrations ──────────────────────────────────────────────────
        _run_migrations(db_name)

        # ── 6. Create media folders ────────────────────────────────────────────
        _create_media_folder(media_folder)

        # ── 7. Create admin user ───────────────────────────────────────────────
        _create_admin_user(db_name, admin_email, admin_password, admin_username)

        # ── 8. Mark tenant as active ───────────────────────────────────────────
        tenant.is_active = True
        tenant.save(using='control')

        logger.info("Tenant '%s' provisioned successfully → DB: %s", slug, db_name)
        return {'success': True, 'tenant_id': tenant.pk}

    except Exception as exc:
        # Something failed — mark tenant inactive and surface the error
        try:
            tenant.is_active = False
            tenant.save(using='control')
        except Exception:
            pass
        logger.exception("Provisioning failed for tenant '%s'", slug)
        return {'success': False, 'error': str(exc)}


def load_all_tenant_dbs() -> None:
    """
    Called at Django startup (AppConfig.ready) to load all active tenant
    DBs into settings.DATABASES so the router can address them.
    """
    try:
        from .models import Tenant
        tenants = Tenant.objects.using('control').filter(is_active=True)
        for t in tenants:
            _register_db_alias(t.db_name)
        logger.info("Loaded %d tenant DB aliases at startup", len(tenants))
    except Exception as exc:
        # Don't crash startup if control DB isn't ready yet (first run)
        logger.warning("Could not load tenant DBs at startup: %s", exc)
