# Shared Brain (persistence layer)

The Shared Brain is AstroChat's long-term memory: a per-user graph of the facts, goals and preferences the user has stated that are worth keeping across sessions, plus the structured profile that feeds the astrology stub. `app/brain.py` defines the `SharedBrain` protocol the rest of the service programs against, an `InMemoryBrain` that is the executable specification of the semantics (used by the test suite and by `BRAIN=memory` runs), and a `Neo4jBrain` that executes the same decisions as parameterized Cypher, including the schema statements, the single write transaction that implements create / touch / supersede, and the translation of driver connectivity failures into one application exception, `BrainUnavailable`. No other module contains Cypher or imports the `neo4j` driver.

**Files:** `app/brain.py`

**Depends on:** `app/models.py` (`Memory`, `UserProfile`, `MemoryCandidate`, `PROFILE_FIELDS`; see [Domain model](03-domain-model.md)), `app/astrology.py` (`parse_date`, `sun_sign`), the `neo4j` driver package. **Used by:** `app/chat.py` ([Orchestration and short-term context](02-orchestration-and-short-term-context.md)), `app/memory.py` ([Memory update](06-memory-update.md)), `app/main.py` ([API layer](01-api-layer.md)); configured as described in [Configuration and deployment](08-configuration-and-deployment.md); tested as described in [Testing and verification](09-testing-and-verification.md). Layer index: [README](README.md).

## What this layer is

`app/brain.py` is the only persistence module in the service. Its module docstring states the split: "Two implementations of the same protocol. InMemoryBrain is the readable reference for the semantics; Neo4jBrain executes the same decisions in Cypher." The module holds:

| Symbol | Kind | Role |
|---|---|---|
| `Outcome` | `Literal["created", "updated", "unchanged"]` | Return type of `upsert_memory`; the only signal the memory-update layer needs to count changes |
| `BrainUnavailable` | `Exception` subclass | The one exception callers handle. Docstring: "The Shared Brain could not be reached. Callers degrade; they never see driver exceptions." |
| `SharedBrain` | `typing.Protocol` | Five `async` methods; both implementations satisfy it structurally (neither subclasses it) |
| `_now`, `_with_sun_sign`, `_new_memory` | module functions | Shared by both implementations: UTC clock, profile normalization, construction of a fresh `ACTIVE` `Memory` |
| `InMemoryBrain` | class | Dict/list storage, `fail` flag to simulate an outage |
| `SCHEMA`, `GET_PROFILE`, `UPSERT_PROFILE`, `SEARCH_MEMORIES`, `LIST_MEMORIES`, `FIND_ACTIVE`, `TOUCH`, `SUPERSEDE`, `CREATE_MEMORY` | string constants | Every Cypher statement the service runs |
| `_profile`, `_memory` | module functions | Map a Neo4j node to `UserProfile` / `Memory` |
| `Neo4jBrain` | class | Driver ownership, `ensure_schema`, `close`, the five protocol methods, `_query` |
| `_CONNECTIVITY_ERRORS` | tuple | `(ServiceUnavailable, SessionExpired, AuthError, OSError)`: the driver exceptions translated to `BrainUnavailable` |

The protocol, verbatim:

```python
class SharedBrain(Protocol):
    async def get_profile(self, user_id: str) -> UserProfile | None: ...
    async def upsert_profile(self, user_id: str, fields: dict[str, str]) -> UserProfile: ...
    async def search_memories(self, user_id: str, category: str | None, limit: int) -> list[Memory]: ...
    async def upsert_memory(self, user_id: str, cand: MemoryCandidate, source_message_id: str) -> Outcome: ...
    async def list_memories(self, user_id: str) -> list[Memory]: ...
```

`Neo4jBrain` additionally exposes `ensure_schema()` and `close()`, which are not part of the protocol. `app/main.py` calls the first only after an `isinstance(app.state.brain, Neo4jBrain)` check and the second through `getattr(app.state.brain, "close", None)`, so an `InMemoryBrain` (which has neither) satisfies the same lifespan code.

The module defines a logger (`log = logging.getLogger(__name__)`) but emits no log lines itself; outage logging happens in the callers (`app/chat.py`, `app/main.py`).

## Why it exists

The PRD's core capability is "a persistent Shared Brain that stores selected user information and retrieves only the context relevant to a new question" (PRD §1). This layer is the storage half of that sentence. The requirements it discharges:

- **PRD FR-3 (Shared Brain):** persist profile attributes, goals, preferences, interests and important memories; "The Shared Brain must be persistent and graph-oriented. Neo4j is the preferred implementation." `Neo4jBrain` is that implementation; profile attributes live on a `Profile` node, everything else on `Memory` nodes distinguished by `type` and `category`.
- **PRD FR-7 (Memory Update):** the system must be able to create a new memory, update an existing one, and "preserve enough provenance to understand where the memory came from." `upsert_memory` returns `created` / `updated` / `unchanged`, and every `Memory` node carries `source_message_id`.
- **PRD §7.1 (memory properties):** `type`, `value`, `category`, `confidence`, `source_message_id`, `created_at`, `updated_at` are all properties of the `Memory` node. The optional `valid_from` / `valid_until` are not written (see Known limits).
- **PRD §7.3 (memory correction):** "the latest explicit user statement should supersede the stale value while retaining provenance. ... The implementation may either update the existing memory node or mark the older memory inactive." The code marks the older node `SUPERSEDED` and links the new one to it with `SUPERSEDES`.
- **PRD §13 (risks):** "User corrections leave stale values" is mitigated by the `ACTIVE` / `SUPERSEDED` status; "Graph unavailable" is mitigated by `BrainUnavailable`, which lets the orchestrator "continue with recent context/profile when possible; expose degraded mode."
- **TDD §4.4 (Shared Brain Repository):** fixes the five-method interface, states that `upsert_memory` "implements Appendix C inside one write transaction and returns `"created" | "updated" | "unchanged"`", that there is no separate `supersede_memory`, and that "Driver and connection failures are translated to `BrainUnavailable` so the orchestrator never imports Neo4j exceptions."
- **TDD §5 (Data Model):** reduces the graph to `User`, `Profile`, `Memory` with `HAS_PROFILE`, `HAS_MEMORY`, `SUPERSEDES`, and explains that goals, preferences and interests are `Memory.type` values rather than typed nodes.
- **TDD §22 (Transaction and Consistency Model):** "Memory writes should be idempotent at the logical-key level" where the logical key is `(user_id, memory.category, memory.key)`; "Neo4j constraints should prevent duplicate user identities; application logic should prevent duplicate active memories for the same logical key."

The problem, in one sentence: a generic chatbot forgets everything between sessions, and a naive memory store either duplicates facts or overwrites them and loses history. This layer gives the service memory that is durable (Neo4j on a volume), graph-shaped (user-anchored nodes and edges, so future traversals are additive), and correctable (supersede with provenance, never overwrite).

## How it works

### The protocol and the shared helpers

Three module functions carry the semantics both implementations share.

