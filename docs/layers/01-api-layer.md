# API layer

The API layer is the HTTP surface of AstroChat: one module, `app/main.py`, containing the app factory `create_app(brain, llm, settings)`, its lifespan (which builds or receives the Shared Brain and the LLM provider, bootstraps the Neo4j schema, and closes the driver on shutdown), five Pydantic schemas, three routes (`POST /chat`, `POST /users`, `GET /users/{user_id}/memories`) and two exception handlers (`LLMError` and `BrainUnavailable`, both mapped to `503`). Routes validate and delegate; they contain no Cypher and no provider SDK calls, which is what lets `tests/test_chat.py` run every required scenario through the real FastAPI app, with `InMemoryBrain` and `MockLLM` injected through the factory by the fixture in `tests/conftest.py`.

**Files:** `app/main.py`

**Depends on:** [Orchestration and short-term context](02-orchestration-and-short-term-context.md) (`app/chat.py`, `app/session.py`), [Shared Brain](05-shared-brain.md) (`app/brain.py`), [LLM providers](07-llm-providers.md) (`app/llm.py`), [Configuration and deployment](08-configuration-and-deployment.md) (`app/config.py`). **Used by:** uvicorn (`app.main:app`, see the `Dockerfile` and the run commands in the repo [README](../../README.md)) and the test fixtures in `tests/conftest.py` ([Testing and verification](09-testing-and-verification.md)). Sibling docs are indexed in [the layer index](README.md).

## What this layer is

`app/main.py` is the last module in the dependency order listed in `CLAUDE.md` (`config`, `models` → `session`, `astrology`, `brain` → `context` → `llm` → `memory` → `chat` → `main`). Its module docstring is the contract: "FastAPI surface: app factory, schemas, routes, error handlers. No Neo4j or LLM calls here."

It holds exactly these things:

| Item | Kind | Purpose |
|---|---|---|
| `logging.basicConfig(...)` | module-level statement | Root logger format and level (`LOG_LEVEL`, default `INFO`) |
| `ChatRequest`, `ChatResponse` | Pydantic models | Body and response of `POST /chat` |
| `UserUpsert`, `ProfileOut` | Pydantic models | Body and response of `POST /users` |
| `MemoryOut` | Pydantic model | Element type of the `GET /users/{user_id}/memories` response |
| `create_app(brain, llm, settings)` | factory function | Builds a `FastAPI` instance with a lifespan, two handlers and three routes |
| `app = create_app()` | module-level instance | The object uvicorn imports as `app.main:app` |

It imports from the rest of the application only what it must wire together: `BrainUnavailable`, `InMemoryBrain`, `Neo4jBrain`, `SharedBrain` from `app/brain.py`; `ChatService` from `app/chat.py`; `Settings` from `app/config.py`; `LLMError`, `LLMProvider`, `build_llm` from `app/llm.py`; and `SessionStore` from `app/session.py`. It does not import `neo4j`, `anthropic` or `openai`; `app/llm.py` is the only module that imports provider SDKs (`CLAUDE.md`, Layout), and `app/brain.py` is the only one that imports the `neo4j` driver (observed by grep; inferred, not a `CLAUDE.md` statement).

## Why it exists

The layer satisfies the externally visible requirements of the product and isolates the rest of the code from HTTP:

- **PRD FR-1 (Chat API)** requires `POST /chat` taking `user_id`, `session_id`, `message` and returning `response`, `user_id`, `session_id`, `context_used`, and permits "additional diagnostic metadata where useful". `ChatRequest` and `ChatResponse` are that contract; `memory_updates` and `degraded` are the metadata (TDD §14 calls them "observability fields rather than core assignment requirements").
- **PRD FR-2 (User Profile)** lists name, date of birth, time of birth, birth place, preferred language and sun sign, and says profile information "may be provided during user creation or by a separate endpoint". `POST /users` with `UserUpsert` and `ProfileOut` is that endpoint; `sun_sign` is derived in the brain, never accepted from the client.
- **PRD §9 (API Requirements)** names `POST /chat` as required and `POST /users` and `GET /users/{user_id}/memories` as recommended optional endpoints for observability and demoability. All three exist.
- **PRD FR-8 (Error Handling)** asks for "useful HTTP errors for client mistakes and safe fallback behavior for downstream failures", naming invalid payloads, missing profile, empty memory, no relevant context, LLM failure and Neo4j failure. This layer owns the client-mistake half (`422`) and the terminal mapping of the two downstream failure types (`503`); the safe-fallback half for Neo4j lives in `ChatService` and shows up here only as `degraded: true` in a `200`.
- **TDD §4.1 (API layer)** fixes the responsibilities: validate request/response schemas, return HTTP errors for malformed requests, delegate business logic to the chat orchestrator, and "avoid direct Neo4j or LLM calls from route handlers". It also fixes the shape: "`app/main.py` holds the app factory, Pydantic schemas, three routes and two exception handlers."
- **TDD §13 (Error Handling and Degraded Modes)** specifies `422` for invalid payloads (§13.1), `200` with `degraded: true` when Neo4j is unavailable (§13.2), and `503` with nothing recorded when the LLM is unavailable (§13.3).
- **TDD §14 (API Contract)** gives the exact `POST /chat` request and response, including the recommended `memory_updates` and `degraded` extension and the precise definition of `context_used`.

