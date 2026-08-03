"""The ONLY module that talks to an LLM provider.

All LLM and embedding traffic goes through the Mesh API, per the brief.
Nothing else in the codebase constructs an AI request, so the provider
contract lives entirely here.

Mesh exposes an OpenAI-compatible surface, so we drive it with the official
`openai` SDK pointed at Mesh's base URL - the pattern verified against the
live API:

    client = OpenAI(api_key=..., base_url="https://api.meshapi.ai/v1")
    client.chat.completions.create(model="tencent/hy3", messages=[...])

Notes on robustness:
  * Embeddings may or may not be available on a given Mesh model. If the
    endpoint 404s/400s we mark it unsupported for the process lifetime and
    let app.ai.embeddings fall back locally, rather than retrying forever.
  * `response_format={"type":"json_object"}` is not honoured by every model,
    so json_mode degrades gracefully: on rejection we retry without it and
    lean on `extract_json` to recover the object.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

from openai import (
    APIConnectionError,
    APIStatusError,
    AsyncOpenAI,
    BadRequestError,
    NotFoundError,
    RateLimitError,
)

from app.ai.llm_log import record_call
from app.ai.tracing import redact_inputs, traceable
from app.config import settings

logger = logging.getLogger(__name__)

RETRYABLE = (APIConnectionError, RateLimitError)

# Statuses where retrying an embedding call cannot possibly succeed:
# 401 bad key, 402 insufficient balance, 403 no model access, 404 no such model.
PERMANENT_EMBED_FAILURES = {401, 402, 403, 404}

# tencent/hy3 is a reasoning model: it spends completion tokens on
# `reasoning_content` before emitting any answer. A small max_tokens gets
# entirely consumed by reasoning and yields empty content, so we enforce a
# floor generous enough for the hidden reasoning pass plus the real answer.
MIN_REASONING_HEADROOM = 700


class MeshError(RuntimeError):
    """Mesh unreachable, unauthorised, or returned an unusable response."""


class MeshNotConfigured(MeshError):
    """No API key set. Callers degrade instead of surfacing a 500."""


class MeshClient:
    def __init__(self) -> None:
        self._client: AsyncOpenAI | None = None
        self._lock = asyncio.Lock()
        self._json_mode_supported = True
        self._embeddings_supported = True

    # -- lifecycle ---------------------------------------------------------
    async def _get_client(self) -> AsyncOpenAI:
        if not settings.mesh_configured:
            raise MeshNotConfigured("MESH_API_KEY is not set")
        if self._client is None:
            async with self._lock:
                if self._client is None:
                    self._client = AsyncOpenAI(
                        api_key=settings.mesh_api_key,
                        base_url=settings.mesh_api_base_url,
                        timeout=settings.mesh_timeout_seconds,
                        max_retries=0,  # we own the retry policy below
                    )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    @property
    def embeddings_supported(self) -> bool:
        return self._embeddings_supported

    # -- chat --------------------------------------------------------------
    # Decorated at class-definition time, so with tracing off this is the plain
    # function with no wrapper and no per-call cost.
    @traceable(run_type="llm", name="mesh.chat", process_inputs=redact_inputs)
    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        purpose: str = "chat",
        user_id: int | None = None,
        temperature: float = 0.7,
        max_tokens: int = 900,
        json_mode: bool = False,
        max_retries: int | None = None,
    ) -> str:
        client = await self._get_client()
        started = time.perf_counter()
        want_json = json_mode and self._json_mode_supported
        last_exc: Exception | None = None
        # Reasoning models eat the budget before answering - see the constant.
        max_tokens = max(max_tokens, MIN_REASONING_HEADROOM)
        retries = settings.mesh_max_retries if max_retries is None else max_retries

        attempt = 0
        while attempt <= retries:
            try:
                kwargs: dict[str, Any] = {
                    "model": settings.mesh_chat_model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }
                if want_json:
                    kwargs["response_format"] = {"type": "json_object"}

                resp = await client.chat.completions.create(**kwargs)
                choice = resp.choices[0] if resp.choices else None
                content = (choice.message.content or "") if choice else ""
                usage = getattr(resp, "usage", None)

                if not content.strip():
                    # Reasoning model ran out of budget mid-thought: retry
                    # with a bigger allowance rather than failing the request.
                    finish = getattr(choice, "finish_reason", "") if choice else ""
                    if finish == "length" and attempt < retries:
                        max_tokens = min(int(max_tokens * 2.5), 8000)
                        logger.info("Empty completion (truncated); retrying with more tokens")
                        attempt += 1
                        continue
                    raise MeshError(f"Mesh returned an empty completion (finish={finish!r})")

                record_call(
                    purpose=purpose,
                    user_id=user_id,
                    model=settings.mesh_chat_model,
                    prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
                    completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    ok=True,
                )
                return content

            except BadRequestError as exc:
                # Most likely response_format is unsupported by this model.
                # Deliberately does NOT consume an attempt: dropping json mode
                # is a capability downgrade, not a failed try, and it must
                # still work when max_retries=0.
                if want_json:
                    logger.info("Mesh rejected json_object mode; retrying without it")
                    self._json_mode_supported = False
                    want_json = False
                    continue
                last_exc = exc
                break

            except RETRYABLE as exc:
                last_exc = exc
                if attempt < retries:
                    await asyncio.sleep(0.6 * (2**attempt))
                    attempt += 1
                    continue
                break

            except APIStatusError as exc:
                status = getattr(exc, "status_code", None)
                last_exc = exc
                if status in (500, 502, 503, 504) and attempt < retries:
                    await asyncio.sleep(0.6 * (2**attempt))
                    attempt += 1
                    continue
                break

            except MeshError as exc:
                last_exc = exc
                break

        record_call(
            purpose=purpose,
            user_id=user_id,
            model=settings.mesh_chat_model,
            latency_ms=int((time.perf_counter() - started) * 1000),
            ok=False,
            error=str(last_exc)[:500],
        )
        raise MeshError(f"Mesh chat failed: {last_exc}") from last_exc

    async def chat_json(
        self,
        messages: list[dict[str, str]],
        *,
        purpose: str = "chat_json",
        user_id: int | None = None,
        temperature: float = 0.4,
        max_tokens: int = 900,
    ) -> dict[str, Any]:
        """Chat that must yield a JSON object."""
        raw = await self.chat(
            messages,
            purpose=purpose,
            user_id=user_id,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=True,
        )
        return extract_json(raw)

    # -- embeddings --------------------------------------------------------
    @traceable(run_type="embedding", name="mesh.embed", process_inputs=redact_inputs)
    async def embed(
        self, texts: list[str], *, purpose: str = "embed", user_id: int | None = None
    ) -> list[list[float]]:
        if not texts:
            return []
        if not self._embeddings_supported:
            raise MeshError("Mesh embeddings previously reported unsupported")

        client = await self._get_client()
        started = time.perf_counter()
        last_exc: Exception | None = None

        for attempt in range(settings.mesh_max_retries + 1):
            try:
                resp = await client.embeddings.create(
                    model=settings.mesh_embed_model, input=texts
                )
                rows = sorted(resp.data, key=lambda r: getattr(r, "index", 0))
                vectors = [list(map(float, r.embedding)) for r in rows]
                record_call(
                    purpose=purpose,
                    user_id=user_id,
                    model=settings.mesh_embed_model,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    ok=True,
                )
                return vectors

            except (NotFoundError, BadRequestError) as exc:
                # Endpoint or model unavailable on Mesh - stop asking.
                self._embeddings_supported = False
                last_exc = exc
                logger.warning(
                    "Mesh embeddings unsupported (%s); using local fallback for this process",
                    exc.__class__.__name__,
                )
                break

            except RETRYABLE as exc:
                last_exc = exc
                if attempt < settings.mesh_max_retries:
                    await asyncio.sleep(0.6 * (2**attempt))
                    continue
                break

            except APIStatusError as exc:
                last_exc = exc
                status = getattr(exc, "status_code", None)
                if status in PERMANENT_EMBED_FAILURES:
                    # 402 insufficient balance / 401 bad key / 403 no access.
                    # Retrying cannot help, and every attempt costs latency on
                    # a user-facing path, so disable for this process.
                    self._embeddings_supported = False
                    logger.warning(
                        "Mesh embeddings disabled (HTTP %s); using local fallback", status
                    )
                    break
                if status in (500, 502, 503, 504) and attempt < settings.mesh_max_retries:
                    await asyncio.sleep(0.6 * (2**attempt))
                    continue
                break

        record_call(
            purpose=purpose,
            user_id=user_id,
            model=settings.mesh_embed_model,
            latency_ms=int((time.perf_counter() - started) * 1000),
            ok=False,
            error=str(last_exc)[:500],
        )
        raise MeshError(f"Mesh embeddings failed: {last_exc}") from last_exc


def extract_json(raw: str) -> dict[str, Any]:
    """Parse a JSON object out of model output.

    Models wrap JSON in ``` fences or add a preamble often enough that a bare
    json.loads is a real source of flakiness.
    """
    text = (raw or "").strip()
    if not text:
        raise MeshError("Empty response where JSON was expected")

    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise MeshError(f"Could not parse JSON from model output: {text[:200]}") from exc

    raise MeshError(f"No JSON object found in model output: {text[:200]}")


mesh_client = MeshClient()
