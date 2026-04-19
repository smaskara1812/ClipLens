"""
Management command: backfill_photo_gps

Scans all Photo rows that have exif_data with GPSInfo but null latitude/longitude,
extracts the decimal lat/lng, saves them, and re-runs named-place assignment.

Usage:
    python manage.py backfill_photo_gps
    python manage.py backfill_photo_gps --dry-run        # preview only, no saves
    python manage.py backfill_photo_gps --reassign-all   # also re-assign places for photos
                                                          # that already have lat/lng
"""
import math
from django.core.management.base import BaseCommand
from django.db.models import Q

from videos.models import Photo, NamedPlace


def _dms(arr, ref):
    """Convert DMS tuple to signed decimal degrees."""
    d, m, s = arr[0], arr[1], arr[2]
    dec = d + m / 60 + s / 3600
    return -dec if ref in ('S', 'W') else dec


def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    R = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _nearest_place(lat, lon, places):
    """Return the closest NamedPlace within its radius, or None."""
    best, best_dist = None, float('inf')
    for place in places:
        d = _haversine_m(lat, lon, place.latitude, place.longitude)
        if d <= place.radius_meters and d < best_dist:
            best, best_dist = place, d
    return best


class Command(BaseCommand):
    help = 'Backfill Photo.latitude/longitude from exif_data and re-assign named places.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            default=False,
            help='Print what would be updated without saving anything.',
        )
        parser.add_argument(
            '--reassign-all',
            action='store_true',
            default=False,
            help='Also re-run named-place assignment for photos that already have lat/lng.',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        reassign_all = options['reassign_all']

        places = list(NamedPlace.objects.all())
        self.stdout.write(f'Loaded {len(places)} named place(s).')

        # ── Phase 1: photos missing lat/lng but with GPS in EXIF ──────────────
        qs = Photo.objects.filter(
            latitude__isnull=True,
            longitude__isnull=True,
            exif_data__has_key='GPSInfo',
        ).only('id', 'title', 'exif_data', 'latitude', 'longitude', 'named_place_id')

        total = qs.count()
        self.stdout.write(f'Found {total} photo(s) with GPS EXIF but no lat/lng in DB.')

        updated = 0
        skipped = 0

        for photo in qs.iterator(chunk_size=200):
            try:
                gps = (photo.exif_data or {}).get('GPSInfo', {})
                if not gps or 'GPSLatitude' not in gps or 'GPSLongitude' not in gps:
                    skipped += 1
                    continue

                lat = round(_dms(gps['GPSLatitude'],  gps.get('GPSLatitudeRef',  'N')), 7)
                lon = round(_dms(gps['GPSLongitude'], gps.get('GPSLongitudeRef', 'E')), 7)
                nearest = _nearest_place(lat, lon, places)

                if dry_run:
                    place_name = nearest.name if nearest else '(none)'
                    self.stdout.write(
                        f'  [DRY] #{photo.id} "{photo.title}" → '
                        f'lat={lat}, lon={lon}, place={place_name}'
                    )
                else:
                    Photo.objects.filter(pk=photo.pk).update(
                        latitude=lat,
                        longitude=lon,
                        named_place=nearest,
                    )

                updated += 1

            except Exception as exc:
                self.stderr.write(f'  ERROR on photo #{photo.id}: {exc}')
                skipped += 1

        verb = 'Would update' if dry_run else 'Updated'
        self.stdout.write(f'{verb} {updated} photo(s) in phase 1. Skipped {skipped}.')

        # ── Phase 2 (--reassign-all): re-run place assignment for all geotagged ─
        if reassign_all and places:
            qs2 = Photo.objects.filter(
                latitude__isnull=False,
                longitude__isnull=False,
            ).only('id', 'title', 'latitude', 'longitude', 'named_place_id')

            total2 = qs2.count()
            self.stdout.write(
                f'\nPhase 2 (--reassign-all): re-assigning places for {total2} geotagged photo(s).'
            )

            reassigned = 0
            for photo in qs2.iterator(chunk_size=200):
                nearest = _nearest_place(photo.latitude, photo.longitude, places)
                if nearest and nearest.pk == photo.named_place_id:
                    continue  # already correct
                if dry_run:
                    place_name = nearest.name if nearest else '(none)'
                    self.stdout.write(
                        f'  [DRY] #{photo.id} "{photo.title}" → place={place_name}'
                    )
                else:
                    Photo.objects.filter(pk=photo.pk).update(named_place=nearest)
                reassigned += 1

            verb2 = 'Would reassign' if dry_run else 'Reassigned'
            self.stdout.write(f'{verb2} {reassigned} photo(s) in phase 2.')

        if dry_run:
            self.stdout.write(self.style.WARNING('Dry run — no changes saved.'))
        else:
            self.stdout.write(self.style.SUCCESS('Done.'))
