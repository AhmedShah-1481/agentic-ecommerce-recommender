"""The ONLY module that talks to an LLM provider.

All LLM and embedding traffic goes through Google's Gemini API via the official
`google-genai` SDK. Nothing else in the codebase constructs an AI request, so
the provider contract lives entirely here.

This is a native Gemini integration, not an OpenAI-compatible shim, and the
difference shows up in three places the rest of the app depends on:

  * **Messages.** Gemini has no `system` role. A system prompt is a separate
    `system_instruction` on the request config, and conversation turns use the
    roles `user` / `model`. `_split_messages` does that translation once, so
    callers keep writing the familiar `[{"role": "system", ...}, ...]` list.

  * **Structured output.** Instead of `response_format={"type":"json_object"}`
    and hoping, Gemini takes a `response_schema` and constrains decoding to it.
    A schema-constrained call cannot come back as prose or a fenced block, so
    `chat_json` gets an object on the first attempt rather than repairing text.
    `extract_json` is kept for the unschema'd path and as a belt-and-braces
    parse.

  * **Thinking.** Gemini models spend output tokens on hidden reasoning before
    answering, so a modest `max_output_tokens` can be consumed entirely by
    thinking and return empty content with `finish_reason=MAX_TOKENS`. JSON
    calls therefore ask for the least thinking the model allows - the schema
    already does the work that thinking would - and the empty-completion path
    retries with a larger budget rather than failing outright. The two model
    generations spell that request differently; `_minimal_thinking` handles it.

Embeddings are separately gated: if the embedding model is unavailable to this
key we mark it unsupported for the process lifetime and let
`app.ai.embeddings` fall back to its local deterministic embedder, rather than
paying latency on a user-facing path to re-learn the same 403 every call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from app.ai.llm_log import record_call
from app.ai.tracing import redact_inputs, traceable
from app.config import settings

logger = logging.getLogger(__name__)

# Statuses where retrying cannot possibly succeed: 400 malformed request,
# 401 bad key, 403 no model access / API disabled, 404 no such model.
PERMANENT_FAILURES = {400, 401, 403, 404}
# Transient: 429 rate limit / quota, and the 5xx family.
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

# Gemini spends output tokens on hidden reasoning before emitting an answer.
# Narrative calls leave thinking on (it measurably improves the copy), so they
# need headroom for the reasoning pass *plus* the answer.
MIN_THINKING_HEADROOM = 1200

# What a purpose is embedding *for*. Gemini optimises the vector differently
# for a stored document than for a query against those documents, and using the
# wrong one costs retrieval quality silently.
TASK_DOCUMENT = "RETRIEVAL_DOCUMENT"
TASK_QUERY = "RETRIEVAL_QUERY"


class GeminiError(RuntimeError):
    """Gemini unreachable, unauthorised, or returned an unusable response."""


class GeminiNotConfigured(GeminiError):
    """No API key set. Callers degrade instead of surfacing a 500."""


def _status_code(exc: Exception) -> int | None:
    """HTTP status behind a google-genai error, if it carries one."""
    code = getattr(exc, "code", None)
    return int(code) if isinstance(code, int) else None


def _split_messages(
    messages: list[dict[str, str]],
) -> tuple[str, list[genai_types.Content]]:
    """OpenAI-style message list -> Gemini `(system_instruction, contents)`.

    Callers across the agent write `system`/`user`/`assistant` messages because
    that is what the node code already reads clearly as. Gemini wants the system
    text hoisted out of the turn list entirely and assistant turns labelled
    `model`, so the translation happens here rather than in six call sites.
    """
    system_parts: list[str] = []
    contents: list[genai_types.Content] = []

    for message in messages:
        role = (message.get("role") or "user").lower()
        text = message.get("content") or ""
        if not text:
            continue
        if role == "system":
            system_parts.append(text)
            continue
        contents.append(
            genai_types.Content(
                role="model" if role in ("assistant", "model") else "user",
                parts=[genai_types.Part.from_text(text=text)],
            )
        )

    # Gemini rejects a request with no turns, and a prompt that was *only*
    # system text is a legitimate shape for a one-shot instruction.
    if not contents and system_parts:
        contents.append(
            genai_types.Content(
                role="user", parts=[genai_types.Part.from_text(text=system_parts.pop())]
            )
        )

    return "\n\n".join(system_parts), contents


def _minimal_thinking(model: str) -> genai_types.ThinkingConfig:
    """Ask for as little hidden reasoning as the model generation permits.

    The two generations spell this differently and each rejects the other's
    spelling with a 400: Gemini 2.5 takes a numeric `thinking_budget` (0 turns
    thinking off), while Gemini 3 replaced it with a `thinking_level` enum whose
    floor is `MINIMAL`. The chat model is configurable, so both spellings have
    to work - and since the version can't be inferred reliably from a model
    string (`gemini-flash-latest` names no generation at all), `chat` treats a
    400 on this config as the wrong guess and retries with the other one.
    """
    if _model_generation(model) >= 3:
        return genai_types.ThinkingConfig(thinking_level="MINIMAL")
    return genai_types.ThinkingConfig(thinking_budget=0)


def _model_generation(model: str) -> int:
    """Leading major version in a model id, or 3 when it names none.

    `gemini-2.5-flash` -> 2, `gemini-3.6-flash` -> 3, `gemini-flash-latest` -> 3.
    An unversioned alias resolves to whatever is current, which is never an
    older generation, so 3 is the right default rather than a guess.
    """
    match = re.search(r"gemini-(\d+)", model or "")
    return int(match.group(1)) if match else 3


class GeminiClient:
    def __init__(self) -> None:
        self._client: genai.Client | None = None
        self._lock = asyncio.Lock()
        self._embeddings_supported = True

    # -- lifecycle ---------------------------------------------------------
    def _get_client(self) -> genai.Client:
        if not settings.gemini_configured:
            raise GeminiNotConfigured("GEMINI_API_KEY is not set")
        if self._client is None:
            self._client = genai.Client(
                api_key=settings.gemini_api_key,
                # HttpOptions.timeout is milliseconds, unlike every other
                # timeout in this codebase.
                http_options=genai_types.HttpOptions(
                    timeout=int(settings.gemini_timeout_seconds * 1000),
                    # We own the retry policy below; the SDK's would multiply
                    # against ours and blow past the request deadline.
                    retry_options=genai_types.HttpRetryOptions(attempts=1),
                ),
            )
        return self._client

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.aio.aclose()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                logger.debug("Gemini client close failed", exc_info=True)

    @property
    def embeddings_supported(self) -> bool:
        return self._embeddings_supported

    # -- chat --------------------------------------------------------------
    # Decorated at class-definition time, so with tracing off this is the plain
    # function with no wrapper and no per-call cost.
    @traceable(run_type="llm", name="gemini.chat", process_inputs=redact_inputs)
    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        purpose: str = "chat",
        user_id: int | None = None,
        temperature: float = 0.7,
        max_tokens: int = 900,
        json_mode: bool = False,
        response_schema: dict[str, Any] | None = None,
        max_retries: int | None = None,
    ) -> str:
        client = self._get_client()
        started = time.perf_counter()
        model = settings.gemini_chat_model
        system_instruction, contents = _split_messages(messages)
        if not contents:
            raise GeminiError("Refusing to call Gemini with an empty prompt")

        want_json = json_mode or response_schema is not None
        retries = settings.gemini_max_retries if max_retries is None else max_retries
        last_exc: Exception | None = None

        # A schema-constrained call needs room for the JSON but not for a
        # reasoning pass; a prose call needs room for both.
        if not want_json:
            max_tokens = max(max_tokens, MIN_THINKING_HEADROOM)

        thinking = _minimal_thinking(model) if want_json else None
        # One correction allowed per call: see `_minimal_thinking`.
        thinking_retried = False

        attempt = 0
        while attempt <= retries:
            config = genai_types.GenerateContentConfig(
                temperature=temperature,
                max_output_tokens=max_tokens,
                system_instruction=system_instruction or None,
                # We never pass tools, so the SDK's automatic function-calling
                # loop has nothing to do - and left on it warns on every single
                # call that we should be using AsyncChat instead.
                automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(
                    disable=True
                ),
            )
            if want_json:
                config.response_mime_type = "application/json"
                if response_schema is not None:
                    config.response_schema = response_schema
                # The schema is the structure; thinking on top of it only
                # competes with the answer for the output budget.
                config.thinking_config = thinking

            try:
                resp = await client.aio.models.generate_content(
                    model=model, contents=contents, config=config
                )
                text = (resp.text or "") if resp is not None else ""
                usage = getattr(resp, "usage_metadata", None)

                if not text.strip():
                    finish = _finish_reason(resp)
                    if finish == "MAX_TOKENS" and attempt < retries:
                        # Ran out of budget mid-thought: buy more rather than
                        # failing a request that was one allowance away.
                        max_tokens = min(int(max_tokens * 2.5), 8000)
                        logger.info(
                            "Empty completion (truncated); retrying with max_tokens=%s",
                            max_tokens,
                        )
                        attempt += 1
                        continue
                    raise GeminiError(
                        f"Gemini returned an empty completion (finish={finish!r})"
                    )

                record_call(
                    purpose=purpose,
                    user_id=user_id,
                    model=model,
                    prompt_tokens=int(getattr(usage, "prompt_token_count", 0) or 0),
                    # Thinking tokens are billed as output but excluded from
                    # candidates_token_count, so the ledger has to add them or
                    # it under-reports what the call actually cost.
                    completion_tokens=int(getattr(usage, "candidates_token_count", 0) or 0)
                    + int(getattr(usage, "thoughts_token_count", 0) or 0),
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    ok=True,
                )
                return text

            except GeminiError as exc:
                last_exc = exc
                break

            except genai_errors.APIError as exc:
                last_exc = exc
                status = _status_code(exc)
                if status == 400 and thinking is not None and not thinking_retried:
                    # Most likely the wrong thinking spelling for this model
                    # generation. Swap it and try once; it costs one round trip
                    # and doesn't consume a retry, because nothing was flaky.
                    thinking_retried = True
                    thinking = _flip_thinking(thinking)
                    logger.info(
                        "Gemini rejected the thinking config; retrying as %s",
                        thinking,
                    )
                    continue
                if status in RETRYABLE_STATUSES and attempt < retries:
                    await asyncio.sleep(0.6 * (2**attempt))
                    attempt += 1
                    continue
                break

            except Exception as exc:  # noqa: BLE001 - connection/timeout errors
                last_exc = exc
                if _is_transport_error(exc) and attempt < retries:
                    await asyncio.sleep(0.6 * (2**attempt))
                    attempt += 1
                    continue
                break

        record_call(
            purpose=purpose,
            user_id=user_id,
            model=model,
            latency_ms=int((time.perf_counter() - started) * 1000),
            ok=False,
            error=str(last_exc)[:500],
        )
        raise GeminiError(f"Gemini chat failed: {last_exc}") from last_exc

    async def chat_json(
        self,
        messages: list[dict[str, str]],
        *,
        purpose: str = "chat_json",
        user_id: int | None = None,
        temperature: float = 0.4,
        max_tokens: int = 900,
        response_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Chat that must yield a JSON object.

        Pass `response_schema` and decoding is constrained to that shape, which
        is the whole reason this app is on Gemini rather than a JSON-mode
        promise: the failure mode "model returned prose where JSON was
        expected" stops existing.
        """
        raw = await self.chat(
            messages,
            purpose=purpose,
            user_id=user_id,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=True,
            response_schema=response_schema,
        )
        return extract_json(raw)

    # -- embeddings --------------------------------------------------------
    @traceable(run_type="embedding", name="gemini.embed", process_inputs=redact_inputs)
    async def embed(
        self,
        texts: list[str],
        *,
        purpose: str = "embed",
        user_id: int | None = None,
        task_type: str = TASK_DOCUMENT,
    ) -> list[list[float]]:
        if not texts:
            return []
        if not self._embeddings_supported:
            raise GeminiError("Gemini embeddings previously reported unsupported")

        client = self._get_client()
        started = time.perf_counter()
        model = settings.gemini_embed_model
        last_exc: Exception | None = None

        config = genai_types.EmbedContentConfig(
            task_type=task_type,
            # Matryoshka truncation. Chroma pins a collection's dimensionality
            # at first insert, so this value is part of the provider identity
            # that catalog.reconcile() compares against.
            output_dimensionality=settings.gemini_embed_dimensions,
        )

        for attempt in range(settings.gemini_max_retries + 1):
            try:
                resp = await client.aio.models.embed_content(
                    model=model, contents=list(texts), config=config
                )
                rows = list(getattr(resp, "embeddings", None) or [])
                if len(rows) != len(texts):
                    raise GeminiError(
                        f"Gemini returned {len(rows)} embeddings for {len(texts)} inputs"
                    )
                vectors = [_normalise(list(map(float, row.values or []))) for row in rows]
                record_call(
                    purpose=purpose,
                    user_id=user_id,
                    model=model,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    ok=True,
                )
                return vectors

            except GeminiError as exc:
                last_exc = exc
                break

            except genai_errors.APIError as exc:
                last_exc = exc
                status = _status_code(exc)
                if status in PERMANENT_FAILURES:
                    # Bad key / no access / no such model. Retrying cannot
                    # help, and every attempt costs latency on a user-facing
                    # path, so disable for this process and let the local
                    # fallback take over.
                    self._embeddings_supported = False
                    logger.warning(
                        "Gemini embeddings disabled (HTTP %s); using local fallback", status
                    )
                    break
                if status in RETRYABLE_STATUSES and attempt < settings.gemini_max_retries:
                    await asyncio.sleep(0.6 * (2**attempt))
                    continue
                break

            except Exception as exc:  # noqa: BLE001 - connection/timeout errors
                last_exc = exc
                if _is_transport_error(exc) and attempt < settings.gemini_max_retries:
                    await asyncio.sleep(0.6 * (2**attempt))
                    continue
                break

        record_call(
            purpose=purpose,
            user_id=user_id,
            model=model,
            latency_ms=int((time.perf_counter() - started) * 1000),
            ok=False,
            error=str(last_exc)[:500],
        )
        raise GeminiError(f"Gemini embeddings failed: {last_exc}") from last_exc


