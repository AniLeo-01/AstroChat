# Orchestration and short-term context

`ChatService.chat()` in `app/chat.py` is the one coroutine that runs the assignment's flow, Chat → Context Selection → Shared Brain → LLM → Response → Memory Update, for every `POST /chat`. It owns ordering and failure policy and nothing else: it decides what to read before generating, what to record after generating, and what to skip when a dependency is down, and it delegates every other decision to a sibling module (`classify` and `select_context` for the prompt, `SharedBrain` for the graph, `LLMProvider` for generation and extraction, `remember` for the write). `SessionStore` in `app/session.py` is the short-term memory it reads from and writes to: the last `RECENT_LIMIT` messages per `(user_id, session_id)`, held in a process-local `collections.deque`. Together they are the layer that keeps "what was just said" and "what is worth remembering" in two different stores, which PRD §2 names as the two distinct context problems the product has to solve.

**Files:** `app/chat.py`, `app/session.py`

**Depends on:** [Domain model](03-domain-model.md) (`ChatMessage`, `Category`), [Query understanding and context selection](04-query-understanding-and-context-selection.md) (`classify`, `select_context`), [Shared Brain](05-shared-brain.md) (`SharedBrain`, `BrainUnavailable`), [Memory update](06-memory-update.md) (`remember`), [LLM providers](07-llm-providers.md) (`LLMProvider`, `LLMError`). **Used by:** [API layer](01-api-layer.md) (`create_app` in `app/main.py` builds one `ChatService` per process and the `/chat` route calls `chat()`). Budgets arrive through [Configuration and deployment](08-configuration-and-deployment.md); the tests are catalogued in [Testing and verification](09-testing-and-verification.md). Layer index: [README](README.md).

## What this layer is

Two modules with no infrastructure code of their own. `app/chat.py` imports no Cypher and no provider SDK; `app/session.py` imports only `collections` and `ChatMessage`. This is the boundary TDD §4.2 draws: "orchestration, not infrastructure-specific Cypher or provider SDK logic".

| Symbol | Module | Role |
|---|---|---|
| `ChatResult` | `app/chat.py` | Dataclass returned by `chat()`: `response`, `context_used`, `memory_updates`, `degraded` |
| `ChatService` | `app/chat.py` | Holds the three collaborators (`brain`, `llm`, `sessions`) and two budgets (`memory_limit`, `min_confidence`); one public coroutine, `chat()` |
| `SessionStore` | `app/session.py` | Short-term store with two synchronous methods, `recent()` and `append()` |

`ChatService` is constructed once per process inside the `lifespan` of `create_app` in `app/main.py`, after the brain and LLM have been chosen:

```python
        app.state.chat = ChatService(app.state.brain, app.state.llm, SessionStore(settings.recent_limit),
                                     settings.memory_limit, settings.min_confidence)
```

The brain and LLM are injected rather than built here, which is what lets `tests/conftest.py` pass `InMemoryBrain()` and `MockLLM()` into `create_app` and drive the real orchestrator without services. Apart from the `SessionStore` it holds, `ChatService` has no state of its own.

