# LLM providers

`app/llm.py` is the only module in AstroChat that imports a provider SDK. It defines the two-method `LLMProvider` Protocol that the orchestrator programs against, one exception type (`LLMError`) that is the only thing the rest of the application ever sees when a provider fails, and three implementations selected by `build_llm(settings)` from `LLM_PROVIDER`: `AnthropicLLM` (Claude through the official `anthropic` SDK, structured outputs for extraction), `OpenAICompatibleLLM` (any server that speaks the OpenAI chat-completions API, `json_object` mode plus our own validation for extraction) and `MockLLM` (deterministic, no key, what the tests use). A single `_guarded()` helper maps both SDKs' outage errors to `LLMError` and deliberately lets 4xx errors through. The extraction prompt (`EXTRACT_PROMPT`, `_extract_system`) and the rule-based extractor (`extract_by_rules`) also live in this file but belong to the memory pipeline; they are covered in [Memory update](06-memory-update.md) and only referenced here.

**Files:** `app/llm.py`

**Depends on:** `app/config.py` (`Settings`, see [Configuration and deployment](08-configuration-and-deployment.md)), `app/models.py` (`LLMRequest`, `MemoryCandidate`, `MEMORY_CATEGORIES`, `Category`, see [Domain model](03-domain-model.md)), `app/context.py` (`classify`, used only by the rule extractor, see [Query understanding and context selection](04-query-understanding-and-context-selection.md)), `app/astrology.py` (`parse_date`, rule extractor only). **Used by:** `app/chat.py` (`ChatService.chat` calls `generate` and `extract_memories`, see [Orchestration and short-term context](02-orchestration-and-short-term-context.md)), `app/main.py` (`build_llm` in the lifespan, the `LLMError` handler that returns 503, see [API layer](01-api-layer.md)). Tests: [Testing and verification](09-testing-and-verification.md). Index: [Layer index](README.md).

## What this layer is

The provider boundary. Above it, `ChatService` (in `app/chat.py`) knows only:

```python
class LLMProvider(Protocol):
    async def generate(self, request: LLMRequest) -> str: ...
    async def extract_memories(self, message: str, today: date) -> list[MemoryCandidate]: ...
```

and one exception:

```python
class LLMError(Exception):
    """The provider failed or declined; nothing was generated."""
```

`generate` turns a fully rendered `LLMRequest` (a `system_prompt` string plus an ordered list of `ChatMessage`, built by `select_context` in `app/context.py`) into the assistant's reply text. `extract_memories` turns the user's latest message and today's date into a list of `MemoryCandidate` objects that `app/memory.py::remember` will validate and persist. Both methods are `async` because every real implementation awaits a network call.

Below the boundary are the three implementations and the pieces they share:

| Name | Kind | Role |
|---|---|---|
| `LLMError` | exception | Sole failure signal crossing the boundary; `app/main.py` maps it to HTTP 503 |
| `LLMProvider` | `typing.Protocol` | Structural interface; nothing subclasses it, the three classes simply have the two methods |
| `_Extraction` | Pydantic model | `{"memories": list[MemoryCandidate]}` wrapper; the structured-output schema for Anthropic and the validation target for the OpenAI path |
| `_guarded(call, sdk)` | async helper | Awaits an SDK coroutine and maps outage exceptions to `LLMError` |
| `AnthropicLLM` | class | `LLM_PROVIDER=anthropic` |
| `OpenAICompatibleLLM` | class | `LLM_PROVIDER=openai` |
| `MockLLM` | class | `LLM_PROVIDER=mock`; injected by tests through `create_app(llm=...)` |
| `_JSON_SHAPE`, `_FENCE` | constants | Prompt suffix and code-fence regex used only by the OpenAI extraction path |
| `build_llm(settings)` | function | Provider selection from `Settings.llm_provider` |
| `EXTRACT_PROMPT`, `_extract_system`, `extract_by_rules` and the `_NAME` ... `_LANGUAGES` regexes | prompt and rule extractor | Covered in [Memory update](06-memory-update.md) |

## Why it exists

**Requirements.** PRD FR-6 ("LLM Abstraction") asks for a Protocol with `generate` and `extract_memories` and states: "The application must not depend directly on a specific provider SDK outside the provider adapter." PRD §3.2 lists "Make the LLM provider replaceable without changing the rest of the application" as a secondary goal, and PRD §13 names "Provider lock-in" as a risk with "Provider interface + adapter pattern" as the mitigation. TDD §2 principle 4 restates it as "Provider independence. LLM provider SDKs stay behind an adapter", TDD §10 specifies the Protocol, the three implementations and `_guarded`, and TDD §24 records the trade-off row "LLM provider | Adapter interface | Avoid vendor lock-in".

**The problems it solves.**

1. *Swapping providers is an environment change, not a code change.* TDD §27 (row for §10) records why the v1 single-provider design became Anthropic plus an OpenAI-compatible adapter: "Not vendor-locked: the same service runs on OpenAI, a local Ollama, vLLM or a gateway by changing env vars." Nothing outside `app/llm.py` mentions `anthropic` or `openai`; `app/chat.py` imports `LLMError, LLMProvider` and `app/main.py` imports `LLMError, LLMProvider, build_llm`.
2. *The orchestrator sees one failure type.* `ChatService.chat` catches `LLMError` around extraction and lets it propagate from generation; it never imports an SDK exception. That is what lets TDD §13.3 ("LLM unavailable" returns 503 and records nothing; extraction failure after a good response keeps the response and skips the write) be implemented in a few lines of `app/chat.py`, and what lets PRD FR-8 "LLM failure" be tested with a flag on the mock.
3. *Tests and demos need no key.* `MockLLM` is what `tests/conftest.py` injects and what `LLM_PROVIDER=mock` runs (README "Run it": "No Neo4j, no key"). Because it is deterministic and records the requests it receives, every scenario in `tests/test_chat.py` can assert on the exact prompt the LLM was given, which is the PRD §11 rubric in executable form (README "Tests").
4. *Extraction output is validated regardless of provider.* Both real providers return `MemoryCandidate` objects that have already passed Pydantic validation (server-side schema enforcement on Anthropic, `model_validate_json` on the OpenAI path), so `app/memory.py::validate` only has to enforce the taxonomy and confidence rules, not the shape. This is the "Structured extraction schema" half of the PRD §13 mitigation for "LLM invents memories".