`_now()` returns `datetime.now(timezone.utc)`; it is the clock for `InMemoryBrain`. `Neo4jBrain` uses it only to build the `Memory` dataclass whose timestamps are then discarded in favor of the server's `datetime()` (see the upsert transaction below).

`_with_sun_sign(fields)` is applied on every profile write by both implementations:

```python
def _with_sun_sign(fields: dict[str, str]) -> dict[str, str]:
    """Normalize date_of_birth to ISO and recompute sun_sign whenever it is set."""
    dob = parse_date(fields["date_of_birth"]) if fields.get("date_of_birth") else None
    if dob:
        fields = {**fields, "date_of_birth": dob.isoformat(), "sun_sign": sun_sign(dob)}
    return fields
```

If the incoming fields include a `date_of_birth` that `astrology.parse_date` can read (ISO, `15 August 1995`, `15th Aug 1995`, `August 15, 1995`, `15/08/1995`), the returned dict replaces it with the ISO `YYYY-MM-DD` form and adds `sun_sign` computed by `astrology.sun_sign`. The input dict is not mutated. If `date_of_birth` is absent, or present but unparseable, the fields pass through unchanged: an unparseable string is stored as given and `sun_sign` is not touched (inferred from the code; no test covers the unparseable path).

`_new_memory(cand, source_message_id)` builds a `Memory` with `id=str(uuid.uuid4())`, the candidate's `key`, `category`, `type`, `value`, `target_timeframe`, `confidence`, `status="ACTIVE"`, the given `source_message_id`, and `created_at = updated_at = _now()`. The candidate's `reason` field is not carried over; provenance in the graph is `source_message_id` only.

### Graph schema

Everything the code creates is described by the `SCHEMA` tuple and the write statements. Nodes and their properties:

| Node | Property | Written as | Written by | Notes |
|---|---|---|---|---|
| `User` | `id` | string (the `user_id`) | `MERGE` in `UPSERT_PROFILE` and `CREATE_MEMORY` | Uniqueness constraint `user_id_unique` |
| `User` | `created_at` | server `datetime()` | `ON CREATE SET` in both `MERGE`s | Set once |
| `User` | `updated_at` | server `datetime()` | `UPSERT_PROFILE` only | `CREATE_MEMORY` does not touch it, so it tracks profile writes only |
| `Profile` | `name` | string | `SET p += $fields` | |
| `Profile` | `date_of_birth` | ISO `YYYY-MM-DD` string | `SET p += $fields` after `_with_sun_sign` | TDD §5.2 lists this as `date`; the code stores a string |
| `Profile` | `time_of_birth` | string | `SET p += $fields` | TDD §5.2 lists `time`; stored as given |
| `Profile` | `birth_place` | string | `SET p += $fields` | |
| `Profile` | `preferred_language` | string | `SET p += $fields` | Reachable only from `POST /users`; a language preference stated in chat becomes a `language.preferred` `Memory` (TDD §7.0) |
| `Profile` | `sun_sign` | string | `SET p += $fields` | Derived by `_with_sun_sign`, never accepted from the caller (`UserUpsert` in `app/main.py` has no such field) |
| `Memory` | `id` | uuid4 string | `CREATE (m:Memory $props)` | Uniqueness constraint `memory_id_unique` |
| `Memory` | `key` | string, e.g. `career.goal` | `$props` | Half of the logical key |
| `Memory` | `category` | string from `models.MEMORY_CATEGORIES` | `$props` | Other half of the logical key; same taxonomy as query classification |
| `Memory` | `type` | `fact` / `goal` / `preference` / `interest` | `$props` | |
| `Memory` | `value` | string | `$props` | Compared by exact string equality on upsert |
| `Memory` | `target_timeframe` | string or absent | `$props` | A `None` in the map creates no property; read back with `.get()` |
| `Memory` | `confidence` | float | `$props`, later `TOUCH` | Raised to the max of old and new on an unchanged upsert |
| `Memory` | `status` | `'ACTIVE'` or `'SUPERSEDED'` | `$props`, later `SUPERSEDE` | |
| `Memory` | `source_message_id` | string or absent | `$props` | The id of the user message the fact was extracted from |
| `Memory` | `created_at` | server `datetime()` | `CREATE_MEMORY` | |
| `Memory` | `updated_at` | server `datetime()` | `CREATE_MEMORY`, `TOUCH`, `SUPERSEDE` | Drives the recency half of the search order |

Relationships:

| Relationship | From | To | Created by | Cardinality |
|---|---|---|---|---|
| `HAS_PROFILE` | `User` | `Profile` | `MERGE (u)-[:HAS_PROFILE]->(p:Profile)` in `UPSERT_PROFILE` | At most one per user, because the whole pattern is `MERGE`d |
| `HAS_MEMORY` | `User` | `Memory` | `CREATE (u)-[:HAS_MEMORY]->(m:Memory $props)` in `CREATE_MEMORY` | One per memory node, including superseded ones |
| `SUPERSEDES` | new `Memory` | old `Memory` | the `FOREACH ... CASE WHEN old IS NULL` clause of `CREATE_MEMORY` | Present only when the upsert replaced an active value |

`Memory` nodes carry no `user_id` property. A memory belongs to a user only through its `HAS_MEMORY` edge, and every user-scoped statement anchors on `(:User {id: $user_id})`. The three statements that address a memory by `id` alone (`TOUCH`, `SUPERSEDE`, the `OPTIONAL MATCH` in `CREATE_MEMORY`) only ever receive an `id` that `FIND_ACTIVE` returned for that user inside the same transaction.

The schema statements, run by `ensure_schema()` in order:

```python
SCHEMA = (
    "CREATE CONSTRAINT user_id_unique IF NOT EXISTS FOR (u:User) REQUIRE u.id IS UNIQUE",
    "CREATE CONSTRAINT memory_id_unique IF NOT EXISTS FOR (m:Memory) REQUIRE m.id IS UNIQUE",
    "CREATE INDEX memory_lookup IF NOT EXISTS FOR (m:Memory) ON (m.category, m.key, m.status)",
)
```

`IF NOT EXISTS` makes the three statements idempotent, so `ensure_schema()` is safe to run at every startup and again from `tests/test_neo4j.py`. The two uniqueness constraints implement TDD §22's "Neo4j constraints should prevent duplicate user identities" and give `MERGE (u:User {id: $user_id})` its guarantee of a single node per id; in Neo4j a uniqueness constraint is backed by an index, so `User.id` and `Memory.id` lookups (`MATCH (:User {id: $user_id})`, `MATCH (m:Memory {id: $id})`) are indexed without a separate statement.

The composite index `memory_lookup` on `(category, key, status)` is the shape of the logical-key lookup. README "Graph schema" states the purpose: "Index: `Memory(category, key, status)` for the logical-key lookup." The three properties, in that order, are exactly the three equality predicates `FIND_ACTIVE` places on the `Memory` node (`{category: $category, key: $key, status: 'ACTIVE'}`), which is the statement that runs at the start of every memory write (inferred from the statement text; TDD Appendix A sketched two single-property indexes on `category` and `key` and is labelled "Illustrative Cypher"; the code chose one composite index instead).

