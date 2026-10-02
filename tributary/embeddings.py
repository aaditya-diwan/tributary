"""Embeddings via a local sentence-transformers model — no AWS, no network.

Offline mode (TRIBUTARY_OFFLINE=1) produces deterministic bag-of-words hash
embeddings so the memory layer and tests run without downloading a model —
similar texts still land near each other.
"""

import hashlib
import math
import re
import threading
import time

from tributary import config, log

logger = log.get_logger(__name__)

_model = None
_model_lock = threading.Lock()  # one load, even if warm_up() and a call race


def _local_model():
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                # Importing torch alone can take 40 s+ on a cold Windows start,
                # and the first load of the default model downloads ~1.3 GB;
                # say so, or the caller just looks hung.
                logger.info("loading embedding model (first run downloads it)",
                            model=config.EMBED_MODEL_ID)
                start = time.time()
                from sentence_transformers import SentenceTransformer

                _model = SentenceTransformer(config.EMBED_MODEL_ID)
                logger.info("embedding model loaded", model=config.EMBED_MODEL_ID,
                            seconds=round(time.time() - start, 1))
    return _model


def warm_up() -> None:
    """Start loading the model in the background, so the first embed() of a
    long-lived process (the MCP server) doesn't pay for it inside a tool call
    that the client may time out."""
    if config.OFFLINE:
        return

    def _load():
        try:
            _local_model()
        except Exception as e:
            logger.error("embedding model warm-up failed; will retry on first use",
                         error=log.preview(e, 200))

    threading.Thread(target=_load, name="embedding-warm-up", daemon=True).start()


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