## How it works

### The Protocol and the request shape

`LLMRequest` (in `app/models.py`) carries `system_prompt: str` and `messages: list[ChatMessage]`, where the list is "recent turns followed by the current user message" and each `ChatMessage` has `role` (`"user"` or `"assistant"`) and `content`. TDD §4.7 explains the split: context is rendered to text once by the prompt builder, so every provider receives the same two things and stays thin. Each provider therefore only has to translate that pair into its own wire format; none of them reads profile or memory objects.

`generate` returns `str`, not the `LLMResponse` sketched in PRD FR-6. TDD §10: "`generate` returns `str`. The PRD's `LLMResponse` type would carry only provider metadata nobody reads yet." `extract_memories` takes `(message: str, today: date)` rather than the PRD's `MemoryExtractionRequest`; the two values are exactly what the prompt needs (the user's own words per TDD §2 principle 3, and the date so that "next year" resolves to a concrete year). No rationale for dropping the request object is recorded beyond the general TDD §27 pattern of removing single-use wrappers; inferred.

### `_guarded()`

```python
async def _guarded(call, sdk):
    """Await an SDK call, mapping outages to LLMError. Both SDKs expose the same Stainless exception names."""
    try:
        return await call
    except (sdk.APIConnectionError, sdk.RateLimitError) as e:
        raise LLMError(f"provider unavailable: {e}") from e
    except sdk.APIStatusError as e:
        if e.status_code >= 500:
            raise LLMError(f"provider error {e.status_code}") from e
        raise  # 4xx is our bug (bad request, auth): surface it as a 500, not a degraded 503
```

Walkthrough:

- `call` is the un-awaited coroutine returned by an SDK method (`self._client.messages.create(...)` or `self._client.chat.completions.create(...)`); `_guarded` awaits it so that exceptions raised during the request surface inside the `try`.
- `sdk` is the module itself (`anthropic` or `openai`), passed by the caller. Exception classes are looked up as attributes on it, which is what lets one function serve both SDKs. TDD §10: "Both SDKs are Stainless-generated and expose the same exception names, so one `_guarded()` helper maps connection and rate-limit errors and 5xx responses to `LLMError`; 4xx responses propagate, because they indicate a bug on our side rather than an outage."
- The exception hierarchy is the same in the installed `anthropic` 1.7.0 and `openai` 3.16.2 (verified by inspecting the MROs): `RateLimitError`, `BadRequestError`, `AuthenticationError` and `InternalServerError` all subclass `APIStatusError`; `APITimeoutError` subclasses `APIConnectionError`; `APIConnectionError` and `APIStatusError` both subclass `APIError`. Clause order matters: `RateLimitError` (HTTP 429) is an `APIStatusError` but is caught by the first clause, so it becomes "provider unavailable" rather than falling into the status-code branch, where 429 would have been re-raised as a 4xx.
- Timeouts are covered without being named, because `APITimeoutError` is an `APIConnectionError`.

The resulting mapping:

| SDK exception | HTTP status | `_guarded` result | Message |
|---|---|---|---|
| `APIConnectionError` (including `APITimeoutError`) | none (no response) | `LLMError` | `provider unavailable: <sdk message>` |
| `RateLimitError` | 429 | `LLMError` | `provider unavailable: <sdk message>` |
| `APIStatusError` with `status_code >= 500` (`InternalServerError` and any other 5xx) | 500 to 599 | `LLMError` | `provider error <status>` |
| `APIStatusError` with `status_code < 500` (`BadRequestError` 400, `AuthenticationError` 401, `PermissionDeniedError` 403, `NotFoundError` 404, `UnprocessableEntityError` 422, ...) | 4xx | re-raised unchanged | the SDK's own exception |
| anything else (`ValueError`, Pydantic `ValidationError`, ...) | n/a | not caught | propagates |

Both SDKs retry before `_guarded` sees anything: the installed clients default to `max_retries=2` (`DEFAULT_MAX_RETRIES` in both packages) and their `_should_retry` retries responses with status 408, 409, 429 and >= 500, honouring an `x-should-retry` header if the server sends one; both also retry transport-level exceptions (timeouts, connection errors) from inside the same loop while retries remain, and the final failure reaches `_guarded` as `APIConnectionError` (which is why `tests/test_openai_llm.py::test_connection_error_is_llm_error` sees "provider unavailable"). `build_llm` passes no `timeout` or `max_retries`, so the SDK defaults apply (connect 5 s, read 600 s in both). The tests pass `max_retries=0` so that a fake 503 fails immediately.

### `AnthropicLLM`

```python
class AnthropicLLM:
    """Claude via the official SDK. Extraction uses structured outputs, so candidates arrive validated."""

    def __init__(self, model: str = "claude-opus-5", effort: str = "medium", **client_kwargs):
        self._client = anthropic.AsyncAnthropic(**client_kwargs)  # key from ANTHROPIC_API_KEY or an `ant auth` profile
        self._model, self._effort = model, effort
```