### `InMemoryBrain`: the executable specification

`InMemoryBrain` stores profiles in `self._profiles: dict[str, UserProfile]` and memories in `self._memories: dict[str, list[Memory]] = defaultdict(list)`, keyed by `user_id`. Its constructor takes `fail: bool = False`; every method first calls `self._check()`, which raises `BrainUnavailable("in-memory brain set to fail")` when `self.fail` is true. Tests flip the attribute directly (`brain.fail = True` in `tests/test_chat.py::test_10_graph_failure_degrades`) to simulate an outage without a driver.

- `get_profile` returns `self._profiles.get(user_id)`: `None` for an unknown user.
- `upsert_profile` does `profile = self._profiles.setdefault(user_id, UserProfile())`, then `setattr(profile, k, v)` for every item of `_with_sun_sign(fields)`, and returns the stored object itself. Only keys that are present are set; existing values for other fields are kept, which is what `tests/test_units.py::test_upsert_profile_derives_sun_sign` checks when a second call with only `birth_place` leaves `name` and `sun_sign` intact. No validation of key names happens here; the callers only ever pass the field names in `models.PROFILE_KEYS` (`app/memory.py`) or `UserUpsert` (`app/main.py`).
- `search_memories` filters the user's list to `m.status == "ACTIVE" and (category is None or m.category == category)`, sorts by `(m.confidence, m.updated_at)` with `reverse=True`, and slices `[:limit]`. `category=None` means every category; the orchestrator passes `None` for `general` queries.
- `upsert_memory` is the reference for Appendix C:

```python
    async def upsert_memory(self, user_id: str, cand: MemoryCandidate, source_message_id: str) -> Outcome:
        self._check()
        rows = self._memories[user_id]
        old = next((m for m in rows if m.status == "ACTIVE" and m.category == cand.category and m.key == cand.key), None)
        if old and old.value == cand.value:
            old.updated_at, old.confidence = _now(), max(old.confidence, cand.confidence)
            return "unchanged"
        if old:
            old.status, old.updated_at = "SUPERSEDED", _now()
        rows.append(_new_memory(cand, source_message_id))
        return "updated" if old else "created"
```

  The active memory with the same `(category, key)` is looked up; if its `value` equals the candidate's, its `updated_at` is refreshed and its `confidence` raised to the larger of the two, and the result is `unchanged`. Otherwise the old row (if any) is marked `SUPERSEDED` with a fresh `updated_at`, a new `ACTIVE` row is appended, and the result is `updated` when something was superseded or `created` when the key was new. Nothing is ever removed from the list, so history accumulates in creation order.

- `list_memories` returns `list(self._memories[user_id])`: every row for the user, `SUPERSEDED` included, in insertion order. This is the debug view behind `GET /users/{user_id}/memories`.

Because `InMemoryBrain` is a plain Python object, its behavior can be read top to bottom and asserted directly; TDD §27 records that it was added as "the executable reference for the Cypher semantics." Any change to the semantics is meant to be made here first, then mirrored in Cypher, then confirmed by `tests/test_neo4j.py` (CLAUDE.md: "change both together and run `test_neo4j.py`").

### `Neo4jBrain`: the driver

```python
class Neo4jBrain:
    def __init__(self, uri: str, user: str, password: str, timeout: float = 3.0):
        # Fail fast so an outage degrades the request in ~timeout seconds instead of the driver's 30s default.
        # ponytail: fixed timeout, no circuit breaker; add one when outages are long enough to matter per request.
        self._driver = AsyncGraphDatabase.driver(
            uri, auth=(user, password), connection_timeout=timeout, max_transaction_retry_time=timeout,
            notifications_min_severity="OFF")  # server hints about empty labels are noise on a fresh graph
```

The constructor creates the async driver and nothing else. `AsyncGraphDatabase.driver` does not open a connection; it validates the URI scheme and configures a connection pool, so constructing a `Neo4jBrain` against an unreachable host succeeds and the first query is what fails (this is how `tests/test_units.py::test_neo4j_connection_failure_is_brain_unavailable` works against `bolt://127.0.0.1:1`).

Three driver settings are set deliberately:

