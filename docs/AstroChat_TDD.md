# Technical Design Document (TDD)

## Personalized Astrology Chat — Shared Brain & Memory
**Product:** MyNaksh Personalized Astrology Chat
**Document:** Technical Design Document
**Status:** Assignment-ready MVP
**Primary stack:** Python, FastAPI, Neo4j, one pluggable LLM provider
**Revision:** v2, refined before implementation; §27 lists every change and its reason.

---

## 1. Technical Objective

Build a production-oriented conversational ML service that combines:

- recent conversation context,
- persistent graph-based user memory,
- profile/astrology attributes,
- context selection,
- an LLM response layer, and
- post-response memory extraction/update.

The design deliberately favors a small number of clean interfaces and predictable behavior so the complete core path can be implemented within the assignment's three-hour limit.

## 2. Design Principles

1. **Separate short-term and long-term memory.** Recent conversation is session-scoped; durable facts live in the Shared Brain.
2. **Retrieve before generating.** Never send the whole graph or entire conversation history to the LLM.
3. **User statements are the source of truth for memory.** Assistant-generated content must not become user memory by default.
4. **Provider independence.** LLM provider SDKs stay behind an adapter.
5. **Bounded context.** Every source has a limit to prevent prompt growth.
6. **Graceful degradation.** A single dependency failure should not unnecessarily break the entire request.
7. **Debuggability.** The system should expose what context categories were used without leaking hidden prompt internals.

## 3. Architecture

```text
                        +----------------------+
                        |       Client         |
                        +----------+-----------+
                                   |
                                   | POST /chat
                                   v
                        +----------+-----------+
                        |      FastAPI API      |
                        | validation/auth-lite  |
                        +----------+-----------+
                                   |
                                   v
                        +----------+-----------+
                        |   Chat Orchestrator   |
                        +----+-------+------+---+
                             |       |      |
             +---------------+       |      +----------------+
             |                       |                       |
             v                       v                       v
   +---------+----------+  +---------+----------+  +---------+----------+
   | Session Context     |  | Shared Brain       |  | Query Understanding |
   | recent messages     |  | Neo4j repository   |  | rules / lightweight |
   +---------+----------+  +---------+----------+  | classifier / LLM    |
             |                       |              +---------+----------+
             +-----------+-----------+                        |
                         v                                    |
                +--------+---------+                          |
                | Context Selector |<-------------------------+
                +--------+---------+
                         |
                         v
                +--------+---------+
                | Prompt / Context |
                | Builder          |
                +--------+---------+
                         |
                         v
                +--------+---------+
                | LLMProvider      |
                | interface        |
                +--------+---------+
                         |
                         v
                +--------+---------+
                | Provider Adapter |
                +------------------+
                         |
                         v
                    LLM Provider

After response:

                    user message
                         |
                         v
                +--------+---------+
                | Memory Extractor |
                +--------+---------+
                         |
                         v
                +--------+---------+
                | Memory Validator |
                +--------+---------+
                         |
                         v
                    Shared Brain
```

## 4. Component Responsibilities

### 4.1 API layer

Responsibilities:

- Validate request/response schemas.
- Return HTTP errors for malformed requests.
- Delegate business logic to the chat orchestrator.
- Avoid direct Neo4j or LLM calls from route handlers.

Module: `app/main.py` holds the app factory, Pydantic schemas, three routes and two exception handlers. Splitting into `routes/` and `schemas/` packages is warranted at roughly three times this endpoint count.

### 4.2 Chat Orchestrator

Owns the end-to-end flow:

```text
validate
 -> load session context
 -> understand query
 -> retrieve Shared Brain candidates
 -> rank/select context
 -> build prompt
 -> generate response
 -> append session history
 -> extract memory
 -> persist/update memory
 -> return response
```

This is the main application service and should contain orchestration, not infrastructure-specific Cypher or provider SDK logic.

### 4.3 Session Context Store

Purpose: short-term conversation context.

Assignment-friendly implementation options:

- In-memory dictionary keyed by `(user_id, session_id)` for the timed submission.
- A repository interface so Redis/Postgres can replace it later.

Interface (one concrete class; no Protocol until a second implementation exists):

```python
class SessionStore:
    def append(self, user_id: str, session_id: str, *messages: ChatMessage) -> None: ...
    def recent(self, user_id: str, session_id: str) -> list[ChatMessage]: ...
```