**`__init__`.** `build_llm` passes no `client_kwargs`, so `anthropic.AsyncAnthropic()` is constructed with no explicit credentials and resolves them itself. The installed SDK's `AsyncAnthropic.__init__` docstring lists the order: explicit constructor arguments; the `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` environment variables; an `ANTHROPIC_PROFILE` environment variable naming a profile under `<config_dir>/configs/`; workload identity federation variables; and finally "the active profile on disk", which is what README "Run it" refers to as "a profile from `ant auth login`". `ANTHROPIC_API_KEY` is therefore not a field of `Settings`; it is read by the SDK. Construction succeeds with no credentials at all (verified), so a missing key is not detected at startup and surfaces on the first request. `**client_kwargs` exists so a test can inject `http_client=` and `max_retries=`, exactly as `tests/test_openai_llm.py` does for the other adapter.

**`generate`.**

```python
    async def generate(self, request: LLMRequest) -> str:
        resp = await _guarded(self._client.messages.create(
            model=self._model, max_tokens=16000, system=request.system_prompt,
            messages=[{"role": m.role, "content": m.content} for m in request.messages],
            output_config={"effort": self._effort},
        ), anthropic)
        if resp.stop_reason == "refusal":
            raise LLMError("model declined the request")
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        if not text:
            raise LLMError("empty response")
        return text
```

Parameters actually sent to `POST /v1/messages` (verified against a fake transport):

| Parameter | Value | Source |
|---|---|---|
| `model` | `self._model` | `LLM_MODEL`, or `claude-opus-5` when empty |
| `max_tokens` | `16000` | constant in code; no rationale recorded (inferred: a ceiling high enough that neither a reply nor an extraction is cut off; there is no `stop_reason == "max_tokens"` handling) |
| `system` | `request.system_prompt` | `select_context` in `app/context.py` |
| `messages` | one `{"role", "content"}` dict per `ChatMessage`, in order | `request.messages` |
| `output_config` | `{"effort": self._effort}` | `LLM_EFFORT`; always sent, including when empty (see Configuration) |

The SDK's `OutputConfigParam.effort` is typed `Optional[Literal["low", "medium", "high", "xhigh", "max"]]`; AstroChat forwards whatever string `LLM_EFFORT` holds without validating it. After the call: a `stop_reason` of `"refusal"` (one of the literals in the SDK's `StopReason` type) becomes `LLMError("model declined the request")`; otherwise the `text` fields of all content blocks with `type == "text"` are concatenated and stripped, and an empty result becomes `LLMError("empty response")`. Non-text blocks (thinking, tool use) are ignored rather than rejected.

**`extract_memories`.**

```python
    async def extract_memories(self, message: str, today: date) -> list[MemoryCandidate]:
        resp = await _guarded(self._client.messages.parse(
            model=self._model, max_tokens=16000, system=_extract_system(today),
            messages=[{"role": "user", "content": message}], output_format=_Extraction,
        ), anthropic)
        return resp.parsed_output.memories if resp.parsed_output else []
```

| Parameter | Value |
|---|---|
| `model` | `self._model` |
| `max_tokens` | `16000` |
| `system` | `_extract_system(today)`: `EXTRACT_PROMPT` formatted with today's ISO date, `today.year + 1` and the sorted `MEMORY_CATEGORIES` (see [Memory update](06-memory-update.md)) |
| `messages` | exactly one user message containing the raw user text |
| `output_format` | `_Extraction` (the Pydantic class, not an instance) |
| `output_config` | not passed by AstroChat; the SDK fills it (below). No `effort` is sent on this call |

`messages.parse` is an SDK convenience wrapper around the same `/v1/messages` endpoint. In the installed version it builds a JSON schema from the class via `pydantic.TypeAdapter(output_format).json_schema()`, transforms it, sends it as `output_config={"format": {"type": "json_schema", "schema": ...}}`, and attaches a post-parser that validates every text block's `text` with `TypeAdapter.validate_json` into a `parsed_output` attribute. `ParsedMessage.parsed_output` (a property) returns the first text block's parsed value, or `None` when no text block parsed. The code returns `resp.parsed_output.memories` when that is set and `[]` otherwise, so a message with no text content (for example a refusal) yields no candidates rather than an error. A `MemoryCandidate` requires all seven fields (`key`, `category`, `type`, `value`, `target_timeframe` which may be `null`, `confidence`, `reason`); with server-side schema enforcement the model is constrained to produce them.

### `OpenAICompatibleLLM`

```python
    def __init__(self, model: str, base_url: str | None = None, api_key: str | None = None,
                 effort: str | None = None, **client_kwargs):
        if not model:
            raise ValueError("LLM_MODEL is required for LLM_PROVIDER=openai (model names are server-specific)")
        # Local servers such as Ollama ignore the key, but the SDK insists on a non-empty one.
        self._client = openai.AsyncOpenAI(base_url=base_url or None, api_key=api_key or "not-needed", **client_kwargs)
        self._model = model
        # reasoning_effort is sent when LLM_EFFORT is set; leave it empty for models that reject the parameter.
        self._extra = {"reasoning_effort": effort} if effort else {}
```

**`__init__`.**

- `model` is mandatory. An empty string raises `ValueError` with the message above; `build_llm` does not catch it, so the application fails during lifespan startup (see `build_llm` below).
- `base_url or None`: an empty `OPENAI_BASE_URL` becomes `None`, and the installed `AsyncOpenAI.__init__` then reads the `OPENAI_BASE_URL` environment variable itself and falls back to `https://api.openai.com/v1` (verified in the SDK source and against a fake transport). Since `Settings.from_env` reads the same variable, both routes agree; "leave the URL empty" in the README provider table means OpenAI itself.
- `api_key or "not-needed"`: the installed SDK raises `OpenAIError("Missing credentials. ...")` when `api_key` is `None` or `""` and `OPENAI_API_KEY` is unset (verified). Local servers such as Ollama do not check the key, so a placeholder is substituted and is sent literally as `Authorization: Bearer not-needed` (verified). Because a non-empty key is always passed, the SDK never consults `OPENAI_API_KEY` on its own; the value reaches it only through `Settings.openai_api_key`.
- `self._extra` holds `{"reasoning_effort": effort}` only when `effort` is truthy; an empty `LLM_EFFORT` yields `{}` and the parameter is absent from the request body. Code comment: "leave it empty for models that reject the parameter."

