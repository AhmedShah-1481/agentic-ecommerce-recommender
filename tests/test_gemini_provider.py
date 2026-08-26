"""The Gemini provider swap.

Three properties are worth a test, and all three are silent when broken:

  * **Message translation.** Gemini has no `system` role. Every node in the
    agent still writes `[{"role": "system", ...}, ...]`, so if the hoist into
    `system_instruction` regresses, the instructions either vanish or arrive as
    an ordinary user turn - and the model keeps answering, just worse.

  * **Structured output.** The schema is what makes "the model returned prose
    where JSON was expected" impossible. Passing it is easy to drop in a
    refactor, and nothing fails loudly when you do: `extract_json` recovers most
    replies most of the time.

  * **Dimensionality.** This is the one that breaks hard. Chroma pins a
    collection's width at its first insert, and both writes *and queries* with
    another width raise. A provider swap changes the width, so the fallback has
    to emit the same width the collection is pinned to, and reconcile has to
    reset when configuration changes it. Without both, a working deployment
    turns into 56 rows stuck at vector_state='failed' and a dashboard that
    recommends nothing.

Nothing here touches the network: the SDK surface is faked at
`GeminiClient._get_client`.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from google.genai import errors as genai_errors
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.ai import embeddings, gemini_client as gc
from app.config import settings
from app.db import Base
from app.models import Product
from app.services import catalog


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------
def _response(text: str, *, finish: str = "STOP", thoughts: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name=finish))],
        usage_metadata=SimpleNamespace(
            prompt_token_count=120,
            candidates_token_count=45,
            thoughts_token_count=thoughts,
        ),
    )


class _Models:
    """Stands in for `client.aio.models`, recording what it was called with."""

    def __init__(self, *outcomes) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[SimpleNamespace] = []

    def _next(self):
        # The last outcome repeats, so a test can supply one response and not
        # care how many attempts the retry policy makes.
        outcome = self._outcomes.pop(0) if len(self._outcomes) > 1 else self._outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def generate_content(self, *, model, contents, config):
        self.calls.append(SimpleNamespace(model=model, contents=contents, config=config))
        return self._next()

    async def embed_content(self, *, model, contents, config):
        self.calls.append(SimpleNamespace(model=model, contents=contents, config=config))
        outcome = self._next()
        if callable(outcome):
            outcome = outcome(contents)
        return outcome


@pytest.fixture()
def client(monkeypatch):
    """A fresh GeminiClient with the SDK faked out and the ledger captured."""
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(settings, "gemini_max_retries", 1)
    # Pinned, not read: the suite loads the developer's `.env`, and the thinking
    # assertions below depend on which model generation is configured.
    monkeypatch.setattr(settings, "gemini_chat_model", "gemini-3.6-flash")
    instance = gc.GeminiClient()
    ledger: list[dict] = []
    monkeypatch.setattr(gc, "record_call", lambda **kw: ledger.append(kw))
    instance.ledger = ledger  # type: ignore[attr-defined]

    def attach(*outcomes) -> _Models:
        models = _Models(*outcomes)
        monkeypatch.setattr(
            instance, "_get_client", lambda: SimpleNamespace(aio=SimpleNamespace(models=models))
        )
        return models

    instance.attach = attach  # type: ignore[attr-defined]
    return instance


@pytest.fixture(autouse=True)
def clean_embedding_state(monkeypatch):
    """`_actual_provider` and the LRU are process globals; a leak between tests
    would make provider assertions depend on execution order."""
    monkeypatch.setattr(embeddings, "_actual_provider", None, raising=False)
    embeddings._cache.clear()
    yield
    embeddings._cache.clear()


# --------------------------------------------------------------------------
# Message translation
# --------------------------------------------------------------------------
def test_the_system_message_becomes_a_system_instruction(client):
    """Gemini has no system role. Left in `contents` the instructions would
    arrive as an ordinary user turn, which the model weights differently."""
    models = client.attach(_response('{"ok": true}'))

    asyncio.run(
        client.chat(
            [
                {"role": "system", "content": "You analyse shopper behaviour."},
                {"role": "user", "content": "Here is the profile."},
            ]
        )
    )

    config = models.calls[0].config
    assert config.system_instruction == "You analyse shopper behaviour."
    assert [c.role for c in models.calls[0].contents] == ["user"]
    assert models.calls[0].contents[0].parts[0].text == "Here is the profile."


def test_an_assistant_turn_is_relabelled_model(client):
    models = client.attach(_response("ok"))

    asyncio.run(
        client.chat(
            [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "reply"},
                {"role": "user", "content": "second"},
            ]
        )
    )

    assert [c.role for c in models.calls[0].contents] == ["user", "model", "user"]


def test_a_system_only_prompt_still_has_a_turn(client):
    """Gemini rejects a request with no contents, and a bare instruction with no
    user turn is a legitimate shape - it must not become an empty request."""
    models = client.attach(_response("ok"))

    asyncio.run(client.chat([{"role": "system", "content": "Summarise the catalog."}]))

    assert len(models.calls[0].contents) == 1
    assert models.calls[0].contents[0].role == "user"


def test_an_entirely_empty_prompt_is_refused_without_calling_gemini(client):
    models = client.attach(_response("ok"))

    with pytest.raises(gc.GeminiError):
        asyncio.run(client.chat([{"role": "user", "content": ""}]))

    assert models.calls == []


# --------------------------------------------------------------------------
# Structured output
# --------------------------------------------------------------------------
SCHEMA = {
    "type": "object",
    "properties": {"queries": {"type": "array", "items": {"type": "string"}}},
    "required": ["queries"],
}


def test_a_schema_constrains_decoding_and_turns_off_thinking(client):
    """Both halves matter. The schema is why the reply cannot be prose; minimal
    thinking is why the output allowance is spent on the answer rather than on
    hidden reasoning that a schema has already made unnecessary."""
    models = client.attach(_response('{"queries": ["cushioned road running shoe"]}'))

    data = asyncio.run(
        client.chat_json(
            [{"role": "user", "content": "refine these"}], response_schema=SCHEMA
        )
    )

    config = models.calls[0].config
    assert config.response_mime_type == "application/json"
    assert config.response_schema == SCHEMA
    assert config.thinking_config.thinking_level == "MINIMAL"
    assert data == {"queries": ["cushioned road running shoe"]}


def test_an_older_model_gets_the_numeric_thinking_budget(client, monkeypatch):
    """Gemini 2.5 has no `thinking_level` and rejects it. Keys minted before the
    2.5 retirement still reach those models, and the model is configurable, so
    the request has to be shaped for whichever generation is named."""
    monkeypatch.setattr(settings, "gemini_chat_model", "gemini-2.5-flash")
    models = client.attach(_response('{"queries": ["trail shoe"]}'))

    asyncio.run(client.chat_json([{"role": "user", "content": "refine"}],
                                 response_schema=SCHEMA))

    assert models.calls[0].config.thinking_config.thinking_budget == 0
    assert models.calls[0].config.thinking_config.thinking_level is None


def test_an_unversioned_model_alias_is_treated_as_current(client, monkeypatch):
    """`gemini-flash-latest` names no generation, and an alias never resolves
    backwards, so guessing the newer spelling is right - and cheap to be wrong
    about, because a 400 corrects it."""
    monkeypatch.setattr(settings, "gemini_chat_model", "gemini-flash-latest")
    models = client.attach(_response('{"queries": ["shoe"]}'))

    asyncio.run(client.chat_json([{"role": "user", "content": "refine"}],
                                 response_schema=SCHEMA))

    assert models.calls[0].config.thinking_config.thinking_level == "MINIMAL"


def test_a_rejected_thinking_config_is_swapped_and_retried(client):
    """The guess from the model name can be wrong - a renamed family, a model
    that takes neither. A 400 on a JSON call is treated as the wrong spelling
    and retried with the other one, because the alternative is a permanent
    fallback to mechanical copy on a working key."""
    models = client.attach(
        genai_errors.ClientError(400, {"error": {"message": "Request contains an "
                                                           "invalid argument."}}),
        _response('{"queries": ["cushioned trainer"]}'),
    )

    data = asyncio.run(client.chat_json([{"role": "user", "content": "refine"}],
                                        response_schema=SCHEMA))

    assert data == {"queries": ["cushioned trainer"]}
    assert models.calls[0].config.thinking_config.thinking_level == "MINIMAL"
    assert models.calls[1].config.thinking_config.thinking_budget == 0, "swapped"
    assert client.ledger[-1]["ok"] is True


def test_the_thinking_swap_is_tried_once_not_looped(client):
    """A 400 for any *other* reason would otherwise alternate spellings until
    the retry budget ran out, turning one bad request into three."""
    models = client.attach(
        genai_errors.ClientError(400, {"error": {"message": "invalid argument"}})
    )

    with pytest.raises(gc.GeminiError):
        asyncio.run(client.chat_json([{"role": "user", "content": "refine"}],
                                     response_schema=SCHEMA))

    assert len(models.calls) == 2, "one guess, one correction, then give up"


def test_prose_calls_keep_thinking_and_get_reasoning_headroom(client):
    """The narrative call is the one place thinking earns its cost, so it must
    not inherit the JSON path's minimal budget - and it needs enough output
    allowance for the reasoning pass plus the copy."""
    models = client.attach(_response("Because you kept coming back to it..."))

    asyncio.run(client.chat([{"role": "user", "content": "write copy"}], max_tokens=200))

    config = models.calls[0].config
    assert config.thinking_config is None
    assert config.max_output_tokens >= gc.MIN_THINKING_HEADROOM


def test_a_fenced_reply_is_still_parsed(client):
    """A schema-constrained reply is bare JSON, but `chat(json_mode=True)` with
    no schema is a supported path where a fence is entirely possible."""
    models = client.attach(_response('```json\n{"queries": ["trail shoes"]}\n```'))

    assert asyncio.run(
        client.chat_json([{"role": "user", "content": "go"}])
    ) == {"queries": ["trail shoes"]}
    assert models.calls[0].config.response_schema is None


# --------------------------------------------------------------------------
# Failure handling
# --------------------------------------------------------------------------
def test_a_truncated_empty_completion_is_retried_with_a_bigger_budget(client):
    """Gemini 2.5 can spend the whole allowance on hidden reasoning and return
    nothing. Failing there would drop a request that was one allowance away."""
    models = client.attach(
        _response("", finish="MAX_TOKENS"),
        _response("the actual answer"),
    )

    assert asyncio.run(client.chat([{"role": "user", "content": "go"}])) == "the actual answer"
    assert len(models.calls) == 2
    assert models.calls[1].config.max_output_tokens > models.calls[0].config.max_output_tokens


def test_a_server_error_is_retried_and_a_client_error_is_not(client):
    transient = client.attach(genai_errors.ServerError(503, {"error": {"message": "busy"}}))
    with pytest.raises(gc.GeminiError):
        asyncio.run(client.chat([{"role": "user", "content": "go"}]))
    assert len(transient.calls) == settings.gemini_max_retries + 1

    permanent = client.attach(genai_errors.ClientError(400, {"error": {"message": "bad"}}))
    with pytest.raises(gc.GeminiError):
        asyncio.run(client.chat([{"role": "user", "content": "go"}]))
    assert len(permanent.calls) == 1, "a malformed request cannot be fixed by repeating it"


def test_a_failed_call_is_still_recorded(client):
    """The AI-usage ledger is the receipt behind the efficiency claims; a
    failure that costs latency but leaves no row makes it a flattering fiction."""
    client.attach(genai_errors.ClientError(403, {"error": {"message": "no access"}}))

    with pytest.raises(gc.GeminiError):
        asyncio.run(client.chat([{"role": "user", "content": "go"}]))

    assert [row["ok"] for row in client.ledger] == [False]


def test_thinking_tokens_are_billed_into_the_ledger(client):
    """Gemini excludes thinking from candidates_token_count but bills it as
    output, so adding it is the difference between the ledger and the invoice."""
    client.attach(_response("copy", thoughts=310))

    asyncio.run(client.chat([{"role": "user", "content": "go"}]))

    assert client.ledger[0]["completion_tokens"] == 45 + 310


def test_no_permission_disables_embeddings_for_the_process(client):
    """403 means this key has no access to the embedding model. Retrying it on
    every user-facing retrieval costs latency to re-learn the same answer."""
    models = client.attach(genai_errors.ClientError(403, {"error": {"message": "no access"}}))

    with pytest.raises(gc.GeminiError):
        asyncio.run(client.embed(["a product description"]))

    assert client.embeddings_supported is False
    assert len(models.calls) == 1

    with pytest.raises(gc.GeminiError):
        asyncio.run(client.embed(["another one"]))
    assert len(models.calls) == 1, "no further calls once it is known to be unavailable"


# --------------------------------------------------------------------------
# Retrieval asymmetry
# --------------------------------------------------------------------------
def test_documents_and_queries_are_embedded_with_different_task_types(client, monkeypatch):
    """Gemini builds an asymmetric pair: a RETRIEVAL_QUERY vector is optimised
    to land near RETRIEVAL_DOCUMENT vectors. Using one task type for both loses
    relevance and raises nothing."""
    monkeypatch.setattr(settings, "gemini_embed_dimensions", 8)
    models = client.attach(
        lambda contents: SimpleNamespace(
            embeddings=[SimpleNamespace(values=[0.5] * 8) for _ in contents]
        )
    )
    monkeypatch.setattr(embeddings, "gemini_client", client)

    asyncio.run(embeddings.embed_texts(["a cushioned trainer"], task=embeddings.TASK_DOCUMENT))
    asyncio.run(embeddings.embed_one("cushioned running shoe", task=embeddings.TASK_QUERY))

    assert [c.config.task_type for c in models.calls] == [
        "RETRIEVAL_DOCUMENT",
        "RETRIEVAL_QUERY",
    ]


def test_the_task_type_is_part_of_the_cache_key(client, monkeypatch):
    """The same text embeds differently as a document and as a query, so a cache
    keyed only on text would serve one where the other belongs."""
    monkeypatch.setattr(settings, "gemini_embed_dimensions", 8)
    models = client.attach(
        lambda contents: SimpleNamespace(
            embeddings=[SimpleNamespace(values=[0.5] * 8) for _ in contents]
        )
    )
    monkeypatch.setattr(embeddings, "gemini_client", client)

    asyncio.run(embeddings.embed_one("trail shoes", task=embeddings.TASK_DOCUMENT))
    asyncio.run(embeddings.embed_one("trail shoes", task=embeddings.TASK_DOCUMENT))
    assert len(models.calls) == 1, "the second identical call is served from the LRU"

    asyncio.run(embeddings.embed_one("trail shoes", task=embeddings.TASK_QUERY))
    assert len(models.calls) == 2


# --------------------------------------------------------------------------
# The fallback, and the width it has to keep
# --------------------------------------------------------------------------
def test_no_key_means_local_vectors_and_no_call(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "")

    vectors = asyncio.run(embeddings.embed_texts(["a cast iron dutch oven"]))

    assert embeddings.using_fallback() is True
    assert len(vectors[0]) == embeddings.FALLBACK_DIM
    assert embeddings.provider_id() == f"local:hash{embeddings.FALLBACK_DIM}"


def test_a_gemini_failure_falls_back_at_the_configured_width(client, monkeypatch):
    """The property that keeps a quota-exhausted deployment alive. Chroma pins
    the collection at Gemini's width, so a fallback vector of the *local*
    default width would fail every upsert from that moment on."""
    monkeypatch.setattr(settings, "gemini_embed_dimensions", 768)
    client.attach(genai_errors.ServerError(500, {"error": {"message": "boom"}}))
    monkeypatch.setattr(embeddings, "gemini_client", client)

    vectors = asyncio.run(embeddings.embed_texts(["a cushioned trainer"]))

    assert len(vectors[0]) == 768 != embeddings.FALLBACK_DIM
    assert embeddings.provider_id() == "local:hash768"


def test_the_target_width_ignores_runtime_health(client, monkeypatch):
    """If `target_dim()` flipped when Gemini started failing, the fallback would
    produce the very mismatch it exists to avoid."""
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(settings, "gemini_embed_dimensions", 1536)
    monkeypatch.setattr(embeddings, "gemini_client", client)
    client.attach(genai_errors.ServerError(500, {"error": {"message": "boom"}}))

    before = embeddings.target_dim()
    asyncio.run(embeddings.embed_texts(["anything"]))

    assert embeddings.using_fallback() is False, "a 500 is transient, not a capability loss"
    assert embeddings.target_dim() == before == 1536


def test_a_wrong_width_from_gemini_is_rejected_rather_than_stored(client, monkeypatch):
    """A vector of unexpected width would pin the collection to it, or fail
    against an existing one. Honest local vectors of the right width are the
    better trade, and reconcile re-embeds when the provider recovers."""
    monkeypatch.setattr(settings, "gemini_embed_dimensions", 768)
    monkeypatch.setattr(embeddings, "gemini_client", client)
    client.attach(
        lambda contents: SimpleNamespace(
            embeddings=[SimpleNamespace(values=[0.1] * 3072) for _ in contents]
        )
    )

    vectors = asyncio.run(embeddings.embed_texts(["a cushioned trainer"]))

    assert len(vectors[0]) == 768
    assert embeddings.provider_id() == "local:hash768"


def test_the_local_provider_id_carries_its_width(monkeypatch):
    """`local:hash512` and `local:hash768` are different vector spaces, so
    reconcile has to be able to tell them apart in stored metadata."""
    monkeypatch.setattr(settings, "gemini_api_key", "")
    assert embeddings.local_provider() == "local:hash512"

    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(settings, "gemini_embed_dimensions", 768)
    assert embeddings.local_provider() == "local:hash768"


# --------------------------------------------------------------------------
# reconcile() across a width change
# --------------------------------------------------------------------------
class FakeStore:
    """Reproduces the one Chroma behaviour that shapes the design: a
    collection's width is pinned by its first insert, and any later write with
    another width raises. Everything else is a dict."""

    def __init__(self) -> None:
        self.docs: dict[int, str] = {}
        self.metas: dict[int, dict] = {}
        self.dim: int | None = None
        self.resets = 0
        self.upserts = 0

    def upsert_product(self, product_id, document, embedding, metadata):
        if self.dim is None:
            self.dim = len(embedding)
        elif len(embedding) != self.dim:
            raise ValueError(
                f"Collection expecting embedding with dimension of {self.dim}, "
                f"got {len(embedding)}"
            )
        self.upserts += 1
        self.docs[product_id] = document
        self.metas[product_id] = dict(metadata)

    def delete_product(self, product_id):
        self.docs.pop(product_id, None)
        self.metas.pop(product_id, None)

    def dimension(self):
        return self.dim

    def reset(self):
        self.docs.clear()
        self.metas.clear()
        self.dim = None
        self.resets += 1

    def all_ids(self):
        return set(self.docs)

    def get_metadata(self, product_ids):
        return {i: self.metas[i] for i in product_ids if i in self.metas}


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    session.add_all(
        [
            Product(id=1, title="Longhaul Cushioned Trainer", description="daily miles",
                    category="Footwear", brand="Kestrelane", price=138.0),
            Product(id=2, title="Cast Iron Dutch Oven", description="slow braises",
                    category="Home & Kitchen", brand="Copperleaf", price=189.0),
        ]
    )
    session.commit()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def fake_store(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(catalog, "store", store)
    return store


def _embed_at(monkeypatch, dim: int) -> None:
    monkeypatch.setattr(settings, "gemini_api_key", "")
    monkeypatch.setattr(embeddings, "FALLBACK_DIM", dim)


def test_reconcile_embeds_a_fresh_catalog(db, fake_store, monkeypatch):
    _embed_at(monkeypatch, 512)

    report = asyncio.run(catalog.reconcile(db))

    assert report["checked"] == 2
    assert report["repaired"] == 2
    assert fake_store.dim == 512
    assert "dimension_reset" not in report


def test_reconcile_resets_the_collection_when_the_width_changes(db, fake_store, monkeypatch):
    """The regression that would otherwise be permanent: without the reset,
    every upsert raises, `sync_product_vector` catches it per row, and the whole
    catalog sits at vector_state='failed' with retrieval returning nothing."""
    _embed_at(monkeypatch, 512)
    asyncio.run(catalog.reconcile(db))
    assert fake_store.dim == 512

    # A provider swap - or just retuning GEMINI_EMBED_DIMENSIONS.
    _embed_at(monkeypatch, 768)
    monkeypatch.setattr(embeddings, "_actual_provider", None, raising=False)
    report = asyncio.run(catalog.reconcile(db))

    assert report["dimension_reset"] == {"from": 512, "to": 768}
    assert fake_store.resets == 1
    assert fake_store.dim == 768
    assert report["repaired"] == 2 and report["still_failing"] == 0
    assert {p.vector_state for p in db.scalars(select(Product))} == {"synced"}


def test_reconcile_does_not_reset_when_the_width_agrees(db, fake_store, monkeypatch):
    """A reset re-embeds the entire catalog. Doing it on a run where nothing
    changed would make the scheduled reconcile the most expensive job we run."""
    _embed_at(monkeypatch, 512)
    asyncio.run(catalog.reconcile(db))
    upserts_after_first = fake_store.upserts

    report = asyncio.run(catalog.reconcile(db))

    assert fake_store.resets == 0
    assert "dimension_reset" not in report
    assert report["reembedded"] == 0
    assert fake_store.upserts == upserts_after_first, "nothing was re-embedded"


def test_an_empty_collection_is_not_treated_as_a_mismatch(db, fake_store, monkeypatch):
    """Width is unknown until something is stored. Reading "no width" as a
    mismatch would reset on every cold start."""
    _embed_at(monkeypatch, 768)
    assert fake_store.dimension() is None

    report = asyncio.run(catalog.reconcile(db))

    assert fake_store.resets == 0
    assert "dimension_reset" not in report
