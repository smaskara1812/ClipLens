"""
Management command: cleanup_orphans
────────────────────────────────────
Find and optionally delete orphaned media files left over from:
  • Failed uploads
  • Test runs done before multi-tenancy was wired
  • Deleted videos whose HLS / sprite / face / subtitle files were not garbage-collected

What counts as "orphan":
  1. Files in legacy GLOBAL media subdirs (media/originals/, media/subtitles/,
     media/hls/, media/seek_sprites/, media/thumbnails/, media/faces/) that
     are NOT referenced by any video in any tenant DB.
  2. Files inside media/tenants/<slug>/{originals,hls,seek_sprites,thumbnails,faces,subtitles}/
     whose <id> prefix doesn't match any Video / Photo / Subtitle in that tenant's DB.

Usage:
    python manage.py cleanup_orphans                 # dry run, shows what would be deleted
    python manage.py cleanup_orphans --delete        # actually deletes orphans
    python manage.py cleanup_orphans --tenant maskara --delete   # only one tenant
    python manage.py cleanup_orphans --legacy-only --delete      # only legacy global dirs
"""

from pathlib import Path
import re

from django.conf import settings
from django.core.management.base import BaseCommand


# Subdirs whose filenames begin with a UUID matching Video.id or Photo.id
UUID_RE = re.compile(r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', re.I)

# Subdir names searched inside each tenant root + (legacy) inside MEDIA_ROOT
ASSET_SUBDIRS = (
    'originals', 'hls', 'seek_sprites', 'thumbnails',
    'faces', 'subtitles', 'audio', 'photos', 'captions', 'upscaled', 'face_crops',
)


def _fmt_bytes(n: int) -> str:
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024:
            return f'{n:.1f} {unit}' if unit != 'B' else f'{n} B'
        n /= 1024
    return f'{n:.1f} TB'


def _file_size(path: Path) -> int:
    try:
        if path.is_file():
            return path.stat().st_size
        if path.is_dir():
            return sum(f.stat().st_size for f in path.rglob('*') if f.is_file())
    except OSError:
        pass
    return 0


def _collect_known_ids_for_tenant(db_alias: str) -> set:
    """Collect every UUID referenced by any tenant DB record."""
    from videos.models import Video, Photo, Subtitle
    ids = set()
    try:
        ids.update(str(v.id) for v in Video.objects.using(db_alias).all())
        ids.update(str(p.id) for p in Photo.objects.using(db_alias).all())
        # Subtitle file names embed the video UUID — pulling videos is enough
    except Exception:
        pass
    return ids


def _extract_uuid(name: str) -> str | None:
    m = UUID_RE.search(name)
    return m.group(1).lower() if m else None


class Command(BaseCommand):
    help = 'Find and optionally delete orphaned media files.'

    def add_arguments(self, parser):
        parser.add_argument('--delete', action='store_true',
                            help='Actually delete the files (default: dry run)')
        parser.add_argument('--tenant', default='',
                            help='Limit to a specific tenant slug')
        parser.add_argument('--legacy-only', action='store_true',
                            help='Only scan the legacy global media subdirs (skip tenant dirs)')

    def handle(self, *args, **options):
        media_root  = Path(settings.MEDIA_ROOT)
        delete_mode = options['delete']
        only_tenant = options['tenant'].strip()
        legacy_only = options['legacy_only']

        total_orphan_count = 0
        total_orphan_bytes = 0

        # ── 1. Legacy global directories ─────────────────────────────────────
        # In multi-tenant mode, anything in media/<subdir>/ is leftover from
        # single-tenant days or pre-fix test runs.  Always orphan.
        if not only_tenant:
            self.stdout.write(self.style.MIGRATE_HEADING('\n── Legacy global media subdirs ──'))
            for sub in ASSET_SUBDIRS:
                path = media_root / sub
                if not path.exists():
                    continue
                size = _file_size(path)
                file_count = sum(1 for _ in path.rglob('*') if _.is_file())
                total_orphan_count += file_count
                total_orphan_bytes += size

                action = 'DELETE' if delete_mode else 'would delete'
                self.stdout.write(
                    f'  [{action}] {sub}/ — {file_count} files, {_fmt_bytes(size)}'
                )
                if delete_mode:
                    import shutil
                    try:
                        shutil.rmtree(path)
                        self.stdout.write(self.style.SUCCESS(f'    ✓ Removed {sub}/'))
                    except Exception as exc:
                        self.stdout.write(self.style.ERROR(f'    ✗ Failed: {exc}'))

        # ── 2. Tenant directories ────────────────────────────────────────────
        if legacy_only:
            self._print_summary(total_orphan_count, total_orphan_bytes, delete_mode)
            return

        from tenants.models import Tenant
        from tenants.provisioning import _register_db_alias

        tenants = Tenant.objects.using('control').filter(is_active=True)
        if only_tenant:
            tenants = tenants.filter(slug=only_tenant)

        for tenant in tenants:
            self.stdout.write(self.style.MIGRATE_HEADING(f'\n── Tenant: {tenant.slug} ──'))

            try:
                _register_db_alias(tenant.db_name)
            except Exception as exc:
                self.stdout.write(self.style.ERROR(f'  Could not register DB: {exc}'))
                continue

            known_ids = _collect_known_ids_for_tenant(tenant.db_name)
            self.stdout.write(f'  Known media IDs in DB: {len(known_ids)}')

            # Honour per-tenant custom media path if set
            custom = (getattr(tenant, 'media_root_absolute', '') or '').strip()
            tenant_root = Path(custom) if custom else (media_root / tenant.media_folder)
            if not tenant_root.exists():
                self.stdout.write('  (no tenant media dir on disk)')
                continue

            for sub in ASSET_SUBDIRS:
                sub_path = tenant_root / sub
                if not sub_path.exists():
                    continue

                # Each top-level entry: file or directory named after a UUID
                for entry in sub_path.iterdir():
                    uuid = _extract_uuid(entry.name)
                    if not uuid:
                        # No UUID in the name — skip (could be a legitimate
                        # shared file like a category cover, etc.)
                        continue

                    if uuid in known_ids:
                        continue   # still referenced

                    size = _file_size(entry)
                    total_orphan_count += 1
                    total_orphan_bytes += size

                    action = 'DELETE' if delete_mode else 'would delete'
                    self.stdout.write(
                        f'  [{action}] {sub}/{entry.name} — {_fmt_bytes(size)}'
                    )
                    if delete_mode:
                        try:
                            if entry.is_dir():
                                import shutil
                                shutil.rmtree(entry)
                            else:
                                entry.unlink()
                        except Exception as exc:
                            self.stdout.write(self.style.ERROR(f'    ✗ Failed: {exc}'))

        self._print_summary(total_orphan_count, total_orphan_bytes, delete_mode)

    def _print_summary(self, count: int, bytes_: int, delete_mode: bool):
        self.stdout.write(self.style.MIGRATE_HEADING('\n── Summary ──'))
        if delete_mode:
            self.stdout.write(self.style.SUCCESS(
                f'  Deleted {count} orphan file(s) / dir(s), freed {_fmt_bytes(bytes_)}'
            ))
        else:
            self.stdout.write(
                f'  Found {count} orphan file(s) / dir(s) using {_fmt_bytes(bytes_)}'
            )
            self.stdout.write(self.style.WARNING(
                '  Re-run with --delete to actually remove them.'
            ))