**`generate`.**

```python
    async def generate(self, request: LLMRequest) -> str:
        messages = [{"role": "system", "content": request.system_prompt},
                    *({"role": m.role, "content": m.content} for m in request.messages)]
        resp = await _guarded(
            self._client.chat.completions.create(model=self._model, messages=messages, **self._extra), openai)
        text = (resp.choices[0].message.content or "").strip()
        if not text:
            raise LLMError("empty response")
        return text
```

Parameters sent to `POST {base_url}/chat/completions`:

| Parameter | Value |
|---|---|
| `model` | `self._model` (`LLM_MODEL`) |
| `messages` | `{"role": "system", "content": request.system_prompt}` first, then one dict per `ChatMessage` in order |
| `reasoning_effort` | `LLM_EFFORT`, only when non-empty |

No `max_tokens`, `temperature` or `response_format` is sent on generation. The reply is `choices[0].message.content`; a `null` content becomes `""` via `or ""`, is stripped, and an empty string raises `LLMError("empty response")`. Only the first choice is read.

**`extract_memories`.**

```python
    async def extract_memories(self, message: str, today: date) -> list[MemoryCandidate]:
        resp = await _guarded(self._client.chat.completions.create(
            model=self._model, response_format={"type": "json_object"}, **self._extra,
            messages=[{"role": "system", "content": _extract_system(today) + _JSON_SHAPE},
                      {"role": "user", "content": message}],
        ), openai)
        raw = _FENCE.sub("", (resp.choices[0].message.content or "").strip())  # smaller models fence their JSON
        try:
            return _Extraction.model_validate_json(raw).memories
        except ValidationError as e:
            raise LLMError(f"extraction returned invalid JSON ({e.error_count()} error(s))") from e
```

| Parameter | Value |
|---|---|
| `model` | `self._model` |
| `response_format` | `{"type": "json_object"}` |
| `messages` | system: `_extract_system(today)` followed by `_JSON_SHAPE`; user: the raw user text |
| `reasoning_effort` | `LLM_EFFORT`, only when non-empty |

`json_object` mode only asks the server for syntactically valid JSON; it does not carry a schema. The shape is therefore spelled out in the prompt by `_JSON_SHAPE`, a one-object example of `{"memories": [{key, category, type, value, target_timeframe, confidence, reason}]}` ending with "Use null when there is no target_timeframe." The class docstring and TDD §10 call this "the lowest common denominator every compatible server supports".

Post-processing: `_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")` removes a leading ` ``` ` or ` ```json ` fence and a trailing ` ``` ` fence, anchored to the start and end of the stripped string only (fences in the middle of text are left alone; verified). `_Extraction.model_validate_json(raw)` then parses and validates in one step. Any `pydantic.ValidationError`, including the one raised for an empty string when `content` was `null`, for non-JSON text, or for an object missing required fields, becomes `LLMError("extraction returned invalid JSON (N error(s))")`. `{"memories": []}` is valid and yields an empty list.

Servers this adapter targets, per the class docstring and the README provider table: OpenAI (empty `OPENAI_BASE_URL`), Ollama (`http://localhost:11434/v1`, or `http://host.docker.internal:11434/v1` from inside `docker compose` per the comment in `docker-compose.yml`), vLLM, Groq, OpenRouter, LM Studio, and gateways. Nothing in the code is specific to any of them.

### `MockLLM`

```python
class MockLLM:
    """Deterministic provider for tests and key-less demos. `fail=True` simulates an outage."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> str:
        if self.fail:
            raise LLMError("mock failure")
        self.requests.append(request)
        context = request.system_prompt.split("Context:", 1)[1].strip()
        return f"(mock) Replying to '{request.messages[-1].content}' using context: {context}"
```

- `fail` is a plain mutable attribute; `tests/test_chat.py::test_9_llm_failure_returns_503_and_mutates_nothing` sets `llm.fail = True`, makes a request, then sets it back to `False` on the same instance.
- `requests` records every `LLMRequest` that reached `generate` and was not failed. Tests read `llm.requests[-1]` to assert on `system_prompt` contents and on the `role` sequence of `messages`. Failed calls are not recorded because the `append` follows the `fail` check.
- `generate` is a pure function of the request: it takes everything after the first `Context:` marker in the system prompt (the marker is written by `select_context` in `app/context.py`: `system = f"{SYSTEM_PROMPT}\n\nContext:\n{context}"`) and echoes it together with the last message's content. This is why `tests/test_chat.py::test_3_new_session_retrieves_memory` can assert `"switch jobs" in r["response"]`: the memory text was in the Context block.
- `extract_memories` raises `LLMError("mock failure")` when `fail` is set and otherwise returns `extract_by_rules(message, today)`, the regex extractor that covers the PRD §5 example sentences (see [Memory update](06-memory-update.md)).

### `build_llm(settings)`

```python
def build_llm(settings: Settings) -> LLMProvider:
    match settings.llm_provider:
        case "mock":
            return MockLLM()
        case "anthropic":
            return AnthropicLLM(settings.llm_model or "claude-opus-5", settings.llm_effort)
        case "openai":
            return OpenAICompatibleLLM(settings.llm_model, settings.openai_base_url, settings.openai_api_key,
                                       settings.llm_effort)
    raise ValueError(f"unknown LLM_PROVIDER {settings.llm_provider!r}; use anthropic, openai or mock")
```

