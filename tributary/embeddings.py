"""Embeddings via a local sentence-transformers model — no AWS, no network.

Offline mode (TRIBUTARY_OFFLINE=1) produces deterministic bag-of-words hash
embeddings so the memory layer and tests run without downloading a model —
similar texts still land near each other.
"""

import hashlib
import math
import re
import time

from tributary import config, log

logger = log.get_logger(__name__)

_model = None


def _local_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        # The first load of the default model downloads ~1.3 GB; say so, or
        # the demo just looks hung.
        logger.info("loading embedding model (first run downloads it)",
                    model=config.EMBED_MODEL_ID)
        start = time.time()
        _model = SentenceTransformer(config.EMBED_MODEL_ID)
        logger.info("embedding model loaded", model=config.EMBED_MODEL_ID,
                    seconds=round(time.time() - start, 1))
    return _model


def embed(text: str) -> list[float]:
    if config.OFFLINE:
        return _fake_embed(text)
    vec = _local_model().encode(text[:8000], normalize_embeddings=True)
    return vec.tolist()


def _fake_embed(text: str) -> list[float]:
    dims = config.EMBED_DIMENSIONS
    vec = [0.0] * dims
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        h = int.from_bytes(hashlib.md5(word.encode()).digest()[:4], "big")
        vec[h % dims] += 1.0
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]