Backed by a `collections.deque(maxlen=RECENT_LIMIT)` per `(user_id, session_id)`, so the cap is enforced at write time. Methods are synchronous because the store is in-process; a Redis replacement would make them `async` and change call sites in one file (`chat.py`).

### 4.4 Shared Brain Repository

Neo4j persistence for profile, memory, and relationships.

Interface (two implementations: `Neo4jBrain` for runtime, `InMemoryBrain` for tests and `BRAIN=memory` local runs):

```python
class SharedBrain(Protocol):
    async def get_profile(self, user_id: str) -> UserProfile | None: ...
    async def upsert_profile(self, user_id: str, fields: dict[str, str]) -> UserProfile: ...
    async def search_memories(self, user_id: str, category: str | None, limit: int) -> list[Memory]: ...
    async def upsert_memory(self, user_id: str, candidate: MemoryCandidate, source_message_id: str) -> Outcome: ...
    async def list_memories(self, user_id: str) -> list[Memory]: ...   # debug endpoint; includes SUPERSEDED
```

`upsert_memory` implements Appendix C inside one write transaction and returns `"created" | "updated" | "unchanged"`. There is no separate `supersede_memory`: superseding is a decision the upsert makes, and splitting it into two calls makes the write non-atomic. Driver and connection failures are translated to `BrainUnavailable` so the orchestrator never imports Neo4j exceptions.

### 4.5 Query Understanding

Classify the query into a small set of domains for retrieval filtering.

Initial domains:

- `profile`
- `career`
- `relationships`
- `finance`
- `health`
- `interests`
- `language`
- `astrology`
- `general`
- `follow_up`

A strict taxonomy is not required; categories exist to narrow the graph search.

MVP approach:

1. Detect obvious follow-up phrases such as "why do you say that", "what about that", "tell me more".
2. Detect explicit category keywords where practical.
3. Ambiguous queries fall through to `general`, which retrieves all active memories (top 8). An LLM classification call is deferred: it would add a round-trip to every ambiguous request to save a few irrelevant memories the system prompt already tells the model to ignore.
4. Always include the current user message as the strongest context signal.

### 4.6 Context Selector

Combines candidate context sources into a bounded set.

MVP ranking is the retrieval query itself: filter `status = 'ACTIVE'` and `category = $category` (all categories for `general`), then `ORDER BY confidence DESC, updated_at DESC LIMIT 8`. A weighted score (relevance, recency, confidence, source priority) is Phase 3 and only worth building once embeddings exist to feed the relevance term.

Selection rules by query category:

| Query category | Recent messages | Memories retrieved | Profile fields |
|---|---|---|---|
| `follow_up` | last 10 | none | none |
| `general` | last 10 | all active, top 8 | name, sun_sign |
| `profile`, `astrology` | last 10 | same category | all fields |
| `language` | last 10 | same category | name, preferred_language |
| life areas (`career`, `relationships`, `finance`, `health`, `interests`) | last 10 | same category | name, sun_sign |

Sun sign rides along on every life-area query because this is an astrology assistant: it is the personalization hook, not noise. Superseded memories are excluded by the query, never by post-filtering.

### 4.7 Prompt / Context Builder

Construct a provider-neutral request. Context is rendered to text once, by the prompt builder, so every provider receives the same two things:

```python
@dataclass
class LLMRequest:
    system_prompt: str            # fixed role text + rendered profile/memory context
    messages: list[ChatMessage]   # recent turns followed by the current user message
```

Rendering context in one place keeps providers thin and makes the exact prompt testable without a provider.

The system prompt should establish:

- role as a personalized astrology assistant,
- use only supplied context,
- do not invent user facts,
- treat explicit user statements as authoritative,
- keep answers conversational and relevant,
- do not reveal internal retrieval or prompt-selection mechanics unless explicitly designed for debugging.

## 5. Data Model

### 5.1 Neo4j graph model

MVP nodes and relationships (everything the code creates):

```text
(:User {id})-[:HAS_PROFILE]->(:Profile)
(:User)-[:HAS_MEMORY]->(:Memory)
(:Memory)-[:SUPERSEDES]->(:Memory)
```

