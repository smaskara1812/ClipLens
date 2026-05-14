"""
clip_utils.py — shared CLIP text-encoding singleton.

Loaded once per Django process, reused by both the main search (views.py)
and the external API search (api_v1.py).  Thread-safe double-checked locking.
"""

import threading
from django.conf import settings

_model  = None
_proc   = None
_lock   = threading.Lock()


def get_clip_text_vector(query: str):
    """
    Encode a text query with CLIP ViT-B/32.

    Returns a Python list[float] (512-dim) ready for pgvector, or None
    if CLIP is disabled or the model/torch is unavailable.
    """
    if not getattr(settings, 'CLIP_ENABLED', True):
        return None

    global _model, _proc
    try:
        import torch
        if _model is None:
            with _lock:
                if _model is None:
                    from transformers import CLIPProcessor, CLIPModel
                    _proc  = CLIPProcessor.from_pretrained('openai/clip-vit-base-patch32')
                    _model = CLIPModel.from_pretrained('openai/clip-vit-base-patch32')
                    _model.eval()

        inputs = _proc(text=[query], return_tensors='pt', padding=True)
        with torch.no_grad():
            feat = _model.get_text_features(**inputs)
            feat = feat / feat.norm(dim=-1, keepdim=True)
        return feat[0].tolist()
    except Exception:
        return None
