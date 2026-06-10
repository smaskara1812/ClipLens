"""
System health checks
─────────────────────
Synchronous probes that confirm every service ClipLens depends on is reachable
and healthy. Used by the /system/health/ page in the control plane.

Each check function returns a dict with this shape:
    {
        'name':    'Human-readable label',
        'status':  'ok' | 'warn' | 'error' | 'na',
        'detail':  'Short version info / error message',
        'hint':    'Optional suggested fix when status != ok',
        'elapsed_ms': int,
    }

Add new checks by writing a function and appending it to ALL_CHECKS.
"""

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

from django.conf import settings


# ── Helpers ────────────────────────────────────────────────────────────────────

def _ok(name, detail='', elapsed=0):
    return {'name': name, 'status': 'ok',    'detail': detail, 'hint': '', 'elapsed_ms': elapsed}

def _warn(name, detail, hint='', elapsed=0):
    return {'name': name, 'status': 'warn',  'detail': detail, 'hint': hint, 'elapsed_ms': elapsed}

def _error(name, detail, hint='', elapsed=0):
    return {'name': name, 'status': 'error', 'detail': detail, 'hint': hint, 'elapsed_ms': elapsed}

def _na(name, detail=''):
    return {'name': name, 'status': 'na',    'detail': detail, 'hint': '', 'elapsed_ms': 0}


def _timed(fn):
    """Run a check, catch exceptions, time it, and ensure the result has elapsed_ms set."""
    start = time.monotonic()
    try:
        result = fn()
    except Exception as exc:
        result = _error(getattr(fn, '__name__', 'check').replace('check_', '').title(),
                        f'Crashed: {type(exc).__name__}: {exc}')
    elapsed = int((time.monotonic() - start) * 1000)
    if isinstance(result, dict) and not result.get('elapsed_ms'):
        result['elapsed_ms'] = elapsed
    return result


def _fmt_bytes(n):
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024:
            return f'{n:.1f} {unit}' if unit != 'B' else f'{int(n)} B'
        n /= 1024
    return f'{n:.1f} PB'


# ── Database checks ────────────────────────────────────────────────────────────

def check_db_default():
    from django.db import connections
    conn = connections['default']
    with conn.cursor() as cur:
        cur.execute("SELECT version()")
        version = cur.fetchone()[0]
        cur.execute("SELECT pg_database_size(current_database())")
        size = cur.fetchone()[0]
    short_version = ' '.join(version.split()[:2])
    return _ok('PostgreSQL (default)', f'{short_version} · {_fmt_bytes(size)} used')


def check_db_control():
    if not getattr(settings, 'MULTI_TENANT', False):
        return _na('Control DB', 'MULTI_TENANT is disabled')
    from django.db import connections
    if 'control' not in settings.DATABASES:
        return _error('Control DB', 'Not in settings.DATABASES',
                      hint='Run python manage.py setup_multitenancy')
    conn = connections['control']
    with conn.cursor() as cur:
        cur.execute("SELECT pg_database_size(current_database())")
        size = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM tenants_tenant")
        tenant_count = cur.fetchone()[0]
    return _ok('Control DB', f'{tenant_count} tenants · {_fmt_bytes(size)} used')


# ── Redis ─────────────────────────────────────────────────────────────────────

def check_redis():
    try:
        import redis
    except ImportError:
        return _error('Redis', 'redis-py not installed',
                      hint='pip install redis')
    url = settings.CELERY_BROKER_URL if hasattr(settings, 'CELERY_BROKER_URL') else \
          getattr(settings, 'REDIS_URL', 'redis://localhost:6379/0')
    try:
        r = redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
        r.ping()
        info = r.info('memory')
        used = _fmt_bytes(info.get('used_memory', 0))
        return _ok('Redis', f'{url.split("@")[-1]} · {used} used')
    except Exception as exc:
        return _error('Redis', f'Cannot connect to {url}',
                      hint=f'Start Redis: brew services start redis ({exc})')