Goals, preferences, interests and life areas are expressed as `Memory.type` and `Memory.category` properties rather than typed nodes. Typed nodes (`:Goal`, `:Preference`, `:LifeArea`, `:AstrologyAttribute`, `:Message`) are deferred until a query needs to traverse them; adding them is additive (a label plus a relationship per memory) and does not change the API.

`type` answers "what kind of memory" (`fact | goal | preference | interest`); `category` answers "which life area" and uses the same taxonomy as query classification (§4.5), so retrieval is a direct equality match.

### 5.2 Recommended properties

#### User

```text
id: string
created_at: datetime
updated_at: datetime
```

#### Profile

```text
name: string | null
date_of_birth: date | null
time_of_birth: time | null
birth_place: string | null
preferred_language: string | null
sun_sign: string | null
```

#### Memory

```text
id: string
type: string
category: string
key: string
value: string
target_timeframe: string | null
confidence: float
status: ACTIVE | SUPERSEDED
source_message_id: string | null
created_at: datetime
updated_at: datetime
valid_until: datetime | null
```

Using `key` plus `category` gives stable identity for updates such as `career.target_year` or `preferred_language`.

## 6. Example Graph

For the statement:

> "I'm planning to switch jobs next year."

The graph can become:

```text
(:User {id: "user-123"})
      |
      | HAS_GOAL
      v
(:Goal {
    key: "career_change",
    type: "career",
    target_year: 2027,
    status: "ACTIVE"
})
```

For a richer generic representation:

```text
User -[:HAS_MEMORY]-> Memory {
  key: "career.goal",
  value: "switch jobs",
  category: "career",
  target_timeframe: "2027"
}
```

The second form is more generic and faster to implement. Typed nodes such as `Goal` can be introduced later if richer graph traversal becomes necessary.

## 7. Memory Lifecycle

```text
User Message
     |
     v
Memory Candidate Extraction
     |
     v
Normalize key/value/category
     |
     v
Confidence + durability checks
     |
     +-------- low value --------> discard
     |
     v
Find active memory with same logical key
     |
     +-------- none -------------> create
     |
     +-------- same value -------> refresh metadata / ignore duplicate
     |
     +-------- changed ----------> supersede previous + create/update latest
```

### 7.0 Profile facts route to the Profile node

Candidates with `category = profile` and a key in `{profile.name, profile.date_of_birth, profile.time_of_birth, profile.birth_place}` are written to the `Profile` node via `upsert_profile`, not stored as `Memory` nodes. Structured stable facts belong in a structured place, and this is what lets a date of birth stated in chat produce a sun sign. When `date_of_birth` changes, `sun_sign` is recomputed. Everything else, including `language.preferred`, is a `Memory` and follows the supersede lifecycle above, which is what makes PRD §7.3 (English then Hindi) demonstrable.

### 7.1 Extraction format

Use structured output where supported:

```json
{
  "memories": [
    {
      "key": "career.goal",
      "category": "career",
      "value": "switch jobs",
      "target_timeframe": "2027",
      "confidence": 0.97,
      "reason": "Explicitly stated by user"
    }
  ]
}
```

### 7.2 What to remember

Store explicit durable facts, goals, preferences, interests, significant future plans, and stable profile information.

### 7.3 What not to remember

Ignore greetings, filler, temporary conversational details, and assistant-generated assertions unless a later explicit user statement confirms them.

## 8. Retrieval Strategy

The Shared Brain should not be dumped into the LLM context.

### Step 1: Query categorization

Example:

```text
"What do you remember about my career goals?"
        -> category = career
```

### Step 2: Candidate retrieval

Example Cypher shape:

```cypher
MATCH (u:User {id: $user_id})-[:HAS_MEMORY]->(m:Memory)
WHERE m.status = 'ACTIVE'
  AND ($category = 'general' OR m.category = $category)
RETURN m
ORDER BY m.confidence DESC, m.updated_at DESC
LIMIT 8
```

### Step 3: Profile filtering

Only select profile properties likely to matter. A career query may include name and preferred language but need not include birth place unless astrology is relevant.

### Step 4: Merge and rank

Merge:

- current message,
- recent session context,
- retrieved memories,
- selected profile/astrology facts.

Then apply hard limits before prompt construction.

## 9. Short-Term Context Strategy

For every `/chat` request:

1. Load the last N messages for the session.
2. Include the current message separately.
3. If it is a follow-up, prioritize immediate previous assistant/user turns.
4. Do not convert all short-term messages into long-term memory.
5. After the response, append the current user message and assistant response to the session store.