The problem it solves, beyond serving HTTP, is testability. Every required scenario (TDD §17) is asserted through `POST /chat` in `tests/test_chat.py`. That is only possible because the route reaches its collaborators through `request.app.state`, which the factory populates from its arguments, so a test can hand the app an `InMemoryBrain` and a `MockLLM` and observe real HTTP status codes and JSON without a database or an API key (README, Tests: "Tests run through the real FastAPI app with `InMemoryBrain` and `MockLLM` injected").

## How it works

### The factory `create_app`

```python
def create_app(brain: SharedBrain | None = None, llm: LLMProvider | None = None,
               settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
```

All three arguments are optional. `settings` defaults to `Settings.from_env()` (`app/config.py`), evaluated when the factory is called. `brain` and `llm` default to `None`, and the decision of what to construct in their place is deferred to the lifespan, not made here. The factory then creates `FastAPI(title="AstroChat", version="0.1.0", lifespan=lifespan)`, registers the two exception handlers, registers the three routes as closures, and returns the app. Nothing in the factory body touches the network.

Because `brain` and `llm` are typed as the protocols `SharedBrain` and `LLMProvider`, anything satisfying those protocols can be injected: `InMemoryBrain` or `Neo4jBrain`; `MockLLM`, `AnthropicLLM` or `OpenAICompatibleLLM`.

### Lifespan: construction, schema bootstrap, degraded startup, shutdown

```python
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.brain = brain or (
            InMemoryBrain() if settings.brain == "memory"
            else Neo4jBrain(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password))
        if isinstance(app.state.brain, Neo4jBrain):
            try:
                await app.state.brain.ensure_schema()
            except BrainUnavailable as e:
                log.warning("Neo4j unreachable at startup, serving degraded until it returns: %s", e)
        app.state.llm = llm or build_llm(settings)
        app.state.chat = ChatService(app.state.brain, app.state.llm, SessionStore(settings.recent_limit),
                                     settings.memory_limit, settings.min_confidence)
        log.info("ready brain=%s llm=%s", type(app.state.brain).__name__, type(app.state.llm).__name__)
        yield
        if close := getattr(app.state.brain, "close", None):
            await close()
```

Step by step, at server startup (before the first request is served):

1. **Brain.** If a `brain` was injected it is used as-is. Otherwise `settings.brain == "memory"` selects `InMemoryBrain()`; any other value selects `Neo4jBrain(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)`. Constructing `Neo4jBrain` creates the driver with a 3 s `connection_timeout` and `max_transaction_retry_time` (`app/brain.py`, `Neo4jBrain.__init__`) but does not open a connection.
2. **Schema bootstrap.** Only when the brain is a `Neo4jBrain`, `ensure_schema()` runs the three statements in `brain.SCHEMA`: unique constraints on `User.id` and `Memory.id` and the composite index `Memory(category, key, status)` used by the logical-key lookup (README, Graph schema). This is the first network call.
3. **Degraded startup.** If `ensure_schema()` raises `BrainUnavailable` (the translation `Neo4jBrain._query` applies to `ServiceUnavailable`, `SessionExpired`, `AuthError` and `OSError`), the lifespan logs a warning and continues; the process serves requests with the brain marked unavailable per call. Because `Neo4jBrain` maps `AuthError` too, wrong credentials also start degraded rather than crashing (observable from `brain._CONNECTIVITY_ERRORS`; inferred consequence). The warning text is the recorded intent: "serving degraded until it returns".
4. **LLM.** If an `llm` was injected it is used; otherwise `build_llm(settings)` (`app/llm.py`) selects `MockLLM`, `AnthropicLLM` or `OpenAICompatibleLLM` from `settings.llm_provider`. `build_llm` raises `ValueError` for an unknown provider or for `openai` without `LLM_MODEL` (`CLAUDE.md`, Gotchas). That exception is not caught here, so it aborts startup: the server does not come up with a misconfigured LLM. Because it is raised before `yield`, the post-`yield` cleanup never runs and the driver constructed in step 1 is not closed.
5. **Orchestrator.** `ChatService(brain, llm, SessionStore(settings.recent_limit), settings.memory_limit, settings.min_confidence)` is built once and stored on `app.state.chat`. The `SessionStore` (the short-term deque store, `app/session.py`) is created here, so there is one per process.
6. **Ready log.** `ready brain=<ClassName> llm=<ClassName>` at `INFO`, which is how an operator confirms which implementations a process is running.

At shutdown, after `yield`, the lifespan calls `close()` on the brain if it has one. `Neo4jBrain.close` awaits `driver.close()`; `InMemoryBrain` has no `close`, so `getattr(..., None)` skips it. The LLM provider objects are not closed.

Two consequences of doing all of this in the lifespan rather than in the factory body:

- Importing `app.main` (which executes `app = create_app()`) reads environment variables but opens no connections and needs no API key. `tests/conftest.py` imports `create_app` from `app.main` with default env and nothing happens until a `TestClient` is entered.
- `ensure_schema()` is a coroutine and `create_app` is a synchronous function; the lifespan is the natural place to await it.

### Schemas

All five are `pydantic.BaseModel` subclasses. `CLAUDE.md` (Gotchas) simplifies the rest of the application as "everything else is a dataclass" apart from `MemoryCandidate`; precisely, the domain types in `app/models.py` are dataclasses except `MemoryCandidate` and the `Category` `StrEnum`, and `app/llm.py` has one more private Pydantic model, `_Extraction`, wrapping the extractor's output. These five are Pydantic because FastAPI uses them for body validation, response serialization and the OpenAPI document. Routes convert between them and the dataclasses with `vars(...)`.

**`ChatRequest`** (body of `POST /chat`)

```python
class ChatRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=4000)
```

