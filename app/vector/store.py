"""Chroma-backed vector store for the product catalog.

Wraps Chroma behind a small interface so the rest of the app never imports
chromadb directly. Embeddings are supplied by us (app.ai.embeddings), never
computed by Chroma, which keeps all AI traffic on the Gemini path.

One Chroma behaviour leaks into the design and is worth stating: a collection's
dimensionality is pinned by its first insert, and every later write *or query*
with a differently-sized vector raises. `dimension()` exposes what the
collection is pinned to so `catalog.reconcile()` can reset before the first bad
upsert rather than after it.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import chromadb
from chromadb.config import Settings as ChromaSettings

from app.config import settings

logger = logging.getLogger(__name__)

_client: chromadb.ClientAPI | None = None
_collection = None
_lock = threading.Lock()


def _get_collection():
    global _client, _collection
    if _collection is None:
        with _lock:
            if _collection is None:
                path = settings.resolve(settings.chroma_path)
                path.mkdir(parents=True, exist_ok=True)
                _client = chromadb.PersistentClient(
                    path=str(path),
                    settings=ChromaSettings(anonymized_telemetry=False, allow_reset=True),
                )
                _collection = _client.get_or_create_collection(
                    name=settings.chroma_collection,
                    metadata={"hnsw:space": "cosine"},
                )
    return _collection


def _flatten_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """Chroma metadata values must be scalars."""
    out: dict[str, Any] = {}
    for key, value in meta.items():
        if isinstance(value, (str, int, float, bool)):
            out[key] = value
        elif isinstance(value, (list, tuple)):
            out[key] = ", ".join(str(v) for v in value)
        elif value is None:
            continue
        else:
            out[key] = str(value)
    return out


def upsert_product(
    product_id: int, document: str, embedding: list[float], metadata: dict[str, Any]
) -> None:
    _get_collection().upsert(
        ids=[str(product_id)],
        documents=[document],
        embeddings=[embedding],
        metadatas=[_flatten_meta(metadata)],
    )


def delete_product(product_id: int) -> None:
    _get_collection().delete(ids=[str(product_id)])


def query(
    embedding: list[float],
    *,
    n_results: int = 10,
    where: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Nearest neighbours as [{id, score, metadata, document}], best first."""
    collection = _get_collection()
    if collection.count() == 0:
        return []

    try:
        raw = collection.query(
            query_embeddings=[embedding],
            n_results=min(n_results, max(collection.count(), 1)),
            where=where or None,
            include=["metadatas", "documents", "distances"],
        )
    except Exception:
        logger.warning("Vector query failed (where=%s); retrying unfiltered", where, exc_info=True)
        try:
            raw = collection.query(
                query_embeddings=[embedding],
                n_results=min(n_results, max(collection.count(), 1)),
                include=["metadatas", "documents", "distances"],
            )
        except Exception:
            # Dropping the where-clause does not help every failure - a
            # dimension mismatch raises on the query itself, whatever the
            # filter - and an unprotected retry turns that into a 500 on the
            # dashboard. No candidates is a state the graph already handles.
            logger.warning("Vector query failed unfiltered as well; no candidates", exc_info=True)
            return []

    ids = (raw.get("ids") or [[]])[0]
    distances = (raw.get("distances") or [[]])[0]
    metadatas = (raw.get("metadatas") or [[]])[0]
    documents = (raw.get("documents") or [[]])[0]

    results = []
    for i, doc_id in enumerate(ids):
        distance = distances[i] if i < len(distances) else 1.0
        results.append(
            {
                "id": int(doc_id),
                "score": max(0.0, 1.0 - float(distance)),  # cosine distance -> similarity
                "distance": float(distance),
                "metadata": metadatas[i] if i < len(metadatas) else {},
                "document": documents[i] if i < len(documents) else "",
            }
        )
    return results


def all_ids() -> set[int]:
    """Every id in the vector store - used to diff against SQL."""
    raw = _get_collection().get(include=[])
    out: set[int] = set()
    for doc_id in raw.get("ids") or []:
        try:
            out.add(int(doc_id))
        except (TypeError, ValueError):
            continue
    return out


def get_metadata(product_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Stored metadata by id - lets reconcile detect stale content hashes."""
    if not product_ids:
        return {}
    raw = _get_collection().get(ids=[str(i) for i in product_ids], include=["metadatas"])
    out: dict[int, dict[str, Any]] = {}
    for doc_id, meta in zip(raw.get("ids") or [], raw.get("metadatas") or []):
        try:
            out[int(doc_id)] = meta or {}
        except (TypeError, ValueError):
            continue
    return out


def count() -> int:
    try:
        return _get_collection().count()
    except Exception:
        logger.warning("Vector count failed", exc_info=True)
        return 0


def dimension() -> int | None:
    """Width the collection is pinned to, or None if it holds no vectors yet.

    Chroma has no API for "what dimension is this collection", because the
    answer only exists once something has been inserted - so we read one stored
    embedding. An empty collection returns None, which means "not yet pinned,
    any width is fine".
    """
    try:
        collection = _get_collection()
        if collection.count() == 0:
            return None
        raw = collection.get(limit=1, include=["embeddings"])
    except Exception:
        logger.warning("Could not read vector dimensionality", exc_info=True)
        return None

    rows = raw.get("embeddings")
    # Chroma returns embeddings as a numpy array, so `or []` and truthiness
    # checks on it are both wrong - compare lengths explicitly.
    if rows is None or len(rows) == 0:
        return None
    return int(len(rows[0]))


def reset() -> None:
    """Drop and recreate the collection (used by tests and full reindex)."""
    global _collection
    with _lock:
        client = _client or chromadb.PersistentClient(
            path=str(settings.resolve(settings.chroma_path)),
            settings=ChromaSettings(anonymized_telemetry=False, allow_reset=True),
        )
        try:
            client.delete_collection(settings.chroma_collection)
        except Exception:
            pass
        _collection = client.get_or_create_collection(
            name=settings.chroma_collection, metadata={"hnsw:space": "cosine"}
        )