# ── Disk space ────────────────────────────────────────────────────────────────

def check_disk_space():
    path = str(settings.MEDIA_ROOT)
    try:
        total, used, free = shutil.disk_usage(path)
    except FileNotFoundError:
        return _error('Disk space', f'MEDIA_ROOT does not exist: {path}',
                      hint='Create the media directory')
    pct_free = (free / total) * 100
    detail = f'{_fmt_bytes(free)} free of {_fmt_bytes(total)} ({pct_free:.0f}% free)'
    if pct_free < 5:
        return _error('Disk space', detail, hint='Critical — free space immediately')
    if pct_free < 15:
        return _warn('Disk space', detail, hint='Running low — consider cleanup_orphans or upgrade')
    return _ok('Disk space', detail)


# ── FFmpeg ────────────────────────────────────────────────────────────────────

def check_ffmpeg():
    binary = getattr(settings, 'FFMPEG_PATH', 'ffmpeg')
    path = shutil.which(binary)
    if not path:
        return _error('FFmpeg', f'`{binary}` not found in PATH',
                      hint='Install: brew install ffmpeg  (or apt install ffmpeg)')
    try:
        out = subprocess.run([path, '-version'], capture_output=True, text=True, timeout=5)
        first_line = out.stdout.split('\n')[0]
        version = first_line.split(' version ')[-1].split(' ')[0] if ' version ' in first_line else '?'
        return _ok('FFmpeg', f'v{version} at {path}')
    except Exception as exc:
        return _warn('FFmpeg', f'Found but version check failed: {exc}')


# ── Celery workers ────────────────────────────────────────────────────────────

def _celery_inspect(worker_name_prefix):
    """Returns dict of {worker_name: pong} for matching workers, or {} on timeout."""
    try:
        from cliplens.celery import app
        inspect = app.control.inspect(timeout=2.0)
        pings = inspect.ping() or {}
        return {n: p for n, p in pings.items() if n.startswith(worker_name_prefix + '@')}
    except Exception:
        return {}


def _check_celery_worker(label, name_prefix, queues):
    pings = _celery_inspect(name_prefix)
    if not pings:
        return _error(f'Celery: {label}', f'No worker responding (looking for {name_prefix}@)',
                      hint=f'Start the worker for queue: {", ".join(queues)}')
    worker_name = list(pings.keys())[0]
    return _ok(f'Celery: {label}', f'{worker_name} responding · queues: {", ".join(queues)}')


def check_celery_main():
    return _check_celery_worker('main', 'main', ['processing', 'captions', 'default'])

def check_celery_audio():
    queue = getattr(settings, 'AUDIO_EVENTS_QUEUE', 'audio')
    return _check_celery_worker('audio', 'audio', [queue])

def check_celery_translation():
    return _check_celery_worker('translation', 'translation', ['translation'])

def check_celery_live():
    return _check_celery_worker('live', 'live', ['live'])


# ── AI model caches ───────────────────────────────────────────────────────────

HOME = Path.home()
HF_CACHE = HOME / '.cache' / 'huggingface'


def _check_hf_model(label, name_fragment, min_mb=10, hint=''):
    """Check that a HuggingFace cached model exists. name_fragment is e.g. 'whisper'."""
    hub = HF_CACHE / 'hub'
    if not hub.exists():
        return _warn(label, 'HuggingFace cache not initialised',
                     hint='Will auto-download on first task — first run will be slow')
    matches = [d for d in hub.iterdir() if d.is_dir() and name_fragment.lower() in d.name.lower()]
    if not matches:
        return _warn(label, 'Not cached yet (lazy download)',
                     hint=hint or 'Will auto-download on first task')
    size = sum(f.stat().st_size for d in matches for f in d.rglob('*') if f.is_file())
    if size / 1024 / 1024 < min_mb:
        return _warn(label, f'Cache partial ({_fmt_bytes(size)})',
                     hint='Re-run the task to complete download')
    return _ok(label, f'Cached · {_fmt_bytes(size)}')