What is deliberately not in this layer: prompt text and the `context_used` vocabulary (`app/context.py`), candidate validation and profile routing (`app/memory.py`), HTTP schemas and status codes (`app/main.py`), driver and SDK error translation (`app/brain.py`, `app/llm.py`), and any retry, fallback or circuit-breaking logic (the only retrying in the service is the Neo4j driver's own transient-error retry, bounded to 3s in `app/brain.py`).

## Why it exists

### Requirements it satisfies

| Source | Requirement | Where it lands in this layer |
|---|---|---|
| PRD §2 | Two distinct problems: short-term context for follow-ups, long-term memory across sessions "without storing every message as permanent memory" | Two stores with different keys and lifetimes; `chat()` is the only code that touches both |
| PRD FR-4 | Keep the latest 8–12 messages per session; never send lifetime history | `SessionStore(limit=RECENT_LIMIT)`, default 10, cap enforced by `deque(maxlen=...)` |
| PRD FR-5 | Classify, retrieve candidates, select relevant context, build a bounded LLM context, in that order | Steps 2 to 5 of `chat()` below, in that order |
| PRD FR-7 | After a response, evaluate whether the user's latest message contains durable information; preserve provenance | Extraction runs after `generate`, on `message` only, with `current.id` as `source_message_id` |
| PRD FR-8 | Safe fallback for LLM failure and Neo4j failure | `LLMError` propagates to a `503`; `BrainUnavailable` is caught twice and becomes `degraded: true` |
| PRD §5.2 | A same-session follow-up about career uses profile, career context and the recent conversation | Non-follow-up turns read the graph and carry `recent` into the prompt |
| PRD §5.3, §8 | "Why do you say that?" should rely primarily on short-term context "rather than retrieving the entire user graph"; for follow-ups "recent session context should dominate" | `Category.FOLLOW_UP` skips the Shared Brain read entirely |
| PRD §13 | "Graph unavailable: continue with recent context/profile when possible; expose degraded mode". "LLM unavailable: ... do not mutate memory from failed generations" | The two `except BrainUnavailable` blocks; no write of any kind happens before `generate` returns |
| TDD §2 | Principles 1 (separate short-term and long-term memory), 2 (retrieve before generating), 3 (user statements are the source of truth), 5 (bounded context), 6 (graceful degradation), 7 (debuggability) | Store separation; read-then-generate ordering; extraction from `message` only; `RECENT_LIMIT` and `MEMORY_LIMIT` passed down; degraded paths; `context_used` and the log line |
| TDD §4.2 | The orchestrator "owns the end-to-end flow" listed there | `chat()` is that list minus its first item, in that order; validation happens in `app/main.py` (TDD §4.1) |
| TDD §4.3 | Session store: in-memory dict keyed by `(user_id, session_id)`, `deque(maxlen=RECENT_LIMIT)`, synchronous methods, one concrete class | `SessionStore` as written |
| TDD §9 | Load last N, include the current message separately, do not convert short-term messages into long-term memory, append user and assistant after the response | `recent` and `current` are separate arguments to `select_context`; extraction reads `message`, not the deque; `append` happens after `generate` |
| TDD §12 | The 14-step request sequence; extraction "may execute synchronously" in the MVP | Steps 3 to 12 of that sequence all map onto `chat()`, though not one-to-one: steps 6 and 7 and steps 11 and 12 each collapse into one call, and minting the message id has no §12 step; extraction is awaited inline |
| TDD §13.2, §13.3 | Neo4j down: `degraded: true`, skip the post-response write when the pre-response read failed. LLM down: `503`, nothing recorded; extraction failure after a good response is logged and skipped | The `not degraded` guard; `generate` outside any `try`; `except LLMError` around extraction |

### The problem it solves

Each sibling module knows how to do one thing and nothing about the others. Without this layer nothing would sequence them, and the answer to "what happens when Neo4j is down" or "what happens when the LLM is down" would be spread across modules that cannot see the whole request. `chat()` makes those decisions once, in one place. That is why `app/main.py` needs only two exception handlers and no policy, why `app/brain.py` and `app/llm.py` translate their errors and stop, and why the response can honestly report `degraded` and `memory_updates`: only the orchestrator knows whether the read failed, whether the write ran, and what it returned.

The store separation is the other half of the problem. Short-term context is verbatim turns from both roles, scoped to one session, lost on restart, and never a source of memory. Long-term memory is extracted user facts, scoped to the user, persistent, and the only thing retrieved in a new session. Keying the two differently, `(user_id, session_id)` for the deque and `user_id` for the graph, is what makes PRD §5.3 (same-session follow-up) and PRD §5.4 (new-session recall) both work at once; `tests/test_chat.py::test_3_new_session_retrieves_memory` asserts exactly that combination, `career.goal` present and `recent_conversation` absent in a fresh session.

## How it works

### Inputs, outputs, side effects

`chat(user_id: str, session_id: str, message: str) -> ChatResult`. The three strings arrive already validated by `ChatRequest` in `app/main.py` (identifiers 1–128 characters, message 1–4000 characters); the orchestrator does no validation of its own. Side effects, in order: at most one `SessionStore.append` (only after a successful generation), at most one `remember` call (conditional, see step 8), and one `log.info` line plus at most one `log.warning` line (the three warning statements are mutually exclusive: a read failure skips the block that holds the other two, and those two are alternative `except` clauses). Of the two exception types this layer handles, only `LLMError` escapes, and only from `generate`.

### `ChatService.chat()` step by step

1. **Load recent turns.** `recent = self.sessions.recent(user_id, session_id)`. A fresh `list[ChatMessage]` with at most `RECENT_LIMIT` entries, empty for a session the process has not seen. The read does not create a session entry (see `SessionStore` below). This is TDD §12 step 3 and TDD §9 step 1.

2. **Classify.** `category = classify(message)` returns a `Category` `StrEnum` from the keyword classifier in `app/context.py`. The orchestrator inspects only two of its values: `Category.FOLLOW_UP` (skip the graph read and the memory update) and `Category.GENERAL` (search all memory categories). Every other value is passed through to `search_memories` and `select_context` unchanged. TDD §12 step 4.

3. **Conditional Shared Brain read.**

   ```python
           # Retrieve: follow-ups rely on recent turns only; everything else consults the Shared Brain.
           profile, memories, degraded = None, [], False
           if category is not Category.FOLLOW_UP:
               try:
                   profile = await self.brain.get_profile(user_id)
                   search = None if category is Category.GENERAL else category
                   memories = await self.brain.search_memories(user_id, search, self.memory_limit)
               except BrainUnavailable as e:
                   log.warning("brain unavailable, answering from short-term context user=%s: %s", user_id, e)
                   degraded = True
   ```

   For a follow-up the block is skipped and the three defaults stand: no profile, no memories, not degraded. Otherwise the profile is read first, then active memories filtered by category (`None` for `general`, which `InMemoryBrain.search_memories` and the `SEARCH_MEMORIES` Cypher both treat as "all categories"), limited to `self.memory_limit`. A `BrainUnavailable` from either call is logged once at `WARNING`, sets `degraded = True`, and leaves whichever defaults were not yet overwritten. Because the two reads are sequential, a failure inside `search_memories` after `get_profile` succeeded leaves `profile` populated and `memories` empty (inferred from the assignment order; no test covers this split). TDD §12 step 5.

4. **Mint the current message with its provenance id.** `current = ChatMessage("user", message, id=str(uuid.uuid4()))`. This is the only place in the service where a message id is generated. The assistant message built in step 7 keeps the dataclass default `id=""`, so an assistant turn can never be cited as a memory source.

5. **Select context.** `selection = select_context(category, current, recent, profile, memories)` returns a `Selection` whose `request` is the `LLMRequest` (system prompt plus `[*recent, current]`) and whose `context_used` is the list the API reports. The orchestrator never reads the prompt; it forwards `request` and passes `context_used` through unchanged. TDD §12 steps 6 and 7 (the selector and the prompt builder are one function in the code).

6. **Generate.** `reply = await self.llm.generate(selection.request)`. There is no `try` around this call. An `LLMError` propagates out of `chat()` to the `_llm_error` handler in `app/main.py`, which answers `503` with `{"detail": "LLM unavailable: ..."}`. Everything before this line was a read or local construction, so a failed generation leaves no trace: no session append, no memory write, no log line from this function. TDD §12 steps 8 and 9, TDD §13.3.

7. **Append both turns.**

   ```python
           # Generate. LLMError propagates to the API as 503; the failed turn is not recorded anywhere.
           reply = await self.llm.generate(selection.request)
           self.sessions.append(user_id, session_id, current, ChatMessage("assistant", reply))
   ```

   One synchronous call carrying both messages, with no `await` between them, so the store never holds a user turn without its answer. `current` is stored with its uuid; the assistant message is stored with `id=""`. TDD §12 step 10, TDD §9 step 5.

8. **Conditional memory extraction and write.**

   ```python
           # Memory update. Skipped for follow-ups (nothing durable to learn) and when the brain is already down.
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

   Extraction is given the raw user `message` and today's date (used by the extractors to resolve "next year"), never `reply` and never the session history. `remember` validates the candidates against `min_confidence`, routes profile keys to the Profile node, upserts the rest with `current.id` as `source_message_id`, and returns the count of writes that changed state. An `LLMError` from extraction is logged and swallowed; `updates` stays `0` and `degraded` is unchanged. A `BrainUnavailable` from the write is logged, `updates` stays `0` (the assignment never completes), and `degraded` becomes `True`. TDD §12 steps 11 and 12.

9. **Structured log line.** One `INFO` record per successful turn:

   ```python
           log.info("chat user=%s session=%s category=%s context_used=%s memory_updates=%d degraded=%s",
                    user_id, session_id, category, selection.context_used, updates, degraded)
   ```

   It carries identifiers and outcomes, not the message, the prompt or the reply. The `WARNING` lines in steps 3 and 8 include `str(e)`, which for `BrainUnavailable` is the driver's connectivity message and for `LLMError` is the provider's outage message, not user content.

10. **Return.** `return ChatResult(reply, selection.context_used, updates, degraded)`. The `/chat` route in `app/main.py` copies the four fields into `ChatResponse` and adds the echoed `user_id` and `session_id`.

### How `degraded` is computed

`degraded` starts `False` and is set `True` in exactly two places, both `except BrainUnavailable` clauses. It is never reset.

| Situation | `degraded` |
|---|---|
| Follow-up turn (no brain contact at all) | `False` |
| Brain read succeeded, write succeeded or was skipped | `False` |
| Brain read raised `BrainUnavailable` (write then skipped by the guard) | `True` |
| Brain read succeeded, extraction raised `LLMError` | `False` |
| Brain read succeeded, `remember` raised `BrainUnavailable` | `True` |

An extraction failure does not mark the response degraded. The repository `README.md` "Failure modes" table records that outcome as "Response returned; write skipped and logged"; the reasoning that `degraded` means "answered without the Shared Brain", which an extraction failure does not affect, is inferred from the code and not recorded elsewhere.

### How `memory_updates` is computed

`updates` starts `0` and is overwritten only by a completed `remember` call.

| Situation | `memory_updates` |
|---|---|
| Follow-up turn | `0` (block skipped) |
| Read failed (`degraded` already `True`) | `0` (block skipped) |
| Extraction raised `LLMError` | `0` |
| `remember` raised `BrainUnavailable` | `0`, even if `upsert_profile` inside `remember` had already succeeded (inferred: the exception aborts the assignment, so partial writes are not counted) |
| `remember` returned | Its return value: the number of profile fields written plus the number of memory upserts whose outcome was not `"unchanged"` |

The `remember` accounting lives in `app/memory.py`; `tests/test_chat.py::test_2_first_message_creates_durable_memory_and_profile` pins the value `4` for the PRD sentence (name, date of birth, birth place to the Profile node, one `career.goal` memory). It follows that `degraded` implies `memory_updates == 0`, and `category is FOLLOW_UP` implies both `memory_updates == 0` and `degraded == False`.

### Which exceptions this layer sees

| Exception | Defined in | Raised into `chat()` by | What `chat()` does |
|---|---|---|---|
| `BrainUnavailable` | `app/brain.py` | `get_profile`, `search_memories` (step 3); `upsert_profile`, `upsert_memory` via `remember` (step 8) | Catches in both places; degrades |
| `LLMError` | `app/llm.py` | `generate` (step 6); `extract_memories` (step 8) | Propagates from `generate`; catches from `extract_memories` |

`app/chat.py` imports exactly these two exception types and no others. It never imports `neo4j.exceptions`, `anthropic` or `openai`. Outage-class failures are translated before they reach it: `Neo4jBrain._query` and `Neo4jBrain.upsert_memory` map `ServiceUnavailable`, `SessionExpired`, `AuthError` and `OSError` to `BrainUnavailable`; `_guarded()` in `app/llm.py` maps `APIConnectionError`, `RateLimitError` and 5xx `APIStatusError` to `LLMError`, and the providers raise `LLMError` themselves for refusals, empty responses and invalid extraction JSON. `InMemoryBrain` and `MockLLM` raise the same two types when their `fail` flag is set, which is how `tests/test_chat.py` exercises both paths.

Failures that are not outages are not this layer's concern and it does not catch them. A 4xx `APIStatusError` is deliberately re-raised by `_guarded()` ("our bug, not an outage", per the repository `CLAUDE.md`), and a Neo4j error outside the connectivity tuple (a Cypher error, say) is not translated. Either passes through `chat()` uncaught to FastAPI's default 500 handling. Whether the turn was appended in that case depends on where it happened: before step 7, nothing is recorded; after it, the session holds the pair.

### Main path (non-follow-up, everything healthy)

```text
route /chat ──► ChatService.chat(user_id, session_id, message)
  1  sessions.recent(user_id, session_id)            ──► list[ChatMessage]  (≤ RECENT_LIMIT)
  2  classify(message)                               ──► Category (not FOLLOW_UP)
  3  brain.get_profile(user_id)                      ──► UserProfile | None
     brain.search_memories(user_id, cat|None, memory_limit) ──► list[Memory] (≤ MEMORY_LIMIT)
  4  ChatMessage("user", message, id=uuid4)
  5  select_context(category, current, recent, profile, memories) ──► Selection
  6  llm.generate(selection.request)                 ──► reply
  7  sessions.append(user_id, session_id, current, ChatMessage("assistant", reply))
  8  llm.extract_memories(message, today)            ──► list[MemoryCandidate]
     remember(brain, user_id, candidates, current.id, min_confidence) ──► int
  9  log.info(...)
 10  ChatResult(reply, context_used, updates, degraded=False)
```

### Follow-up path

```text
route /chat ──► ChatService.chat(user_id, session_id, "Why do you say that?")
  1  sessions.recent(...)                            ──► the stored pairs
  2  classify(...)                                   ──► Category.FOLLOW_UP
  3  (skipped: profile=None, memories=[], degraded=False)
  4  ChatMessage("user", message, id=uuid4)
  5  select_context(FOLLOW_UP, current, recent, None, [])
        ──► context_used == ["recent_conversation"] (or [] if the session is new)
  6  llm.generate(...)                               ──► reply
  7  sessions.append(...)
  8  (skipped: nothing extracted, nothing written)
  9  log.info(... memory_updates=0 degraded=False)
 10  ChatResult(reply, ["recent_conversation"], 0, False)
```

Two consequences worth stating. First, `classify` tries the keyword categories before the follow-up patterns (see `_KEYWORDS` and `_FOLLOW_UP` in `app/context.py`), so a message containing any category keyword is never a follow-up; the shortcut fires only for messages with no domain signal, such as "Why do you say that?" or "Tell me more". Second, TDD §9 step 3 says a follow-up should "prioritize immediate previous assistant/user turns"; the code implements that by omission rather than reordering: all recent turns are sent for every category, and for a follow-up they are the only context, so they dominate (inferred).

### `SessionStore` operations

```python
class SessionStore:
    """Short-term context: the last N messages per (user_id, session_id).

    ponytail: process-local dict; swap for Redis when running more than one replica.
    """

    def __init__(self, limit: int = 10):
        self._sessions: dict[tuple[str, str], deque[ChatMessage]] = defaultdict(lambda: deque(maxlen=limit))

    def recent(self, user_id: str, session_id: str) -> list[ChatMessage]:
        return list(self._sessions.get((user_id, session_id), ()))

    def append(self, user_id: str, session_id: str, *messages: ChatMessage) -> None:
        self._sessions[(user_id, session_id)].extend(messages)
```

| Aspect | Behaviour in the code |
|---|---|
| Key | The tuple `(user_id, session_id)`. The same `session_id` under two users is two independent histories |
| Storage | `collections.defaultdict` whose factory is `lambda: deque(maxlen=limit)`; one deque per key, created on the first `append` |
| `recent` | Uses `dict.get(key, ())`, which does not trigger the `defaultdict` factory, so reading an unknown session creates nothing and returns `[]`. It returns `list(...)`, a copy, so callers cannot mutate the deque |
| `append` | `*messages` are added with `deque.extend`; when the deque is full, `maxlen` discards the oldest entries from the left. The cap is enforced at write time, so `recent` never has to truncate |
| Element type | `ChatMessage(role, content, id="")` from `app/models.py`; the store does not inspect roles or ids |
| Concurrency | Methods are synchronous with no `await` inside, so on FastAPI's single event loop an `append` cannot interleave with another request's `append` (inferred). No locks |
| Lifetime | Process-local; there is no delete, expiry or size limit on the number of sessions, only on messages per session |

## Contracts and invariants

### `ChatResult`

```python
@dataclass
class ChatResult:
    response: str
    context_used: list[str]
    memory_updates: int = 0
    degraded: bool = False
```

| Field | Type | Meaning | Source |
|---|---|---|---|
| `response` | `str` | The provider's reply text, unmodified | `llm.generate` |
| `context_used` | `list[str]` | Exactly what `select_context` injected: `recent_conversation`, `user_profile`, `astrology` in that order when present, then memory keys in retrieval order (TDD §14) | `Selection.context_used`, passed through |
| `memory_updates` | `int` | Writes that changed state this turn, `0` whenever the update block did not complete | `remember` return value or `0` |
| `degraded` | `bool` | The Shared Brain could not be reached on the read or on the write | The two `except BrainUnavailable` clauses |

TDD §14 classifies `memory_updates` and `degraded` as "observability fields rather than core assignment requirements", and PRD FR-1 allows "additional diagnostic metadata where useful". They are nonetheless part of the response schema and asserted by tests.

### Invariants other layers may rely on

1. **Session history alternates `user`, `assistant` and begins with `user`.** Every append is one user turn followed by its assistant turn in a single synchronous call; a failed generation appends nothing; no other code writes to the store. With the default even `RECENT_LIMIT` of 10, dropping the oldest two on overflow keeps the window starting with a user turn. An odd limit would leave the window starting with an assistant turn once it fills (inferred; keep `RECENT_LIMIT` even). `tests/test_chat.py::test_4_follow_up_uses_recent_context_only` asserts the roles `["user", "assistant", "user", "assistant", "user"]` in the request the LLM received.
2. **The recent list is capped.** `len(recent) <= RECENT_LIMIT` always, so `select_context` does not truncate, and the LLM receives at most `RECENT_LIMIT + 1` messages (TDD §4.7 and §9 step 2: recent turns followed by the current message; §21 gives the budgets).
3. **A turn is in history if and only if its generation succeeded.** Consequently every stored user turn has a reply, and the failed turn is invisible to the next request (`test_9_llm_failure_returns_503_and_mutates_nothing`).
4. **Only the user's current message is ever extracted from**, and only after a successful reply. Assistant text and prior turns never reach `extract_memories` (TDD §2 principle 3, TDD §9 step 4).
5. **`source_message_id` is a fresh `uuid4` string per request**, non-empty, minted before `select_context` and passed unchanged to `remember`. Every memory written from one message shares it; no two requests share it. It is not returned in the API response and, since no `:Message` node exists (TDD §5.1 defers them), it is a correlation token rather than a foreign key (inferred).
6. **`BrainUnavailable` never escapes `chat()`.** Both places it can arise are wrapped. The `_brain_error` handler in `app/main.py` exists for `/users` and `/users/{user_id}/memories`, which call the brain directly, not for `/chat`.
7. **`LLMError` escapes `chat()` only from `generate`.** From `extract_memories` it is caught.
8. **`degraded` implies `memory_updates == 0`; `FOLLOW_UP` implies `memory_updates == 0` and `degraded == False`.** See the two tables above.
9. **The orchestrator does not alter `context_used`, the prompt or the reply.** What `select_context` and `generate` return is what the API reports, so the contracts in [Query understanding and context selection](04-query-understanding-and-context-selection.md) hold at the HTTP boundary unchanged.

## Design decisions and alternatives rejected

| Decision | Chosen | Rejected | Why (source) |
|---|---|---|---|
| When to run memory extraction and the write | Synchronously, inline, after the reply is known and the turn is appended | A queue or background worker (TDD §23 Phase 2) | "Simplest to test end to end; a queue is a one-line move of the last block in `chat.py`" (repository `README.md`, Trade-offs). TDD §12: extraction "may execute synchronously to simplify implementation and testing"; TDD §24 chooses "Synchronous MVP" with the reason "Simpler end-to-end demo; background worker later". A side effect (inferred): `memory_updates` can be exact in the same response, which a worker could not provide |
| Follow-ups and the graph | `Category.FOLLOW_UP` skips `get_profile` and `search_memories` | Always reading the graph and letting the selector drop it | PRD §5.3: rely on short-term context "rather than retrieving the entire user graph"; TDD §4.6 table gives `follow_up` no memories and no profile fields; PRD §13 lists "irrelevant memories pollute responses" as a risk. Skipping the read also saves the round-trip (inferred) |
| Follow-ups and extraction | `FOLLOW_UP` skips `extract_memories` and `remember` | Extracting from every turn | Code comment in `app/chat.py`: "nothing durable to learn". A follow-up is by construction a reference to the previous turn ("why?", "tell me more"), and because keyword categories win over the follow-up patterns in `classify`, a message that states a fact with a domain keyword is never routed here (inferred from `app/context.py`) |
| A failed LLM turn | Not appended, not extracted, `503` | Appending the user message anyway; returning deterministic fallback text | TDD §13.3: "Do not append the failed turn to session history and do not persist memories: nothing about the request is recorded"; TDD §27 (§13 row): "Keeps session history alternating". PRD §13 had suggested "a deterministic fallback message"; TDD §13.3 rejects it: "there is no deterministic fallback text that is honest for an astrology question" |
| Write after a failed read | Skipped via the `not degraded` guard | Attempting the write regardless | TDD §13.2: "Skip the post-response memory write when the pre-response read already failed: one logged failure per request, not two"; TDD §27 (§13 row): "avoids logging one outage twice". Attempting it would also spend an extraction LLM call and another ~3s driver timeout on a write that is very likely to fail (inferred; the 3s figure is TDD §13.2) |
| Extraction failure after a good reply | Logged, `memory_updates = 0`, `degraded` unchanged, `200` | Failing the request; flagging `degraded` | TDD §13.3: "the user still gets the response; the memory write is skipped and logged"; repository `README.md`, Failure modes: "Response returned; write skipped and logged". Not flagging `degraded` is inferred: the reply itself used the Shared Brain normally |
| Short-term store backing | In-process `dict` of `deque(maxlen=limit)` | Redis (TDD §23 Phase 2); Postgres or an event log for message persistence (TDD §23 Phase 2, "if required") | TDD §4.3: "In-memory dictionary keyed by `(user_id, session_id)` for the timed submission"; TDD §24: "Fast to build; interface allows Redis later"; repository `README.md`, Trade-offs: "Fast; the store is one class with two methods, so Redis is a drop-in". The `ponytail:` docstring names the ceiling: more than one replica |
| Store interface | One concrete `SessionStore` class | A `SessionContextStore` Protocol (the v1 design) | TDD §27 (§4.3 row): "One implementation; a Protocol with a single implementer documents nothing"; TDD §4.3: "one concrete class; no Protocol until a second implementation exists". By contrast `SharedBrain` and `LLMProvider` are Protocols because each has more than one implementation |
| Store method signatures | Synchronous `recent` and `append` | `async` methods from day one | TDD §4.3: "Methods are synchronous because the store is in-process; a Redis replacement would make them `async` and change call sites in one file (`chat.py`)" |
| Cap enforcement | `deque(maxlen=RECENT_LIMIT)` at write time | Slicing on read | TDD §4.3: "so the cap is enforced at write time". `recent` therefore stays a plain copy (inferred) |
| Provenance | A `uuid4` minted per user message, stored on each memory as `source_message_id` | Persisting messages as `:Message` nodes and linking to them | PRD §7.1 requires `source_message_id`; TDD §5.1 defers `:Message` nodes "until a query needs to traverse them". The uuid satisfies the field without a message table (inferred) |
| Error vocabulary | Two exception types, both defined by the modules that raise them | Catching driver or SDK exceptions in the orchestrator | TDD §4.4: "translated to `BrainUnavailable` so the orchestrator never imports Neo4j exceptions"; TDD §10: `_guarded()` maps outages to `LLMError`; TDD §2 principle 4 (provider independence) |

## Failure modes and degraded behavior

| Failure | Raised by | What `chat()` does | HTTP result (via `app/main.py`) | Session store | Shared Brain |
|---|---|---|---|---|---|
| Shared Brain unreachable before generation | `get_profile` or `search_memories` | Catches `BrainUnavailable`, logs `WARNING`, `degraded = True`, continues with `profile = None` (or the profile if it was read first) and `memories = []`; skips the memory block | `200`, `degraded: true`, `memory_updates: 0`, `context_used` holds only `recent_conversation` if there is history (plus profile tags in the partial-read case) | Both turns appended | Nothing written |
| LLM generation fails | `generate` | Nothing; `LLMError` propagates | `503`, `{"detail": "LLM unavailable: ..."}` | Nothing appended | Nothing written |
| Extraction fails after a good reply | `extract_memories` | Catches `LLMError`, logs `WARNING`, `memory_updates` stays `0`, `degraded` stays `False` | `200`, `degraded: false`, `memory_updates: 0` | Both turns appended | Nothing written |
| Shared Brain unreachable at write | `upsert_profile` or `upsert_memory` inside `remember` | Catches `BrainUnavailable`, logs `WARNING`, `degraded = True` | `200`, `degraded: true`, `memory_updates: 0` | Both turns appended | Possibly partial: a profile upsert that completed before a later memory upsert failed stays written (inferred) |
| Non-outage error (SDK 4xx, non-connectivity Neo4j error, programming error) | Anywhere | Not caught | FastAPI default `500` | Appended only if the error came after step 7 | Whatever completed |
| Missing profile or empty memory | Not a failure | `profile = None`, `memories = []` are the normal shape | `200`; the tags simply do not appear in `context_used` | Both turns appended | Writes proceed normally |

Three properties of the degraded path follow from the code. It is stateless: no flag survives the request, so the very next request retries the brain (there is no circuit breaker; the `ponytail:` comment in `Neo4jBrain.__init__` in `app/brain.py` names that as the upgrade). It is bounded in time: with `Neo4jBrain`, the driver's 3s connection timeout and 3s retry window (TDD §13.2, [Shared Brain](05-shared-brain.md)) mean the read failure costs about three seconds, and because the write is then skipped the request pays that once. And short-term context keeps working throughout: `tests/test_chat.py::test_10_graph_failure_degrades` sends a follow-up after a degraded turn and gets `["recent_conversation"]`.

## Configuration

This layer reads no environment variables itself. `Settings.from_env()` in `app/config.py` parses them and `create_app` in `app/main.py` passes three of them in.

| Variable | `Settings` field | Default | Consumed here as | Effect |
|---|---|---|---|---|
| `RECENT_LIMIT` | `recent_limit` | `10` | `SessionStore(settings.recent_limit)`, the `maxlen` of every per-session deque | Upper bound on messages kept per `(user_id, session_id)` and on prior turns sent to the LLM. PRD FR-4 recommends 8–12, TDD §9 recommends 10. Keep it even so the window always starts with a user turn (inferred) |
| `MEMORY_LIMIT` | `memory_limit` | `8` | `ChatService.memory_limit`, forwarded as the `limit` argument of `brain.search_memories` | Upper bound on memories retrieved per non-follow-up turn, and therefore on the number of memory keys in `context_used` |
| `MIN_CONFIDENCE` | `min_confidence` | `0.6` | `ChatService.min_confidence`, forwarded to `remember`, which hands it to `validate` in `app/memory.py` | Candidates below it are dropped before any write, lowering `memory_updates` |

`ChatService.__init__` defaults (`memory_limit=8`, `min_confidence=0.6`) and `SessionStore.__init__`'s `limit=10` duplicate the `Settings` defaults so both classes can be constructed without a `Settings` object (inferred). `tests/conftest.py` builds the app with `Settings(brain="memory", llm_provider="mock")`, so every test runs with the three defaults above. `BRAIN`, `LLM_PROVIDER`, `LLM_MODEL`, `LLM_EFFORT`, `NEO4J_*` and the provider keys select which objects are injected into `ChatService`; they are described in [Configuration and deployment](08-configuration-and-deployment.md).

## Tests that pin this layer

No test constructs `SessionStore` or `ChatService` directly; every assertion goes through `create_app` and FastAPI's `TestClient` with `InMemoryBrain` and `MockLLM` injected (`tests/conftest.py`). The tests below are the ones whose assertions depend on this layer's behaviour.

| Test | The one thing it asserts about this layer |
|---|---|
| `tests/test_chat.py::test_1_new_user_succeeds_with_no_context` | A never-seen `(user, session)` yields `context_used == []` and `degraded is False`: `recent()` on an unknown key returns empty without creating or failing |
| `tests/test_chat.py::test_2_first_message_creates_durable_memory_and_profile` | `memory_updates == 4` is `remember`'s return value passed through unchanged, and the brain holds the resulting profile and `career.goal` memory after the `200` |
| `tests/test_chat.py::test_3_new_session_retrieves_memory` | A new `session_id` for the same user has no `recent_conversation` while `career.goal` is still retrieved: sessions are keyed by `(user_id, session_id)`, memories by `user_id` |
| `tests/test_chat.py::test_4_follow_up_uses_recent_context_only` | On a follow-up, `context_used == ["recent_conversation"]`, `memory_updates == 0`, the LLM received five alternating messages (two stored pairs plus the current turn), and no memory key reached the system prompt: the graph read and the extraction were both skipped |
| `tests/test_chat.py::test_5_memory_persists_across_sessions` | Memories written from session `s1` are retrieved in `s2` and `s3`: the long-term store is independent of the session key |
| `tests/test_chat.py::test_7_user_correction_supersedes` | A correction reports `memory_updates == 1`, and a later session receives only the active value |
| `tests/test_chat.py::test_8_missing_profile_is_fine` | `profile = None` produces a normal `200` with no `user_profile` or `astrology` tag |
| `tests/test_chat.py::test_9_llm_failure_returns_503_and_mutates_nothing` | `LLMError` from `generate` becomes a `503` whose detail mentions the LLM; the brain has no memories and no profile afterwards; the next message in the same session shows no `recent_conversation`, so the failed turn was never appended |
| `tests/test_chat.py::test_10_graph_failure_degrades` | With the brain failing, a non-follow-up turn returns `200` with `degraded is True`, `memory_updates == 0` and `context_used == []`; a follow-up in the same session still gets `["recent_conversation"]` |
| `tests/test_chat.py::test_profile_endpoint_feeds_astrology_context` | `context_used` reaches the API exactly as `select_context` produced it (`["user_profile", "astrology"]`) |
| `tests/test_units.py::test_neo4j_connection_failure_is_brain_unavailable` | A real driver connection failure surfaces as `BrainUnavailable`, the type the two `except` clauses in `chat()` depend on |

## Known limits and future work

### `ponytail:` markers in the covered files

`app/session.py`, in the `SessionStore` docstring:

```text
ponytail: process-local dict; swap for Redis when running more than one replica.
```

Upgrade path: TDD §4.3 (a Redis replacement "would make them `async` and change call sites in one file (`chat.py`)"), TDD §23 Phase 2 ("Redis for session context"), repository `README.md`, Production path ("Redis session store"). The two call sites are `self.sessions.recent(...)` and `self.sessions.append(...)` in `ChatService.chat()`.

`app/chat.py` contains no `ponytail:` comment.

### Other limits

| Limit | Consequence | Recorded path or note |
|---|---|---|
| Sessions never expire | Distinct `(user_id, session_id)` pairs accumulate until the process restarts; only messages per session are bounded | Inferred from the code. A Redis TTL would cover it |
| History is process-local | Lost on restart; not shared between replicas | TDD §23 Phase 2: "Postgres or event log for message persistence if required" |
| Extraction is on the request path | Every non-follow-up turn pays a second LLM round-trip before the response returns | TDD §12: extraction can be moved "so that user-facing latency is decoupled from the response path later"; repository `README.md`, Production path: "background worker for extraction" |
| The log line omits `request_id`, `latency_ms`, `llm_provider`, `error_type` | Correlating a request across services needs tracing that is not wired | TDD §19 lists those fields; repository `README.md`, Production path: "add tracing" |
| Concurrent requests on one session | Both may read the same `recent` before either appends; pairs are then appended in completion order. Alternation is preserved, cross-request ordering is not guaranteed | Inferred from the code |
| Odd `RECENT_LIMIT` | After the window fills it begins with an assistant turn | Inferred; default is even |
| No summarisation of long sessions | Context older than `RECENT_LIMIT` messages is simply gone | PRD §12 "nice to have"; TDD §23 Phase 3 "Conversation summarization" |
| `memory_updates` under-reports on a mid-write outage | A profile upsert that succeeded before a later `BrainUnavailable` is not counted | Inferred from the `remember` call being a single assignment |
| Partial read on outage | If `get_profile` succeeds and `search_memories` fails, the reply uses the profile while `degraded` is `True` | Inferred from the sequential reads in step 3 |
| `date.today()` for extraction | "Next year" resolves against the process's local date | Inferred |
| `source_message_id` cannot be resolved to text | Messages are not persisted, so the id links memories to each other but not to a stored message | TDD §5.1 defers `:Message` nodes |