Recommended `N = 10` as an assignment-friendly default.

## 10. LLM Abstraction

```python
class LLMProvider(Protocol):
    async def generate(self, request: LLMRequest) -> str: ...
    async def extract_memories(self, message: str, today: date) -> list[MemoryCandidate]: ...
```

All implementations live in `app/llm.py`; `build_llm(settings)` picks one from `LLM_PROVIDER`:

- `AnthropicLLM` (`anthropic`): Claude via the official `anthropic` SDK (`claude-opus-5` unless `LLM_MODEL` is set). Generation is one `messages.create` call; extraction uses structured outputs (`messages.parse` with a Pydantic schema) so candidates arrive validated.
- `OpenAICompatibleLLM` (`openai`): any server speaking the OpenAI chat-completions API, configured by `OPENAI_BASE_URL`, `OPENAI_API_KEY` and `LLM_MODEL` (required, since model names are server-specific). Extraction requests `json_object` mode with the expected shape in the prompt, the lowest common denominator across OpenAI, Ollama, vLLM, Groq, OpenRouter and LM Studio, then validates with the same Pydantic schema; invalid output is an `LLMError`, so the response is still returned and only the memory write is skipped. `LLM_EFFORT` is forwarded as `reasoning_effort` (omitted when empty, for models that reject it).
- `MockLLM` (`mock`): deterministic. `generate` echoes which context it received; `extract_memories` is a small rule-based extractor (name, date and place of birth, "planning to ..." goals, "I prefer ..." language). Tests use it exclusively; it also serves as a no-key demo mode (`LLM_PROVIDER=mock`).

`generate` returns `str`. The PRD's `LLMResponse` type would carry only provider metadata nobody reads yet.

Both SDKs are Stainless-generated and expose the same exception names, so one `_guarded()` helper maps connection and rate-limit errors and 5xx responses to `LLMError`; 4xx responses propagate, because they indicate a bug on our side rather than an outage.

## 11. Astrology Layer

The assignment does not require a full astrology engine. The design therefore treats astrology as a profile enrichment layer.

Example:

```python
@dataclass
class AstrologyProfile:
    sun_sign: str | None
```

A simple deterministic stub can derive or accept a sun sign, while a real astrology engine can replace it later without changing chat orchestration.

## 12. End-to-End Request Sequence

```text
1. Client -> FastAPI: POST /chat
2. FastAPI -> Orchestrator: validated request
3. Orchestrator -> SessionStore: recent messages
4. Orchestrator -> QueryUnderstanding: category/follow-up intent
5. Orchestrator -> SharedBrain: candidate memories + profile
6. Orchestrator -> ContextSelector: rank/filter candidates
7. ContextSelector -> PromptBuilder: bounded context
8. PromptBuilder -> LLMProvider: generation request
9. LLMProvider -> Orchestrator: response
10. Orchestrator -> SessionStore: append user + assistant messages
11. Orchestrator -> LLMProvider: memory extraction request (or deterministic extractor)
12. Orchestrator -> SharedBrain: create/update memories
13. Orchestrator -> FastAPI: response payload
14. FastAPI -> Client: JSON response
```

Memory extraction can be executed after response generation so that user-facing latency is decoupled from the response path later. In the timed MVP, it may execute synchronously to simplify implementation and testing.

## 13. Error Handling and Degraded Modes

### 13.1 Validation errors

Return `422 Unprocessable Entity` from FastAPI for invalid payloads.

### 13.2 Neo4j unavailable

Preferred degraded behavior:

- Continue using recent conversation context.
- Continue using request-provided profile if available.
- Generate a response without persistent memory.
- Return `degraded: true` in the response.
- The driver is configured with a 3s connection timeout and 3s transaction-retry window, so an outage costs one request about three seconds rather than the driver's 30s default.
- Skip the post-response memory write when the pre-response read already failed: one logged failure per request, not two.

### 13.3 LLM unavailable

Preferred MVP behavior:

- Return `503 Service Unavailable`; there is no deterministic fallback text that is honest for an astrology question.
- Do not append the failed turn to session history and do not persist memories: nothing about the request is recorded.
- If generation succeeds but extraction fails, the user still gets the response; the memory write is skipped and logged.

A production deployment could add a fallback model.

### 13.4 Empty memory

