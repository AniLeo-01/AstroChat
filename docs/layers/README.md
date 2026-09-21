# AstroChat layers: overview

AstroChat is a Python 3.12 FastAPI service that answers astrology chat messages using two kinds of memory: the last ten messages of the current session (short-term, held in the process) and a persistent, graph-shaped Shared Brain in Neo4j (long-term, keyed by user) that holds one Profile node and versioned Memory nodes per user. Every `POST /chat` runs one fixed sequence, Chat → Context Selection → Shared Brain → LLM → Response → Memory Update, implemented as ten small modules in `app/` with a strict one-way dependency order. The LLM sits behind an adapter (`anthropic`, any OpenAI-compatible server, or a deterministic mock), the graph sits behind a protocol with two implementations (Neo4j and an in-memory reference), and astrology itself is a sun-sign stub because the product under test is the memory pipeline, not astrology (CLAUDE.md, "What this is"; README, opening paragraphs). This document is the map of the whole system; the nine sibling documents each cover one layer in the same what / why / how format and are listed at the end.

**Files:** `app/__init__.py`, `app/config.py`, `app/models.py`, `app/session.py`, `app/astrology.py`, `app/brain.py`, `app/context.py`, `app/llm.py`, `app/memory.py`, `app/chat.py`, `app/main.py`

**Read next:** [API layer](01-api-layer.md), [Orchestration and short-term context](02-orchestration-and-short-term-context.md), [Domain model](03-domain-model.md), [Query understanding and context selection](04-query-understanding-and-context-selection.md), [Shared Brain](05-shared-brain.md), [Memory update](06-memory-update.md), [LLM providers](07-llm-providers.md), [Configuration and deployment](08-configuration-and-deployment.md), [Testing and verification](09-testing-and-verification.md)

## What this layer is

"This layer" is the whole service: one FastAPI application (`app/main.py`) exposing three routes, one orchestrator class (`ChatService` in `app/chat.py`), and the modules the orchestrator calls. There is no package hierarchy: every concern is one file directly under `app/`, and the files import each other in a single direction. CLAUDE.md states the order as `config`, `models` → `session`, `astrology`, `brain` → `context` → `llm` → `memory` → `chat` → `main`; the imports in the code are exactly these:

| Module | Imports from `app/` | Imported by | Responsibility |
|---|---|---|---|
| `app/config.py` | nothing | `llm`, `main` | `Settings` frozen dataclass; `Settings.from_env()` reads every environment variable the app understands except `LOG_LEVEL` and `ANTHROPIC_API_KEY` |
| `app/models.py` | nothing | `session`, `brain`, `context`, `llm`, `memory`, `chat` | `Category` taxonomy, `MEMORY_CATEGORIES`, `MEMORY_TYPES`, `PROFILE_FIELDS`, `PROFILE_KEYS`; dataclasses `ChatMessage`, `UserProfile`, `Memory`, `LLMRequest`; Pydantic `MemoryCandidate` |
| `app/session.py` | `models` | `chat`, `main` | `SessionStore`: one `deque(maxlen=limit)` of `ChatMessage` per `(user_id, session_id)` |
| `app/astrology.py` | nothing | `brain`, `llm` | `sun_sign(date)` and `parse_date(text)` |
| `app/brain.py` | `astrology`, `models` | `memory`, `chat`, `main` | `SharedBrain` protocol, `BrainUnavailable`, `InMemoryBrain`, `Neo4jBrain` and all Cypher; the only module importing the `neo4j` driver |
| `app/context.py` | `models` | `llm`, `chat` | `SYSTEM_PROMPT`, `classify()`, `select_context()`, the profile-field table, `context_used` construction |
| `app/llm.py` | `astrology`, `config`, `context`, `models` | `chat`, `main` | `LLMProvider` protocol, `LLMError`, `_guarded()`, `AnthropicLLM`, `OpenAICompatibleLLM`, `MockLLM`, `extract_by_rules()`, `build_llm()`; the only module importing `anthropic` or `openai` |
| `app/memory.py` | `brain`, `models` | `chat` | `validate()` and `remember()`: candidate filtering, profile routing, upserts |
| `app/chat.py` | `brain`, `context`, `llm`, `memory`, `models`, `session` | `main` | `ChatService.chat()` and `ChatResult` |
| `app/main.py` | `brain`, `chat`, `config`, `llm`, `session` | nothing (entry point) | `create_app()` factory, lifespan wiring, request/response schemas, three routes, two exception handlers |

Drawn as layers, where each module depends only on modules above it:

```text
layer 0   config.py    models.py    astrology.py    (no intra-app imports)
layer 1   session.py   brain.py                     (session: models; brain: astrology, models)
layer 2   context.py                                (models)
layer 3   llm.py                                    (astrology, config, context, models)
layer 4   memory.py                                 (brain, models)
layer 5   chat.py                                   (brain, context, llm, memory, models, session)
layer 6   main.py                                   (brain, chat, config, llm, session)
```

CLAUDE.md groups `astrology` with `session` and `brain` in its ordering; by imports it depends on nothing in `app/` and sits at layer 0 with `config` and `models`.

Three boundaries matter more than the rest. `app/brain.py` is the only module that knows Neo4j exists and translates every driver connectivity error into `BrainUnavailable`. `app/llm.py` is the only module that knows a provider SDK exists and translates every outage into `LLMError`. `app/chat.py` therefore catches exactly two exception types, and `app/main.py` registers exactly two exception handlers for the same two types (CLAUDE.md, "Layout"; TDD §4.4 and §10).

The runtime shape is a single process: uvicorn serving the FastAPI app, an in-process session dictionary, an async Neo4j driver connection (or an in-process dictionary when `BRAIN=memory`), and outbound HTTP(S) to an LLM provider (or none when `LLM_PROVIDER=mock`). The `docker-compose.yml` file runs exactly two containers, `neo4j` (image `neo4j:5`, with a `cypher-shell` health check) and `app` (built from the `Dockerfile`, which installs with `uv sync --frozen --no-dev` and runs `uvicorn app.main:app` on port 8000).

## Why it exists

The PRD's core requirement is one flow, quoted as the assignment's explicit priority: "Chat → Context Selection → Shared Brain → LLM → Response → Memory Update" (PRD §1). The PRD frames the problem as a generic chatbot treating each message too independently, and requires the system to solve "two distinct context problems" (PRD §2):

1. **Short-term context**: understand follow-up questions using recent messages in the active conversation.
2. **Long-term memory**: preserve useful information across sessions without storing every message as permanent memory.