| `LLM_PROVIDER` | Returns | Constructor arguments | Can fail at startup |
|---|---|---|---|
| `mock` | `MockLLM()` | none | no |
| `anthropic` | `AnthropicLLM(...)` | `settings.llm_model or "claude-opus-5"`, `settings.llm_effort` | no (a missing key surfaces on the first request) |
| `openai` | `OpenAICompatibleLLM(...)` | `settings.llm_model`, `settings.openai_base_url`, `settings.openai_api_key`, `settings.llm_effort` | yes: `ValueError("LLM_MODEL is required ...")` when `LLM_MODEL` is empty |
| anything else | raises | | yes: `ValueError("unknown LLM_PROVIDER 'x'; use anthropic, openai or mock")` |

Matching is exact and case-sensitive. `build_llm` is called once, inside the `lifespan` context manager of `create_app` in `app/main.py`: `app.state.llm = llm or build_llm(settings)`. Tests bypass it by passing `llm=MockLLM()` to `create_app`. Because the call is in the lifespan, both `ValueError`s abort application startup rather than a request.

## Contracts and invariants

- **`generate` returns a non-empty `str` or raises `LLMError`.** All three implementations enforce this: `AnthropicLLM` and `OpenAICompatibleLLM` strip the text and raise `LLMError("empty response")` on empty; `MockLLM` always returns a non-empty formatted string. `ChatService.chat` relies on it by appending `ChatMessage("assistant", reply)` to the session without checking.
- **`extract_memories` returns a `list[MemoryCandidate]` whose elements passed Pydantic validation, or raises `LLMError`.** Anthropic: server-side `json_schema` enforcement plus the SDK's `validate_json`. OpenAI: `model_validate_json` with `ValidationError` mapped to `LLMError`. Mock: `extract_by_rules` constructs `MemoryCandidate` objects directly. Taxonomy, confidence and emptiness checks are not this layer's job; `app/memory.py::validate` does them.
- **Outages become `LLMError`; 4xx do not.** `APIConnectionError`, `RateLimitError` and 5xx `APIStatusError` are mapped; any 4xx `APIStatusError` propagates as the SDK exception. This is asserted by `tests/test_openai_llm.py::test_status_mapping` (400 stays `openai.BadRequestError`) and stated in TDD §10 and CLAUDE.md ("In both providers 4xx errors deliberately propagate (our bug, not an outage)").
- **Message order is preserved and the system prompt travels separately.** Anthropic receives `system=` plus the turns; OpenAI receives a `system` message first, then the turns in the same order as `request.messages`. Neither provider reorders, deduplicates or truncates; budgets are enforced upstream by `select_context` and `SessionStore` (TDD §2 principle 5).
- **Only the user's message is sent for extraction.** Both real providers send exactly one user message containing `message`; no assistant text and no recent turns are included (TDD §2 principle 3). The extraction system prompt is the same `_extract_system(today)` on both paths; the OpenAI path appends `_JSON_SHAPE`.
- **`MockLLM` is deterministic and side-effect free apart from `requests`.** No randomness, no time dependence in `generate`; `extract_memories` depends on `today` only to resolve "next year". Given the same `LLMRequest` it returns the same string.
- **`MockLLM.generate` requires the `Context:` marker.** `split("Context:", 1)[1]` raises `IndexError` on a prompt without it. Every prompt built by `select_context` contains it, so this only matters for code that constructs `LLMRequest` by hand.
- **No provider mutates the request.** `LLMRequest` and its `ChatMessage`s are read, never written.
- **`_guarded` is transparent on success.** It returns the awaited value unchanged; each caller does its own response shaping afterwards.

## Design decisions and alternatives rejected

| Decision | Chosen | Rejected | Why (source) |
|---|---|---|---|
| Provider access | Official `anthropic` and `openai` SDKs | Hand-rolled `httpx` calls to each REST API | TDD §10 specifies "via the official `anthropic` SDK"; the SDKs supply retries, a shared typed exception hierarchy (what makes `_guarded` possible), credential resolution including `ant auth` profiles, and `messages.parse` for structured outputs. Rewriting those in `httpx` would be more code inside the one module that is supposed to stay thin. Rationale beyond TDD §10 is inferred |
| Error mapping | One `_guarded(call, sdk)` helper | A `try/except` block per provider method, or a per-provider exception-translation table | TDD §10: both SDKs are Stainless-generated with identical exception names, so one helper covers four call sites. Verified: the MROs of the relevant classes match in `anthropic` 1.7.0 and `openai` 3.16.2 |
| 4xx handling | Re-raise; surfaces as 500 | Map every SDK error to `LLMError` (503) | Code comment in `_guarded`: "4xx is our bug (bad request, auth): surface it as a 500, not a degraded 503". A 503 would be read by operators as a provider outage and by clients as retryable, hiding a bad model name, an invalid key or a malformed request (TDD §10) |
| Anthropic extraction | `messages.parse(..., output_format=_Extraction)` | Prompt-only JSON plus our own parsing | TDD §10: "extraction uses structured outputs (`messages.parse` with a Pydantic schema) so candidates arrive validated." Also the first half of the PRD §13 mitigation for invented memories |
| OpenAI-compatible extraction | `response_format={"type": "json_object"}` plus `_JSON_SHAPE` in the prompt plus `model_validate_json` | `response_format={"type": "json_schema", ...}` structured outputs | TDD §10 and the class docstring: `json_object` is "the lowest common denominator across OpenAI, Ollama, vLLM, Groq, OpenRouter and LM Studio". A schema-bearing `response_format` is not something every compatible server accepts; validating on our side gives the same guarantee to `app/memory.py` regardless of server |
| Code fences in extraction output | Strip a leading/trailing fence with `_FENCE` before validating | Reject fenced output as invalid | Code comment: "smaller models fence their JSON". Stripping costs one regex and turns a common local-model habit into a non-event |
| `reasoning_effort` | Sent only when `LLM_EFFORT` is non-empty | Always send; or a per-model allowlist | TDD §10 "(omitted when empty, for models that reject it)"; code comment. An empty variable is the smallest possible opt-out and needs no model knowledge in the code. The allowlist alternative is inferred |
| Model name for `openai` | `LLM_MODEL` required; `ValueError` at startup | A guessed default such as an OpenAI model id | TDD §10: "required, since model names are server-specific". A default that is right for api.openai.com is wrong for Ollama, vLLM or a gateway, and would fail at the first request instead of at startup |
| Model name for `anthropic` | Falls back to `claude-opus-5` | Require `LLM_MODEL` here too | TDD §10 and §27 name `claude-opus-5` as the provider's default; there is exactly one server, so a default is meaningful |
| Placeholder key | `api_key or "not-needed"` | Require `OPENAI_API_KEY` | Code comment: "Local servers such as Ollama ignore the key, but the SDK insists on a non-empty one." Verified: the SDK raises on `None` and `""` |
| `generate` return type | `str` | PRD FR-6's `LLMResponse` | TDD §10: the type "would carry only provider metadata nobody reads yet". (The TDD §27 table records the provider line-up change for §10; the return-type note is in §10's own text) |
| Extraction input | `(message: str, today: date)` | PRD FR-6's `MemoryExtractionRequest` | Not recorded; inferred from TDD §27's general removal of single-use wrappers and from the prompt needing exactly those two values |
| Schema wrapper | `_Extraction` with a single `memories` list | Ask for a bare JSON array | TDD §7.1 shows the `{"memories": [...]}` shape; an object at the top level is also what `json_object` mode returns |
| Fallback text on LLM failure | None; `LLMError` becomes 503 | PRD §13 "Return a deterministic fallback message" | TDD §13.3: "there is no deterministic fallback text that is honest for an astrology question." Decided in the orchestration/API layers, but it is why `LLMError` carries a message and nothing else |
| Mock | `MockLLM` with `fail` flag and recorded requests | `unittest.mock` patches of the SDK clients | TDD §10 and §16.2: tests use it exclusively and it doubles as the no-key demo mode. Recording `LLMRequest`s is what lets the scenarios assert on the prompt (README "Tests") |