Treat as a valid state. Do not return an error merely because no Shared Brain facts exist.

### 13.5 No relevant context

Call the LLM with the current message and recent conversation only. `context_used` should identify only the actually used categories.

## 14. API Contract

### POST /chat

Request:

```json
{
  "user_id": "user-123",
  "session_id": "session-456",
  "message": "What should I focus on in my career?"
}
```

Response:

```json
{
  "response": "Based on your career goals...",
  "user_id": "user-123",
  "session_id": "session-456",
  "context_used": [
    "career_goal",
    "user_profile"
  ]
}
```

Recommended response extension:

```json
{
  "response": "...",
  "user_id": "user-123",
  "session_id": "session-456",
  "context_used": ["career_goal", "user_profile"],
  "memory_updates": 1,
  "degraded": false
}
```

`memory_updates` and `degraded` are observability fields rather than core assignment requirements.

`context_used` is defined precisely so tests can assert on it:

- `recent_conversation`: at least one prior session message was included.
- `user_profile`: at least one Profile field was included.
- `astrology`: the sun sign was included.
- one entry per memory included, named by its key, e.g. `career.goal`, `language.preferred`.

Order is stable: the three source tags first, then memory keys in retrieval order.

## 15. Project Structure

```text
app/
├── __init__.py
├── config.py       # Settings from environment
├── models.py       # ChatMessage, UserProfile, Memory, MemoryCandidate, LLMRequest, enums
├── session.py      # SessionStore: deque per (user_id, session_id)
├── brain.py        # SharedBrain protocol, BrainUnavailable, InMemoryBrain, Neo4jBrain (+ Cypher)
├── astrology.py    # sun_sign(date_of_birth) stub
├── context.py      # classify_query(), select_context(), build_request()
├── llm.py          # LLMProvider protocol, LLMError, AnthropicLLM, MockLLM (+ rule extractor)
├── memory.py       # validate candidates, route profile facts, apply upserts
├── chat.py         # ChatService orchestrator
└── main.py         # FastAPI app factory, schemas, routes, error handlers

tests/
├── conftest.py     # InMemoryBrain + MockLLM + TestClient fixtures
├── test_chat.py    # the 10 required scenarios through POST /chat
├── test_units.py   # classifier, extractor, sun sign, upsert semantics
└── test_neo4j.py   # same brain assertions against real Neo4j; skipped without NEO4J_TEST_URI

README.md
pyproject.toml / uv.lock
.env.example
Dockerfile
docker-compose.yml   # neo4j + app
```

One module per concern, one file each. The original layout split 11 concerns across 24 files; most would have held under 30 lines.

## 16. Testing Strategy

### 16.1 Unit tests

Focus on deterministic logic:

- context selection,
- memory validation,
- memory upsert decisions,
- follow-up detection,
- request schema validation.

### 16.2 Integration tests

The default test run uses `InMemoryBrain` and `MockLLM` and needs no services. `tests/test_neo4j.py` runs the same upsert, search and supersede assertions against a real Neo4j and is skipped unless `NEO4J_TEST_URI` is set.

Verify:

- memory persistence,
- retrieval by category,
- superseding/correction behavior.

### 16.3 API tests

Test the complete `/chat` flow with mocked LLM and repository dependencies.

## 17. Required Test Scenarios

| # | Scenario | Expected result |
|---:|---|---|
| 1 | New user | Response succeeds; no prior memory required |
| 2 | Durable memory creation | Explicit long-term goal is persisted |
| 3 | Memory retrieval | New session retrieves stored career goal |
| 4 | Follow-up question | Recent context resolves "Why do you say that?" |
| 5 | Cross-session persistence | Prior memory remains available after session change |
| 6 | Irrelevant memory | Unrelated facts are excluded from context |
| 7 | User correction | New explicit fact supersedes stale active value |
| 8 | Missing profile | Response succeeds without requiring all profile fields |
| 9 | LLM failure | Graceful error/fallback, no unsafe memory mutation |
| 10 | Graph failure | Response may degrade to short-term/profile context |

## 18. Evaluation Harness

A minimal evaluation fixture can store cases like:

```json
{
  "name": "career_recall",
  "setup_memories": [
    {
      "key": "career.goal",
      "value": "switch jobs",
      "target_timeframe": "2027"
    }
  ],
  "query": "What do you remember about my career goals?",
  "expected_context": ["career.goal"],
  "expected_facts": ["switch jobs", "2027"]
}
```

