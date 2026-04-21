"""
Management command: seed_scale_data

Creates realistic synthetic data for scale testing without running media processing.

Example:
  python manage.py seed_scale_data --users 100 --frames-per-video 250 --segments-per-video 120 --faces-per-video 70
"""

import json
import math
import random
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from videos.models import (
    Category,
    Channel,
    DetectedFace,
    FaceIdentity,
    NamedPlace,
    Photo,
    Video,
    VideoFrame,
    VideoSegment,
    WatchHistory,
    WatchTimeEntry,
)


LABEL_POOL = [
    "person", "car", "tree", "road", "building", "phone", "laptop", "table",
    "chair", "truck", "bus", "dog", "cat", "bottle", "cup", "sign", "bridge",
    "mountain", "beach", "river", "office", "screen", "keyboard", "book",
]

TAG_POOL = [
    "training", "meeting", "factory", "quality", "safety", "inspection",
    "outdoor", "conference", "demo", "internal", "release", "feature",
    "ai", "operations", "plant", "travel", "event",
]

PLACE_COLORS = ["#3b82f6", "#22c55e", "#f97316", "#a855f7", "#ef4444", "#14b8a6"]


def _rand_vec(dim: int):
    vals = [random.uniform(-1.0, 1.0) for _ in range(dim)]
    mag = math.sqrt(sum(v * v for v in vals)) or 1.0
    return [v / mag for v in vals]


def _chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