- `connection_timeout=timeout` (3.0s): the TCP connect deadline. The pinned driver's default is 30 seconds.
- `max_transaction_retry_time=timeout` (3.0s): the window in which managed transactions (`execute_query`, `execute_write`) retry transient failures before giving up. The driver's default is also 30 seconds. TDD §13.2 records the intent: "The driver is configured with a 3s connection timeout and 3s transaction-retry window, so an outage costs one request about three seconds rather than the driver's 30s default." Without this, every `/chat` during an outage would block for the full retry window before degrading.
- `notifications_min_severity="OFF"`: suppresses server notifications (the driver default is to leave the server's setting in place). The inline comment gives the reason: on a fresh database, `MATCH` on a label that has no nodes yet makes the server attach a notification saying the label does not exist, which the driver would log on every request until the first user is created.

The version pairing is recorded in README ("neo4j driver 6.3 against Neo4j 5.26") and CLAUDE.md ("neo4j driver is 6.x; tested against Neo4j 5.26"). `pyproject.toml` requires `neo4j>=5`; `uv.lock` pins 6.3.1; `docker-compose.yml` runs the `neo4j:5` image. The practical consequence of the 6.x driver that the code has to handle is the temporal type: `datetime()` values come back as `neo4j.time.DateTime`, not `datetime.datetime`, hence `.to_native()` in `_memory`.

`ensure_schema()` runs each `SCHEMA` statement through `_query`, sequentially. `close()` awaits `self._driver.close()`.

### `Neo4jBrain`: every statement

| Constant | Parameters | Used by | Purpose |
|---|---|---|---|
| `SCHEMA` (3 statements) | none | `ensure_schema` | Uniqueness of `User.id` and `Memory.id`; composite index for the logical-key lookup |
| `GET_PROFILE` | `user_id` | `get_profile` | Return the user's `Profile` node, or no rows |
| `UPSERT_PROFILE` | `user_id`, `fields` | `upsert_profile` | Create user and profile if missing, merge the given fields, return the profile |
| `SEARCH_MEMORIES` | `user_id`, `category` (nullable), `limit` | `search_memories` | Active memories, optionally one category, ranked, bounded |
| `LIST_MEMORIES` | `user_id` | `list_memories` | Every memory of the user, any status, oldest first |
| `FIND_ACTIVE` | `user_id`, `category`, `key` | `upsert_memory` (tx) | The one active memory for a logical key, projected to `id`, `value`, `confidence` |
| `TOUCH` | `id`, `confidence` | `upsert_memory` (tx) | Refresh `updated_at` and set `confidence` on an unchanged memory |
| `SUPERSEDE` | `id` | `upsert_memory` (tx) | Mark a memory `SUPERSEDED` and refresh `updated_at` |
| `CREATE_MEMORY` | `user_id`, `props`, `old_id` (nullable) | `upsert_memory` (tx) | Create the new active memory, its `HAS_MEMORY` edge, and, if `old_id` is set, the `SUPERSEDES` edge |

Statement by statement:

**`GET_PROFILE`**

```python
GET_PROFILE = "MATCH (:User {id: $user_id})-[:HAS_PROFILE]->(p:Profile) RETURN p"
```

A pattern match anchored on the user's id. It yields one row when a profile exists and none otherwise, so `get_profile` returns `_profile(rows[0]["p"]) if rows else None`. A user who exists only through memories (created by `CREATE_MEMORY`) has no `Profile` node and gets `None`, which the context selector turns into "no `user_profile` tag".

**`UPSERT_PROFILE`**

```python
UPSERT_PROFILE = """
MERGE (u:User {id: $user_id}) ON CREATE SET u.created_at = datetime()
SET u.updated_at = datetime()
MERGE (u)-[:HAS_PROFILE]->(p:Profile)
SET p += $fields
RETURN p"""
```

`MERGE` finds or creates the `User`, stamping `created_at` only on creation and `updated_at` every time. The second `MERGE` finds or creates the single `HAS_PROFILE` edge and `Profile` node. `SET p += $fields` is the map-merge form of `SET`: keys present in `$fields` are written, keys absent from it are left alone. `upsert_profile` passes `fields=_with_sun_sign(fields)`, so a date of birth arrives already normalized and `sun_sign` rides along in the same map. Partial updates therefore never blank other fields, which is what lets `POST /users` and the chat extractor each contribute the fields they know.

**`SEARCH_MEMORIES`**

```python
SEARCH_MEMORIES = """
MATCH (:User {id: $user_id})-[:HAS_MEMORY]->(m:Memory)
WHERE m.status = 'ACTIVE' AND ($category IS NULL OR m.category = $category)
RETURN m ORDER BY m.confidence DESC, m.updated_at DESC LIMIT $limit"""
```

The retrieval query of TDD §8 / Appendix B with one difference: the TDD sketch wrote `$category = 'general' OR m.category = $category`, the code writes `$category IS NULL OR m.category = $category`. The orchestrator (`app/chat.py`) translates `Category.GENERAL` to `None` before calling, so the brain never needs to know which query category means "everything" (inferred). The `WHERE` clause excludes superseded memories at the database, which TDD §4.6 requires ("Superseded memories are excluded by the query, never by post-filtering"). Ordering is confidence first, then recency, matching `InMemoryBrain.search_memories` exactly. `LIMIT $limit` is parameterized; the orchestrator passes `settings.memory_limit` (default 8).

**`LIST_MEMORIES`**

```python
LIST_MEMORIES = "MATCH (:User {id: $user_id})-[:HAS_MEMORY]->(m:Memory) RETURN m ORDER BY m.created_at"
```

No status filter and no limit: the inspection view. Ascending `created_at` gives the same order as `InMemoryBrain`'s insertion order.

**`FIND_ACTIVE`**

```python
FIND_ACTIVE = """
MATCH (:User {id: $user_id})-[:HAS_MEMORY]->(m:Memory {category: $category, key: $key, status: 'ACTIVE'})
RETURN m.id AS id, m.value AS value, m.confidence AS confidence"""
```

The logical-key lookup: anchored on the user, then the three equality predicates that the `memory_lookup` index covers. It projects only the three columns the upsert decision reads, not the whole node. Under the one-active-per-key invariant it returns zero or one row; the transaction reads it with `result.single()`, which yields `None` for zero rows and the record otherwise.

**`TOUCH`** and **`SUPERSEDE`**

```python
TOUCH = "MATCH (m:Memory {id: $id}) SET m.updated_at = datetime(), m.confidence = $confidence"
SUPERSEDE = "MATCH (m:Memory {id: $id}) SET m.status = 'SUPERSEDED', m.updated_at = datetime()"
```

Both address the node found by `FIND_ACTIVE` by its unique `id`. `TOUCH` receives `confidence=max(old["confidence"], cand.confidence)` computed in Python, mirroring the in-memory `max(old.confidence, cand.confidence)`. `SUPERSEDE` changes only `status` and `updated_at`; `created_at`, `value`, `source_message_id` and everything else on the old node are preserved as history.

**`CREATE_MEMORY`**

```python
CREATE_MEMORY = """
MERGE (u:User {id: $user_id}) ON CREATE SET u.created_at = datetime()
CREATE (u)-[:HAS_MEMORY]->(m:Memory $props)
SET m.created_at = datetime(), m.updated_at = datetime()
WITH m
OPTIONAL MATCH (old:Memory {id: $old_id})
FOREACH (o IN CASE WHEN old IS NULL THEN [] ELSE [old] END | CREATE (m)-[:SUPERSEDES]->(o))"""
```

Line by line: the user is merged so that a memory can be the first thing known about a user (no profile required). `CREATE (u)-[:HAS_MEMORY]->(m:Memory $props)` creates the node with all properties from the `$props` map in one clause; map entries whose value is `None` (`target_timeframe`, `source_message_id` when absent) create no property. Both timestamps are then set from the server clock. `WITH m` is required by Cypher between the updating clauses and the following read clause. `OPTIONAL MATCH (old:Memory {id: $old_id})` binds `old` to the superseded node when `$old_id` is a string and to `null` when the parameter is `None` (a `{id: null}` pattern matches nothing, and `OPTIONAL` turns "nothing" into a null row rather than no row). Cypher has no `IF`, so the conditional edge uses the standard idiom: `CASE WHEN old IS NULL THEN [] ELSE [old] END` produces a zero- or one-element list, and `FOREACH` runs `CREATE (m)-[:SUPERSEDES]->(o)` once per element. The result is one statement that serves both the `created` and the `updated` branch, so the transaction never needs a fourth round trip to link the new node to the old one.

### The upsert transaction

`Neo4jBrain.upsert_memory` is documented in code as "Appendix C in one write transaction: find active by logical key, then create / touch / supersede." It is the only method that does not go through `_query`, because it needs several statements to see one another's effects and commit or roll back together. The sequence:

1. `async with self._driver.session() as session:` opens a session; `session.execute_write(tx_fn)` runs the closure inside a managed write transaction. The driver commits when `tx_fn` returns, rolls back if it raises, and retries the whole function on transient errors for at most `max_transaction_retry_time` (3s).
2. `tx.run(FIND_ACTIVE, user_id=..., category=cand.category, key=cand.key)` then `old = await result.single()`: `old` is `None` or a record with `id`, `value`, `confidence`.
3. If `old` exists and `old["value"] == cand.value`: `tx.run(TOUCH, id=old["id"], confidence=max(old["confidence"], cand.confidence))` and return `"unchanged"`. The transaction commits; one property write happened.
4. Otherwise, if `old` exists: `tx.run(SUPERSEDE, id=old["id"])`.
5. `mem = _new_memory(cand, source_message_id)`; `props = {k: v for k, v in vars(mem).items() if not k.endswith("_at")}`. The map has nine keys (`id`, `key`, `category`, `type`, `value`, `target_timeframe`, `confidence`, `status`, `source_message_id`); `created_at` and `updated_at` are excluded so the server clock stamps them.
6. `tx.run(CREATE_MEMORY, user_id=user_id, props=props, old_id=old["id"] if old else None)`: creates the node, its `HAS_MEMORY` edge, and the `SUPERSEDES` edge when `old_id` is set.
7. Return `"updated" if old else "created"`; the transaction commits.
8. Around the whole thing: `except _CONNECTIVITY_ERRORS as e: raise BrainUnavailable(str(e)) from e`. Any other exception (a Cypher error, a constraint violation) propagates unchanged after the driver has rolled the transaction back.

Steps 4 and 6 are the reason for a single transaction. TDD §4.4 and §27 record the decision: "There is no separate `supersede_memory`: superseding is a decision the upsert makes, and splitting it into two calls makes the write non-atomic." If `SUPERSEDE` committed on its own and `CREATE_MEMORY` then failed, the user would be left with no active value for that key; inside one transaction, either both happen or neither does, and `FIND_ACTIVE` in step 2 is guaranteed to see the state the rest of the function acts on.

### Row mapping

```python
def _profile(node) -> UserProfile:
    return UserProfile(**{k: node.get(k) for k in PROFILE_FIELDS})


def _memory(node) -> Memory:
    return Memory(
        id=node["id"], key=node["key"], category=node["category"], type=node["type"], value=node["value"],
        target_timeframe=node.get("target_timeframe"), confidence=node["confidence"], status=node["status"],
        source_message_id=node.get("source_message_id"),
        created_at=node["created_at"].to_native(), updated_at=node["updated_at"].to_native(),
    )
```

`_profile` reads exactly the six names in `models.PROFILE_FIELDS` with `.get()`, so a missing property becomes `None` and any unexpected property on the node is ignored. `_memory` uses subscript access for the properties `CREATE_MEMORY` always writes and `.get()` for the two that may be absent. `created_at` and `updated_at` are `neo4j.time.DateTime` instances because they were produced by the server's `datetime()`; `.to_native()` converts them to Python `datetime` objects, timezone-aware (`tests/test_neo4j.py` asserts `everything[0].created_at.tzinfo is not None`). CLAUDE.md lists this as a gotcha of the 6.x driver.

### `_query` and error translation

```python
    async def _query(self, cypher: str, **params):
        try:
            return (await self._driver.execute_query(cypher, parameters_=params)).records
        except _CONNECTIVITY_ERRORS as e:
            raise BrainUnavailable(str(e)) from e
```

Every single-statement method (`ensure_schema`, `get_profile`, `upsert_profile`, `search_memories`, `list_memories`) goes through `_query`. `execute_query` is the driver's one-shot API: it acquires a session, runs the statement in a managed transaction (retried within the 3s window), and returns an `EagerResult` whose `.records` is a list. Parameters are passed as one dict through `parameters_`, the driver's reserved keyword for that purpose; `_query` collects them from `**params` so callers write `self._query(GET_PROFILE, user_id=user_id)`.

The `except` clause is the whole of the layer's error policy. `_CONNECTIVITY_ERRORS` is `(ServiceUnavailable, SessionExpired, AuthError, OSError)`:

| Exception | Meaning | Why it is an outage |
|---|---|---|
| `neo4j.exceptions.ServiceUnavailable` | No server could be reached, or the connection was lost | The database is down or unreachable |
| `neo4j.exceptions.SessionExpired` | The session is no longer valid, for example after a cluster member changed role | Transient infrastructure state, not a fault in the request |
| `neo4j.exceptions.AuthError` | The server rejected the credentials | From the application's point of view the brain is unusable until configuration changes; treating it as a bug would turn every request into a 500 |
| `OSError` | Raw socket-level failures (`ConnectionRefusedError`, `TimeoutError`, `socket.gaierror` are all subclasses) | Network conditions the driver may surface directly |

Everything else propagates: `CypherSyntaxError`, `ConstraintError`, other `ClientError`s, `TransientError`, `DatabaseError`. TDD §4.4 says "Driver and connection failures are translated to `BrainUnavailable`", and nothing more. A Cypher or constraint error means a statement in this file, or the data it was given, is wrong; hiding that behind `degraded: true` would make a bug look like a network blip. The same policy is applied to the LLM adapters in `app/llm.py`, where CLAUDE.md notes that "4xx errors deliberately propagate (our bug, not an outage)"; that the brain follows the same reasoning is inferred, since no comment in `app/brain.py` states it.

### Lifecycle: how the driver is created and closed

`app/main.py`'s `create_app` builds the brain inside the FastAPI lifespan:

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
        ...
        yield
        if close := getattr(app.state.brain, "close", None):
            await close()
```

If a `brain` was injected (the test fixtures pass an `InMemoryBrain`), it is used as is. Otherwise `settings.brain == "memory"` selects `InMemoryBrain()`, and any other value selects `Neo4jBrain(...)` with the three connection settings and the default 3s timeout; `main.py` does not pass `timeout`. `ensure_schema()` is the first network call: if Neo4j is unreachable, the resulting `BrainUnavailable` is logged as a warning and startup continues, so the service comes up and answers from short-term context until the database returns (the driver's pool reconnects on the next call without any action by the application). On shutdown, the `close` attribute is looked up dynamically and awaited if present, so the same block serves both implementations. One driver lives for the process; the `ChatService` and the two `/users` routes all reach it through `app.state.brain`.

Callers: `app/chat.py` calls `get_profile` and `search_memories` before generation (skipped for `follow_up`), catching `BrainUnavailable` to set `degraded`; `app/memory.py`'s `remember` calls `upsert_profile` for candidates whose key is in `PROFILE_KEYS` and `upsert_memory` for the rest, counting every outcome other than `"unchanged"`; `app/main.py`'s `POST /users` calls `upsert_profile` and `GET /users/{user_id}/memories` calls `list_memories`, with a `BrainUnavailable` exception handler returning 503 for those two routes.

## Contracts and invariants

- **Logical identity is `(user_id, category, key)`.** Both implementations look up an existing memory by exactly these three values plus `status = 'ACTIVE'` (TDD §22).
- **At most one `ACTIVE` memory per logical key.** Enforced by application logic in `upsert_memory` (find-then-supersede inside one transaction), not by a database constraint; TDD §22 assigns it to "application logic".
- **Corrections supersede; nothing is overwritten in place.** A changed value sets `old.status = 'SUPERSEDED'`, creates a new `ACTIVE` node, and links `(new)-[:SUPERSEDES]->(old)`. The old node keeps its `value`, `created_at` and `source_message_id`. The new node's `source_message_id` is the message that stated the correction (`tests/test_units.py::test_upsert_outcomes` checks it is `m3`).
- **`search_memories` never returns `SUPERSEDED` memories.** The status filter is in the Cypher `WHERE` and in the in-memory list comprehension.
- **`list_memories` returns everything**, `SUPERSEDED` included, oldest first. It is the only way to see history.
- **Ordering of `search_memories` is `confidence DESC, updated_at DESC`, then `LIMIT`.** In `tests/test_neo4j.py`, `career.goal` at 0.95 precedes `language.preferred` at 0.9 in an unfiltered search.
- **`category=None` means all categories**; a string restricts to that category with an equality match.
- **Dates are ISO strings.** `Profile.date_of_birth` is written as `YYYY-MM-DD` whenever `parse_date` recognizes the input; `Memory.value` for dates is `YYYY-MM-DD` by the extractor's schema (`MemoryCandidate.value` description: "dates as YYYY-MM-DD"), not enforced here.
- **`sun_sign` is derived, never accepted.** Every profile write passes `_with_sun_sign`; a new `date_of_birth` always recomputes it. `UserUpsert` in `app/main.py` has no `sun_sign` field, and `PROFILE_KEYS` in `app/models.py` maps no memory key to it.
- **`Outcome` meanings:** `created` means no active memory existed for the key and one was created; `updated` means an active memory with a different value was superseded and a new one created; `unchanged` means an active memory with the same value existed, its `updated_at` was refreshed and its `confidence` raised to the maximum of old and new. `unchanged` still performs a write; `app/memory.py` counts it as zero changes.
- **Value comparison is exact string equality** after `memory.validate` has stripped whitespace; case is significant.
- **`unchanged` never lowers confidence**; `max(old, new)` is used in both implementations.
- **Timestamps are timezone-aware `datetime`s** in both implementations: `datetime.now(timezone.utc)` in memory, the server's `datetime()` converted with `.to_native()` in Neo4j.
- **`BrainUnavailable` is the only exception callers must handle.** `app/chat.py` imports `BrainUnavailable` and `SharedBrain` from this module and nothing from `neo4j`. Anything else escaping the brain is a bug and surfaces as an unhandled 500.
- **Parameterized Cypher throughout.** No statement interpolates user input; TDD §20 lists "Parameterize Cypher queries" as a baseline control.
- **Profile writes are partial merges.** Fields not present in the call are untouched in both implementations.
- **The `User` node is created on first contact of either kind** (profile or memory); a `Profile` node exists only after a profile write.

## Design decisions and alternatives rejected

### Generic `Memory` nodes instead of typed nodes

Chosen: one `Memory` label with `type` (`fact | goal | preference | interest`) and `category` (the query taxonomy) as properties. Rejected: `:Goal`, `:Preference`, `:LifeArea`, `:AstrologyAttribute`, `:Message` nodes with their own relationship types. Why: TDD §5.1 defers typed nodes "until a query needs to traverse them; adding them is additive (a label plus a relationship per memory) and does not change the API"; TDD §6 calls the generic form "more generic and faster to implement"; TDD §27 records that the typed-node list "contradicted" the §6 recommendation and was cut; README "Trade-offs": "typed nodes add nothing until a traversal needs them." A second effect (inferred): with `category` a property rather than a label, retrieval by category is one equality predicate that a composite index can serve, and `general` is `category IS NULL` rather than a union over labels.

### Upsert in one transaction instead of `upsert_memory` + `supersede_memory`

Chosen: `upsert_memory` performs find, then touch or supersede-and-create, inside one `execute_write`, and returns an `Outcome`. Rejected: the v1 design's separate `supersede_memory` repository call, with the orchestrator deciding when to call it. Why: TDD §4.4 and §27: "Superseding is a decision the upsert makes; two calls make the write non-atomic." The decision needs the current active value, which only the repository has, and the supersede and create must commit together or the key is left with no active value.

### Python-side branching inside the transaction instead of one conditional Cypher statement

Chosen: `tx_fn` runs `FIND_ACTIVE`, branches in Python, and issues `TOUCH` or `SUPERSEDE` + `CREATE_MEMORY`. Rejected: a single Cypher statement encoding all three branches. Why (inferred; no rationale is recorded): Cypher has no `IF`, so three branches would need nested `FOREACH`/`CASE` constructs or APOC procedures, and the statement would still have to compute and return the outcome. The Python branch reads line for line like `InMemoryBrain.upsert_memory`, which serves the TDD §27 goal of keeping the in-memory class "the executable reference for the Cypher semantics". The one conditional that is pushed into Cypher, the `FOREACH ... CASE WHEN old IS NULL` edge creation in `CREATE_MEMORY`, is the one where doing so removes a round trip without obscuring the logic.

### A separate `Profile` node instead of properties on `User`

Chosen: `(:User)-[:HAS_PROFILE]->(:Profile)` with the six personal fields on `Profile`. Rejected: storing `name`, `date_of_birth`, etc. directly on `User`. Why: TDD §5.1 specifies `HAS_PROFILE` from the start; no further rationale is recorded. Inferred benefits visible in the code: `SET p += $fields` operates on a node whose only properties are user-supplied personal fields, so a caller-supplied map can never collide with `id`, `created_at` or `updated_at`; `GET_PROFILE` returning zero rows is the natural "no profile yet" signal that `get_profile` maps to `None`; and `_profile` maps the node with `PROFILE_FIELDS` alone.

### Composite index on `(category, key, status)` instead of two single-property indexes

Chosen: `memory_lookup` on `(m.category, m.key, m.status)`. Rejected: TDD Appendix A's illustrative `memory_category` and `memory_key` single-property indexes. Why: README "Graph schema" names the purpose ("for the logical-key lookup"); the composite matches `FIND_ACTIVE`'s three equality predicates, which run at the start of every memory write (inferred from the statement text).

### Catching only connectivity errors

Chosen: `_CONNECTIVITY_ERRORS = (ServiceUnavailable, SessionExpired, AuthError, OSError)`. Rejected: catching `neo4j.exceptions.Neo4jError` or `Exception` and degrading on anything. Why: TDD §4.4 scopes the translation to "driver and connection failures". A Cypher or constraint error is a defect in this module or its input and must be visible; degrading silently would hide it behind `degraded: true`. The LLM layer applies the same distinction (CLAUDE.md: 4xx "deliberately propagate (our bug, not an outage)"); applying it here is inferred.

### Fixed 3s timeout instead of a circuit breaker

Chosen: `connection_timeout=3.0`, `max_transaction_retry_time=3.0`, every request tries the database. Rejected: a circuit breaker that stops trying after N failures. Why: the `ponytail:` comment in `Neo4jBrain.__init__`: "fixed timeout, no circuit breaker; add one when outages are long enough to matter per request." TDD §13.2 accepts "an outage costs one request about three seconds". README "Production path" lists "circuit breaker around the brain instead of a fixed 3s timeout" as hardening work.

### Server-side `datetime()` instead of Python timestamps

Chosen: all `created_at` / `updated_at` values on `User` and `Memory` are set with Cypher `datetime()`; the Python timestamps from `_new_memory` are explicitly dropped (`if not k.endswith("_at")`) before `CREATE_MEMORY`. Rejected: passing `_now()` values as parameters. Why (inferred; not recorded): one clock, the database's, orders every `updated_at` the search query sorts by, regardless of how many application processes write; and `TOUCH` / `SUPERSEDE` do not need a timestamp parameter at all. The cost is the `neo4j.time.DateTime` return type and the `.to_native()` conversion.

### `$category IS NULL` instead of `$category = 'general'`

Chosen: the caller passes `None` to mean all categories. Rejected: TDD §8 / Appendix B's `$category = 'general'` sentinel inside the query. Why (inferred): the brain has no reason to know the query taxonomy's name for "unfiltered"; `general` is a valid memory category in `models.MEMORY_CATEGORIES`, so `m.category = 'general'` and "all categories" must be distinguishable, which `None` does and a string sentinel would not.

### `InMemoryBrain` as a first-class implementation

Chosen: a dict-backed implementation shipped in the same module and selectable with `BRAIN=memory`. Rejected: mocking the driver in tests. Why: TDD §27: "Tests and no-Neo4j demo runs; it is also the executable reference for the Cypher semantics." The scenario tests in `tests/test_chat.py` run the real FastAPI app with this brain injected and need no services.

## Failure modes and degraded behavior

| Situation | What the code does | Observable result |
|---|---|---|
| Neo4j unreachable at startup | `ensure_schema()` raises `BrainUnavailable`; lifespan logs a warning and continues | Service starts; every `/chat` is degraded until Neo4j returns. Constraints and index are not created by this process (see Known limits) |
| Neo4j unreachable during the pre-generation read | `get_profile` / `search_memories` raise `BrainUnavailable` after about `connection_timeout` (3s) | `app/chat.py` sets `degraded=True`, answers from recent turns only, skips the memory write; `200` with `degraded: true`, `memory_updates: 0` (`tests/test_chat.py::test_10_graph_failure_degrades`) |
| Neo4j fails during the post-response write | `upsert_profile` / `upsert_memory` raise `BrainUnavailable`; a write transaction in flight is rolled back by the driver | Response already generated is returned with `degraded: true`; earlier writes in the same `remember` call (each its own transaction) stay committed, and `memory_updates` reports 0 |
| Neo4j unreachable on `POST /users` or `GET /users/{user_id}/memories` | `BrainUnavailable` propagates out of the route | `503` with `"Shared Brain unavailable: ..."` from the handler in `app/main.py` |
| Wrong `NEO4J_PASSWORD` | `AuthError` is in `_CONNECTIVITY_ERRORS` | Same as an outage: degraded chat, 503 on the profile routes, warning at startup |
| Neo4j comes back | The driver's connection pool reconnects on the next call | Chat stops degrading with no restart |
| Cypher or constraint error | Not translated; propagates through `chat()` (which catches only `BrainUnavailable` and `LLMError`) | Unhandled exception: FastAPI returns `500`. This is deliberate: it is a bug, not an outage |
| Unsupported `NEO4J_URI` scheme | `AsyncGraphDatabase.driver` raises `neo4j.exceptions.ConfigurationError` from the constructor, inside the lifespan | Startup fails (observed with the pinned driver; no test covers it) |
| `InMemoryBrain(fail=True)` or `brain.fail = True` | Every method raises `BrainUnavailable` | Same degraded paths without a driver |
| Unparseable `date_of_birth` in a profile write | `_with_sun_sign` leaves the fields unchanged | The raw string is stored; `sun_sign` is not updated |
| No `Profile` node, or no memories | `get_profile` returns `None`; `search_memories` returns `[]` | Normal `200`; the corresponding `context_used` tags are simply absent (`tests/test_chat.py::test_8_missing_profile_is_fine`) |
| LLM fails before any brain write | Not this layer's failure, but relevant: `chat()` raises before `remember` runs | Nothing is written (`tests/test_chat.py::test_9_llm_failure_returns_503_and_mutates_nothing`) |

The one-warning rule from TDD §13.2 ("Skip the post-response memory write when the pre-response read already failed: one logged failure per request, not two") is implemented in `app/chat.py`, which checks `degraded` before extraction; this layer just raises consistently.

## Configuration

| Variable | Default | Effect on this layer |
|---|---|---|
| `BRAIN` | `neo4j` | `memory` selects `InMemoryBrain()`; any other value selects `Neo4jBrain` (no validation of the string) |
| `NEO4J_URI` | `bolt://localhost:7687` | First argument to `Neo4jBrain`; `docker-compose.yml` sets `bolt://neo4j:7687` for the app container |
| `NEO4J_USER` | `neo4j` | Auth principal |
| `NEO4J_PASSWORD` | `password` | Auth credential; `docker-compose.yml` starts Neo4j with `NEO4J_AUTH: neo4j/password` to match |
| `MEMORY_LIMIT` | `8` | Not read by this module, but it is the `limit` the orchestrator passes to `search_memories` |
| `NEO4J_TEST_URI` | unset | Test-only: enables `tests/test_neo4j.py` |

The timeout is not an environment setting. It is the constructor default `Neo4jBrain(..., timeout: float = 3.0)`, applied to both `connection_timeout` and `max_transaction_retry_time`; `app/main.py` does not override it. Changing it means changing the default or the call in `create_app`. `notifications_min_severity="OFF"` is likewise hard-coded. `.env.example` documents the four `BRAIN` / `NEO4J_*` variables under "Shared Brain".

## Tests that pin this layer

| Test | Asserts |
|---|---|
| `tests/test_units.py::test_upsert_outcomes` | The outcome sequence `created` -> `unchanged` -> `updated` for the same key; after the correction `search_memories` returns only `("Hindi", "m3")`; `list_memories` shows `English` as `SUPERSEDED` with confidence raised to 0.95 by the touch and `Hindi` as `ACTIVE` at 0.8 |
| `tests/test_units.py::test_upsert_profile_derives_sun_sign` | `"15 August 1995"` is stored as `"1995-08-15"` with `sun_sign == "Leo"`; a later write of only `birth_place` keeps `name` and `sun_sign` |
| `tests/test_units.py::test_neo4j_connection_failure_is_brain_unavailable` | `Neo4jBrain("bolt://127.0.0.1:1", ...).get_profile("u")` raises `BrainUnavailable` (the driver exception is translated); `close()` runs in `finally` |
| `tests/test_neo4j.py::test_neo4j_roundtrip` | Against a live database: `ensure_schema`; `get_profile` is `None` for a new user; `upsert_profile` yields `("Rahul", "Leo")` and leaves `birth_place` `None`; the same `created` / `unchanged` / `updated` sequence; a second key is `created`; category search returns `["Hindi"]`, unfiltered search orders `career.goal` (0.95) before `language.preferred` (0.9), an unused category returns `[]`; `list_memories` has three rows with the expected statuses and timezone-aware `created_at`; a raw Cypher query confirms `(Hindi)-[:SUPERSEDES]->(English)`; the user's subgraph is deleted afterwards |
| `tests/test_chat.py::test_2_first_message_creates_durable_memory_and_profile` | Through `POST /chat`: the PRD sentence produces one `ACTIVE` `career.goal` memory with `target_timeframe` = next year and a profile of `("Rahul", "1995-08-15", "Delhi", "Leo")` |
| `tests/test_chat.py::test_5_memory_persists_across_sessions` | `GET /users/u1/memories` lists `[("career.goal", "ACTIVE")]`; two later sessions retrieve it |
| `tests/test_chat.py::test_6_irrelevant_memory_excluded` | A `health` memory is not returned for a `career` query (the category filter) |
| `tests/test_chat.py::test_7_user_correction_supersedes` | "I prefer English." then "Actually, I prefer Hindi." leaves `search_memories` with `["Hindi"]` and `list_memories` statuses `{"English": "SUPERSEDED", "Hindi": "ACTIVE"}`; a new session's prompt contains `Hindi` and not `English` |
| `tests/test_chat.py::test_9_llm_failure_returns_503_and_mutates_nothing` | After an LLM failure `list_memories` is `[]` and `get_profile` is `None` |
| `tests/test_chat.py::test_10_graph_failure_degrades` | With `brain.fail = True`: `degraded` is true, `memory_updates` is 0, `context_used` is `[]`; a follow-up still gets `["recent_conversation"]` |
| `tests/test_chat.py::test_profile_endpoint_feeds_astrology_context` | `POST /users` with a DOB returns `sun_sign == "Leo"` and the sign reaches the prompt on an astrology query |

`tests/conftest.py` supplies the `brain` fixture (`InMemoryBrain()`) and injects it with `create_app(brain=brain, ...)`, so the scenario tests exercise the real app against the in-memory implementation.

Running them:

```bash
uv run pytest -q                                   # everything above except the live round trip (skipped)
docker run -d -p 7687:7687 -e NEO4J_AUTH=neo4j/password neo4j:5
NEO4J_TEST_URI=bolt://localhost:7687 NEO4J_PASSWORD=password uv run pytest tests/test_neo4j.py -q
```

`tests/test_neo4j.py` sets `pytestmark = pytest.mark.skipif(not URI, ...)` on the module, so without `NEO4J_TEST_URI` it reports one skipped test. `NEO4J_USER` defaults to `neo4j` and `NEO4J_PASSWORD` to `password` inside the test. The test creates a user with a random `test-<uuid>` id and removes that user's subgraph in `finally`, so it can run against a database that holds other data.

## Known limits and future work

Deliberate shortcut markers in the covered file (`grep -rn "ponytail:" app/brain.py`), verbatim:

- `app/brain.py`, `Neo4jBrain.__init__`: `# ponytail: fixed timeout, no circuit breaker; add one when outages are long enough to matter per request.` Upgrade path: a circuit breaker around the brain, as README "Production path" lists ("circuit breaker around the brain instead of a fixed 3s timeout"). Until then, during an outage every non-follow-up `/chat` pays roughly the 3s connection timeout before degrading.

Recorded future work from the design documents:

- **Decay and `valid_until`.** PRD §7.1 lists optional `valid_from` / `valid_until`; TDD §5.2 includes `valid_until: datetime | null` in the recommended `Memory` properties; README "Production path" says "importance and decay (`valid_until` is already on the schema)". That refers to the design schema: the `Memory` dataclass in `app/models.py` has no such field and `CREATE_MEMORY` never writes it. Adding it is a property on the node, a field on the dataclass, and a `WHERE` predicate in `SEARCH_MEMORIES`.
- **Embedding-based retrieval.** TDD §4.6 and §27 defer a weighted relevance score until embeddings exist; README "Production path": "embedding-based retrieval alongside the category filter"; TDD §23 Phase 3 lists hybrid graph + vector retrieval. Today retrieval is the equality filter and the two-key sort in `SEARCH_MEMORIES`.
- **Typed nodes and richer traversal.** TDD §5.1 and README "Graph schema": adding `:Goal` etc. is additive.
- **Conflict resolution beyond last-write-wins.** README "Production path". The current rule is Appendix C: the latest explicit statement supersedes.

Limits visible in the code (inferred; none has a recorded rationale or a test):

- `ensure_schema()` runs once, at startup. If Neo4j is unreachable then, the constraints and index are not created for the life of the process even after the database returns; queries still work (constraints and indexes are optimizations and guards, not prerequisites), but without the uniqueness constraint two concurrent `MERGE (u:User {id: ...})` calls for a new user are not protected against creating two `User` nodes, until a restart or a manual run of the `SCHEMA` statements.
- No lock is taken on the `User` node during `FIND_ACTIVE`, so two concurrent `upsert_memory` calls for the same user and key could each see no active row and both create one, violating the one-active-per-key invariant. Nothing in the service serializes requests per user.
- `connection_timeout` and `max_transaction_retry_time` bound connecting and retrying, not query execution. A reachable but very slow server is not cut off at 3s; no transaction timeout is configured on the session.
- `_query` uses `execute_query`'s default routing, which is write routing. Against a single instance this is irrelevant; against a cluster, reads would go to the leader until a read routing hint is passed for `GET_PROFILE`, `SEARCH_MEMORIES` and `LIST_MEMORIES`.
- `remember` in `app/memory.py` issues one transaction per candidate. A brain failure midway leaves earlier candidates committed and later ones unwritten, and reports `memory_updates: 0` for the whole request.
- `_with_sun_sign` only normalizes a `date_of_birth` that `parse_date` recognizes. An unparseable value (for example `sometime in 1995`) is stored verbatim on the `Profile` node, and any `sun_sign` computed from an earlier, parseable date is left in place, so a profile can carry a `sun_sign` that no longer corresponds to its `date_of_birth`. `POST /users` cannot produce this because `UserUpsert.date_of_birth` is validated as a `date`, but a chat-extracted value routed through `memory.remember` can.
- `upsert_profile` applies `SET p += $fields` (and, in `InMemoryBrain`, `setattr`) unconditionally. It never compares incoming values with stored ones, so restating an identical profile fact re-writes the property, bumps `User.updated_at`, and is counted by `memory.remember`, which adds `len(profile_fields)` to `memory_updates` on every profile write. Profiles have no `unchanged` outcome; the method returns the `UserProfile`, not an `Outcome`.
- `LIST_MEMORIES` has no `LIMIT`; the debug endpoint returns the user's entire history.
- `MemoryCandidate.reason` is never persisted, so provenance is the `source_message_id` alone; the message text itself is not stored anywhere durable (session history is in-process).
- Value equality for the `unchanged` decision is exact and case-sensitive; `memory.validate` strips whitespace but does not fold case, so `Hindi` and `hindi` would supersede each other.
- Neither implementation validates profile field names; a caller passing an unknown key would set an arbitrary attribute (`InMemoryBrain`) or property (`SET p += $fields`). The two callers only pass known names.
- `User.updated_at` is only touched by `UPSERT_PROFILE`; memory writes do not update it.
- `Profile.date_of_birth` and `time_of_birth` are strings, not the `date` / `time` types TDD §5.2 suggests; a real astrology engine replacing `app/astrology.py` may want typed temporal values.
