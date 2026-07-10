"""Embeddings via Amazon Bedrock (Titan Text Embeddings V2).

Offline mode (TRIBUTARY_OFFLINE=1) produces deterministic bag-of-words hash
embeddings so the memory layer and tests run without AWS credentials —
similar texts still land near each other.
"""

import hashlib
import json
import math
import re

from tributary import config

_client = None


def _bedrock():
    global _client
    if _client is None:
        import boto3

        _client = boto3.client("bedrock-runtime", region_name=config.AWS_REGION)
    return _client


def embed(text: str) -> list[float]:
    if config.OFFLINE:
        return _fake_embed(text)
    resp = _bedrock().invoke_model(
        modelId=config.BEDROCK_EMBED_MODEL_ID,
        body=json.dumps(
            {
                "inputText": text[:8000],
                "dimensions": config.EMBED_DIMENSIONS,
                "normalize": True,
            }
        ),
    )
    return json.loads(resp["body"].read())["embedding"]


def _fake_embed(text: str) -> list[float]:
    dims = config.EMBED_DIMENSIONS
    vec = [0.0] * dims
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        h = int.from_bytes(hashlib.md5(word.encode()).digest()[:4], "big")
        vec[h % dims] += 1.0
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]