The TDD turns these into its first two design principles: "Separate short-term and long-term memory" and "Retrieve before generating. Never send the whole graph or entire conversation history to the LLM" (TDD §2). Everything in `app/` exists to serve those two problems and the guardrails around them: user statements are the only source of memory (TDD §2 principle 3), the provider is replaceable (principle 4), every context source is bounded (principle 5), one dependency failure does not fail the request (principle 6), and `context_used` exposes what was injected without leaking the prompt (principle 7).

### The two-memory model

The two memories are different data structures, in different modules, with different lifetimes, keys and write rules. `ChatService.chat` in `app/chat.py` is the only place that reads both, and `select_context` in `app/context.py` is the only place that merges them into one prompt.

| | Short-term: session history | Long-term: Shared Brain |
|---|---|---|
| Module and type | `SessionStore` in `app/session.py`: `dict[(user_id, session_id), deque[ChatMessage]]` | `SharedBrain` protocol in `app/brain.py`, implemented by `Neo4jBrain` (runtime) and `InMemoryBrain` (tests, `BRAIN=memory`) |
| Key | `(user_id, session_id)`; a new session starts empty | `user_id`; visible from every session of that user |
| Contents | Raw `ChatMessage` turns, both `user` and `assistant`, in order | One `Profile` node (`name`, `date_of_birth`, `time_of_birth`, `birth_place`, `preferred_language`, derived `sun_sign`) and `Memory` nodes identified by `(user_id, category, key)` with `status` `ACTIVE` or `SUPERSEDED` |
| Bound | `deque(maxlen=limit)`, default 10 (`RECENT_LIMIT`), enforced at write time | `search_memories(..., limit)`, default 8 (`MEMORY_LIMIT`), enforced by the retrieval query |
| Persistence | Process memory; lost on restart; not shared across replicas (`ponytail:` note in `app/session.py`) | Neo4j (constraints and index created by `Neo4jBrain.ensure_schema`) or a dictionary for the in-memory brain |
| Written when | Only after `generate` succeeds: `SessionStore.append(user_id, session_id, current, ChatMessage("assistant", reply))` | From chat, only from the user's message, after `validate`, through `remember` in `app/memory.py`; never from assistant text; skipped on `follow_up` turns and when the brain is already degraded. The Profile node is also written directly by `POST /users` (`upsert_user` in `app/main.py`), which bypasses `validate` and records no `source_message_id` |
| Read when | Every request (`SessionStore.recent`) | Every request except `follow_up` (`get_profile`, then `search_memories`) |
| Where it lands in the prompt | `LLMRequest.messages` (recent turns followed by the current message) | `LLMRequest.system_prompt`, rendered as "User profile:" and "What the user has told you before" blocks |
| `context_used` tag | `recent_conversation` | `user_profile`, `astrology`, then one memory key per memory |
| Correction semantics | None; turns are append-only and age out | Supersede: old memory becomes `SUPERSEDED`, new `ACTIVE`, `(new)-[:SUPERSEDES]->(old)` |

The bridge between them is the `follow_up` category: a follow-up ("Why do you say that?") is answered from the session history alone, with no graph read and no extraction, because the PRD says short-term context should dominate for such turns (PRD §8; TDD §4.6 table row `follow_up`). A new-session question ("What do you remember about my career goals?") has no history and is answered from the graph alone (PRD §5.4). Scenario 3 versus scenario 1 in `tests/test_chat.py` is the README's "with vs without Shared Brain" comparison (README, Tests).

## How it works

### Startup

`create_app(brain=None, llm=None, settings=None)` in `app/main.py` is the factory; the module ends with `app = create_app()` for uvicorn. `settings` defaults to `Settings.from_env()` (`app/config.py`). Construction of the dependencies happens inside the `lifespan` context manager, so nothing connects to Neo4j or a provider at import time:

1. `app.state.brain` is the injected brain, else `InMemoryBrain()` when `settings.brain == "memory"`, else `Neo4jBrain(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)` with the constructor's default 3.0 second timeout.
2. For a `Neo4jBrain`, `ensure_schema()` runs the three statements in `SCHEMA` (unique `User.id`, unique `Memory.id`, composite index `memory_lookup` on `(category, key, status)`). A `BrainUnavailable` here is logged as a warning ("serving degraded until it returns") and startup continues.
3. `app.state.llm` is the injected provider, else `build_llm(settings)` (`app/llm.py`), which raises `ValueError` for an unknown `LLM_PROVIDER` or for `openai` without `LLM_MODEL`; that error occurs during lifespan startup.
4. `app.state.chat = ChatService(brain, llm, SessionStore(settings.recent_limit), settings.memory_limit, settings.min_confidence)`.
5. On shutdown, `close()` is awaited if the brain has one (`Neo4jBrain.close`).

Logging is configured at import of `app/main.py` by `logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), ...)`. Tests build the same app through the same factory with fakes injected (`tests/conftest.py`).

### Request lifecycle for one `POST /chat`

Each step names the module and the function that performs it. Steps 1 to 8 produce the response; steps 9 to 12 are the post-response path; step 13 returns.