| Field | Type | Constraint | Effect and rationale |
|---|---|---|---|
| `user_id` | `str` | required, 1 to 128 characters | Identifies the `User` node and the session key. Empty string rejected with `422` so no nameless `User` node can be merged. Length bound is not recorded in the TDD or README; inferred as a sanity cap on an identifier that becomes a graph key and a dict key. |
| `session_id` | `str` | required, 1 to 128 characters | Second half of the `(user_id, session_id)` short-term context key. Same rationale, inferred. |
| `message` | `str` | required, 1 to 4000 characters | The only free text the LLM sees from the client. Empty message rejected (asserted by `tests/test_chat.py::test_invalid_payload_is_422`). The 4000 upper bound is not recorded; inferred as a prompt-size cap at the trust boundary, consistent with TDD §21's statement that limiting prompt size is the most important performance control. |

Pydantic's defaults apply otherwise: unknown extra keys are ignored, not rejected; `min_length` counts characters without stripping, so a whitespace-only message is accepted.

**`ChatResponse`** (response of `POST /chat`)

| Field | Type | Source |
|---|---|---|
| `response` | `str` | `ChatResult.response`, the LLM's reply text |
| `user_id` | `str` | Echoed from `ChatRequest.user_id` |
| `session_id` | `str` | Echoed from `ChatRequest.session_id` |
| `context_used` | `list[str]` | `ChatResult.context_used`, built by `select_context` in `app/context.py`; passed through unchanged |
| `memory_updates` | `int` | `ChatResult.memory_updates`, the count returned by `memory.remember`: one per profile field written plus one per Memory upsert that returned `created` or `updated` |
| `degraded` | `bool` | `ChatResult.degraded`, `True` when a Shared Brain read or write failed during this request |

No constraints; this is the shape PRD FR-1 requires plus the TDD §14 "recommended response extension".

**`UserUpsert`** (body of `POST /users`)

```python
class UserUpsert(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)
    name: str | None = None
    date_of_birth: date | None = None
    time_of_birth: str | None = None
    birth_place: str | None = None
    preferred_language: str | None = None
```

| Field | Type | Constraint | Effect and rationale |
|---|---|---|---|
| `user_id` | `str` | required, 1 to 128 characters | Same bound as `ChatRequest.user_id`. |
| `name` | `str \| None` | optional | Stored on the `Profile` node as given. |
| `date_of_birth` | `date \| None` | optional; must parse as a date | Pydantic parses an ISO 8601 date string such as `1995-08-15` into `datetime.date`; anything it cannot parse is a `422` before the route body runs (`tests/test_chat.py::test_invalid_payload_is_422` posts `"not-a-date"` and asserts `422`). Rationale (inferred from `app/brain.py`): `SharedBrain.upsert_profile` takes `dict[str, str]` and `_with_sun_sign` recomputes `sun_sign` only when `parse_date` succeeds, otherwise it stores the string unchanged; typing the field as `date` guarantees a client can never persist an unparseable birth date through this endpoint, and that whenever a birth date is stored a `sun_sign` is stored with it (a profile without a `date_of_birth` still has `sun_sign: null`). The route re-serializes with `.isoformat()`, so the brain always receives `YYYY-MM-DD`. |
| `time_of_birth` | `str \| None` | optional | Free text; no format enforced. |
| `birth_place` | `str \| None` | optional | Free text. |
| `preferred_language` | `str \| None` | optional | Stored on the `Profile` node. Note that a language preference stated in chat is stored as the Memory `language.preferred` instead, deliberately, so the supersede flow is demonstrable (`CLAUDE.md`, Invariants; TDD §27 row §7.0). |

`sun_sign` is intentionally absent: it is derived, never accepted (README, API: "`sun_sign` is derived").

**`ProfileOut`** (response of `POST /users`)

Seven fields: `user_id: str`, then `name`, `date_of_birth`, `time_of_birth`, `birth_place`, `preferred_language`, `sun_sign`, each `str | None` with no default. The six nullable fields are exactly the six fields of the `UserProfile` dataclass in `app/models.py` (`PROFILE_FIELDS`), which is why `ProfileOut(user_id=req.user_id, **vars(profile))` works without mapping. `date_of_birth` is a `str` here, not a `date`, because `UserProfile.date_of_birth` stores the normalized ISO string. Fields the user never set come back as `null`.

**`MemoryOut`** (element of the `GET /users/{user_id}/memories` response)