## Failure modes and degraded behavior

The orchestration in `app/chat.py` determines what a user sees. Generation happens before the session append and before the memory update; extraction happens after both. `app/main.py` registers one handler for `LLMError` (`503`, body `{"detail": "LLM unavailable: <message>"}`); any other exception is unhandled and becomes a 500.

| Where | What happened | Exception at the boundary | What the client sees | Side effects |
|---|---|---|---|---|
| `generate` | Connection refused, DNS failure, timeout (after SDK retries) | `LLMError("provider unavailable: ...")` | `503 {"detail": "LLM unavailable: provider unavailable: ..."}` | None: no session append, no memory write (TDD §13.3; `tests/test_chat.py::test_9_...`) |
| `generate` | 429 rate limit (after SDK retries) | `LLMError("provider unavailable: ...")` | 503 | None |
| `generate` | 5xx from the provider (after SDK retries) | `LLMError("provider error 5xx")` | 503 | None |
| `generate` | 4xx: bad request, invalid or missing key, unknown model, rejected `reasoning_effort` | SDK exception re-raised (`BadRequestError`, `AuthenticationError`, `NotFoundError`, ...) | 500, unhandled exception with the SDK traceback in the log | None |
| `generate` (Anthropic) | `stop_reason == "refusal"` | `LLMError("model declined the request")` | 503 | None |
| `generate` | Empty or whitespace-only text (`null` content on OpenAI) | `LLMError("empty response")` | 503 | None |
| `generate` (mock) | `fail=True` | `LLMError("mock failure")` | 503 | None |
| `extract_memories` | Any of the `LLMError` cases above, including OpenAI-path invalid JSON (`extraction returned invalid JSON (N error(s))`) | `LLMError` caught in `ChatService.chat` | `200` with the generated `response`, `memory_updates: 0`, `degraded: false` | Both turns are in the session; a `WARNING` "memory extraction failed, response still returned" is logged; nothing written to the brain (TDD §13.3; README "Failure modes": "Extraction fails after a good response") |
| `extract_memories` | 4xx from the provider | SDK exception re-raised; not caught by `chat.py`, which handles only `LLMError` and `BrainUnavailable` | 500, even though generation succeeded | Both turns are already in the session; no memory write |
| `extract_memories` (Anthropic) | Text block that does not validate against `_Extraction` | Pydantic `ValidationError` raised inside the SDK's post-parser; not an SDK `APIError`, so `_guarded` does not map it | 500 (inferred from the code path; not observed) | As above. With server-side `json_schema` enforcement this is not expected |
| `extract_memories` (Anthropic) | Response with no text block (for example a refusal) | none; `parsed_output` is `None` | 200, `memory_updates: 0` | Nothing written; no warning logged because no error was raised |
| `build_llm` | `LLM_PROVIDER=openai` without `LLM_MODEL`, or an unknown provider name | `ValueError` | Application does not start (raised in the lifespan) | None |

Note that `degraded: true` is reserved for Shared Brain failures (`BrainUnavailable`); LLM extraction failures leave it `false`. Note also the asymmetry that TDD §13.3 asks for: a failed generation records nothing, while a failed extraction keeps the response and the session history.

## Configuration

All of these are read by `Settings.from_env` in `app/config.py` except `ANTHROPIC_API_KEY`, which the Anthropic SDK reads directly. `docker-compose.yml` forwards each of them with `${VAR:-default}`. Details of the settings mechanism are in [Configuration and deployment](08-configuration-and-deployment.md).