def check_whisper():
    return _check_hf_model('Whisper', 'whisper', min_mb=50)

def check_clip():
    return _check_hf_model('CLIP', 'clip', min_mb=100)

def check_blip():
    return _check_hf_model('BLIP', 'blip', min_mb=200)

def check_nllb():
    return _check_hf_model('NLLB-200', 'nllb', min_mb=500,
                           hint='Largest model (~2.4 GB) — first translation run will be slow')

def check_yolo():
    # Ultralytics auto-downloads to current dir or ~/.ultralytics
    model_name = getattr(settings, 'YOLO_MODEL', 'yolov8n') + '.pt'
    candidates = [
        HOME / '.ultralytics' / model_name,
        Path.cwd() / model_name,
        Path(settings.BASE_DIR) / model_name,
    ]
    for c in candidates:
        if c.exists():
            return _ok('YOLO', f'{model_name} · {_fmt_bytes(c.stat().st_size)} at {c}')
    return _warn('YOLO', f'{model_name} not yet downloaded',
                 hint='Auto-downloads on first frame analysis task')


def check_insightface():
    path = HOME / '.insightface' / 'models' / 'buffalo_l'
    if not path.exists():
        return _warn('InsightFace', 'buffalo_l models not downloaded',
                     hint='Auto-downloads on first face-recognition task (~300 MB)')
    size = sum(f.stat().st_size for f in path.rglob('*') if f.is_file())
    return _ok('InsightFace', f'buffalo_l · {_fmt_bytes(size)}')


def check_panns():
    # PANNs weights live under MEDIA_ROOT/panns_data/ (the runtime task downloads
    # them there). Fall back to ~/panns_data/ for installations that put them there.
    candidates = [
        Path(settings.MEDIA_ROOT) / 'panns_data',
        HOME / 'panns_data',
    ]
    panns_dir = next((p for p in candidates if p.exists()), None)
    if panns_dir is None:
        return _warn('PANNs', 'Audio event model not downloaded',
                     hint='Auto-downloads on first audio events task (~150 MB)')
    weights = list(panns_dir.glob('Cnn14_mAP*.pth'))
    if not weights:
        return _warn('PANNs', f'Directory exists at {panns_dir} but Cnn14*.pth missing',
                     hint='Re-run an audio events task to redownload')
    size = sum(f.stat().st_size for f in panns_dir.rglob('*') if f.is_file())
    return _ok('PANNs', f'CNN14 · {_fmt_bytes(size)} at {panns_dir.name}/')


# ── GPU / CUDA ────────────────────────────────────────────────────────────────

def check_gpu():
    try:
        import torch
    except ImportError:
        return _na('GPU / CUDA', 'PyTorch not installed (Whisper still works via CPU)')
    if not torch.cuda.is_available():
        return _na('GPU / CUDA', 'No CUDA device detected — running on CPU')
    n = torch.cuda.device_count()
    parts = []
    for i in range(n):
        name = torch.cuda.get_device_name(i)
        mem = torch.cuda.get_device_properties(i).total_memory
        parts.append(f'{name} ({_fmt_bytes(mem)})')
    return _ok('GPU / CUDA', f'{n}× GPU · ' + ' · '.join(parts))


# ── Integrations ──────────────────────────────────────────────────────────────

def check_stripe():
    if not getattr(settings, 'STRIPE_ENABLED', False):
        return _na('Stripe', 'STRIPE_SECRET_KEY not set — mock-purchase mode')
    try:
        import stripe
        stripe.api_key = settings.STRIPE_SECRET_KEY
        account = stripe.Account.retrieve()
        mode = 'live' if not settings.STRIPE_SECRET_KEY.startswith('sk_test') else 'test'
        return _ok('Stripe', f'{mode} mode · account {account.id[:14]}…')
    except Exception as exc:
        return _error('Stripe', f'Key invalid: {exc}',
                      hint='Verify STRIPE_SECRET_KEY in .env')