def _flip_thinking(config: genai_types.ThinkingConfig) -> genai_types.ThinkingConfig:
    """The other generation's way of asking for minimal thinking."""
    if config.thinking_level is not None:
        return genai_types.ThinkingConfig(thinking_budget=0)
    return genai_types.ThinkingConfig(thinking_level="MINIMAL")


def _finish_reason(resp: Any) -> str:
    """Name of the finish reason on the first candidate, or "" if absent."""
    candidates = getattr(resp, "candidates", None) or []
    if not candidates:
        return ""
    reason = getattr(candidates[0], "finish_reason", None)
    return getattr(reason, "name", None) or (str(reason) if reason else "")


def _is_transport_error(exc: Exception) -> bool:
    """Connection reset / DNS / read timeout, i.e. worth another attempt.

    google-genai lets httpx's own exceptions through rather than wrapping them,
    and the SDK does not re-export them, so this matches on the class name to
    avoid taking a direct httpx dependency for one predicate.
    """
    name = type(exc).__name__
    return isinstance(exc, (TimeoutError, ConnectionError)) or (
        "Timeout" in name or "Connect" in name or "Protocol" in name
    )


def _normalise(vector: list[float]) -> list[float]:
    """L2-normalise a vector.

    Gemini returns unit-length vectors at the model's native 3072 dimensions,
    but a Matryoshka-truncated `output_dimensionality` drops the tail without
    rescaling, so a 768-dim vector comes back slightly short of unit length.
    Chroma's cosine space tolerates that, but the reranker normalises scores
    against the candidate set, so consistent magnitudes keep the relevance band
    meaning the same thing across providers.
    """
    total = sum(component * component for component in vector) ** 0.5
    if total <= 0:
        return vector
    return [component / total for component in vector]


def extract_json(raw: str) -> dict[str, Any]:
    """Parse a JSON object out of model output.

    A schema-constrained call returns bare JSON, so this is usually one
    `json.loads`. The fence-stripping and brace-scanning survive from the
    OpenAI-compatible days because `chat(json_mode=True)` with no schema is
    still a supported path, and a preamble there is entirely possible.
    """
    text = (raw or "").strip()
    if not text:
        raise GeminiError("Empty response where JSON was expected")

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
            raise GeminiError(f"Could not parse JSON from model output: {text[:200]}") from exc

    raise GeminiError(f"No JSON object found in model output: {text[:200]}")


gemini_client = GeminiClient()