| Variable | `Settings` field | Default | Effect in this layer |
|---|---|---|---|
| `LLM_PROVIDER` | `llm_provider` | `anthropic` | Selects the class in `build_llm`: `anthropic`, `openai` or `mock`; anything else aborts startup with `ValueError` |
| `LLM_MODEL` | `llm_model` | `""` | `anthropic`: model id, `claude-opus-5` when empty. `openai`: required, server-specific (for example `gpt-4o` or `llama3.1` per `.env.example`); empty aborts startup with `ValueError`. `mock`: ignored |
| `LLM_EFFORT` | `llm_effort` | `medium` | `anthropic`: sent as `output_config={"effort": value}` on `generate` only, not on extraction; sent even when empty (`{"effort": ""}`, verified), although README "Configuration" and the `config.py` comment say empty omits it. `openai`: sent as `reasoning_effort` on both calls when non-empty, omitted entirely when empty. Documented values are `low`, `medium`, `high`; the Anthropic SDK's type also lists `xhigh` and `max`; AstroChat does not validate the value |
| `ANTHROPIC_API_KEY` | none | unset | Read by `anthropic.AsyncAnthropic()`; alternatives are `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_PROFILE`, or an active `ant auth login` profile on disk (SDK credential order, see `AnthropicLLM.__init__` above). Not checked at startup |
| `OPENAI_BASE_URL` | `openai_base_url` | `""` | Passed as `base_url`; empty means the SDK default `https://api.openai.com/v1`. Ollama: `http://localhost:11434/v1`; from inside `docker compose`: `http://host.docker.internal:11434/v1` |
| `OPENAI_API_KEY` | `openai_api_key` | `""` | Passed as `api_key`; empty becomes the literal `not-needed`, which local servers ignore |

Worked examples (README "Run it"):

- Anthropic with the default model: `export ANTHROPIC_API_KEY=sk-ant-...` then `docker compose up --build`, or `uv run --env-file .env uvicorn app.main:app --reload` with `LLM_PROVIDER=anthropic` in `.env`.
- Local Ollama: `LLM_PROVIDER=openai OPENAI_BASE_URL=http://localhost:11434/v1 LLM_MODEL=llama3.1 uv run uvicorn app.main:app`. No key is needed; the placeholder is sent. Add `LLM_EFFORT=` (empty) if the model rejects `reasoning_effort`.
- No key, no Neo4j: `BRAIN=memory LLM_PROVIDER=mock uv run uvicorn app.main:app --reload`.

## Tests that pin this layer

`uv run pytest -q` runs the suite in a few seconds with no services or keys (48 passed, 1 skipped at the time of writing; the skip is `tests/test_neo4j.py` without `NEO4J_TEST_URI`). See [Testing and verification](09-testing-and-verification.md) for the suite as a whole.

**The MockTransport technique.** `tests/test_openai_llm.py::fake(handler, effort)` constructs `OpenAICompatibleLLM("local-model", base_url="http://fake/v1", api_key="k", effort=effort, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), max_retries=0)`, so the real `openai` SDK serialises the request and parses the response while a plain Python `handler(req) -> httpx.Response` plays the server, in process and without network. `completion(content)` returns the minimal `chat.completion` JSON the SDK needs, and `max_retries=0` makes error statuses fail on the first attempt.

