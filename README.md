# AstroChat

MyNaksh personalized astrology chat: a FastAPI service that answers using both the current conversation and a persistent, graph-shaped **Shared Brain** of what the user has said before. The LLM is pluggable: Anthropic, any OpenAI-compatible server, or a deterministic mock.

Every `POST /chat` follows one flow:

    Chat → Context Selection → Shared Brain (Neo4j) → LLM → Response → Memory Update

Astrology itself is a stub (a sun sign derived from date of birth). The product here is the memory pipeline: remembering the right things, retrieving only the relevant ones, and correcting them when the user does.

Spec: [`docs/AstroChat_PRD.md`](docs/AstroChat_PRD.md) (requirements) and [`docs/AstroChat_TDD.md`](docs/AstroChat_TDD.md) (design; §27 lists what was refined before implementation and why).

## Run it

Prerequisites: Docker, or Python 3.12+ with [uv](https://docs.astral.sh/uv/).

**Pick an LLM provider** with environment variables (all listed in `.env.example`):

| `LLM_PROVIDER` | Needs | Notes |
|---|---|---|
| `anthropic` (default) | `ANTHROPIC_API_KEY` | `LLM_MODEL` defaults to `claude-opus-5`; `LLM_EFFORT` maps to `output_config.effort` |
| `openai` | `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `LLM_MODEL` | Any OpenAI-compatible chat-completions server: OpenAI (leave the URL empty), Ollama (`http://localhost:11434/v1`), vLLM, Groq, OpenRouter, LM Studio, gateways. `LLM_EFFORT` is sent as `reasoning_effort`; set it empty for models that reject it |
| `mock` | nothing | Deterministic; what the tests use |

**Everything in Docker** (Neo4j + app):

```bash
export ANTHROPIC_API_KEY=sk-ant-...                      # or the openai trio, or LLM_PROVIDER=mock
docker compose up --build
# API http://localhost:8000  (Swagger at /docs)   Neo4j Browser http://localhost:7474  (neo4j / password)
```

**Locally**:

```bash
docker run -d --name neo4j -p 7687:7687 -p 7474:7474 -e NEO4J_AUTH=neo4j/password neo4j:5
cp .env.example .env                    # set your provider's variables
uv sync
uv run --env-file .env uvicorn app.main:app --reload
```

**No Neo4j, no key**: `BRAIN=memory LLM_PROVIDER=mock uv run uvicorn app.main:app`

Example for a local Ollama model: `LLM_PROVIDER=openai OPENAI_BASE_URL=http://localhost:11434/v1 LLM_MODEL=llama3.1 uv run uvicorn app.main:app`. The Anthropic client resolves credentials the standard way: `ANTHROPIC_API_KEY`, or a profile from `ant auth login`.

## The PRD conversation, end to end

Output below is from the mock LLM against a real Neo4j, so the interesting fields are `context_used` and `memory_updates`.

```bash
post() { curl -s localhost:8000/chat -H 'content-type: application/json' \
  -d "{\"user_id\":\"rahul\",\"session_id\":\"$1\",\"message\":\"$2\"}"; echo; }

post s1 "My name is Rahul. I was born on 15 August 1995 in Delhi. I'm planning to switch jobs next year."
# {"context_used": [], "memory_updates": 4, ...}            name, dob, birth place -> Profile (sun sign Leo); career goal -> Memory

post s1 "What should I focus on for my career?"
# {"context_used": ["recent_conversation", "user_profile", "astrology", "career.goal"], ...}

post s1 "Why do you say that?"
# {"context_used": ["recent_conversation"], ...}             follow-up: short-term context only, no graph read

post s2 "What do you remember about my career goals?"
# {"context_used": ["user_profile", "astrology", "career.goal"], ...}   new session: memory came from the graph

post s2 "I prefer English."            # {"context_used": [...], "memory_updates": 1}
post s2 "Actually, I prefer Hindi."    # {"context_used": [..., "language.preferred"], "memory_updates": 1}

curl -s localhost:8000/users/rahul/memories
#  career.goal        = switch jobs   2027   ACTIVE
#  language.preferred = English              SUPERSEDED
#  language.preferred = Hindi                ACTIVE
```

And in Neo4j:

```cypher
MATCH (u:User {id:'rahul'})-[r]->(n) OPTIONAL MATCH (n)-[:SUPERSEDES]->(o)
RETURN type(r), labels(n)[0], coalesce(n.key, n.name), coalesce(n.value, n.sun_sign), n.status, o.value
// HAS_PROFILE Profile  Rahul               Leo          null        null
// HAS_MEMORY  Memory   career.goal         switch jobs  ACTIVE      null
// HAS_MEMORY  Memory   language.preferred  Hindi        ACTIVE      English
// HAS_MEMORY  Memory   language.preferred  English      SUPERSEDED  null
```

## Tests

```bash
uv run pytest                                                       # 47 tests, a few seconds, no services
NEO4J_TEST_URI=bolt://localhost:7687 NEO4J_PASSWORD=password uv run pytest   # + live Neo4j round trip
```

Tests run through the real FastAPI app with `InMemoryBrain` and `MockLLM` injected. The required scenarios (TDD §17) map to `tests/test_chat.py`:

| # | Scenario | Test |
|--:|---|---|
| 1 | New user | `test_1_new_user_succeeds_with_no_context` |
| 2 | Durable memory creation | `test_2_first_message_creates_durable_memory_and_profile` |
| 3 | Memory retrieval in a new session | `test_3_new_session_retrieves_memory` |
| 4 | Follow-up resolved from recent context | `test_4_follow_up_uses_recent_context_only` |
| 5 | Cross-session persistence | `test_5_memory_persists_across_sessions` |
| 6 | Irrelevant memory excluded | `test_6_irrelevant_memory_excluded` |
| 7 | User correction supersedes | `test_7_user_correction_supersedes` |
| 8 | Missing profile | `test_8_missing_profile_is_fine` |
| 9 | LLM failure | `test_9_llm_failure_returns_503_and_mutates_nothing` |
| 10 | Graph failure | `test_10_graph_failure_degrades` |

`tests/test_units.py` covers the classifier, sun-sign boundaries, date parsing, candidate validation, upsert outcomes and the driver-error translation. `tests/test_openai_llm.py` drives the OpenAI-compatible adapter against an in-process fake server (request shape, JSON-mode extraction, fenced output, error mapping). `tests/test_neo4j.py` repeats the brain semantics against a live database and is skipped without `NEO4J_TEST_URI`.

Each scenario asserts the expected `context_used` and the expected facts in the prompt the LLM received. That is the PRD §11 rubric in executable form; the "with vs without Shared Brain" comparison is scenario 3 versus scenario 1.

## API

`POST /chat`

```json
{"user_id": "user-123", "session_id": "session-456", "message": "What should I focus on in my career?"}
```
```json
{"response": "...", "user_id": "user-123", "session_id": "session-456",
 "context_used": ["recent_conversation", "user_profile", "astrology", "career.goal"],
 "memory_updates": 0, "degraded": false}
```

`POST /users` upserts profile fields (`name`, `date_of_birth`, `time_of_birth`, `birth_place`, `preferred_language`); `sun_sign` is derived. `GET /users/{user_id}/memories` lists every memory including superseded ones, for inspection.

Errors: `422` invalid payload, `503` when the LLM is unavailable (nothing is recorded for that turn). A Neo4j outage does not fail `/chat`; the response carries `degraded: true`.

## Architecture

```
app/
  main.py      FastAPI factory, schemas, 3 routes, 2 exception handlers      (no Neo4j or LLM calls)
  chat.py      ChatService: the orchestration sequence                        (no Cypher, no SDK)
  context.py   classify() -> select_context() -> LLMRequest + context_used
  brain.py     SharedBrain protocol; InMemoryBrain (tests/demo); Neo4jBrain + all Cypher
  memory.py    validate candidates, route profile facts, upsert the rest
  llm.py       LLMProvider protocol; AnthropicLLM; OpenAICompatibleLLM; MockLLM; build_llm()  (only module importing provider SDKs)
  session.py   short-term store: deque(maxlen=10) per (user_id, session_id)
  astrology.py sun_sign(date) stub
  models.py    ChatMessage, UserProfile, Memory, MemoryCandidate, LLMRequest, Category
  config.py    Settings from environment
```

Request sequence: load recent turns → classify query → (unless follow-up) read profile + category-filtered memories → build bounded prompt → generate → append both turns to the session → (unless follow-up or degraded) extract candidates from the **user's** message → validate → upsert.

## Graph schema

```
(:User {id, created_at, updated_at})
  -[:HAS_PROFILE]-> (:Profile {name, date_of_birth, time_of_birth, birth_place, preferred_language, sun_sign})
  -[:HAS_MEMORY]->  (:Memory {id, key, category, type, value, target_timeframe, confidence,
                              status: ACTIVE|SUPERSEDED, source_message_id, created_at, updated_at})
(:Memory)-[:SUPERSEDES]->(:Memory)
```

Constraints: `User.id` and `Memory.id` unique. Index: `Memory(category, key, status)` for the logical-key lookup. Goals, preferences and interests are `Memory.type` values rather than typed nodes; adding typed nodes later is additive.

## Memory strategy

- **Source of truth is the user.** Only the user's latest message is extracted from. Assistant text never becomes memory.
- **Logical key** `(user_id, category, key)` identifies a fact, e.g. `career.goal`, `language.preferred`. At most one `ACTIVE` memory per key.
- **Upsert semantics** (one write transaction): no active memory → create; same value → refresh `updated_at`, keep the higher confidence; different value → mark old `SUPERSEDED`, create new `ACTIVE`, link `(new)-[:SUPERSEDES]->(old)`. History and provenance (`source_message_id`) are preserved.
- **Profile facts route to the Profile node.** `profile.name`, `profile.date_of_birth`, `profile.time_of_birth`, `profile.birth_place` stated in chat update the Profile and recompute `sun_sign`. Everything else is a Memory.
- **What is stored**: explicit goals, preferences, interests, life plans, stable profile facts. **What is not**: greetings, filler, questions, hypotheticals, anything below `MIN_CONFIDENCE` (0.6), off-taxonomy categories or types.
- **Extraction**: the Anthropic provider asks for structured output against the `MemoryCandidate` schema (key, category, type, value, target_timeframe, confidence, reason). The OpenAI-compatible provider requests `json_object` mode with the shape spelled out in the prompt, strips code fences, and validates with the same schema; anything invalid is an `LLMError`. The mock provider uses a small rule set that covers the PRD sentences. If extraction fails the user still gets the response; the write is skipped and logged.

## Context selection

1. **Classify** the message with a first-match keyword classifier into `profile | career | relationships | finance | health | interests | language | astrology | general | follow_up`. Follow-up detection: leading phrases ("why", "tell me more", "what about that") or a short message with a bare pronoun. Ambiguous → `general`.
2. **Retrieve** active memories where `category = $category` (all categories for `general`), ordered by confidence then recency, `LIMIT 8`. Follow-ups skip the graph entirely.
3. **Select profile fields** by category: everything for `profile`/`astrology`; name + preferred language for `language`; name + sun sign for life areas and `general`; nothing for `follow_up`. Sun sign rides along on life-area questions because it is the personalization hook of an astrology assistant.
4. **Build** one system prompt (fixed rules + rendered context) and a message list (≤10 recent turns + current message).

`context_used` reports exactly what was injected: `recent_conversation`, `user_profile`, `astrology`, then memory keys in retrieval order.

## Failure modes

| Failure | Behavior |
|---|---|
| Invalid payload | `422` from FastAPI validation |
| Missing profile / empty memory | Normal `200`; those tags simply do not appear in `context_used` |
| No relevant memory | LLM gets recent turns + profile only |
| LLM unavailable | `503`; turn not appended to history; no memory writes |
| Neo4j unavailable | `200` with `degraded: true`; answered from recent turns; memory write skipped; driver fails fast (3s) |
| Extraction fails after a good response | Response returned; write skipped and logged |

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `BRAIN` | `neo4j` | `neo4j` or `memory` (in-process, for demos) |
| `NEO4J_URI` / `NEO4J_USER` / `NEO4J_PASSWORD` | `bolt://localhost:7687` / `neo4j` / `password` | Graph connection |
| `LLM_PROVIDER` | `anthropic` | `anthropic`, `openai` or `mock` |
| `LLM_MODEL` | unset | Required for `openai` (server-specific); `anthropic` falls back to `claude-opus-5` |
| `LLM_EFFORT` | `medium` | `low` / `medium` / `high`. Anthropic `effort`, OpenAI-compatible `reasoning_effort`; empty omits the parameter |
| `ANTHROPIC_API_KEY` | unset | Anthropic provider |
| `OPENAI_BASE_URL` / `OPENAI_API_KEY` | unset | OpenAI-compatible provider; empty URL means api.openai.com, key may be empty for local servers |
| `RECENT_LIMIT` / `MEMORY_LIMIT` | `10` / `8` | Context budgets |
| `MIN_CONFIDENCE` | `0.6` | Extraction threshold |

## Trade-offs

| Decision | Choice | Why |
|---|---|---|
| Graph DB | Neo4j, generic `Memory` nodes | Required by the assignment; typed nodes add nothing until a traversal needs them |
| Session history | In-process deque | Fast; the store is one class with two methods, so Redis is a drop-in |
| Query classification | Keywords, deterministic | Zero extra LLM calls; misroutes fall to `general`, which still retrieves everything |
| Ranking | Cypher filter + order (confidence, recency) | Explainable; a weighted score has no semantic signal to weigh without embeddings |
| Extraction | Structured LLM output + deterministic validation | Flexible extraction, hard guardrails on what is persisted |
| Correction | Supersede, never overwrite | Provenance and history for free |
| Memory update | Synchronous | Simplest to test end to end; a queue is a one-line move of the last block in `chat.py` |
| Astrology | Sun-sign stub | Explicitly out of scope |

## Production path

- **Hardening**: Redis session store; background worker for extraction; authn/authz on `user_id`; rate limits; structured logs already carry `request` fields (category, `context_used`, `memory_updates`, `degraded`), add tracing; circuit breaker around the brain instead of a fixed 3s timeout.
- **Smarter memory**: embedding-based retrieval alongside the category filter; importance and decay (`valid_until` is already on the schema); conflict resolution beyond last-write-wins; conversation summarization for long sessions; a real astrology engine behind `astrology.py`.
- **Privacy**: the Shared Brain is user data. Parameterized Cypher throughout; credentials from the environment; prompts are not logged; add redaction and per-user access control before exposing the debug endpoint.

Stack versions used: Python 3.12+, FastAPI 0.141, neo4j driver 6.3 against Neo4j 5.26, anthropic SDK 1.7, openai SDK 3.16.
