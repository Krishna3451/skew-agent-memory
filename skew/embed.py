"""Embeddings behind one interface.

Bedrock (Titan v2, 1024-dim) when AWS credentials are present; otherwise a
deterministic local encoder of the same dimensionality. The benchmark measures
*concurrency correctness*, which does not depend on embedding quality -- but the
dimensions must match so the Bedrock path drops in without a schema change.
"""
from __future__ import annotations

import hashlib
import math
import re

from .config import EMBED_DIM, EMBED_MODEL, AWS_REGION

_TOKEN = re.compile(r"[a-z0-9]+")


class LocalEncoder:
    """Hashed bag-of-words projected onto the unit sphere. Deterministic."""

    name = "local-hash"

    def encode(self, text: str) -> list[float]:
        vec = [0.0] * EMBED_DIM
        for tok in _TOKEN.findall(text.lower()):
            h = hashlib.blake2b(tok.encode(), digest_size=8).digest()
            idx = int.from_bytes(h[:4], "big") % EMBED_DIM
            sign = 1.0 if h[4] & 1 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


class BedrockEncoder:
    name = EMBED_MODEL

    def __init__(self) -> None:
        import boto3  # imported lazily so the local path needs no AWS deps

        self._client = boto3.client("bedrock-runtime", region_name=AWS_REGION)

    def encode(self, text: str) -> list[float]:
        import json

        resp = self._client.invoke_model(
            modelId=EMBED_MODEL,
            body=json.dumps({"inputText": text, "dimensions": EMBED_DIM,
                             "normalize": True}),
        )
        return json.loads(resp["body"].read())["embedding"]


def get_encoder():
    """Bedrock if reachable, else the local encoder. Never raises."""
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError

        enc = BedrockEncoder()
        enc.encode("warmup")
        return enc
    except Exception:
        return LocalEncoder()


def to_pgvector(vec: list[float]) -> str:
    return "[" + ",".join(f"{v:.6f}" for v in vec) + "]"