For each case, compare:

1. baseline response without Shared Brain retrieval,
2. personalized response with retrieval,
3. selected context against expected context.

A simple pass/fail or 0–2 rubric is sufficient for the assignment.

## 19. Observability

Log structured events, not full prompts by default.

Suggested event fields:

```text
request_id
user_id
session_id
query_category
memories_retrieved
memories_selected
context_used
memory_updates
latency_ms
llm_provider
error_type
```

Do not log sensitive profile values unnecessarily. Production logging should support redaction and access controls.

## 20. Security and Privacy Considerations

The Shared Brain is persistent user intelligence and therefore should be treated as user data.

Baseline controls:

- Validate `user_id` and enforce authorization in a real deployment.
- Do not allow arbitrary user access to another user's graph nodes.
- Parameterize Cypher queries.
- Do not include credentials in source code.
- Store LLM/Neo4j credentials in environment variables or a secret manager.
- Minimize sensitive data sent to the LLM.
- Avoid logging raw memory contents unless needed for development.

The assignment does not define authentication or authorization requirements; these controls are production considerations rather than MVP acceptance criteria.

## 21. Performance and Context Budgets

The most important performance control is limiting retrieval and prompt size.

Suggested defaults:

```text
Recent messages:      <= 10
Long-term memories:   <= 8
Profile fields:       only relevant fields
Astrology fields:     only relevant fields
```

Expected synchronous path:

```text
FastAPI validation
 -> Neo4j retrieval
 -> context selection
 -> one LLM generation call
 -> optional memory extraction/update
```

The design should avoid unnecessary LLM calls. Query classification should remain deterministic unless an ambiguous query benefits from model-based classification.

## 22. Transaction and Consistency Model

Memory writes should be idempotent at the logical-key level.

For example:

```text
(user_id, memory.category, memory.key)
```

should identify a logical fact. Neo4j constraints should prevent duplicate user identities; application logic should prevent duplicate active memories for the same logical key.

For corrections:

```text
old active memory -> SUPERSEDED
new memory      -> ACTIVE
```

This makes retrieval deterministic and preserves history/provenance.

## 23. Production Evolution Path

### Phase 1 — Assignment MVP

- In-memory session store
- Neo4j Shared Brain
- One LLM provider
- Rules + structured LLM memory extraction
- Simple retrieval ranking
- Automated tests

### Phase 2 — Production hardening

- Redis for session context
- Postgres or event log for message persistence if required
- Queue/background worker for memory extraction
- LLM fallback provider
- Authentication/authorization
- Rate limits
- Prompt/version management
- Metrics and tracing

### Phase 3 — Intelligent memory

- Embedding-based semantic retrieval
- Hybrid graph + vector retrieval
- Memory importance and decay
- Conflict resolution
- Conversation summarization
- Personalized multilingual responses
- Advanced graph traversal

## 24. Key Trade-offs

| Decision | Choice | Reason |
|---|---|---|
| Graph DB | Neo4j | Explicitly preferred by assignment; natural fit for user relationships |
| Session history | In-memory MVP | Fast to build; interface allows Redis later |
| Retrieval | Category + confidence + recency | Explainable and feasible in three hours |
| Memory extraction | Structured LLM + deterministic validation | Better flexibility while protecting against obvious low-value memories |
| Astrology | Stub/profile field | Assignment explicitly says full engine is not required |
| Memory correction | Supersede old active value | Preserves provenance and avoids ambiguous retrieval |
| LLM provider | Adapter interface | Avoid vendor lock-in |
| Async memory update | Synchronous MVP | Simpler end-to-end demo; background worker later |

## 25. Implementation Order for the 3-Hour Window

### 0:00–0:25

- Scaffold FastAPI project.
- Define domain models and API schemas.
- Add config and dependency injection.

### 0:25–1:10

- Implement Neo4j connection/repository.
- Implement user/profile and memory graph writes.
- Add core retrieval query.

### 1:10–1:45

- Implement session context store.
- Implement query categorization/context selection.
- Implement prompt builder.

### 1:45–2:15

- Implement LLM interface and provider adapter.
- Connect `/chat` end-to-end.

### 2:15–2:40

- Implement memory extraction + validation + upsert/correction.
- Add degraded paths.