1. **Validate the payload** (`app/main.py`, `ChatRequest`). FastAPI parses the JSON body into `ChatRequest`: `user_id` and `session_id` of 1 to 128 characters, `message` of 1 to 4000 characters. A violation returns `422` from FastAPI's own validation handler and nothing below runs.
2. **Delegate** (`app/main.py`, route function `chat` inside `create_app`). The route calls `request.app.state.chat.chat(req.user_id, req.session_id, req.message)` and does nothing else; TDD §4.1 forbids Neo4j or LLM calls in route handlers.
3. **Load short-term context** (`app/chat.py`, `ChatService.chat` → `app/session.py`, `SessionStore.recent`). Returns the deque for `(user_id, session_id)` as a list of at most `recent_limit` messages; an unknown session returns `[]` and no entry is created (`dict.get(..., ())`).
4. **Classify the message** (`app/context.py`, `classify`). The `_KEYWORDS` patterns are tried in the order astrology, language, profile, career, relationships, finance, health, interests; the first match wins. With no keyword match, a leading follow-up phrase (`_FOLLOW_UP`: "why", "tell me more", "what about that", ...) or a message of at most six words containing a bare pronoun (`_PRONOUN`) yields `Category.FOLLOW_UP`; everything else is `Category.GENERAL`.
5. **Read the Shared Brain** (`app/chat.py` → `app/brain.py`). Skipped entirely when the category is `FOLLOW_UP`. Otherwise `get_profile(user_id)` and then `search_memories(user_id, category, memory_limit)`, where the category argument is `None` for `GENERAL` so that all categories are retrieved. `Neo4jBrain.search_memories` runs `SEARCH_MEMORIES` (`m.status = 'ACTIVE'`, optional category equality, `ORDER BY m.confidence DESC, m.updated_at DESC LIMIT $limit`); `InMemoryBrain.search_memories` sorts and slices the same way in Python. A `BrainUnavailable` from either call is logged as a warning, sets `degraded = True`, and the request continues with whatever was read before the failure (`profile` stays `None` if `get_profile` failed; `memories` stays `[]`).
6. **Mint the message id** (`app/chat.py`). `current = ChatMessage("user", message, id=str(uuid.uuid4()))`. This id becomes `source_message_id` on every memory created from this turn, which is the provenance the PRD asks for (PRD FR-7).
7. **Select context and build the prompt** (`app/context.py`, `select_context`). The profile fields to include come from `_PROFILE_FIELDS_FOR` (all six for `profile` and `astrology`; `name` and `preferred_language` for `language`; none for `follow_up`; `name` and `sun_sign` for every other category). The function assembles `context_used`, renders the profile block and the memory block (`_render` prints `- [category] key = value (timeframe: ...)`), and returns `Selection(LLMRequest(system, [*recent, current]), used)`. When there is nothing to render, the Context section reads "not needed for this turn; answer from the conversation above." for a follow-up and "none yet; this may be a new user." otherwise. The system prompt is always `SYSTEM_PROMPT` followed by `Context:` and that rendering.
8. **Generate** (`app/llm.py`, `generate` on the configured provider). `AnthropicLLM.generate` calls `messages.create` with `max_tokens=16000` and `output_config={"effort": ...}`, raises `LLMError` on `stop_reason == "refusal"` or empty text; `OpenAICompatibleLLM.generate` sends the system prompt as a `system` message followed by the turns and raises `LLMError` on empty content; `MockLLM.generate` records the request in `.requests` and echoes the Context block. `_guarded` maps `APIConnectionError`, `RateLimitError` and `APIStatusError` with status 500 or above to `LLMError`; 4xx errors propagate unchanged. `ChatService.chat` does not catch `LLMError` here; it reaches the `_llm_error` handler in `app/main.py`, which returns `503`, and steps 9 to 13 never run.
9. **Append both turns to the session** (`app/session.py`, `SessionStore.append`). `current` and `ChatMessage("assistant", reply)` are appended together; the deque evicts the oldest messages beyond the cap.
10. **Extract memory candidates** (`app/llm.py`, `extract_memories(message, date.today())`). Runs only when `not degraded and category is not Category.FOLLOW_UP`. The input is the user's message alone, never the reply or the history. `AnthropicLLM.extract_memories` uses `messages.parse(..., output_format=_Extraction)`; `OpenAICompatibleLLM.extract_memories` requests `response_format={"type": "json_object"}` with `_JSON_SHAPE` appended to the prompt, strips code fences with `_FENCE`, and validates with `_Extraction.model_validate_json` (a `ValidationError` becomes `LLMError`); `MockLLM.extract_memories` calls `extract_by_rules`, a regex extractor covering the PRD example sentences.
11. **Validate and persist** (`app/memory.py`, `remember` → `validate`, then `app/brain.py`). `validate` lower-cases `key`, `category` and `type`, strips `value`, and drops candidates with `confidence < min_confidence`, an empty key or value, a category outside `MEMORY_CATEGORIES`, or a type outside `MEMORY_TYPES`. `remember` collects candidates whose key is in `PROFILE_KEYS` into one `brain.upsert_profile(user_id, fields)` call (`_with_sun_sign` normalizes `date_of_birth` to ISO and recomputes `sun_sign` when the date parses) and sends every other candidate to `brain.upsert_memory(user_id, cand, source_message_id)`, which returns `"created"`, `"updated"` or `"unchanged"` after finding the `ACTIVE` memory with the same `(user_id, category, key)`: none → create; same value → refresh `updated_at` and keep the higher confidence; different value → mark the old one `SUPERSEDED`, create the new `ACTIVE` one, link `(new)-[:SUPERSEDES]->(old)`. `Neo4jBrain.upsert_memory` does this inside one `execute_write` transaction (`FIND_ACTIVE`, `TOUCH`, `SUPERSEDE`, `CREATE_MEMORY`). `remember` returns the number of profile fields written plus the number of upserts that were not `"unchanged"`.
12. **Absorb update failures** (`app/chat.py`). The whole update block is wrapped so that the already-generated reply is still returned:

    ```python
            updates = 0
            if not degraded and category is not Category.FOLLOW_UP:
                try:
                    candidates = await self.llm.extract_memories(message, date.today())
                    updates = await remember(self.brain, user_id, candidates, current.id, self.min_confidence)
                except LLMError as e:
                    log.warning("memory extraction failed, response still returned: %s", e)
                except BrainUnavailable as e:
                    log.warning("memory write failed, response still returned: %s", e)
                    degraded = True
    ```

    An `LLMError` from extraction leaves `memory_updates` at 0 and `degraded` unchanged. A `BrainUnavailable` during the write sets `degraded = True` and leaves `memory_updates` at 0, even if `upsert_profile` had already succeeded before a later `upsert_memory` failed.
13. **Log and return** (`app/chat.py` → `app/main.py`). One `log.info` line carries `user`, `session`, `category`, `context_used`, `memory_updates` and `degraded`. `ChatResult(reply, selection.context_used, updates, degraded)` is copied into `ChatResponse` (`response`, `user_id`, `session_id`, `context_used`, `memory_updates`, `degraded`) and returned with status `200`.

This is TDD §12's fourteen-step sequence with the "PromptBuilder" folded into `select_context` and the extractor folded into the provider; the README's "Request sequence" paragraph under Architecture is the same list in one sentence.

### The other two routes

Both are optional in the PRD (PRD §9) and exist for profile setup and inspection; neither touches the session store or the LLM.

- `POST /users` (`app/main.py`, `upsert_user`): validates `UserUpsert` (`user_id` required; optional `name`, `date_of_birth` as a Pydantic `date`, `time_of_birth`, `birth_place`, `preferred_language`), converts the date to ISO text, returns `422` with "no profile fields provided" when every optional field is absent, and calls `brain.upsert_profile`. The returned `ProfileOut` carries the derived `sun_sign`.
- `GET /users/{user_id}/memories` (`app/main.py`, `list_memories`): calls `brain.list_memories` and returns every memory including `SUPERSEDED` ones (`LIST_MEMORIES` orders by `created_at`).
- A `BrainUnavailable` raised by either route reaches the `_brain_error` handler and becomes `503` with "Shared Brain unavailable". `/chat` never reaches that handler because `ChatService.chat` catches `BrainUnavailable` itself.