class Command(BaseCommand):
    help = "Seed realistic synthetic scale data (videos/frames/segments/faces/watch history/photos)."

    def add_arguments(self, parser):
        parser.add_argument("--videos", type=int, default=0)
        parser.add_argument("--channels", type=int, default=0)
        parser.add_argument("--categories", type=int, default=16)
        parser.add_argument("--users", type=int, default=120)
        parser.add_argument("--places", type=int, default=35)
        parser.add_argument("--photos", type=int, default=0)

        parser.add_argument("--frames-per-video", type=int, default=240)
        parser.add_argument("--segments-per-video", type=int, default=110)
        parser.add_argument("--faces-per-video", type=int, default=65)
        parser.add_argument("--watch-history-per-user", type=int, default=50)

        parser.add_argument("--identity-count", type=int, default=220)
        parser.add_argument("--batch-size", type=int, default=2000)

        parser.add_argument("--seed", type=int, default=42)
        parser.add_argument("--with-vectors", action="store_true", default=False)
        parser.add_argument("--reset-seed-data", action="store_true", default=False)
        parser.add_argument("--dry-run", action="store_true", default=False)

    def handle(self, *args, **opts):
        random.seed(opts["seed"])
        now = timezone.now()

        raw_users = max(0, opts["users"])
        channels_n = max(1, raw_users)
        categories_n = max(1, opts["categories"])
        users_n = max(1, raw_users)
        places_n = max(0, opts["places"])
        frames_per_video = max(0, opts["frames_per_video"])
        segments_per_video = max(0, opts["segments_per_video"])
        faces_per_video = max(0, opts["faces_per_video"])
        wh_per_user = max(0, opts["watch_history_per_user"])
        identity_count = max(1, opts["identity_count"])
        batch_size = max(100, opts["batch_size"])
        with_vectors = bool(opts["with_vectors"])
        dry_run = bool(opts["dry_run"])

        videos_per_channel = [random.randint(10, 12) for _ in range(channels_n)]
        photos_per_channel = [random.randint(10, 12) for _ in range(channels_n)]
        videos_n = sum(videos_per_channel)
        photos_n = sum(photos_per_channel)

        est_frames = videos_n * frames_per_video
        est_segments = videos_n * segments_per_video
        est_faces = videos_n * faces_per_video
        est_wh = users_n * wh_per_user

        self.stdout.write("Planned synthetic inserts:")
        self.stdout.write(f"  channels={channels_n}, categories={categories_n}, users={users_n}, places={places_n}")
        self.stdout.write(f"  users={users_n} => channels={channels_n} (1 owner per channel)")
        self.stdout.write(f"  videos={videos_n} (10-12/channel), photos={photos_n} (10-12/channel)")
        self.stdout.write(f"  video_frames≈{est_frames}, video_segments≈{est_segments}, detected_faces≈{est_faces}")
        self.stdout.write(f"  watch_history≈{est_wh}, vectors={'on' if with_vectors else 'off'}")
        if opts["videos"] or opts["photos"] or opts["channels"]:
            self.stdout.write(
                self.style.WARNING(
                    "Ignoring --videos/--photos/--channels; totals are auto-derived from --users."
                )
            )

        # Cleanup-only mode:
        #   python manage.py seed_scale_data --users 0 --reset-seed-data
        if opts["reset_seed_data"] and raw_users == 0:
            if dry_run:
                self.stdout.write(self.style.WARNING("Dry run only. Would reset existing seed data and exit."))
                return
            with transaction.atomic():
                self._reset_seed_data()
            self.stdout.write(self.style.SUCCESS("Seed data reset complete (cleanup-only mode)."))
            return

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run only. No rows written."))
            return

        with transaction.atomic():
            if opts["reset_seed_data"]:
                self._reset_seed_data()

            users = self._ensure_users(users_n)
            channels = self._ensure_channels(channels_n, users)
            categories = self._ensure_categories(categories_n)
            places = self._ensure_places(places_n, users)
            identities = self._ensure_face_identities(identity_count)

            videos = self._create_videos(channels, videos_per_channel, categories, places, now)
            self._create_video_frames(videos, frames_per_video, with_vectors, batch_size)
            self._create_video_segments(videos, segments_per_video, batch_size)
            self._create_detected_faces(videos, identities, faces_per_video, batch_size)

            self._create_watch_history(users, videos, wh_per_user, now, batch_size)
            self._create_watch_time_entries(videos, now, batch_size)
            self._create_photos(channels, photos_per_channel, categories, places, identities, with_vectors, now, batch_size)

        self.stdout.write(self.style.SUCCESS("Synthetic scale data generated successfully."))

    def _reset_seed_data(self):
        self.stdout.write("Resetting previous seed_scale_data rows...")
        WatchHistory.objects.filter(user__username__startswith="seed_user_").delete()
        WatchTimeEntry.objects.filter(video__uploaded_by="seed_scale_data").delete()
        DetectedFace.objects.filter(video__uploaded_by="seed_scale_data").delete()
        DetectedFace.objects.filter(photo__uploaded_by="seed_scale_data").delete()
        VideoSegment.objects.filter(video__uploaded_by="seed_scale_data").delete()
        VideoFrame.objects.filter(video__uploaded_by="seed_scale_data").delete()
        Photo.all_objects.filter(uploaded_by="seed_scale_data").delete()
        Video.all_objects.filter(uploaded_by="seed_scale_data").delete()
        FaceIdentity.objects.filter(name__startswith="Seed Person ").delete()
        NamedPlace.objects.filter(slug__startswith="seed-place-").delete()
        Channel.objects.filter(slug__startswith="seed-channel-").delete()
        Category.objects.filter(slug__startswith="seed-category-").delete()
        User.objects.filter(username__startswith="seed_user_").delete()

    def _ensure_channels(self, n, users):
        channels = []
        for i in range(1, n + 1):
            name = f"Seed Channel {i:02d}"
            slug = f"seed-channel-{i:02d}"
            owner = users[i - 1]
            ch, _ = Channel.objects.get_or_create(
                slug=slug,
                defaults={
                    "name": name,
                    "description": f"Synthetic channel {i}",
                    "owner": owner,
                },
            )
            if ch.owner_id != owner.id:
                ch.owner = owner
                ch.save(update_fields=["owner"])
            channels.append(ch)
        self.stdout.write(f"Channels ready: {len(channels)}")
        return channels

    def _ensure_categories(self, n):
        categories = []
        for i in range(1, n + 1):
            name = f"Seed Category {i:02d}"
            slug = f"seed-category-{i:02d}"
            cat, _ = Category.objects.get_or_create(slug=slug, defaults={"name": name})
            categories.append(cat)
        self.stdout.write(f"Categories ready: {len(categories)}")
        return categories

    def _ensure_users(self, n):
        users = []
        for i in range(1, n + 1):
            username = f"seed_user_{i:04d}"
            user, created = User.objects.get_or_create(
                username=username,
                defaults={
                    "email": f"{username}@example.com",
                    "first_name": "Seed",
                    "last_name": f"User{i:04d}",
                },
            )
            user.set_password("Seed123")
            if created:
                user.save()
            else:
                user.save(update_fields=["password"])
            users.append(user)
        self.stdout.write(f"Users ready: {len(users)}")
        return users

    def _ensure_places(self, n, users):
        places = []
        if n <= 0:
            return places

        base_lat, base_lng = 37.7749, -122.4194
        for i in range(1, n + 1):
            lat = base_lat + random.uniform(-0.35, 0.35)
            lng = base_lng + random.uniform(-0.35, 0.35)
            name = f"Seed Place {i:02d}"
            slug = f"seed-place-{i:02d}"
            radius = random.choice([150, 250, 400, 600, 800, 1200])
            pl, _ = NamedPlace.objects.get_or_create(
                slug=slug,
                defaults={
                    "name": name,
                    "latitude": lat,
                    "longitude": lng,
                    "radius_meters": radius,
                    "color": random.choice(PLACE_COLORS),
                    "description": f"Synthetic place {i}",
                    "created_by": random.choice(users) if users else None,
                },
            )
            places.append(pl)
        self.stdout.write(f"Named places ready: {len(places)}")
        return places

    def _ensure_face_identities(self, n):
        identities = []
        for i in range(1, n + 1):
            name = f"Seed Person {i:04d}"
            ident, _ = FaceIdentity.objects.get_or_create(
                name=name,
                defaults={
                    "is_auto_named": i % 4 != 0,
                    "thumbnail": "",
                    "ref_embedding": json.dumps([round(random.uniform(-1, 1), 5) for _ in range(12)]),
                },
            )
            identities.append(ident)
        self.stdout.write(f"Face identities ready: {len(identities)}")
        return identities

    def _create_videos(self, channels, per_channel_counts, categories, places, now):
        videos = []
        serial = 1
        for idx, ch in enumerate(channels):
            count = per_channel_counts[idx]
            for _ in range(count):
                duration = random.uniform(180, 5400)
                cat = random.choice(categories)
                pl = random.choice(places) if places and random.random() < 0.55 else None

                lat = lng = None
                if pl:
                    lat = pl.latitude + random.uniform(-0.003, 0.003)
                    lng = pl.longitude + random.uniform(-0.003, 0.003)
                elif places and random.random() < 0.25:
                    ref = random.choice(places)
                    lat = ref.latitude + random.uniform(-0.08, 0.08)
                    lng = ref.longitude + random.uniform(-0.08, 0.08)

                title = f"{random.choice(['Factory', 'Team', 'Field', 'Ops', 'Product'])} Session {serial:04d}"
                tag_count = random.randint(2, 5)
                tags = ", ".join(random.sample(TAG_POOL, k=tag_count))
                created_at = now - timedelta(days=random.randint(0, 730), hours=random.randint(0, 23))
                views = max(0, int(random.lognormvariate(4.5, 1.2)))
                vis = random.choices(
                    [Video.VISIBILITY_PUBLIC, Video.VISIBILITY_PRIVATE, Video.VISIBILITY_SUBSCRIBERS],
                    weights=[78, 12, 10],
                    k=1,
                )[0]

                video = Video(
                    title=title,
                    description=f"Synthetic description for {title}.",
                    channel=ch,
                    category=cat,
                    tags=tags,
                    original_filename=f"seed_video_{serial:04d}.mp4",
                    hls_path=f"hls/{serial:04d}/master.m3u8",
                    duration=round(duration, 2),
                    file_size=random.randint(120_000_000, 3_600_000_000),
                    upscaled_size=random.randint(0, 7_500_000_000),
                    resolution=random.choice(["1920x1080", "1280x720", "3840x2160"]),
                    available_qualities="360p,480p,720p,1080p",
                    status=Video.STATUS_READY,
                    visibility=vis,
                    comments_enabled=random.random() < 0.92,
                    views_count=views,
                    seek_sprite=f"sprites/{serial:04d}.jpg",
                    latitude=lat,
                    longitude=lng,
                    named_place=pl,
                    uploaded_by="seed_scale_data",
                    created_at=created_at,
                    updated_at=created_at + timedelta(days=random.randint(0, 45)),
                )
                videos.append(video)
                serial += 1

        Video.objects.bulk_create(videos, batch_size=1000)
        created = list(Video.objects.filter(uploaded_by="seed_scale_data").order_by("-created_at")[:len(videos)])
        self.stdout.write(f"Videos created: {len(created)}")
        return created

    def _create_video_frames(self, videos, frames_per_video, with_vectors, batch_size):
        if not videos or frames_per_video <= 0:
            return
        rows = []
        for v in videos:
            dur = float(v.duration or 600.0)
            step = dur / max(frames_per_video, 1)
            for i in range(frames_per_video):
                ts = max(0.0, min(dur, i * step + random.uniform(-0.35, 0.35)))
                labels = ", ".join(random.sample(LABEL_POOL, k=random.randint(2, 6)))
                desc = f"{random.choice(['Indoor', 'Outdoor', 'Operational', 'Training'])} scene at {int(ts)}s."
                face_count = random.choices([0, 1, 2, 3, 4], weights=[42, 30, 16, 8, 4], k=1)[0]
                rows.append(
                    VideoFrame(
                        video_id=v.id,
                        timestamp=round(ts, 3),
                        labels=labels,
                        face_count=face_count,
                        face_names="",
                        description=desc,
                        clip_embedding=_rand_vec(512) if with_vectors else None,
                    )
                )
                if len(rows) >= batch_size:
                    VideoFrame.objects.bulk_create(rows, batch_size=batch_size)
                    rows.clear()
        if rows:
            VideoFrame.objects.bulk_create(rows, batch_size=batch_size)
        self.stdout.write(f"VideoFrame rows created: ~{len(videos) * frames_per_video}")

    def _create_video_segments(self, videos, segments_per_video, batch_size):
        if not videos or segments_per_video <= 0:
            return
        rows = []
        terms = ["quality check", "safety briefing", "status update", "customer issue", "deployment"]
        for v in videos:
            dur = float(v.duration or 600.0)
            step = dur / max(segments_per_video, 1)
            for i in range(segments_per_video):
                start = max(0.0, i * step + random.uniform(-0.6, 0.6))
                seg_len = random.uniform(2.0, 7.5)
                end = min(dur, start + seg_len)
                txt = (
                    f"We discussed {random.choice(terms)} and next actions. "
                    f"Segment {i + 1} for seed video."
                )
                rows.append(
                    VideoSegment(
                        video_id=v.id,
                        start_seconds=round(start, 3),
                        end_seconds=round(end, 3),
                        text=txt,
                        speaker_label=f"SPEAKER_{random.randint(0, 8):02d}",
                    )
                )
                if len(rows) >= batch_size:
                    VideoSegment.objects.bulk_create(rows, batch_size=batch_size)
                    rows.clear()
        if rows:
            VideoSegment.objects.bulk_create(rows, batch_size=batch_size)
        self.stdout.write(f"VideoSegment rows created: ~{len(videos) * segments_per_video}")

    def _create_detected_faces(self, videos, identities, faces_per_video, batch_size):
        if not videos or faces_per_video <= 0:
            return
        rows = []
        weighted_identities = random.sample(identities, k=min(30, len(identities)))
        for v in videos:
            dur = float(v.duration or 600.0)
            for i in range(faces_per_video):
                ts = round(random.uniform(0, dur), 3)
                x1, y1 = random.randint(0, 1200), random.randint(0, 650)
                w, h = random.randint(70, 220), random.randint(70, 220)
                bbox = json.dumps([x1, y1, x1 + w, y1 + h])
                identity = random.choice(weighted_identities if random.random() < 0.7 else identities)
                status = random.choices(
                    [DetectedFace.STATUS_CONFIRMED, DetectedFace.STATUS_UNREVIEWED, DetectedFace.STATUS_REJECTED],
                    weights=[70, 22, 8],
                    k=1,
                )[0]
                rows.append(
                    DetectedFace(
                        video_id=v.id,
                        frame_id=None,
                        identity_id=identity.id,
                        timestamp=ts,
                        bbox=bbox,
                        embedding="",
                        confidence=round(random.uniform(0.62, 0.99), 4),
                        crop_path=f"faces/{v.id}/{i:04d}.jpg",
                        status=status,
                    )
                )
                if len(rows) >= batch_size:
                    DetectedFace.objects.bulk_create(rows, batch_size=batch_size)
                    rows.clear()
        if rows:
            DetectedFace.objects.bulk_create(rows, batch_size=batch_size)
        self.stdout.write(f"DetectedFace rows created: ~{len(videos) * faces_per_video}")

    def _create_watch_history(self, users, videos, per_user, now, batch_size):
        if not users or not videos or per_user <= 0:
            return
        rows = []
        max_per_user = min(per_user, len(videos))
        for user in users:
            picks = random.sample(videos, k=max_per_user)
            for v in picks:
                progress = round(random.uniform(5, max(30.0, float(v.duration or 600.0))), 2)
                completed = progress >= (float(v.duration or 600.0) * 0.92)
                rows.append(
                    WatchHistory(
                        user_id=user.id,
                        video_id=v.id,
                        progress_seconds=progress,
                        completed=completed,
                        watched_at=now - timedelta(days=random.randint(0, 180), hours=random.randint(0, 23)),
                    )
                )
                if len(rows) >= batch_size:
                    WatchHistory.objects.bulk_create(
                        rows, batch_size=batch_size, ignore_conflicts=True
                    )
                    rows.clear()
        if rows:
            WatchHistory.objects.bulk_create(rows, batch_size=batch_size, ignore_conflicts=True)
        self.stdout.write(f"WatchHistory rows created: ~{len(users) * max_per_user}")

    def _create_watch_time_entries(self, videos, now, batch_size):
        if not videos:
            return
        rows = []
        for v in videos:
            for d in range(30):
                dt = (now - timedelta(days=d)).date()
                rows.append(
                    WatchTimeEntry(
                        video_id=v.id,
                        date=dt,
                        total_seconds=random.randint(0, 120_000),
                    )
                )
                if len(rows) >= batch_size:
                    WatchTimeEntry.objects.bulk_create(
                        rows, batch_size=batch_size, ignore_conflicts=True
                    )
                    rows.clear()
        if rows:
            WatchTimeEntry.objects.bulk_create(rows, batch_size=batch_size, ignore_conflicts=True)
        self.stdout.write(f"WatchTimeEntry rows created: ~{len(videos) * 30}")

    def _create_photos(self, channels, per_channel_counts, categories, places, identities, with_vectors, now, batch_size):
        n = sum(per_channel_counts)
        if n <= 0:
            return
        photos = []
        serial = 1
        for idx, ch in enumerate(channels):
            count = per_channel_counts[idx]
            for _ in range(count):
                cat = random.choice(categories)
                pl = random.choice(places) if places and random.random() < 0.45 else None
                lat = lng = None
                if pl:
                    lat = pl.latitude + random.uniform(-0.003, 0.003)
                    lng = pl.longitude + random.uniform(-0.003, 0.003)
                width = random.choice([1280, 1920, 2048, 2560, 3840])
                height = random.choice([720, 1080, 1365, 1440, 2160])
                face_count = random.choices([0, 1, 2, 3], weights=[52, 28, 15, 5], k=1)[0]
                tags = ", ".join(random.sample(TAG_POOL, k=random.randint(2, 5)))

                photos.append(
                    Photo(
                        title=f"Seed Photo {serial:05d}",
                        description="Synthetic photo metadata for load testing.",
                        channel=ch,
                        category=cat,
                        tags=tags,
                        file=f"photos/originals/seed_photo_{serial:05d}.jpg",
                        thumbnail=f"photos/thumbnails/seed_photo_{serial:05d}.jpg",
                        width=width,
                        height=height,
                        file_size=random.randint(200_000, 8_000_000),
                        labels=", ".join(random.sample(LABEL_POOL, k=random.randint(2, 5))),
                        face_count=face_count,
                        face_names=", ".join(
                            random.choice(identities).name for _ in range(face_count)
                        ) if face_count else "",
                        scene_description=random.choice(
                            ["Assembly floor view", "Meeting room discussion", "Outdoor inspection", "Warehouse aisle"]
                        ),
                        clip_embedding=_rand_vec(512) if with_vectors else None,
                        ocr_text=random.choice(["", "LOT-245", "ZONE A", "SAFETY FIRST", "SHIFT B"]),
                        latitude=lat,
                        longitude=lng,
                        named_place=pl,
                        is_archived=(random.random() < 0.12),
                        is_potential_duplicate=(random.random() < 0.06),
                        status=Photo.STATUS_READY,
                        visibility=random.choice([Photo.VISIBILITY_PUBLIC, Photo.VISIBILITY_PRIVATE]),
                        views_count=max(0, int(random.lognormvariate(3.6, 1.1))),
                        uploaded_by="seed_scale_data",
                        created_at=now - timedelta(days=random.randint(0, 365)),
                        updated_at=now - timedelta(days=random.randint(0, 90)),
                    )
                )
                serial += 1
                if len(photos) >= batch_size:
                    Photo.objects.bulk_create(photos, batch_size=batch_size)
                    photos.clear()
        if photos:
            Photo.objects.bulk_create(photos, batch_size=batch_size)
        self.stdout.write(f"Photos created: {n}")