def check_ollama():
    """
    Ollama powers the optional AI video summary feature.
    - If USE_OLLAMA=false → 'na' (intentionally disabled)
    - If USE_OLLAMA=true  → ping OLLAMA_BASE_URL/api/tags and verify the
      configured model is pulled.
    """
    use_ollama = getattr(settings, 'USE_OLLAMA', False)
    base_url   = getattr(settings, 'OLLAMA_BASE_URL', 'http://localhost:11434').rstrip('/')
    model_name = getattr(settings, 'OLLAMA_MODEL', 'llama3.2')

    if not use_ollama:
        return _na('Ollama (AI summaries)',
                   f'USE_OLLAMA=false — video summary feature disabled '
                   f'(would use {model_name} at {base_url})')

    try:
        import urllib.request, json as _json
        req = urllib.request.Request(f'{base_url}/api/tags',
                                     headers={'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = _json.loads(resp.read().decode('utf-8'))
    except Exception as exc:
        return _error('Ollama (AI summaries)',
                      f'Cannot reach {base_url}: {exc}',
                      hint='Start Ollama: `ollama serve` or `brew services start ollama`')

    models = data.get('models') or []
    # Ollama returns names like "llama3.2:latest" — match prefix
    matched = next(
        (m for m in models if m.get('name', '').split(':')[0] == model_name.split(':')[0]),
        None,
    )
    if not matched:
        available = ', '.join(m.get('name', '?') for m in models[:5]) or 'none'
        return _warn('Ollama (AI summaries)',
                     f'Reachable but model "{model_name}" not pulled (available: {available})',
                     hint=f'Run: ollama pull {model_name}')

    size = matched.get('size', 0)
    return _ok('Ollama (AI summaries)',
               f'{matched.get("name", model_name)} · {_fmt_bytes(size)} · {len(models)} model(s) installed')


def check_smtp():
    """
    Only attempt a TCP probe if EMAIL_HOST has been explicitly set in .env.
    Django's default is 'localhost:25' which is almost never what someone wants
    — treating that as 'not configured' avoids a misleading red row.
    """
    # Look directly at the env var rather than `settings.EMAIL_HOST` so we can
    # tell the difference between "user set it" and "Django default".
    host = os.getenv('EMAIL_HOST', '').strip()
    if not host:
        return _na('SMTP', 'EMAIL_HOST not set in .env — outbound emails disabled '
                           '(nothing in the codebase sends mail yet)')
    port = int(os.getenv('EMAIL_PORT', '587') or 587)
    try:
        sock = socket.create_connection((host, port), timeout=3)
        sock.close()
        user = os.getenv('EMAIL_HOST_USER', '') or '(no auth)'
        return _ok('SMTP', f'{host}:{port} reachable · user: {user}')
    except Exception as exc:
        return _error('SMTP', f'Cannot reach {host}:{port}: {exc}',
                      hint='Check EMAIL_HOST / EMAIL_PORT in .env or firewall')


def check_pyannote():
    """
    Speaker diarization needs pyannote model weights cached locally. HF_TOKEN
    is only required for the FIRST download — after that the cache is enough,
    so we check the cache directly rather than relying on the env var.
    """
    hub = HF_CACHE / 'hub'
    if not hub.exists():
        return _warn('Pyannote (diarization)', 'HuggingFace cache not initialised',
                     hint='Set HF_TOKEN once and run a diarization task — models auto-download')

    needed = ['pyannote--segmentation', 'pyannote--speaker-diarization']
    found = []
    for n in needed:
        match = next((d for d in hub.iterdir() if d.is_dir() and n in d.name), None)
        if match:
            found.append(match)

    if not found:
        token = getattr(settings, 'HF_TOKEN', '') or ''
        if not token or token in ('s', 'changeme', 'your-token'):
            return _warn('Pyannote (diarization)', 'Models not cached and HF_TOKEN not set',
                         hint='Set HF_TOKEN in .env, accept terms at hf.co/pyannote/speaker-diarization-3.1, '
                              'then run a diarization task to download (~500 MB)')
        return _warn('Pyannote (diarization)', 'HF_TOKEN set but models not yet downloaded',
                     hint='Run a diarization task to trigger download')

    size = sum(f.stat().st_size for d in found for f in d.rglob('*') if f.is_file())
    return _ok('Pyannote (diarization)', f'{len(found)}/{len(needed)} models cached · {_fmt_bytes(size)}')


# ── Per-tenant checks ─────────────────────────────────────────────────────────

def check_tenant_dbs():
    """Every active tenant's database must accept a connection + trivial query."""
    from .models import Tenant
    from .provisioning import _register_db_alias
    from django.db import connections

    tenants = list(Tenant.objects.using('control').filter(is_active=True))
    if not tenants:
        return _na('Tenant databases', 'No active tenants')

    bad = []
    for t in tenants:
        try:
            _register_db_alias(t.db_name)
            with connections[t.db_name].cursor() as cur:
                cur.execute('SELECT 1')
        except Exception as exc:
            bad.append(f'{t.slug} ({type(exc).__name__})')
    if bad:
        return _error('Tenant databases', f'{len(bad)}/{len(tenants)} unreachable: ' + ', '.join(bad),
                      hint='Check PostgreSQL and that each DB exists')
    return _ok('Tenant databases', f'All {len(tenants)} tenant DBs reachable')


def check_tenant_media_roots():
    """Every active tenant's media root must exist and be writable
    (honours custom media_root_absolute paths — catches unmounted NAS/SSHFS)."""
    import os as _os
    from .models import Tenant

    tenants = list(Tenant.objects.using('control').filter(is_active=True))
    if not tenants:
        return _na('Tenant media roots', 'No active tenants')

    bad = []
    for t in tenants:
        custom = (getattr(t, 'media_root_absolute', '') or '').strip()
        root = custom if custom else str(Path(settings.MEDIA_ROOT) / t.media_folder)
        if not _os.path.isdir(root):
            bad.append(f'{t.slug}: missing ({root})')
            continue
        test = _os.path.join(root, '.health_write_test')
        try:
            with open(test, 'wb') as fh:
                fh.write(b'x')
            _os.remove(test)
        except OSError:
            bad.append(f'{t.slug}: not writable')
    if bad:
        return _error('Tenant media roots', '; '.join(bad),
                      hint='Custom paths must be mounted and writable — check NAS/SSHFS mounts')
    return _ok('Tenant media roots', f'All {len(tenants)} roots writable')


def check_recent_task_failures():
    """Unresolved Celery failures in the last 24 h (from the FailedTask log)."""
    from datetime import timedelta
    from django.utils import timezone
    from .models import FailedTask

    cutoff = timezone.now() - timedelta(hours=24)
    n = FailedTask.objects.using('control').filter(
        resolved=False, failed_at__gte=cutoff).count()
    total_unresolved = FailedTask.objects.using('control').filter(resolved=False).count()
    if n == 0 and total_unresolved == 0:
        return _ok('Task failures (24h)', 'No unresolved failures')
    if n == 0:
        return _warn('Task failures (24h)',
                     f'None in last 24h, but {total_unresolved} older unresolved',
                     hint='Review /tasks/failed/')
    if n >= 10:
        return _error('Task failures (24h)', f'{n} unresolved failures in last 24h',
                      hint='Something is systematically broken — see /tasks/failed/')
    return _warn('Task failures (24h)', f'{n} unresolved failure(s) in last 24h',
                 hint='Review /tasks/failed/')


# ── Master list ───────────────────────────────────────────────────────────────

# Each entry: (stable_id, group_label, check_function).
# stable_id is what the browser uses to look up the result endpoint.
# group_label is used to insert section headers in the table.
ALL_CHECKS = [
    # ── Boot dependencies ────────────────────────────────────────────
    ('db_default',       'Boot dependencies', check_db_default),
    ('db_control',       'Boot dependencies', check_db_control),
    ('redis',            'Boot dependencies', check_redis),
    ('disk_space',       'Boot dependencies', check_disk_space),
    ('ffmpeg',           'Boot dependencies', check_ffmpeg),
    # ── Per-tenant ───────────────────────────────────────────────────
    ('tenant_dbs',         'Tenants', check_tenant_dbs),
    ('tenant_media_roots', 'Tenants', check_tenant_media_roots),
    ('task_failures',      'Tenants', check_recent_task_failures),
    # ── Celery workers ───────────────────────────────────────────────
    ('celery_main',      'Celery workers',    check_celery_main),
    ('celery_audio',     'Celery workers',    check_celery_audio),
    ('celery_translation','Celery workers',   check_celery_translation),
    ('celery_live',      'Celery workers',    check_celery_live),
    # ── AI model caches ──────────────────────────────────────────────
    ('whisper',          'AI model caches',   check_whisper),
    ('clip',             'AI model caches',   check_clip),
    ('blip',             'AI model caches',   check_blip),
    ('nllb',             'AI model caches',   check_nllb),
    ('yolo',             'AI model caches',   check_yolo),
    ('insightface',      'AI model caches',   check_insightface),
    ('panns',            'AI model caches',   check_panns),
    ('pyannote',         'AI model caches',   check_pyannote),
    # ── Hardware ─────────────────────────────────────────────────────
    ('gpu',              'Hardware',          check_gpu),
    # ── Integrations ─────────────────────────────────────────────────
    ('stripe',           'Integrations',      check_stripe),
    ('smtp',             'Integrations',      check_smtp),
    ('ollama',           'Integrations',      check_ollama),
]


# Indexed lookup so the API endpoint can run a single check by id
CHECKS_BY_ID = {cid: (group, fn) for (cid, group, fn) in ALL_CHECKS}


def run_check_by_id(check_id: str) -> dict | None:
    """Run a single check by its stable id. Returns the result dict, or None if id is unknown."""
    entry = CHECKS_BY_ID.get(check_id)
    if not entry:
        return None
    _group, fn = entry
    return _timed(fn)


# Checks that are slow or irrelevant for automated monitoring. Model-cache
# checks scan GBs of disk and never regress on their own; GPU/integrations
# are static config. The automated sweep cares about things that BREAK.
AUTOMATED_SKIP_IDS = {
    'whisper', 'clip', 'blip', 'nllb', 'yolo',
    'insightface', 'panns', 'pyannote', 'gpu',
}




def get_check_manifest() -> list:
    """
    Return the list of check id/name/group tuples so the page can render
    placeholder rows before any checks have run.
    """
    return [
        {'id': cid, 'group': group, 'name': fn.__name__.replace('check_', '').replace('_', ' ').title()}
        for (cid, group, fn) in ALL_CHECKS
    ]


def run_all_checks(automated: bool = False):
    """
    Run every check synchronously. Used by /api/health/, tests, and the
    hourly beat sweep. Results are annotated with id + group.

    automated=True skips the slow model-cache scans and static checks
    (AUTOMATED_SKIP_IDS) — the sweep only cares about things that can
    break at runtime.
    """
    results = []
    for cid, group, fn in ALL_CHECKS:
        if automated and cid in AUTOMATED_SKIP_IDS:
            continue
        res = _timed(fn)
        res['id'] = cid
        res['group'] = group
        results.append(res)
    return results


def summarise(results):
    """Aggregate counts."""
    counts = {'ok': 0, 'warn': 0, 'error': 0, 'na': 0}
    for r in results:
        counts[r['status']] = counts.get(r['status'], 0) + 1
    total_ms = sum(r.get('elapsed_ms', 0) for r in results)
    return {
        **counts,
        'total':      len(results),
        'total_ms':   total_ms,
        'all_green':  counts['error'] == 0 and counts['warn'] == 0,
        'has_errors': counts['error'] > 0,
    }
