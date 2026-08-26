"""Embedding provider: Gemini first, deterministic local fallback second.

The fallback is a hashed character n-gram + token vectorizer. It is not
semantically as good as a trained model, but it is dependency-free,
deterministic, and puts related product text near each other - enough to build
and demo the whole retrieval pipeline with no Gemini key present.

Two things about dimensionality, because they are the difference between the
fallback working and the fallback crashing:

  * **Chroma pins a collection's dimensionality at first insert.** Writing a
    vector of a different width raises, and so does *querying* with one. So the
    local fallback is dimension-parametric and emits `target_dim()` - whatever
    this process intends to write - rather than a fixed 512. A key that runs out
    of quota halfway through then degrades to worse vectors of the right width
    instead of a failed upsert.

  * **`target_dim()` is derived from configuration, not from runtime health.**
    If it flipped when Gemini started failing, the fallback would produce the
    very mismatch it exists to avoid.

Switching providers DOES invalidate existing vectors, so `provider_id()` is
stored in vector metadata and a mismatch triggers a re-index via the reconcile
job - which also resets the collection when the width changed.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from collections import OrderedDict

from app.ai.gemini_client import TASK_DOCUMENT, TASK_QUERY, gemini_client
from app.config import settings

logger = logging.getLogger(__name__)

__all__ = [
    "TASK_DOCUMENT",
    "TASK_QUERY",
    "cache_stats",
    "configured_provider",
    "embed_one",
    "embed_texts",
    "local_provider",
    "provider_id",
    "provider_observed",
    "resolve_provider",
    "target_dim",
    "using_fallback",
]

# Width the local embedder uses when Gemini is not in the picture at all. With a
# key configured, it matches the Gemini output dimensionality instead.
FALLBACK_DIM = 512
_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Query embeddings repeat constantly (same searches, same profiles), so an
# LRU here removes a large share of would-be Gemini calls.
_CACHE_MAX = 512
_cache: OrderedDict[str, list[float]] = OrderedDict()


def _cache_key(text: str, provider: str, task: str) -> str:
    # `task` is part of the key because Gemini returns a different vector for
    # the same text depending on whether it is being stored or searched with.
    return hashlib.sha256(f"{provider}::{task}::{text}".encode()).hexdigest()


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
# Dimensionality
# --------------------------------------------------------------------------
def target_dim() -> int:
    """The vector width this process writes, whichever producer wins.

    Read from configuration rather than from live provider health on purpose:
    the whole point is that a Gemini failure mid-run yields a *same-width*
    local vector, so the Chroma upsert still succeeds and retrieval degrades
    instead of breaking. `catalog.reconcile()` compares this against the
    collection's actual width and resets when they diverge.
    """
    if settings.gemini_configured and settings.gemini_embed_enabled:
        return int(settings.gemini_embed_dimensions)
    return FALLBACK_DIM


# --------------------------------------------------------------------------
# Local deterministic fallback
# --------------------------------------------------------------------------
def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower())


def _local_embed_one(text: str, dim: int) -> list[float]:
    """Hashed bag of tokens, bigrams, and char 4-grams, L2-normalised."""
    vec = [0.0] * dim
    tokens = _tokenize(text)
    if not tokens:
        return vec

    def bump(feature: str, weight: float) -> None:
        h = hashlib.blake2b(feature.encode(), digest_size=8).digest()
        idx = int.from_bytes(h[:4], "big") % dim
        sign = 1.0 if h[4] & 1 else -1.0
        vec[idx] += sign * weight

    for tok in tokens:
        bump(tok, 1.0)
        # char n-grams give partial-word overlap ("cushioned" ~ "cushion")
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
def local_provider() -> str:
    """Identity of the local embedder, width included.

    The width is part of the identity because `local:hash512` and
    `local:hash768` are genuinely different vector spaces - comparing one
    against the other is meaningless, so reconcile has to be able to tell them
    apart in stored metadata.
    """
    return f"local:hash{target_dim()}"


def _gemini_provider() -> str:
    return f"gemini:{settings.gemini_embed_model}@{settings.gemini_embed_dimensions}"


# Which provider actually produced the most recent vectors. `using_fallback()`
# is optimistic before the first call (Gemini is assumed available until it
# fails), so writing provider_id() into vector metadata before any call would
# mislabel local vectors as Gemini ones - and reconcile would then never repair
# them. This is updated by embed_texts() once the truth is known.
_actual_provider: str | None = None


# Embedded once per process to settle which provider is really live, so
# reconcile compares stored metadata against an observed value instead of a
# guess. Cheap: one call, then served from the LRU forever.
PROBE_TEXT = "bazario embedding provider probe"


def provider_id() -> str:
    """The provider that genuinely produced the vectors we are storing.

    Stored in vector metadata so the reconcile job can detect a provider
    change and re-embed the catalog. Only meaningful after an embed call has
    actually happened - callers that compare against stored metadata must use
    `resolve_provider()` instead, or they will compare against a guess.
    """
    if _actual_provider is not None:
        return _actual_provider
    return local_provider() if using_fallback() else _gemini_provider()


def provider_observed() -> bool:
    """True once a real embed call has told us which provider is live."""
    return _actual_provider is not None


async def resolve_provider() -> str:
    """Return the provider, probing once if it has not been observed yet.

    `provider_id()` is optimistic before the first call: it assumes Gemini works
    until proven otherwise. Comparing stored vector metadata against that guess
    would mark the whole catalog provider-stale on every cold start of a
    deployment where Gemini embeddings are unavailable, re-embedding everything
    for nothing. One probe embed removes the guesswork.
    """
    if _actual_provider is None:
        await embed_texts([PROBE_TEXT], purpose="provider_probe")
    return _actual_provider or local_provider()


def configured_provider() -> str:
    """What we would *try* to use - for display, not for metadata."""
    return local_provider() if using_fallback() else _gemini_provider()


def using_fallback() -> bool:
    if not (settings.gemini_configured and settings.gemini_embed_enabled):
        return True
    # Gemini told us embeddings aren't available to this key.
    return not gemini_client.embeddings_supported


async def embed_texts(
    texts: list[str],
    *,
    purpose: str = "embed",
    user_id: int | None = None,
    task: str = TASK_DOCUMENT,
) -> list[list[float]]:
    """Embed a batch. Falls back locally if Gemini is unset or failing.

    `task` is Gemini's retrieval asymmetry: catalog text is stored as a
    document, a shopper's search is a query against those documents, and using
    one where the other belongs costs relevance without raising anything. The
    local fallback ignores it - a hash has no notion of direction - but it still
    keys the cache, so switching back to Gemini does not serve query vectors
    where document vectors belong.
    """
    if not texts:
        return []

    global _actual_provider

    provider = provider_id()
    results: list[list[float] | None] = [None] * len(texts)
    misses: list[int] = []

    for i, text in enumerate(texts):
        hit = _cache_get(_cache_key(text, provider, task))
        if hit is not None:
            results[i] = hit
        else:
            misses.append(i)

    if misses:
        pending = [texts[i] for i in misses]
        vectors: list[list[float]] | None = None
        used_gemini = False
        dim = target_dim()

        if not using_fallback():
            try:
                vectors = await gemini_client.embed(
                    pending, purpose=purpose, user_id=user_id, task_type=task
                )
                if len(vectors) != len(pending):
                    logger.warning("Gemini returned %d vectors for %d inputs; using fallback",
                                   len(vectors), len(pending))
                    vectors = None
                elif any(len(v) != dim for v in vectors):
                    # Would poison a pinned Chroma collection. Better to write
                    # honest local vectors of the right width and let reconcile
                    # re-embed than to fail every upsert from here on.
                    logger.warning(
                        "Gemini returned %s-dim vectors, expected %s; using fallback",
                        len(vectors[0]) if vectors else 0, dim,
                    )
                    vectors = None
                else:
                    used_gemini = True
            except Exception as exc:  # GeminiError, timeouts, bad shapes
                logger.warning("Gemini embeddings failed (%s); using local fallback", exc)
                vectors = None

        if vectors is None:
            vectors = [_local_embed_one(t, dim) for t in pending]

        # Record what actually produced these vectors, so metadata is honest.
        actual = _gemini_provider() if used_gemini else local_provider()
        if _actual_provider != actual:
            if _actual_provider is not None:
                logger.info("Embedding provider changed: %s -> %s", _actual_provider, actual)
            _actual_provider = actual

        # Cache under the provider that really produced them - a later switch
        # must not serve stale vectors from the wrong model.
        for idx, vec in zip(misses, vectors):
            results[idx] = vec
            _cache_put(_cache_key(texts[idx], actual, task), vec)

    zeros = [0.0] * target_dim()
    return [v if v is not None else list(zeros) for v in results]


async def embed_one(
    text: str,
    *,
    purpose: str = "embed",
    user_id: int | None = None,
    task: str = TASK_DOCUMENT,
) -> list[float]:
    vectors = await embed_texts([text], purpose=purpose, user_id=user_id, task=task)
    return vectors[0] if vectors else []