## Contracts and invariants

These hold across module boundaries; the tests that pin each are named in [Testing and verification](09-testing-and-verification.md) and summarized below.

- **Logical memory identity is `(user_id, category, key)`, and at most one `ACTIVE` memory exists per key.** Enforced by `upsert_memory` in both brains (`FIND_ACTIVE` in Cypher; a `next(...)` scan in `InMemoryBrain`), which supersedes before it creates. The `memory_lookup` index on `(category, key, status)` serves that lookup. There is no database constraint for it; TDD §22 assigns this to application logic.
- **Corrections supersede, never overwrite.** The old node keeps its value and `source_message_id`, gets `status = SUPERSEDED` and a fresh `updated_at`; the new node is `ACTIVE` and linked `(new)-[:SUPERSEDES]->(old)`. A repeat of the same value touches `updated_at` and raises `confidence` to the maximum of old and new, returning `"unchanged"` (README, Memory strategy; TDD Appendix C).
- **Only the user's message is a memory source in chat.** `extract_memories` receives the request's `message` string; the assistant reply is only ever appended to the session. Extraction is skipped on `follow_up` turns and when the pre-response read already failed (TDD §2 principle 3; TDD §13.2; CLAUDE.md, Invariants). The one other write path is `POST /users`, which sets Profile fields directly and creates no Memory nodes.
- **Profile facts stated in chat go to the Profile node.** `PROFILE_KEYS` in `app/models.py` maps exactly `profile.name`, `profile.date_of_birth`, `profile.time_of_birth`, `profile.birth_place` to Profile fields; `remember` routes those and only those, by key alone (the candidate's `category` is not checked; TDD §7.0 words the rule as `category = profile` plus a key in the set, see Specification drift below); `_with_sun_sign` recomputes `sun_sign` whenever a parseable `date_of_birth` is written. Any other key, including `language.preferred`, is a Memory, which is what makes the English-then-Hindi correction demonstrable (TDD §7.0).
- **`context_used` is exactly the three source tags followed by memory keys in retrieval order.** From `select_context`:

  ```python
      used: list[str] = []
      if recent:
          used.append("recent_conversation")
      if any(k != "sun_sign" for k in facts):
          used.append("user_profile")
      if "sun_sign" in facts:
          used.append("astrology")
      used += [m.key for m in memories]
  ```

  `user_profile` therefore means at least one selected profile field other than `sun_sign` was rendered; `astrology` means `sun_sign` was rendered; superseded memories never appear because the retrieval query filters on `ACTIVE`. Tests assert whole lists, for example `["recent_conversation"]` for a follow-up and `["user_profile", "astrology"]` for an astrology question by a user with a profile and no memories (TDD §14; CLAUDE.md, Invariants).
- **Exception boundary.** `app/brain.py` converts `_CONNECTIVITY_ERRORS = (ServiceUnavailable, SessionExpired, AuthError, OSError)` to `BrainUnavailable`; `app/llm.py` converts connection, rate-limit and 5xx errors to `LLMError` in `_guarded`. `ChatService` catches exactly those two types; `app/main.py` handles exactly those two types; neither imports a driver or SDK exception (CLAUDE.md, "Layout"; TDD §4.4, §10).
- **A failed generation leaves no trace.** `LLMError` from `generate` propagates before `SessionStore.append` and before extraction, so the session history stays strictly alternating user/assistant and the graph is untouched (TDD §13.3 and §27 row §13).
- **Follow-ups read nothing durable and write nothing durable.** No `get_profile`, no `search_memories`, no profile fields, no extraction; `context_used` is `["recent_conversation"]` when history exists and `[]` on a fresh session.
- **One taxonomy for queries and memories.** `Category` in `app/models.py` is a `StrEnum` of ten values used both by `classify` and as `Memory.category`; `MEMORY_CATEGORIES` is the same set minus `follow_up`, so `general` is a valid memory category and retrieval is an equality match (TDD §5.1; `models.Category` docstring).
- **Both brains implement identical semantics.** `InMemoryBrain` is documented in the module docstring of `app/brain.py` as "the readable reference for the semantics"; `tests/test_units.py` pins the outcomes against the in-memory brain and `tests/test_neo4j.py` repeats them against a live database.
- **Settings are frozen and injectable.** `Settings` is a `frozen=True` dataclass; tests pass `Settings(brain="memory", llm_provider="mock")` into `create_app` instead of setting environment variables.

### Context budgets

| Budget | Default | Enforced in | Configured by |
|---|---|---|---|
| Recent messages kept per `(user_id, session_id)` | 10 | `SessionStore.__init__`: `deque(maxlen=limit)`, so the cap applies at write time | `RECENT_LIMIT` → `Settings.recent_limit` → `SessionStore(settings.recent_limit)` in `app/main.py` |
| Messages sent to the LLM | at most 10 recent + 1 current | `select_context`: `LLMRequest(system, [*recent, current])` | derived from the row above |
| Memories retrieved per request | 8 | `search_memories(user_id, category, limit)`: `LIMIT $limit` in `SEARCH_MEMORIES`, `rows[:limit]` in `InMemoryBrain` | `MEMORY_LIMIT` → `Settings.memory_limit` → `ChatService.memory_limit` |
| Profile fields rendered | `profile`, `astrology`: all 6; `language`: `name`, `preferred_language`; `follow_up`: none; every other category: `name`, `sun_sign` | `_PROFILE_FIELDS_FOR` and `_DEFAULT_PROFILE_FIELDS` in `app/context.py` | not configurable (code table, TDD §4.6) |
| Extraction confidence floor | 0.6 | `validate` in `app/memory.py`: `c.confidence < min_confidence` drops the candidate | `MIN_CONFIDENCE` → `Settings.min_confidence` → `ChatService.min_confidence` → `remember` |
| Message length | 1 to 4000 characters | `ChatRequest.message` in `app/main.py` | not configurable |
| `user_id`, `session_id` length | 1 to 128 characters | `ChatRequest`, `UserUpsert` in `app/main.py` | not configurable |
| Generation and extraction output | `max_tokens=16000` | `AnthropicLLM` only; `OpenAICompatibleLLM` sets no token limit | not configurable |
| Wait on an unreachable Neo4j | 3.0 s connection timeout and 3.0 s transaction-retry window | `Neo4jBrain.__init__` (`connection_timeout`, `max_transaction_retry_time`) | constructor argument only; `app/main.py` uses the default |

## Design decisions and alternatives rejected

System-level decisions only; each layer document has its own table. "TDD §27" refers to the Revision Notes table, which records every deviation from the v1 design and its reason; "README, Trade-offs" is the table of the same name.

| Decision | Chosen | Rejected | Why, and source |
|---|---|---|---|
| Module layout | One module per concern, all directly in `app/`, strict dependency order | The v1 tree of 24 files across 11 concerns with `routes/` and `schemas/` packages | "Most planned files would hold under 30 lines; one file per concern is easier to navigate and review" (TDD §27 row §4.1, §15). TDD §4.1 sets the threshold for splitting `app/main.py` at roughly three times the current endpoint count |
| Reference implementation for the graph | `InMemoryBrain` as a first-class `SharedBrain` implementation used by tests and `BRAIN=memory` | Mocking the Neo4j driver, or Neo4j-only tests | Tests need no services, and the in-memory class "is also the executable reference for the Cypher semantics" (TDD §27 row §4.4, §16; module docstring of `app/brain.py`) |
| Query classification | Deterministic keyword classifier, first match wins, ambiguous messages fall to `general` which retrieves every category | An LLM classification call per request | "Saves an LLM round-trip per request; the system prompt already handles residual noise" (TDD §27 row §4.5; TDD §4.5 item 3; README, Trade-offs: "misroutes fall to `general`, which still retrieves everything") |
| Ranking | The retrieval query's filter and `ORDER BY confidence DESC, updated_at DESC` | A weighted score of relevance, recency, confidence and source priority | "The formula had no semantic signal to weigh until embeddings exist" (TDD §27 row §4.6; README, Trade-offs) |
| Provider boundary | `LLMProvider` protocol, three adapters in `app/llm.py`, one shared `_guarded` | Direct SDK use elsewhere; per-provider error handling | Provider independence is TDD §2 principle 4; both SDKs are Stainless-generated and "expose the same exception names, so one `_guarded()` helper" suffices (TDD §10). The second real provider is the OpenAI-compatible adapter so the service is "not vendor-locked" (TDD §27 row §10) |
| Provider 4xx errors | Propagate unchanged (surface as a server error) | Mapping them to `503` like outages | "4xx is our bug (bad request, auth)", not an outage (comment in `_guarded`; TDD §10; CLAUDE.md, Gotchas) |
| `generate` return type | `str` | The PRD's `LLMResponse` object (PRD FR-6) | It "would carry only provider metadata nobody reads yet" (TDD §10) |
| `LLMRequest` shape | `system_prompt` plus `messages`, rendered once in `select_context` | Per-provider rendering of structured context | "Context rendered once in the prompt builder; providers stay thin and the prompt is testable" (TDD §27 row §4.7) |
| Graph model | `User`, `Profile`, `Memory`, `SUPERSEDES`; goal/preference/interest as `Memory.type` | Typed nodes such as `:Goal`, `:Preference`, `:LifeArea`, `:AstrologyAttribute`, `:Message` | "§6 already recommended generic memories; the typed-node list contradicted it"; adding typed nodes later is additive (TDD §27 row §5.1; TDD §5.1; README, Trade-offs) |
| Correction | Supersede with a `SUPERSEDES` edge; never overwrite | Update the existing node in place (PRD §7.3 allows either) | "Provenance and history for free" (README, Trade-offs; TDD §22, §24) |
| Supersede API | A decision inside `upsert_memory`, which returns an outcome | A separate `supersede_memory` call | "Superseding is a decision the upsert makes; two calls make the write non-atomic" (TDD §27 row §4.4) |
| Profile facts from chat | Routed to the Profile node through `PROFILE_KEYS` | Stored as `Memory` nodes like everything else | "Lets a DOB stated in chat produce a sun sign; keeps supersede semantics for the language example" (TDD §27 row §7.0; TDD §7.0) |
| Session store | One concrete `SessionStore` with two synchronous methods | A `SessionContextStore` protocol ready for Redis | "One implementation; a Protocol with a single implementer documents nothing"; Redis is a drop-in because the store is one class with two methods (TDD §27 row §4.3; README, Trade-offs) |
| LLM outage on `/chat` | `503`, nothing recorded | A deterministic fallback message (PRD §13 mitigation for "LLM unavailable") | "There is no deterministic fallback text that is honest for an astrology question" (TDD §13.3) |
| Write after a failed read | Skip the post-response memory write when the pre-response read failed | Attempt the write anyway | "One logged failure per request, not two" (TDD §13.2; TDD §27 row §13) |
| Memory update timing | Synchronous, inside the request | Queue or background worker | "Simplest to test end to end; a queue is a one-line move of the last block in `chat.py`" (README, Trade-offs; TDD §12) |
| `context_used` | Exact definition: three fixed tags, then memory keys | The PRD's illustrative `["career_goal", "user_profile"]` | "Tests and the evaluation harness assert on it" (TDD §27 row §14; TDD §14) |
| Candidate schema | `MemoryCandidate` is Pydantic; every other model is a dataclass; numeric constraints live in `validate` | All Pydantic, or a Pydantic model with `Field` constraints | It "doubles as the LLM structured-output schema" (docstring in `app/models.py`); "Keep numeric constraints out of its fields; `memory.validate` enforces them" (CLAUDE.md, Gotchas). The reason for that placement is not recorded; inferred: a constraint on the schema would fail the whole parse, while `validate` drops one candidate at a time |
| Astrology | Tropical sun sign derived from `date_of_birth` | A real engine | A full engine is an explicit non-goal (PRD §4; TDD §11; README, Trade-offs) |
| Observability fields | `memory_updates` and `degraded` added to the response | The PRD's minimal response | "Observability fields rather than core assignment requirements" (TDD §14) |

## Failure modes and degraded behavior

The PRD requires handling of invalid payload, missing profile, empty memory, no relevant context, LLM failure and Neo4j failure (PRD FR-8). The table adds the cases the code distinguishes beyond those six.

| Failure | Detected in | Client sees | Session history | Shared Brain | Pinned by |
|---|---|---|---|---|---|
| Invalid payload | FastAPI validation of `ChatRequest` or `UserUpsert` in `app/main.py`; `upsert_user` also raises `HTTPException(422)` when no profile field is given | `422` | untouched | untouched | `test_invalid_payload_is_422` covers the Pydantic validation on both routes; the no-fields `HTTPException(422)` branch of `upsert_user` has no test |
| Missing profile | `get_profile` returns `None`; `select_context` has no facts | `200`; no `user_profile` or `astrology` tag; Context reads "none yet; this may be a new user." when there are also no memories | appended | memories from the message are still written | `test_8_missing_profile_is_fine` |
| Empty memory | `search_memories` returns `[]` | `200`; no memory keys in `context_used` | appended | write proceeds | `test_1_new_user_succeeds_with_no_context` |
| No relevant memory | Category filter matches nothing | `200`; the LLM receives recent turns and profile only | appended | write proceeds | `test_6_irrelevant_memory_excluded` |
| LLM unavailable during generation | `_guarded` raises `LLMError` (connection error, rate limit, 5xx); `AnthropicLLM.generate` also raises on refusal or empty text; `OpenAICompatibleLLM.generate` on empty content | `503` with `{"detail": "LLM unavailable: ..."}` from `_llm_error` | not appended | nothing read after this point, nothing written | `test_9_llm_failure_returns_503_and_mutates_nothing`; `test_status_mapping`, `test_connection_error_is_llm_error`, `test_generate_empty_content_is_llm_error` |
| Provider rejects the request (4xx) | `_guarded` re-raises the SDK exception | Unhandled exception, a server error rather than `503` (comment in `_guarded`, `app/llm.py`: "surface it as a 500, not a degraded 503"; CLAUDE.md, Gotchas: 4xx errors "deliberately propagate (our bug, not an outage)") | not appended | nothing written | `test_status_mapping` (400 → `openai.BadRequestError`) |
| Neo4j unavailable before generation | `BrainUnavailable` from `get_profile` or `search_memories` in `ChatService.chat` | `200` with `degraded: true`; answered from recent turns (and any profile read before the failure); `memory_updates: 0` | appended | no memories retrieved; the post-response write is skipped; the driver gives up after about 3 s | `test_10_graph_failure_degrades`; `test_neo4j_connection_failure_is_brain_unavailable` |
| Neo4j unavailable during the write only | `BrainUnavailable` from `remember` | `200` with `degraded: true` and `memory_updates: 0` | appended | writes that completed before the failure persist (`upsert_profile` runs before the `upsert_memory` loop) | no dedicated test; behavior read from `ChatService.chat` |
| Extraction fails after a good reply | `LLMError` from `extract_memories`: provider outage, or invalid JSON from an OpenAI-compatible server (`ValidationError` → `LLMError`) | `200` with `degraded: false` and `memory_updates: 0`; a warning is logged | appended | nothing written | Adapter level: `test_extract_invalid_output_is_llm_error`. No `/chat` scenario, because `MockLLM.fail` fails generation and extraction together |
| Neo4j unavailable at startup | `ensure_schema` raises `BrainUnavailable` inside `lifespan` | The app serves; every non-follow-up `/chat` is `degraded: true` until Neo4j returns (follow-ups never touch the brain and stay `degraded: false`) | n/a | schema statements are not retried automatically | none |
| Neo4j unavailable for `/users` or `/users/{user_id}/memories` | `BrainUnavailable` propagates from the route | `503` with "Shared Brain unavailable" from `_brain_error` | n/a | untouched | none |
| Invalid LLM configuration | `build_llm` raises `ValueError` (unknown provider, or `openai` without `LLM_MODEL`) | Startup fails inside `lifespan` | n/a | n/a | `test_build_llm_selects_provider` |

The README's Failure modes table and TDD §13 are the specification for the first six rows; the remaining rows are behavior read from the code.

## Configuration

Every environment variable the repository reads, with the layer that consumes it. `Settings.from_env()` in `app/config.py` reads the first twelve; details and the container wiring live in [Configuration and deployment](08-configuration-and-deployment.md).

| Variable | Default | Consumed by |
|---|---|---|
| `BRAIN` | `neo4j` | `lifespan` in `app/main.py`: `memory` selects `InMemoryBrain`, anything else selects `Neo4jBrain` |
| `NEO4J_URI` | `bolt://localhost:7687` | `Neo4jBrain.__init__` via `app/main.py`; `docker-compose.yml` overrides it to `bolt://neo4j:7687` |
| `NEO4J_USER` | `neo4j` | `Neo4jBrain.__init__` via `app/main.py`; also read by `tests/test_neo4j.py` |
| `NEO4J_PASSWORD` | `password` | `Neo4jBrain.__init__` via `app/main.py`; also read by `tests/test_neo4j.py` |
| `LLM_PROVIDER` | `anthropic` | `build_llm` in `app/llm.py`: `anthropic`, `openai` or `mock`; anything else raises `ValueError` |
| `LLM_MODEL` | empty | `build_llm`: `anthropic` falls back to `claude-opus-5`; `openai` requires it (`OpenAICompatibleLLM.__init__` raises `ValueError` when empty) |
| `LLM_EFFORT` | `medium` | `AnthropicLLM.generate` sends it as `output_config={"effort": ...}` on every call (extraction does not send it); `OpenAICompatibleLLM` sends it as `reasoning_effort` on both calls and omits the parameter when the value is empty |
| `OPENAI_BASE_URL` | empty | `OpenAICompatibleLLM.__init__`; empty becomes `None`, which the SDK resolves to api.openai.com |
| `OPENAI_API_KEY` | empty | `OpenAICompatibleLLM.__init__`; empty becomes the placeholder `"not-needed"` for local servers |
| `RECENT_LIMIT` | `10` | `SessionStore(settings.recent_limit)` in `app/main.py`; parsed with `int()` |
| `MEMORY_LIMIT` | `8` | `ChatService.memory_limit`, passed to `search_memories`; parsed with `int()` |
| `MIN_CONFIDENCE` | `0.6` | `ChatService.min_confidence`, passed to `remember` and `validate`; parsed with `float()` |
| `LOG_LEVEL` | `INFO` | `logging.basicConfig` at import of `app/main.py`; not part of `Settings` |
| `ANTHROPIC_API_KEY` | unset | Read by the `anthropic` SDK inside `AnthropicLLM.__init__` (`anthropic.AsyncAnthropic()`), or replaced by an `ant auth` profile; not part of `Settings` |
| `NEO4J_TEST_URI` | unset | `tests/test_neo4j.py` only; when unset the live round trip is skipped |

`.env.example` lists all of these except `NEO4J_TEST_URI`. `docker-compose.yml` passes through `LLM_PROVIDER`, `LLM_MODEL`, `LLM_EFFORT`, `ANTHROPIC_API_KEY`, `OPENAI_BASE_URL` and `OPENAI_API_KEY` from the host environment with the same defaults, hard-codes the Neo4j trio, and leaves `BRAIN`, the three budgets and `LOG_LEVEL` at their code defaults. `pyproject.toml` declares `requires-python = ">=3.12"` and the runtime dependencies `fastapi`, `uvicorn[standard]`, `neo4j>=5`, `anthropic`, `openai>=3.16.2`; the `dev` group adds `pytest`, `pytest-asyncio`, `httpx`.

## Tests that pin this layer

The suite is organized by layer. The `/chat` scenarios in `tests/test_chat.py` drive the real application with two fakes injected, `InMemoryBrain` for the graph and `MockLLM` for the provider; `tests/test_openai_llm.py` uses an in-process fake HTTP server instead, and `tests/test_neo4j.py` plus `test_neo4j_connection_failure_is_brain_unavailable` in `tests/test_units.py` construct a real `Neo4jBrain`. The fixtures in `tests/conftest.py` build the app through the same factory the server uses, `create_app(brain=brain, llm=llm, settings=Settings(brain="memory", llm_provider="mock"))`, and wrap it in a `TestClient` context manager so the lifespan runs. pytest is configured in `pyproject.toml` with `asyncio_mode = "auto"` and `pythonpath = ["."]`. `uv run pytest -q` needs no services; at the time of writing it reports 48 passed and 1 skipped (the live Neo4j round trip), and no linter is configured (CLAUDE.md, Commands).

| File | Layer under test | What it pins |
|---|---|---|
| `tests/test_chat.py` | The whole request lifecycle through `POST /chat` | The ten PRD scenarios below, plus `test_invalid_payload_is_422`, `test_profile_endpoint_feeds_astrology_context` (a `POST /users` profile produces `["user_profile", "astrology"]` on a horoscope question) and `test_rule_extractor_matches_prd_example` |
| `tests/test_units.py` | `classify`, `sun_sign`, `parse_date`, `validate`, `InMemoryBrain.upsert_memory` and `upsert_profile`, `Neo4jBrain` error translation | Classifier routing for eleven messages, sign boundaries, date formats, candidate normalization and filtering, the three upsert outcomes with statuses and confidences, `BrainUnavailable` from an unreachable `bolt://127.0.0.1:1` |
| `tests/test_openai_llm.py` | `OpenAICompatibleLLM`, `build_llm` | Request shape (system message first, `Bearer` header, `reasoning_effort` present or omitted), `json_object` extraction with fenced output, invalid output as `LLMError`, empty content as `LLMError`, status mapping (500, 503, 429 → `LLMError`; 400 → `openai.BadRequestError`), connection error as `LLMError`, provider selection and startup errors; all against an in-process `httpx.MockTransport` |
| `tests/test_neo4j.py` | `Neo4jBrain` against a live database | Schema creation, profile upsert with sun sign, the three upsert outcomes, category search, `list_memories`, timezone-aware timestamps, and the `SUPERSEDES` edge; skipped unless `NEO4J_TEST_URI` is set |

The ten required scenarios (TDD §17; README, Tests) and their tests in `tests/test_chat.py`:

| # | Scenario | Test | Cross-layer assertion |
|--:|---|---|---|
| 1 | New user | `test_1_new_user_succeeds_with_no_context` | `200`, `context_used == []`, `degraded is False` |
| 2 | Durable memory creation | `test_2_first_message_creates_durable_memory_and_profile` | `memory_updates == 4`; `career.goal = switch jobs` with next year's timeframe is `ACTIVE`; Profile has name, ISO date of birth, birth place and `sun_sign == "Leo"` |
| 3 | Memory retrieval in a new session | `test_3_new_session_retrieves_memory` | `career.goal` in `context_used`, `recent_conversation` absent, "switch jobs" in the reply |
| 4 | Follow-up resolved from recent context | `test_4_follow_up_uses_recent_context_only` | `context_used == ["recent_conversation"]`, `memory_updates == 0`, five alternating roles in the prompt, no `career.goal` in the system prompt |
| 5 | Cross-session persistence | `test_5_memory_persists_across_sessions` | The debug endpoint lists exactly `("career.goal", "ACTIVE")`; sessions `s2` and `s3` both retrieve it |
| 6 | Irrelevant memory excluded | `test_6_irrelevant_memory_excluded` | A `health.goal` is neither in `context_used` nor in the system prompt for a career question |
| 7 | User correction supersedes | `test_7_user_correction_supersedes` | `memory_updates == 1`; active language value is only `Hindi`; statuses `{"English": "SUPERSEDED", "Hindi": "ACTIVE"}`; a later question sees `Hindi` and not `English` |
| 8 | Missing profile | `test_8_missing_profile_is_fine` | `200` with neither `user_profile` nor `astrology` |
| 9 | LLM failure | `test_9_llm_failure_returns_503_and_mutates_nothing` | `503` with "LLM" in `detail`; no memories, no profile; the failed turn is absent from the next request's `context_used` |
| 10 | Graph failure | `test_10_graph_failure_degrades` | `degraded is True`, `memory_updates == 0`, `context_used == []`; a follow-up still yields `["recent_conversation"]` |

The scenarios assert on the response fields (`context_used`, `memory_updates`, `degraded`, status code), on brain state through the `brain` fixture (scenarios 2, 6, 7, 9), and on the `LLMRequest` the mock recorded (scenarios 4, 6, 7 and `test_profile_endpoint_feeds_astrology_context`, via `llm.requests[-1]`); the README calls this "the PRD §11 rubric in executable form". `AnthropicLLM` has no automated test because it needs a key (CLAUDE.md, Gotchas).

## Known limits and future work

### Deliberate shortcuts marked in code

`grep -rn "ponytail:" app/` lists four markers. Each names the ceiling and the upgrade path; they are reproduced verbatim.

| File and location | Comment (verbatim) | Upgrade path |
|---|---|---|
| `app/astrology.py`, module level above `_SIGN_ENDS` | `# ponytail: tropical sun sign only; a real engine (sidereal rashi, moon sign, nakshatra) replaces this module.` | Replace the module; `sun_sign` is called only from `_with_sun_sign` in `app/brain.py`, so orchestration does not change (TDD §11) |
| `app/session.py`, `SessionStore` docstring | `ponytail: process-local dict; swap for Redis when running more than one replica.` | A Redis-backed class with the same `recent` and `append` methods; TDD §4.3 notes the methods would become `async` and the call sites are all in `app/chat.py` |
| `app/brain.py`, `Neo4jBrain.__init__` | `# ponytail: fixed timeout, no circuit breaker; add one when outages are long enough to matter per request.` | A circuit breaker around the brain "instead of a fixed 3s timeout" (README, Production path) |
| `app/context.py`, above `_KEYWORDS` | `# ponytail: keyword classifier, first match wins; swap for an LLM/embedding classifier when evals show misroutes.` | An LLM or embedding classifier behind the same `classify(message) -> Category` signature; TDD §4.5 deferred it because it "would add a round-trip to every ambiguous request" |

### Production path

The README's "Production path" section and TDD §23 (Phase 2 hardening, Phase 3 intelligent memory) list the same work: a Redis session store; a background worker for extraction; authentication and authorization on `user_id`; rate limits; tracing on top of the structured log line that already carries category, `context_used`, `memory_updates` and `degraded`; a circuit breaker around the brain; embedding-based retrieval alongside the category filter; importance and decay; conflict resolution beyond last-write-wins; conversation summarization for long sessions; a real astrology engine; and, for privacy, redaction and per-user access control before the debug endpoint is exposed. Parameterized Cypher, credentials from the environment and not logging prompts are already in place (README, Production path, Privacy).

### Limits visible in the code

- `user_id` is trusted as given; any caller can read or write any user's graph (TDD §20 defers authentication to production).
- The memory update is synchronous, so a non-follow-up turn costs two provider calls before the response returns (README, Trade-offs).
- `memory_updates` counts profile fields written plus memory upserts that changed state; it is 0 whenever the update block raised, even if some writes landed first.
- An extraction failure is not visible in the response; `degraded` stays `false` and only a warning is logged.
- `extract_memories` sees only the current message, so a fact spread across two turns is not captured.
- `LLM_EFFORT` is omitted when empty only by `OpenAICompatibleLLM`; `AnthropicLLM.generate` always sends `output_config={"effort": ...}`, and `AnthropicLLM.extract_memories` never sends it.
- The keyword classifier misroutes some messages by construction: categories are tried in a fixed order, so "What does my horoscope say about money?" is `astrology`, not `finance` (pinned by `test_profile_endpoint_feeds_astrology_context`), and the patterns have no trailing word boundary, so "workout" matches the `career` keyword `work`. Misroutes to `general` still retrieve everything; misroutes to a wrong life area retrieve that area only.

### Specification drift observed

Statements in the design documents that the code does not currently match, recorded here so a reader of the TDD or README is not misled:

- TDD §5.2 lists `valid_until` on `Memory`, and the README's Production path says "`valid_until` is already on the schema". The `Memory` dataclass in `app/models.py` and the Cypher in `app/brain.py` have no such property.
- TDD §15 names `classify_query()`, `select_context()` and `build_request()` in `app/context.py`; the code has `classify()` and `select_context()`, with request building folded into the latter. The same section's file tree does not list `OpenAICompatibleLLM` or `tests/test_openai_llm.py`, although TDD §10 describes the adapter.
- TDD §11 sketches an `AstrologyProfile` dataclass; in the code `sun_sign` is a field of `UserProfile`.
- TDD §7.0 routes a candidate to the Profile node when `category = profile` and its key is in the profile set; `remember` in `app/memory.py` checks only `c.key in PROFILE_KEYS`, so a candidate with key `profile.name` and any category lands on the Profile.
- TDD Appendix A shows separate indexes on `Memory.category` and `Memory.key`; `SCHEMA` in `app/brain.py` creates one composite index, `memory_lookup`, on `(category, key, status)`.
- The README's Tests section says "47 tests"; the suite currently collects 49 (48 run, 1 skipped without `NEO4J_TEST_URI`).

## Documents in this directory

Each sibling document covers one layer in the same what / why / how format as this one, citing the TDD, README and `ponytail:` markers for rationale or labelling it "inferred". Read this overview first for the map, then [02](02-orchestration-and-short-term-context.md) for the sequence, then the layer you are changing. Cross-references use these fixed filenames.

| Document | Files covered | One line |
|---|---|---|
| [README.md](README.md) (this document) | all of `app/` | This overview: layer map, request lifecycle, cross-layer invariants, failure modes, budgets, configuration, and the two-memory model |
| [01-api-layer.md](01-api-layer.md) | `app/main.py` | `create_app` factory and lifespan wiring, Pydantic request and response schemas, the three routes, the two exception handlers |
| [02-orchestration-and-short-term-context.md](02-orchestration-and-short-term-context.md) | `app/chat.py`, `app/session.py` | `ChatService.chat` step by step, `ChatResult`, the `degraded` flag, and the `SessionStore` deque |
| [03-domain-model.md](03-domain-model.md) | `app/models.py`, `app/astrology.py` | The `Category` taxonomy, dataclasses versus the Pydantic `MemoryCandidate`, `PROFILE_KEYS`, `sun_sign` and `parse_date` |
| [04-query-understanding-and-context-selection.md](04-query-understanding-and-context-selection.md) | `app/context.py` | `classify`, `select_context`, `SYSTEM_PROMPT`, the profile-field table, and how `context_used` is built |
| [05-shared-brain.md](05-shared-brain.md) | `app/brain.py` | The `SharedBrain` protocol, `InMemoryBrain` as executable spec, `Neo4jBrain`, every Cypher statement, the schema, and `BrainUnavailable` |
| [06-memory-update.md](06-memory-update.md) | `app/memory.py`, with the extraction prompts and `extract_by_rules` in `app/llm.py` | `validate`, `remember`, profile routing, the create / unchanged / updated outcomes |
| [07-llm-providers.md](07-llm-providers.md) | `app/llm.py` | `LLMProvider`, `_guarded`, `AnthropicLLM`, `OpenAICompatibleLLM`, `MockLLM`, `build_llm` |
| [08-configuration-and-deployment.md](08-configuration-and-deployment.md) | `app/config.py`, `.env.example`, `Dockerfile`, `docker-compose.yml`, `pyproject.toml` | `Settings.from_env`, every environment variable, the two containers, and the run commands |
| [09-testing-and-verification.md](09-testing-and-verification.md) | `tests/` | Fixtures, the ten scenarios, unit tests, the fake OpenAI-compatible server, the live Neo4j round trip |
