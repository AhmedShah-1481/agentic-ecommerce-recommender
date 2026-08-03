"""Embedding provider: Mesh first, deterministic local fallback second.

The fallback is a hashed character n-gram + token vectorizer. It is not
semantically as good as a trained model, but it is dependency-free,
deterministic, and puts related course text near each other - enough to
build and demo the whole retrieval pipeline with no Mesh key present.

Both providers emit the same dimensionality so the Chroma collection stays
valid if you switch. Switching providers DOES invalidate existing vectors,
so `provider_id()` is stored in vector metadata and a mismatch triggers a
re-index via the reconcile job.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from collections import OrderedDict

from app.ai.mesh_client import mesh_client
from app.config import settings

logger = logging.getLogger(__name__)

FALLBACK_DIM = 512
_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Query embeddings repeat constantly (same searches, same profiles), so an
# LRU here removes a large share of would-be Mesh calls.
_CACHE_MAX = 512
_cache: OrderedDict[str, list[float]] = OrderedDict()


def _cache_key(text: str, provider: str) -> str:
    return hashlib.sha256(f"{provider}::{text}".encode()).hexdigest()


def _cache_get(key: str) -> list[float] | None:
    vec = _cache.get(key)
    if vec is not None:
        _cache.move_to_end(key)
    return vec


def _cache_put(key: str, vec: list[float]) -> None:
    _cache[key] = vec
    _cache.move_to_end(key)
    while len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)


def cache_stats() -> dict[str, int]:
    return {"size": len(_cache), "capacity": _CACHE_MAX}


# --------------------------------------------------------------------------
# Local deterministic fallback
# --------------------------------------------------------------------------
def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower())


def _local_embed_one(text: str) -> list[float]:
    """Hashed bag of tokens, bigrams, and char 4-grams, L2-normalised."""
    vec = [0.0] * FALLBACK_DIM
    tokens = _tokenize(text)
    if not tokens:
        return vec

    def bump(feature: str, weight: float) -> None:
        h = hashlib.blake2b(feature.encode(), digest_size=8).digest()
        idx = int.from_bytes(h[:4], "big") % FALLBACK_DIM
        sign = 1.0 if h[4] & 1 else -1.0
        vec[idx] += sign * weight

    for tok in tokens:
        bump(tok, 1.0)
        # char n-grams give partial-word overlap ("agentic" ~ "agent")
        padded = f"^{tok}$"
        for i in range(len(padded) - 3):
            bump(padded[i : i + 4], 0.35)

    for a, b in zip(tokens, tokens[1:]):
        bump(f"{a}_{b}", 0.7)

    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------
LOCAL_PROVIDER = f"local:hash{FALLBACK_DIM}"

# Which provider actually produced the most recent vectors. `using_fallback()`
# is optimistic before the first call (Mesh is assumed available until it
# fails), so writing provider_id() into vector metadata before any call would
# mislabel local vectors as Mesh ones - and reconcile would then never repair
# them. This is updated by embed_texts() once the truth is known.
_actual_provider: str | None = None


# Embedded once per process to settle which provider is really live, so
# reconcile compares stored metadata against an observed value instead of a
# guess. Cheap: one call, then served from the LRU forever.
PROBE_TEXT = "learnloop embedding provider probe"


def provider_id() -> str:
    """The provider that genuinely produced the vectors we are storing.

    Stored in vector metadata so the reconcile job can detect a provider
    change and re-embed the catalog. Only meaningful after an embed call has
    actually happened - callers that compare against stored metadata must use
    `resolve_provider()` instead, or they will compare against a guess.
    """
    if _actual_provider is not None:
        return _actual_provider
    return LOCAL_PROVIDER if using_fallback() else f"mesh:{settings.mesh_embed_model}"


def provider_observed() -> bool:
    """True once a real embed call has told us which provider is live."""
    return _actual_provider is not None


async def resolve_provider() -> str:
    """Return the provider, probing once if it has not been observed yet.

    `provider_id()` is optimistic before the first call: it assumes Mesh works
    until proven otherwise. Comparing stored vector metadata against that guess
    would mark the whole catalog provider-stale on every cold start of a
    deployment where Mesh embeddings are unavailable, re-embedding everything
    for nothing. One probe embed removes the guesswork.
    """
    if _actual_provider is None:
        await embed_texts([PROBE_TEXT], purpose="provider_probe")
    return _actual_provider or LOCAL_PROVIDER


def configured_provider() -> str:
    """What we would *try* to use - for display, not for metadata."""
    return LOCAL_PROVIDER if using_fallback() else f"mesh:{settings.mesh_embed_model}"


def using_fallback() -> bool:
    if not (settings.mesh_configured and settings.mesh_embed_enabled):
        return True
    # Mesh told us embeddings aren't available for this deployment.
    return not mesh_client.embeddings_supported


async def embed_texts(
    texts: list[str], *, purpose: str = "embed", user_id: int | None = None
) -> list[list[float]]:
    """Embed a batch. Falls back locally if Mesh is unset or failing."""
    if not texts:
        return []

    global _actual_provider

    provider = provider_id()
    results: list[list[float] | None] = [None] * len(texts)
    misses: list[int] = []

    for i, text in enumerate(texts):
        hit = _cache_get(_cache_key(text, provider))
        if hit is not None:
            results[i] = hit
        else:
            misses.append(i)

    if misses:
        pending = [texts[i] for i in misses]
        vectors: list[list[float]] | None = None
        used_mesh = False

        if not using_fallback():
            try:
                vectors = await mesh_client.embed(pending, purpose=purpose, user_id=user_id)
                if len(vectors) != len(pending):
                    logger.warning("Mesh returned %d vectors for %d inputs; using fallback",
                                   len(vectors), len(pending))
                    vectors = None
                else:
                    used_mesh = True
            except Exception as exc:  # MeshError, timeouts, bad shapes
                logger.warning("Mesh embeddings failed (%s); using local fallback", exc)
                vectors = None

        if vectors is None:
            vectors = [_local_embed_one(t) for t in pending]

        # Record what actually produced these vectors, so metadata is honest.
        actual = f"mesh:{settings.mesh_embed_model}" if used_mesh else LOCAL_PROVIDER
        if _actual_provider != actual:
            if _actual_provider is not None:
                logger.info("Embedding provider changed: %s -> %s", _actual_provider, actual)
            _actual_provider = actual

        # Cache under the provider that really produced them - a later switch
        # must not serve stale vectors from the wrong model.
        for idx, vec in zip(misses, vectors):
            results[idx] = vec
            _cache_put(_cache_key(texts[idx], actual), vec)

    return [v if v is not None else [0.0] * FALLBACK_DIM for v in results]


async def embed_one(text: str, *, purpose: str = "embed", user_id: int | None = None) -> list[float]:
    vectors = await embed_texts([text], purpose=purpose, user_id=user_id)
    return vectors[0] if vectors else []