| Test | The one thing it asserts |
|---|---|
| `tests/test_openai_llm.py::test_generate_sends_system_then_turns` | Request goes to `http://fake/v1/chat/completions` with `Authorization: Bearer k`, `model == "local-model"`, `reasoning_effort == "medium"`, messages `[system, user, assistant, user]` in order; the padded reply is returned stripped |
| `tests/test_openai_llm.py::test_empty_effort_omits_reasoning_effort` | With `effort=""` the request body has no `reasoning_effort` key |
| `tests/test_openai_llm.py::test_extract_requests_json_mode_and_tolerates_fences` | Extraction sends `response_format == {"type": "json_object"}` and `reasoning_effort`, the system prompt contains "Respond with JSON only" and the resolved next year `2027` (for `today = 2026-09-21`), and a ` ```json ` fenced payload parses to one `career.goal` candidate with value, timeframe and confidence intact |
| `tests/test_openai_llm.py::test_extract_invalid_output_is_llm_error[not json]`, `[{"memories": [{"key": "x"}]}]`, `[None]` | Non-JSON, an object missing required fields, and `null` content each raise `LLMError` |
| `tests/test_openai_llm.py::test_generate_empty_content_is_llm_error` | `null` content on generation raises `LLMError` matching "empty" |
| `tests/test_openai_llm.py::test_status_mapping[500]`, `[503]`, `[429]` | Each status raises `LLMError` |
| `tests/test_openai_llm.py::test_status_mapping[400]` | 400 raises `openai.BadRequestError`, not `LLMError` (4xx propagates) |
| `tests/test_openai_llm.py::test_connection_error_is_llm_error` | A handler raising `httpx.ConnectError` yields `LLMError` matching "unavailable" |
| `tests/test_openai_llm.py::test_build_llm_selects_provider` | `mock`, `anthropic`, `openai` map to `MockLLM`, `AnthropicLLM`, `OpenAICompatibleLLM`; `openai` without `llm_model` raises `ValueError` matching "LLM_MODEL"; `gemini` raises `ValueError` matching "unknown". Constructing `AnthropicLLM` here needs no key |
| `tests/test_chat.py::test_9_llm_failure_returns_503_and_mutates_nothing` | With `MockLLM.fail = True`, `POST /chat` returns 503 with "LLM" in `detail`, the brain has no memories and no profile, and after `fail = False` the next turn in the same session shows no `recent_conversation` (the failed turn was never appended) |
| `tests/test_chat.py::test_4_follow_up_uses_recent_context_only` | Via `llm.requests[-1]`: the message roles sent are `[user, assistant, user, assistant, user]` and `career.goal` is absent from the system prompt |
| `tests/test_chat.py::test_6_irrelevant_memory_excluded`, `test_7_user_correction_supersedes`, `test_profile_endpoint_feeds_astrology_context` | Via `llm.requests[-1].system_prompt`: the excluded memory text is absent, only the active `Hindi` value is present, `Leo` is present |
| `tests/test_chat.py::test_3_new_session_retrieves_memory` | `"switch jobs" in r["response"]`, which holds only because `MockLLM.generate` echoes the Context block |
| `tests/conftest.py::llm`, `client` | `MockLLM()` is injected through `create_app(brain=, llm=, settings=Settings(brain="memory", llm_provider="mock"))`, so `build_llm` is bypassed in scenario tests |

Not tested: `AnthropicLLM.generate` and `AnthropicLLM.extract_memories`. CLAUDE.md: "`AnthropicLLM` has no automated test (needs a key)". `tests/test_units.py` does not touch this module.

## Known limits and future work

**`ponytail:` markers.** There are none in `app/llm.py`. The four in the repository (`app/astrology.py`, `app/session.py`, `app/brain.py`, `app/context.py`) belong to other layers.

**Untested Anthropic path.** The request shapes and the refusal and empty-effort behaviours described above were checked once against a fake transport while writing this document, not by a committed test. The `httpx.MockTransport` technique does not transfer as-is: the installed `anthropic` 1.7.0 rejects a plain `httpx.AsyncClient` with `TypeError: ... this SDK uses httpx2. Use httpx2.AsyncClient instead.` A test would need `httpx2.AsyncClient(transport=httpx2.MockTransport(handler))`, an `async def handler` (that `MockTransport` awaits the handler's result), and `httpx2.Response` objects, passed through `AnthropicLLM(..., api_key="k", http_client=..., max_retries=0)`. The `openai` 3.16.2 SDK also imports `httpx2` internally yet accepts the plain `httpx.AsyncClient` the existing tests pass.

**Empty `LLM_EFFORT` on the Anthropic path.** README "Configuration" and the comment in `app/config.py` say an empty value omits the parameter on both providers. `AnthropicLLM.generate` always sends `output_config={"effort": self._effort}`, so an empty `LLM_EFFORT` reaches the API as `{"effort": ""}` (verified against a fake transport). What the API does with that is not verified here; if it rejects the value with a 400, `_guarded` re-raises the `BadRequestError` and every `/chat` request ends as a 500 rather than a 503. Either the code should mirror `OpenAICompatibleLLM._extra`, or the documentation should say the opt-out is OpenAI-only.

**Effort is not applied to Anthropic extraction.** `messages.parse` is called without `output_config`, so extraction always runs at the API default effort while generation honours `LLM_EFFORT`. Not recorded as intentional.

**Unmapped exceptions during extraction.** `ChatService.chat` catches only `LLMError` and `BrainUnavailable` around the memory update. A 4xx SDK exception, or a Pydantic `ValidationError` raised inside the Anthropic SDK's post-parser, escapes after the response has already been generated and the turns appended, turning a successful generation into a 500. The 4xx case is consistent with the "our bug" rule; the `ValidationError` case is a gap that `OpenAICompatibleLLM.extract_memories` closes for its own path and `AnthropicLLM.extract_memories` does not.

**An odd `RECENT_LIMIT` can put an assistant turn first.** `SessionStore` in `app/session.py` is a `deque(maxlen=limit)` that receives turns in user/assistant pairs. With the default `10` the full window always starts with a user message; with an odd limit (verified with `maxlen=3`: after two turns the window is `[assistant, user, assistant]`) `request.messages` sent by both providers begins with role `assistant` followed by the current user message. Neither adapter reorders or drops messages, and whether a given provider accepts a conversation that opens with an assistant turn is server-side behaviour not verified here; if it is rejected as a 400, the request ends as a 500. The window itself belongs to [Orchestration and short-term context](02-orchestration-and-short-term-context.md).

**`MockLLM.generate` depends on the `Context:` marker.** As noted under Contracts, `request.system_prompt.split("Context:", 1)[1]` raises `IndexError` for a prompt that lacks the literal `Context:`. Every prompt from `select_context` contains it; a hand-built `LLMRequest` in a new test would not necessarily.

**Dead alternative in the rule extractor.** `_LIKE` is `r"\bi (?:love|enjoy|like|am into|'m into) ..."`: the mandatory space after `i` means the `'m into` branch can only match `i 'm into`, so "I'm into cricket." extracts nothing while "I am into cricket." does (verified). This is `extract_by_rules` territory, covered in [Memory update](06-memory-update.md); it is recorded here because the pattern lives in `app/llm.py`.

**No `max_tokens` on the OpenAI path, fixed 16000 on Anthropic.** Neither number nor its absence is justified in the design documents. A long reply on an OpenAI-compatible server is bounded only by the server's default; on Anthropic a truncated reply (`stop_reason == "max_tokens"`) is returned as-is.

**Retries and timeouts are SDK defaults.** Two retries with backoff and a 600 s read timeout mean a hung provider can hold a request far longer than the Shared Brain's 3 s budget. TDD §13.2 tuned the Neo4j driver; nothing equivalent exists here.

**Single provider per process; no fallback or routing.** PRD §12 defers "Model routing/fallback across providers" and PRD §14 and TDD §23 Phase 2 list an "LLM fallback provider". The Protocol makes a composite provider (try one adapter, fall back to another on `LLMError`) a pure addition to this module, but nothing is built. TDD §13.3: "A production deployment could add a fallback model."

**Extraction validation is split across layers by design.** This module guarantees shape; `app/memory.py::validate` guarantees taxonomy and confidence. Anyone adding a provider must return validated `MemoryCandidate` objects or raise `LLMError`, and must not import the SDK anywhere else (PRD FR-6, TDD §2 principle 4).

**Streaming, tool use, token accounting and prompt caching are out of scope.** `generate` returns a complete string; non-text content blocks from Anthropic are silently dropped; no usage metadata is surfaced (TDD §10's reason for returning `str`). Adding any of these is where an `LLMResponse` type would start to earn its place.