Eleven fields mirroring the `Memory` dataclass in `app/models.py` one for one: `id`, `key`, `category`, `type`, `value` (`str`), `target_timeframe` (`str | None`), `confidence` (`float`), `status` (`str`, `ACTIVE` or `SUPERSEDED`), `source_message_id` (`str | None`), `created_at` and `updated_at` (`datetime`, serialized as ISO 8601). `MemoryOut(**vars(m))` depends on this correspondence in one direction: a required field added to `MemoryOut` and missing from `Memory` fails at request time with a Pydantic error, while a field added to `Memory` and not to `MemoryOut` is silently dropped from the response (Pydantic's default `extra="ignore"`).

### Routes

**`POST /chat`** (`chat` in `create_app`)

```python
    @app.post("/chat", response_model=ChatResponse)
    async def chat(req: ChatRequest, request: Request):
        result = await request.app.state.chat.chat(req.user_id, req.session_id, req.message)
        return ChatResponse(response=result.response, user_id=req.user_id, session_id=req.session_id,
                            context_used=result.context_used, memory_updates=result.memory_updates,
                            degraded=result.degraded)
```

Delegates entirely to `ChatService.chat(user_id, session_id, message)` in `app/chat.py`, which runs the whole pipeline (recent turns → classify → Shared Brain read → context selection → generate → session append → extract → remember) and returns a `ChatResult` dataclass. The route copies the four result fields into a `ChatResponse` and echoes the two identifiers from the request. It does not catch anything; `LLMError` raised by `ChatService` propagates to the exception handler.

**`POST /users`** (`upsert_user`)

```python
    @app.post("/users", response_model=ProfileOut)
    async def upsert_user(req: UserUpsert, request: Request):
        fields = {k: (v.isoformat() if isinstance(v, date) else v)
                  for k, v in req.model_dump(exclude={"user_id"}, exclude_none=True).items()}
        if not fields:
            raise HTTPException(422, "no profile fields provided")
        profile = await request.app.state.brain.upsert_profile(req.user_id, fields)
        return ProfileOut(user_id=req.user_id, **vars(profile))
```

Builds the `fields` dict from every provided (non-`None`) field except `user_id`, converting the parsed `date` back to its ISO string, and rejects a body that carries only `user_id` with a hand-raised `422`. It then calls `SharedBrain.upsert_profile(user_id, fields)` directly on `app.state.brain` (not through `ChatService`; there is no chat turn involved). The brain merges the `User` and `Profile` nodes, applies the fields, and recomputes `sun_sign` when `date_of_birth` is present (`_with_sun_sign` in `app/brain.py`). The returned `UserProfile` becomes the `ProfileOut`. Because of `exclude_none=True`, sending `"name": null` is the same as omitting `name`; this endpoint cannot clear a field.

**`GET /users/{user_id}/memories`** (`list_memories`)

```python
    @app.get("/users/{user_id}/memories", response_model=list[MemoryOut])
    async def list_memories(user_id: str, request: Request):
        return [MemoryOut(**vars(m)) for m in await request.app.state.brain.list_memories(user_id)]
```

Calls `SharedBrain.list_memories(user_id)` and converts each `Memory` to a `MemoryOut`. It returns every memory for the user including `SUPERSEDED` ones (TDD §4.4: "debug endpoint; includes SUPERSEDED"; README, API: "lists every memory including superseded ones, for inspection"). `Neo4jBrain` orders by `created_at` (`LIST_MEMORIES`); `InMemoryBrain` returns insertion order, which is the same thing. An unknown `user_id` yields `[]`, not `404`. The path parameter is a plain `str` with no length constraint.

### Exception handlers

```python
    @app.exception_handler(LLMError)
    async def _llm_error(_: Request, exc: LLMError):
        return JSONResponse({"detail": f"LLM unavailable: {exc}"}, status_code=503)

    @app.exception_handler(BrainUnavailable)
    async def _brain_error(_: Request, exc: BrainUnavailable):
        return JSONResponse({"detail": f"Shared Brain unavailable: {exc}"}, status_code=503)
```

Both produce `503` with a `detail` string prefixed so a client can tell which dependency failed (`tests/test_chat.py::test_9_llm_failure_returns_503_and_mutates_nothing` asserts `"LLM" in r.json()["detail"]`).

Where each can actually fire:

- `LLMError` reaches the handler from `POST /chat` when `ChatService.chat` calls `self.llm.generate(...)` and the provider raises. `ChatService` deliberately does not catch it there (comment in `app/chat.py`: "LLMError propagates to the API as 503; the failed turn is not recorded anywhere"). An `LLMError` from `extract_memories` after a successful generation is caught inside `ChatService` and never reaches this layer.
- `BrainUnavailable` never reaches the handler from `POST /chat`: `ChatService.chat` catches it on the read path (setting `degraded = True`) and on the write path (`CLAUDE.md`: `ChatService` "sees only `BrainUnavailable` and `LLMError`, never driver or SDK exceptions"). It reaches the handler from `POST /users` and `GET /users/{user_id}/memories`, which call the brain directly and have no degraded mode. `tests/test_chat.py::test_10_graph_failure_degrades` pins the `/chat` side (`200` with `degraded: true`); no test exercises the `/users` side.

**Why 4xx SDK errors are not handled here.** `_guarded` in `app/llm.py` maps provider outages to `LLMError`: connection errors, rate limits and any `APIStatusError` with status ≥ 500. It re-raises 4xx status errors unchanged with the comment "4xx is our bug (bad request, auth): surface it as a 500, not a degraded 503" (`CLAUDE.md`, Gotchas: "In both providers 4xx errors deliberately propagate (our bug, not an outage)"). Those SDK exceptions are not `LLMError`, no handler matches them, and Starlette's server-error middleware turns them into `500 Internal Server Error`. Mapping them here would also require `app/main.py` to import `anthropic`/`openai` exception types, breaking the rule that only `app/llm.py` imports provider SDKs (inferred).

### Module-level `app` and logging

```python
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)
```

```python
app = create_app()
```

`app = create_app()` at module bottom is the ASGI object uvicorn loads: `uvicorn app.main:app` in the README and `CLAUDE.md` run commands and in the `Dockerfile` `CMD`. Import-time side effects are limited to `logging.basicConfig(...)` and `Settings.from_env()`. `Settings.from_env` parses `RECENT_LIMIT` and `MEMORY_LIMIT` with `int()` and `MIN_CONFIDENCE` with `float()`, so a non-numeric value raises `ValueError` at import; `logging.basicConfig` raises `ValueError: Unknown level` for a `LOG_LEVEL` that is not a Python logging level name. `basicConfig` is a no-op if the root logger already has handlers (standard library behavior), so a host process that configured logging first keeps its configuration. `LOG_LEVEL` is read with `os.getenv` directly and is not part of `Settings`.

This module logs two events of its own: the startup warning when Neo4j is unreachable and the `ready brain=... llm=...` info line. The per-request event (`chat user=... session=... category=... context_used=... memory_updates=... degraded=...`) is emitted by `ChatService` in `app/chat.py`. Against the TDD §19 field list it carries `user_id`, `session_id`, `query_category`, `context_used` and `memory_updates`; omits `request_id`, `memories_retrieved`, `memories_selected`, `latency_ms`, `llm_provider` and `error_type`; and adds `degraded`, which §19 does not list. Prompts are not logged (README, Production path: "prompts are not logged").

### Sequence for a `POST /chat` request at this layer

1. Uvicorn/Starlette receives `POST /chat` with a JSON body.
2. FastAPI validates the body against `ChatRequest`. On failure it returns `422` with a `detail` list of Pydantic errors (each with `loc`, `msg`, `type`); the route body never runs and nothing downstream is touched.
3. The route calls `request.app.state.chat.chat(req.user_id, req.session_id, req.message)`, the `ChatService` instance the lifespan built. See [Orchestration](02-orchestration-and-short-term-context.md) for what happens inside.
4. On success the `ChatResult` becomes a `ChatResponse` and FastAPI serializes it as `200` JSON.
5. If `LLMError` escapes, `_llm_error` returns `503 {"detail": "LLM unavailable: ..."}`; per `ChatService`, the turn was not appended to the session and no memory was written.
6. Any other exception (a provider 4xx, a bug) is unhandled and becomes `500`.

TDD §12 numbers these as steps 1, 2, 13 and 14 of the end-to-end sequence; everything between is the orchestrator's.

## Contracts and invariants

| Route | Request | Success | Errors |
|---|---|---|---|
| `POST /chat` | JSON `ChatRequest`: `user_id` (1–128 chars), `session_id` (1–128 chars), `message` (1–4000 chars) | `200` JSON `ChatResponse`: `response`, `user_id`, `session_id`, `context_used`, `memory_updates`, `degraded` | `422` invalid body; `503` LLM unavailable; `500` anything else |
| `POST /users` | JSON `UserUpsert`: `user_id` (1–128 chars) plus at least one of `name`, `date_of_birth` (ISO date), `time_of_birth`, `birth_place`, `preferred_language` | `200` JSON `ProfileOut`: `user_id` and all six profile fields (`null` when unset), `sun_sign` derived | `422` invalid body or unparseable date; `422 {"detail": "no profile fields provided"}` when only `user_id` is sent; `503` Shared Brain unavailable |
| `GET /users/{user_id}/memories` | path `user_id` (any non-empty string) | `200` JSON array of `MemoryOut`, in creation order, `ACTIVE` and `SUPERSEDED` alike, `[]` for an unknown user | `503` Shared Brain unavailable |

Callers may rely on:

- `user_id` and `session_id` in a `/chat` response are the request values, verbatim.
- `context_used` is exactly what was injected into the prompt, in a fixed order: `recent_conversation`, `user_profile`, `astrology` (each present only if that source contributed), then one memory key per included memory in retrieval order (TDD §14; `CLAUDE.md`, Invariants: "Tests assert whole lists"). This layer passes the list through untouched; the definition is owned by [Query understanding and context selection](04-query-understanding-and-context-selection.md).
- `memory_updates` is the count returned by `memory.remember` ([Memory update](06-memory-update.md)): each profile field written counts one even when it restates the stored value (`changed += len(profile_fields)` is unconditional), while each Memory upsert counts one only when it returned `created` or `updated`, so re-stating an identical active memory adds zero. It is `0` on `follow_up` turns and whenever `degraded` is `true`.
- `degraded: true` on a `200` means a Shared Brain call failed during that request. If the read failed, the `response` was generated from recent turns only and no write was attempted (TDD §13.2). If only the write failed, the `response` was generated with full profile and memory context, any profile fields or memories `remember` had already written before the failure are persisted, and `memory_updates` reports `0`. A `503` body carries only `detail`, no `degraded` field.
- A `503` from `/chat` guarantees that nothing about the turn was recorded: no session append, no memory or profile write (TDD §13.3; `tests/test_chat.py::test_9_llm_failure_returns_503_and_mutates_nothing`).
- `422` means the request never reached the orchestrator or the brain.
- `sun_sign` in `ProfileOut` is recomputed whenever `date_of_birth` is set, by `sun_sign()` in `app/astrology.py` (a tropical sun-sign stub).
- A missing profile or an empty memory set is not an error; `/chat` returns `200` and the corresponding tags are absent from `context_used` (TDD §13.4, §13.5; `tests/test_chat.py::test_8_missing_profile_is_fine`).
- There is no authentication: the service trusts `user_id` as sent (TDD §20; README, Production path).
- Interactive documentation is served at `/docs` (README, Run it), generated from these schemas.

The layer's own invariants, enforced by review rather than a test: no route imports or calls `neo4j`, `anthropic` or `openai`; routes reach collaborators only through `request.app.state`; the module has exactly three routes and two handlers (TDD §4.1, README Architecture).

## Design decisions and alternatives rejected

| Decision | Chosen | Rejected | Why |
|---|---|---|---|
| Module layout | One `app/main.py` with factory, schemas, routes and handlers | `routes/` and `schemas/` packages (the v1 design) | TDD §27 (rows §4.1, §15): the 24-file tree was flattened to 11 modules because "most planned files would hold under 30 lines; one file per concern is easier to navigate and review". TDD §4.1: splitting "is warranted at roughly three times this endpoint count". |
| Dependency wiring | Factory arguments stored on `app.state` in the lifespan; routes read `request.app.state` | FastAPI `Depends` with `dependency_overrides`; module-level global brain/LLM | `CLAUDE.md`: "`create_app(brain=, llm=, settings=)` factory; tests inject fakes here." Inferred: one wiring point with three keyword arguments, no override registry to keep in sync, and a module-level `app` whose import has no network side effects (globals would connect at import; overrides would be a second injection mechanism). |
| When to construct the brain and LLM | Inside the lifespan | In the factory body | Inferred from the code: `ensure_schema()` is async and `create_app` is sync, so the bootstrap must live in the lifespan anyway; deferring construction also keeps `import app.main` free of connections and credentials, so `tests/conftest.py` can import the module without Neo4j or an API key (the import still runs `Settings.from_env()` and `logging.basicConfig`, so malformed `RECENT_LIMIT`, `MEMORY_LIMIT`, `MIN_CONFIDENCE` or `LOG_LEVEL` values fail it). |
| Neo4j unreachable at startup | Log a warning and serve degraded | Fail startup | Recorded in the warning text itself ("serving degraded until it returns") and consistent with TDD §13.2, which makes Neo4j unavailability a degraded mode, not an outage. The 3 s driver timeout (`Neo4jBrain.__init__`) bounds the startup delay. |
| LLM unavailable | `503`, nothing recorded | Deterministic fallback text; recording the user turn anyway | TDD §13.3: "there is no deterministic fallback text that is honest for an astrology question"; not appending the failed turn "keeps session history alternating" (TDD §27 row §13). "A production deployment could add a fallback model" (TDD §13.3). |
| Neo4j unavailable during `/chat` | `200` with `degraded: true`, handled in the orchestrator | `503` | TDD §13.2 and PRD FR-8 ("safe fallback behavior for downstream failures"). At this layer the `BrainUnavailable` handler is therefore a backstop for the two `/users` routes, which have no sensible degraded answer. |
| Provider 4xx errors | Propagate as `500` | Map to `503` alongside outages | Comment in `app/llm.py` `_guarded`: "4xx is our bug (bad request, auth): surface it as a 500, not a degraded 503"; `CLAUDE.md`, Gotchas. A `503` would tell operators to wait for a provider that is not actually down. |
| `date_of_birth` type | `date \| None` | `str \| None` | Inferred: the brain stores whatever string it is given when `parse_date` fails; parsing at the boundary turns garbage into a `422` (pinned by `tests/test_chat.py::test_invalid_payload_is_422`) instead of a profile with a bad date and no `sun_sign`. |
| Empty `/users` body | Hand-raised `422 "no profile fields provided"` | Accept as a no-op | Inferred: `UPSERT_PROFILE` in `app/brain.py` runs `MERGE` on both `User` and `Profile` regardless of `fields`, so a no-op request would still create empty nodes. |
| `sun_sign` on `UserUpsert` | Not accepted | Client-supplied | README, API: "`sun_sign` is derived"; PRD FR-2 lists it as a profile attribute and TDD §11 allows a stub that can "derive or accept a sun sign". The implementation derives only, in `app/astrology.py`. |
| Schema types | Pydantic for the five HTTP models; dataclasses for the domain types | Pydantic throughout, or dataclasses at the boundary | `CLAUDE.md`, Gotchas, states the rule as "everything else is a dataclass" apart from `MemoryCandidate`, which is Pydantic because it doubles as the LLM structured-output schema (the private `_Extraction` wrapper in `app/llm.py` is the other Pydantic model; `Category` is a `StrEnum`). The HTTP models need Pydantic for validation and OpenAPI. `vars()` bridges the two with no mapping code. |
| Debug endpoint scope | Returns every memory including `SUPERSEDED`, no filter | Active only, or paginated | TDD §4.4 and README, API: it exists "for inspection"; PRD §9 calls it "a debugging/inspection surface". |
| Auth and rate limits | None | Auth on `user_id`, rate limiting | TDD §20: "The assignment does not define authentication or authorization requirements"; TDD §23 Phase 2 and README Production path list both as hardening work. |

## Failure modes and degraded behavior

| Condition | Detected by | HTTP result | Side effects |
|---|---|---|---|
| Malformed JSON, missing field, or constraint violation (`ChatRequest`, `UserUpsert`) | FastAPI body validation | `422`, `detail` list | None; route body not executed (TDD §13.1; README, Failure modes) |
| `date_of_birth` that does not parse as a date | Pydantic `date` parsing | `422` | None |
| `POST /users` with only `user_id` | `upsert_user` | `422 {"detail": "no profile fields provided"}` | None |
| `LLMError` from `generate`: provider connection error, rate limit or 5xx; an empty completion; or, with `AnthropicLLM`, a refusal `stop_reason` | `_llm_error` handler | `503 {"detail": "LLM unavailable: ..."}` | Nothing recorded: no session append, no memory write (TDD §13.3). The `detail` says "unavailable" even when the cause was a refusal or an empty completion |
| LLM failure during extraction after a good reply | Caught in `ChatService` | `200`, `memory_updates: 0` | Reply returned; write skipped and logged (README, Failure modes) |
| Neo4j down during `/chat` read | Caught in `ChatService` | `200`, `degraded: true`, `context_used` without profile or memory tags | Memory write skipped; the request costs about 3 s (`Neo4jBrain` timeout; TDD §13.2). `follow_up` turns never call the brain and are unaffected |
| Neo4j down during `/chat` write only | Caught in `ChatService` | `200`, `degraded: true`, `memory_updates: 0` | Reply (built with full context) returned and session appended; the write is partial: the profile upsert and any Memory upserts `remember` completed before the failure are persisted, the rest are logged and skipped |
| Neo4j down during `POST /users` or `GET .../memories` | `_brain_error` handler | `503 {"detail": "Shared Brain unavailable: ..."}` | None |
| Provider 4xx (bad API key, malformed request, unknown model) during `generate` | Unhandled | `500` | Nothing recorded; logged by the server as an unhandled exception; deliberate (see above) |
| Provider 4xx during `extract_memories` | Unhandled (`ChatService` catches only `LLMError` and `BrainUnavailable` there) | `500` | The turn was already appended to the session and the generated reply never reaches the client; no memory write |
| Neo4j unreachable or credentials rejected at startup | Lifespan `try/except BrainUnavailable` | Process starts; each non-`follow_up` `/chat` is degraded and each `/users` call is `503` until Neo4j is reachable | Warning logged; `ensure_schema` is not retried in that process |
| Unknown `LLM_PROVIDER`, or `openai` without `LLM_MODEL` | `build_llm` raises `ValueError` in the lifespan | Startup fails; no requests served | The brain was already constructed and `ensure_schema` already ran; the post-`yield` `close()` never runs, so the driver is not closed (`CLAUDE.md`, Gotchas; consequence inferred) |
| Non-numeric `RECENT_LIMIT`, `MEMORY_LIMIT` or `MIN_CONFIDENCE`; unknown `LOG_LEVEL` | `Settings.from_env` / `logging.basicConfig` at import | Import of `app.main` fails | None |
| Missing profile, empty memory, no relevant memory | Not a failure | `200`; tags absent from `context_used` | TDD §13.4, §13.5 |

Shutdown: the lifespan awaits `Neo4jBrain.close()` so the driver's connection pool is released; with `InMemoryBrain` there is nothing to close. The `BRAIN=memory` demo mode (`CLAUDE.md`, Commands) loses all data at shutdown by design.

## Configuration

Values reach this layer through `Settings.from_env()` in `app/config.py` (read once when `create_app()` runs, i.e. at import of `app.main`) except `LOG_LEVEL`, which `app/main.py` reads directly. Defaults are those in the `Settings` dataclass and `.env.example`.

| Variable | `Settings` field | Default | Effect in this layer |
|---|---|---|---|
| `LOG_LEVEL` | none (`os.getenv` in `app/main.py`) | `INFO` | Root logger level for `logging.basicConfig`; must be a Python level name |
| `BRAIN` | `brain` | `neo4j` | `memory` selects `InMemoryBrain` in the lifespan; any other value selects `Neo4jBrain` (the value is not otherwise validated) |
| `NEO4J_URI` | `neo4j_uri` | `bolt://localhost:7687` | Passed to `Neo4jBrain(...)` |
| `NEO4J_USER` | `neo4j_user` | `neo4j` | Passed to `Neo4jBrain(...)` |
| `NEO4J_PASSWORD` | `neo4j_password` | `password` | Passed to `Neo4jBrain(...)` |
| `LLM_PROVIDER` | `llm_provider` | `anthropic` | Consumed by `build_llm(settings)` in the lifespan: `anthropic`, `openai` or `mock` |
| `LLM_MODEL` | `llm_model` | empty | Consumed by `build_llm`; required for `openai`, `anthropic` falls back to `claude-opus-5` |
| `LLM_EFFORT` | `llm_effort` | `medium` | Consumed by `build_llm` |
| `OPENAI_BASE_URL` / `OPENAI_API_KEY` | `openai_base_url` / `openai_api_key` | empty | Consumed by `build_llm` for the `openai` provider |
| `ANTHROPIC_API_KEY` | none | unset | Read by the Anthropic SDK when `AnthropicLLM` is constructed in the lifespan; never read by `app/main.py` |
| `RECENT_LIMIT` | `recent_limit` | `10` | `SessionStore(settings.recent_limit)` deque cap, constructed in the lifespan |
| `MEMORY_LIMIT` | `memory_limit` | `8` | Passed to `ChatService` as the retrieval `LIMIT` |
| `MIN_CONFIDENCE` | `min_confidence` | `0.6` | Passed to `ChatService` as the extraction threshold |

When `brain` or `llm` is injected into `create_app`, the corresponding `BRAIN`, `NEO4J_*`, `LLM_*` and `OPENAI_*` values are not consulted; `RECENT_LIMIT`, `MEMORY_LIMIT` and `MIN_CONFIDENCE` still apply. `tests/conftest.py` passes `Settings(brain="memory", llm_provider="mock")` explicitly so the fixture's app is independent of the developer's environment; the import of `app.main` still evaluates `Settings.from_env()` and `LOG_LEVEL`, so those must at least parse. Deployment wiring (Dockerfile `CMD`, compose environment) is covered in [Configuration and deployment](08-configuration-and-deployment.md).

## Tests that pin this layer

All HTTP-level tests live in `tests/test_chat.py` and run through the fixture in `tests/conftest.py`:

```python
@pytest.fixture
def client(brain, llm):
    app = create_app(brain=brain, llm=llm, settings=Settings(brain="memory", llm_provider="mock"))
    with TestClient(app) as c:
        yield c
```

The `with TestClient(app)` form matters: Starlette runs the lifespan only inside the context manager, and `app.state.chat` does not exist until it has run. Tests that also need the fakes take the `brain` and `llm` fixtures directly (the same objects the app holds) to assert on graph state and captured prompts.

| Test | What it pins in this layer |
|---|---|
| `tests/conftest.py::client` | `create_app` accepts injected `InMemoryBrain`, `MockLLM` and `Settings`, and the lifespan wires them without touching Neo4j or a provider |
| `tests/test_chat.py::chat` (helper) | Every scenario posts to `/chat` and asserts `200` |
| `tests/test_chat.py::test_1_new_user_succeeds_with_no_context` | `ChatResponse` echoes `user_id` and `session_id`; `context_used == []` and `degraded is False` for a new user |
| `tests/test_chat.py::test_2_first_message_creates_durable_memory_and_profile` | `memory_updates` reports the count of writes (`4`) for the PRD intro sentence |
| `tests/test_chat.py::test_4_follow_up_uses_recent_context_only` | `memory_updates == 0` and `context_used == ["recent_conversation"]` on a follow-up |
| `tests/test_chat.py::test_5_memory_persists_across_sessions` | `GET /users/u1/memories` returns `MemoryOut` objects with `key` and `status` (the only test that calls this route) |
| `tests/test_chat.py::test_8_missing_profile_is_fine` | Missing profile is `200`, not an error |
| `tests/test_chat.py::test_9_llm_failure_returns_503_and_mutates_nothing` | `LLMError` → `503` with `"LLM"` in `detail`; no memory, no profile, no session append |
| `tests/test_chat.py::test_10_graph_failure_degrades` | `BrainUnavailable` inside `/chat` → `200` with `degraded: true` and `memory_updates == 0`, never `503` |
| `tests/test_chat.py::test_invalid_payload_is_422` | Missing `session_id` → `422`; empty `message` → `422`; `date_of_birth: "not-a-date"` → `422` |
| `tests/test_chat.py::test_profile_endpoint_feeds_astrology_context` | `POST /users` returns `200` with derived `sun_sign == "Leo"`; the stored profile then appears in `/chat` as `["user_profile", "astrology"]` |

Not covered by any test: the `422 "no profile fields provided"` branch, the `BrainUnavailable` → `503` handler (no test sets `brain.fail = True` and then calls a `/users` route), the degraded-startup branch of the lifespan, the shutdown `close()` call, and the fact that `GET /users/{user_id}/memories` returns `SUPERSEDED` rows over HTTP (`test_7_user_correction_supersedes` checks that through `brain.list_memories` directly). `tests/test_units.py::test_neo4j_connection_failure_is_brain_unavailable` and `tests/test_openai_llm.py::test_build_llm_selects_provider` pin the neighbours this layer relies on (`BrainUnavailable` translation and `build_llm`'s `ValueError`s) but do not go through `app/main.py`. See [Testing and verification](09-testing-and-verification.md).

## Known limits and future work

`ponytail:` markers in `app/main.py`: none. (`grep -rn "ponytail:" app/` finds them only in `app/astrology.py`, `app/session.py`, `app/brain.py` and `app/context.py`.) Two of those shape this layer's behavior from below: `app/session.py` ("process-local dict; swap for Redis when running more than one replica") means the `SessionStore` built in the lifespan is per process, so a multi-replica deployment loses short-term context across replicas; and `app/brain.py` ("fixed timeout, no circuit breaker; add one when outages are long enough to matter per request") is why every non-`follow_up` `/chat` during a Neo4j outage costs about 3 s (follow-ups never call the brain).

Recorded future work touching this layer:

- **Authentication and authorization on `user_id`, and rate limits.** README, Production path ("authn/authz on `user_id`; rate limits"); TDD §20 ("Validate `user_id` and enforce authorization in a real deployment"); TDD §23 Phase 2. Today any caller can read or write any user's profile and memories.
- **Access control and redaction before exposing the debug endpoint.** README, Production path, Privacy: "add redaction and per-user access control before exposing the debug endpoint". `GET /users/{user_id}/memories` returns raw memory values.
- **Tracing and request identifiers.** TDD §19 suggests `request_id`, `latency_ms` and `error_type`; README, Production path: "add tracing". This layer adds no request id or timing to the log line.
- **LLM fallback provider.** TDD §13.3 and §23 Phase 2. Would change the `503` path.
- **Background memory extraction.** TDD §12 and README, Trade-offs ("a queue is a one-line move of the last block in `chat.py`"). Would make `memory_updates` in the synchronous response meaningless or eventually consistent.

Limits observable in the code with no recorded plan (all inferred):

- If Neo4j is unreachable at startup, `ensure_schema()` is never retried; constraints and the `memory_lookup` index are created only on the next restart with Neo4j up.
- `BRAIN` is not validated: any value other than `memory` means Neo4j, so a typo such as `BRAIN=inmemory` silently tries to connect to Neo4j.
- The path parameter of `GET /users/{user_id}/memories` has no length bound, unlike the `1..128` bound on `user_id` in both request bodies.
- `POST /users` cannot clear a field to `null` because of `exclude_none=True`.
- A whitespace-only `message` passes `min_length=1`.
- The LLM client objects are not closed at shutdown; only the brain's `close()` is awaited.
- `ProfileOut(**vars(profile))` and `MemoryOut(**vars(m))` depend on the schemas matching the `UserProfile` and `Memory` dataclasses. The check is one-sided and happens at request time, not import: a required field added to a schema and missing from the dataclass raises; a field added to a dataclass and not to the schema is silently dropped from the response (Pydantic default `extra="ignore"`).