### 2:40–3:00

- Add 8–10 tests.
- Write README.
- Run sample requests and capture sample responses.

## 26. Definition of Done

The technical implementation is ready for submission when:

- `POST /chat` works end-to-end.
- Neo4j stores and retrieves durable user information.
- Follow-up messages use short-term context.
- New sessions retrieve previous memories.
- Irrelevant memories are filtered out.
- User corrections update the active memory.
- LLM/provider and Neo4j failure paths are handled.
- Tests cover the required scenarios.
- README documents architecture, schema, memory strategy, selection approach, trade-offs, and production considerations.

## 27. Revision Notes (v2)

Changes made to this document before implementation, with the reason for each:

| Section | Change | Why |
|---|---|---|
| §4.1, §15 | 24-file package tree flattened to 11 modules | Most planned files would hold under 30 lines; one file per concern is easier to navigate and review |
| §4.3 | `SessionContextStore` Protocol dropped; one concrete `SessionStore` | One implementation; a Protocol with a single implementer documents nothing |
| §4.4 | `supersede_memory` folded into `upsert_memory`, which returns an outcome | Superseding is a decision the upsert makes; two calls make the write non-atomic |
| §4.4, §16 | `InMemoryBrain` added as a first-class implementation | Tests and no-Neo4j demo runs; it is also the executable reference for the Cypher semantics |
| §4.5 | Classification is deterministic only; ambiguous falls to `general` | Saves an LLM round-trip per request; the system prompt already handles residual noise |
| §4.6 | Weighted score formula replaced by the retrieval query's filter and order | The formula had no semantic signal to weigh until embeddings exist |
| §4.7 | `LLMRequest` reduced to `system_prompt` + `messages` | Context rendered once in the prompt builder; providers stay thin and the prompt is testable |
| §5.1 | Node model reduced to `User`, `Profile`, `Memory` | §6 already recommended generic memories; the typed-node list contradicted it |
| §7.0 | Profile facts stated in chat route to the `Profile` node | Lets a DOB stated in chat produce a sun sign; keeps supersede semantics for the language example |
| §10 | Providers: Anthropic (`claude-opus-5`, structured outputs) and an OpenAI-compatible adapter (`OPENAI_BASE_URL`, `json_object` mode) | Not vendor-locked: the same service runs on OpenAI, a local Ollama, vLLM or a gateway by changing env vars |
| §13 | Failed LLM turn is not appended to history; memory write skipped after a read failure | Keeps session history alternating and avoids logging one outage twice |
| §14 | `context_used` given an exact definition | Tests and the evaluation harness assert on it |

---

## Appendix A — Example Neo4j Constraints and Indexes

Illustrative Cypher:

```cypher
CREATE CONSTRAINT user_id_unique IF NOT EXISTS
FOR (u:User) REQUIRE u.id IS UNIQUE;

CREATE CONSTRAINT memory_id_unique IF NOT EXISTS
FOR (m:Memory) REQUIRE m.id IS UNIQUE;

CREATE INDEX memory_category IF NOT EXISTS
FOR (m:Memory) ON (m.category);

CREATE INDEX memory_key IF NOT EXISTS
FOR (m:Memory) ON (m.key);
```

## Appendix B — Example Retrieval Query

```cypher
MATCH (u:User {id: $user_id})-[:HAS_MEMORY]->(m:Memory)
WHERE m.status = 'ACTIVE'
  AND ($category = 'general' OR m.category = $category)
RETURN m {
  .id,
  .key,
  .category,
  .value,
  .target_timeframe,
  .confidence,
  .updated_at
}
ORDER BY m.confidence DESC, m.updated_at DESC
LIMIT $limit;
```

## Appendix C — Example Memory Upsert Semantics

```text
logical_key = (user_id, category, key)

if no active memory:
    create
elif existing.value == candidate.value:
    refresh updated_at / provenance if needed
else:
    mark existing SUPERSEDED
    create new ACTIVE memory
```

## Appendix D — Assignment Alignment

This TDD is grounded in the supplied MyNaksh assignment requirements for FastAPI/Python, a persistent graph-based Shared Brain, short-term and long-term memory separation, relevant context selection before the LLM, provider abstraction, memory updates, automated tests, error handling, and the explicit three-hour end-to-end priority. See the source assignment for the authoritative wording.
