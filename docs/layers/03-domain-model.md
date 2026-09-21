# Domain model and astrology stub

`app/models.py` holds every type the other layers hand to each other: the `Category` taxonomy that the query classifier and the memory store share, the four constants that fence what may be remembered and where it lands (`MEMORY_CATEGORIES`, `MEMORY_TYPES`, `PROFILE_FIELDS`, `PROFILE_KEYS`), four dataclasses for internal records (`ChatMessage`, `UserProfile`, `Memory`, `LLMRequest`) and one Pydantic model (`MemoryCandidate`) that doubles as the structured-output schema the LLM extractor fills in. `app/astrology.py` is the deterministic stand-in for an astrology engine: a tropical sun-sign lookup and a lenient date parser. Both are leaf modules; neither imports anything else from `app/`, and everything downstream imports them.

**Files:** `app/models.py`, `app/astrology.py`

**Depends on:** nothing inside `app/` (standard library plus Pydantic). **Used by:** every other layer: [Orchestration and short-term context](02-orchestration-and-short-term-context.md), [Query understanding and context selection](04-query-understanding-and-context-selection.md), [Shared Brain](05-shared-brain.md), [Memory update](06-memory-update.md), [LLM providers](07-llm-providers.md) and, through the response schemas in `app/main.py`, the [API layer](01-api-layer.md). Layer index: [README](README.md).

## What this layer is

Two modules at the bottom of the dependency order recorded in `CLAUDE.md` (`config`, `models` → `session`, `astrology`, `brain` → `context` → `llm` → `memory` → `chat` → `main`).

`app/models.py` defines, in source order:

| Name | Kind | Purpose |
|---|---|---|
| `Category` | `enum.StrEnum`, 10 members | The one taxonomy for query classification and for `Memory.category` |
| `MEMORY_CATEGORIES` | `set[str]` | `Category` values a memory may carry (all but `follow_up`) |
| `MEMORY_TYPES` | `set[str]` | `fact`, `goal`, `preference`, `interest` |
| `PROFILE_FIELDS` | `tuple[str, ...]` | The six property names of the `Profile` node, in `UserProfile` field order |
| `PROFILE_KEYS` | `dict[str, str]` | Memory keys that are structured profile facts, mapped to the `Profile` field they update |
| `ChatMessage` | dataclass | One turn: `role`, `content`, optional `id` |
| `UserProfile` | dataclass | Read model of the `Profile` node, plus `fields()` |
| `MemoryCandidate` | Pydantic `BaseModel` | What an extractor proposes; also the LLM structured-output schema |
| `Memory` | dataclass | A persisted memory with status and provenance |
| `LLMRequest` | dataclass | `system_prompt` + `messages`, the provider-neutral generation request |

`app/astrology.py` defines `_SIGN_ENDS` (13 rows of `(month, day, sign)`), `_DATE_FORMATS` (6 `strptime` formats), `sun_sign(dob: date) -> str` and `parse_date(text: str) -> date | None`.

What this layer is not. It performs no I/O and holds no business rules: taxonomy membership and the confidence threshold are enforced in `memory.validate` (see [Memory update](06-memory-update.md)), the one-active-memory-per-key rule in `brain.upsert_memory` (see [Shared Brain](05-shared-brain.md)), and request validation in the Pydantic schemas of `app/main.py`, which are separate classes (`ChatRequest`, `UserUpsert`, `ProfileOut`, `MemoryOut`) that mirror `UserProfile` and `Memory` field by field and are populated with `ProfileOut(user_id=..., **vars(profile))` and `MemoryOut(**vars(m))`. There is no `LLMResponse` type: `LLMProvider.generate` returns `str` (TDD §10: "The PRD's `LLMResponse` type would carry only provider metadata nobody reads yet").

## Why it exists

Requirements this layer satisfies directly:

- PRD FR-2 (User Profile) lists the minimum profile: name, date of birth, time of birth, birth place, preferred language, zodiac/sun sign. `UserProfile` has exactly those six fields, and `PROFILE_FIELDS` names them for the graph. FR-2 also says "Astrology calculations may be simplified or stubbed", which is the licence for `app/astrology.py`.
- PRD FR-3 (Shared Brain) asks to persist profile attributes, goals, preferences, interests, important memories, life areas and astrology attributes. The model covers these with three properties rather than seven node types: `Memory.type` (`fact | goal | preference | interest`) says what kind of memory, `Memory.category` (a `Category` value) says which life area, and profile and astrology attributes live on the `Profile` node as `UserProfile` fields (TDD §5.1).
- PRD §7.1 (Memory categories) says each durable memory should have `type`, `value`, `category`, `confidence`, `source_message_id`, `created_at`, `updated_at` and optional `valid_from` / `valid_until`. `Memory` carries every mandatory field plus `id`, `key`, `target_timeframe` and `status`; it does not carry `valid_from` or `valid_until` (TDD §5.2 lists `valid_until` among recommended properties, but neither the dataclass nor the Cypher in `app/brain.py` writes it).
- PRD FR-7 (Memory Update) requires associating a memory with "a category and entity" and preserving provenance. `Memory.category` + `Memory.key` (with the owning `user_id`) form the logical identity, and `Memory.source_message_id` carries the `ChatMessage.id` of the user turn it came from.
- PRD FR-6 (LLM Abstraction) names `MemoryCandidate` as the return type of `extract_memories`, and TDD §7.1 gives the extraction format (`key`, `category`, `value`, `target_timeframe`, `confidence`, `reason`); `MemoryCandidate` is that format plus `type`.
- TDD §4.5 (Query Understanding) fixes the ten domains `profile`, `career`, `relationships`, `finance`, `health`, `interests`, `language`, `astrology`, `general`, `follow_up`. `Category` has these ten members, one for one, and nothing else.
- TDD §5 (Data Model) reduces the graph to `User`, `Profile`, `Memory` and defines their properties; `UserProfile` and `Memory` are the Python views of the `Profile` and `Memory` nodes.
- TDD §11 (Astrology Layer) treats astrology as "a profile enrichment layer": "A simple deterministic stub can derive or accept a sun sign, while a real astrology engine can replace it later without changing chat orchestration." `sun_sign()` is that stub; `sun_sign` is a `Profile` field rather than the separate `AstrologyProfile` dataclass sketched in §11 (inferred: it is one field, and the graph schema in the README puts it on the `Profile` node).
- TDD §4.7 defines `LLMRequest` as exactly `system_prompt` + `messages`, confirmed as a deliberate reduction in TDD §27.

The problem the layer solves is vocabulary drift. The same category string is produced by `context.classify`, used as the `$category` filter in `brain.SEARCH_MEMORIES`, listed in the extraction prompt (`llm._extract_system`), checked by `memory.validate`, and stored on every `Memory` node. Putting the taxonomy in one `StrEnum` and deriving `MEMORY_CATEGORIES` and the `MemoryCandidate.category` description from it means these five places cannot disagree (TDD §5.1: "uses the same taxonomy as query classification (§4.5), so retrieval is a direct equality match").

## How it works

### `Category`

```python
class Category(StrEnum):
    """One taxonomy for both query classification and memory categories, so retrieval is an equality match."""

    PROFILE = "profile"
    CAREER = "career"
    RELATIONSHIPS = "relationships"
    FINANCE = "finance"
    HEALTH = "health"
    INTERESTS = "interests"
    LANGUAGE = "language"
    ASTROLOGY = "astrology"
    GENERAL = "general"
    FOLLOW_UP = "follow_up"
```

| Member | Value | Valid as a query category | Valid as a memory category | Profile fields selected for the query (`context._PROFILE_FIELDS_FOR`) | Memories retrieved for the query (`chat.ChatService.chat`) |
|---|---|---|---|---|---|
| `PROFILE` | `profile` | yes | yes | all of `PROFILE_FIELDS` | `category = 'profile'` |
| `CAREER` | `career` | yes | yes | `name`, `sun_sign` (default) | same category |
| `RELATIONSHIPS` | `relationships` | yes | yes | default | same category |
| `FINANCE` | `finance` | yes | yes | default | same category |
| `HEALTH` | `health` | yes | yes | default | same category |
| `INTERESTS` | `interests` | yes | yes | default | same category |
| `LANGUAGE` | `language` | yes | yes | `name`, `preferred_language` | same category |
| `ASTROLOGY` | `astrology` | yes | yes | all of `PROFILE_FIELDS` | same category |
| `GENERAL` | `general` | yes (the fall-through) | yes | default | all categories (`search = None`) |
| `FOLLOW_UP` | `follow_up` | yes | no | none | none; the graph is not read |

Because it is a `StrEnum`, a member is a `str`: `Category.CAREER == "career"` is true, `str(Category.CAREER)` and `f"{Category.CAREER}"` both give `"career"`. This is what lets the same object serve as a dict key in `context._KEYWORDS` and `context._PROFILE_FIELDS_FOR`, be compared against the plain `str` stored in `Memory.category` (`m.category == category` in `InMemoryBrain.search_memories`), be passed straight through as the `$category` Cypher parameter in `Neo4jBrain.search_memories`, and print readably in the `chat` log line (`category=%s`).

Consumers: `context.classify` returns a `Category` and `context.select_context` takes one; `chat.ChatService.chat` tests `category is not Category.FOLLOW_UP` (skip graph read and extraction) and `category is Category.GENERAL` (retrieve all categories); `llm.extract_by_rules` calls `classify(goal)` to pick the category of a stated goal and maps `Category.FOLLOW_UP` to `Category.GENERAL` so the emitted candidate stays inside `MEMORY_CATEGORIES`. The enum fixes membership, not precedence: the order in which keywords are tried is the order of the dict literal in `context._KEYWORDS`, described in [Query understanding](04-query-understanding-and-context-selection.md).

### `MEMORY_CATEGORIES`

```python
MEMORY_CATEGORIES = {c.value for c in Category if c is not Category.FOLLOW_UP}
```

Nine plain `str` values (the `.value`, not the enum members). Consumers: `memory.validate` drops any candidate whose lower-cased `category` is not in the set; `llm._extract_system` renders `", ".join(sorted(MEMORY_CATEGORIES))` into `EXTRACT_PROMPT` for both real providers; the `MemoryCandidate.category` field description is built from the same expression at import time, so the JSON schema sent for structured output reads `one of: astrology, career, finance, general, health, interests, language, profile, relationships`.

Why `general` is allowed. A statement can be durable without belonging to a named life area; `extract_by_rules` emits `general.goal` for any "I plan to ..." whose text matches no keyword. `CLAUDE.md` records the invariant: "`general` is a valid memory category, `follow_up` is not." One consequence follows from `chat.py` passing `search = None` only for `general` queries: a memory stored with `category = general` is retrieved on `general` queries (which read every category) and on no others, because every other query filters on its own category.

Why `follow_up` is not. `follow_up` describes the turn, not the user: it means "this message refers back to the previous answer". Nothing durable is learned from such a turn, so `ChatService.chat` skips extraction entirely for it (`if not degraded and category is not Category.FOLLOW_UP`), and a memory could never legitimately carry it. Excluding it from `MEMORY_CATEGORIES` makes `validate` drop any extractor that emits it anyway, and the `FOLLOW_UP → GENERAL` remap in `extract_by_rules` keeps the rule extractor honest when a goal's text happens to look like a follow-up.

### `MEMORY_TYPES` and `type` versus `category`

```python
MEMORY_TYPES = {"fact", "goal", "preference", "interest"}
```

TDD §5.1: "`type` answers 'what kind of memory' (`fact | goal | preference | interest`); `category` answers 'which life area'." So `career.goal` is `type = goal`, `category = career`; `language.preferred` is `type = preference`, `category = language`; `profile.name` is `type = fact`, `category = profile`; `interests.cricket` is `type = interest`, `category = interests`.

Consumers: `memory.validate` drops candidates whose lower-cased `type` is not in the set. That is the only place `type` changes behaviour. It is not part of the logical identity `(user_id, category, key)`: `brain.FIND_ACTIVE` matches on `category`, `key` and `status` only, so a candidate with the same key and a different type supersedes the existing memory like any other value change. Retrieval and rendering ignore it (`context._render` shows `[{category}] {key} = {value}`), and it is surfaced to clients only through `MemoryOut.type` on `GET /users/{user_id}/memories`.

The four values are spelled out as literals in two other places, `MemoryCandidate.type`'s description (`"one of: fact, goal, preference, interest"`) and `EXTRACT_PROMPT` (`type: fact | goal | preference | interest`), neither of which is derived from the set. See Known limits.

### `PROFILE_FIELDS`

```python
PROFILE_FIELDS = ("name", "date_of_birth", "time_of_birth", "birth_place", "preferred_language", "sun_sign")
```

The property names of the `Profile` node, in the same order as the `UserProfile` field declarations. Consumers: `brain._profile` rebuilds a `UserProfile` from a Neo4j node with `UserProfile(**{k: node.get(k) for k in PROFILE_FIELDS})`, so a property missing on the node becomes `None`; `context._PROFILE_FIELDS_FOR` uses the whole tuple as the field set for `profile` and `astrology` queries (TDD §4.6 table: "all fields").

### `PROFILE_KEYS`

```python
# Memory keys that are structured profile facts and therefore live on the Profile node (TDD §7.0).
PROFILE_KEYS = {
    "profile.name": "name",
    "profile.date_of_birth": "date_of_birth",
    "profile.time_of_birth": "time_of_birth",
    "profile.birth_place": "birth_place",
}
```

The single consumer is `memory.remember`, which splits the validated candidates by key:

```python
profile_fields = {PROFILE_KEYS[c.key]: c.value for c in valid if c.key in PROFILE_KEYS}
if profile_fields:
    await brain.upsert_profile(user_id, profile_fields)
    changed += len(profile_fields)
for c in valid:
    if c.key not in PROFILE_KEYS and await brain.upsert_memory(user_id, c, source_message_id) != "unchanged":
        changed += 1
```

Candidates with one of the four keys are collected into one `upsert_profile` call and counted as one update each; every other candidate becomes a `Memory` upsert. Routing looks at the key alone; the candidate's `category` has already passed the `MEMORY_CATEGORIES` check in `validate` but is not consulted here (TDD §7.0 phrases the rule as "`category = profile` and a key in {...}"; the code implements the key half).

Why these four and not `preferred_language`. TDD §7.0: "Structured stable facts belong in a structured place, and this is what lets a date of birth stated in chat produce a sun sign. When `date_of_birth` changes, `sun_sign` is recomputed. Everything else, including `language.preferred`, is a `Memory` and follows the supersede lifecycle above, which is what makes PRD §7.3 (English then Hindi) demonstrable." `CLAUDE.md` says the same in one line: "`language.preferred` stays a Memory so the supersede flow is demonstrable." A language stated in chat therefore becomes the memory `language.preferred` (the rule extractor's key; `EXTRACT_PROMPT` gives the same example) with an `ACTIVE`/`SUPERSEDED` history, while `Profile.preferred_language` is set only through `POST /users` (`main.UserUpsert`). On a `language` query both paths can surface: `ChatService.chat` retrieves memories in the `language` category and `select_context` selects the profile fields `name` and `preferred_language`. `sun_sign` is likewise absent because it is derived by `brain._with_sun_sign`, never stated.

### `ChatMessage`

```python
@dataclass
class ChatMessage:
    role: str  # "user" | "assistant"
    content: str
    id: str = ""
```

`role` is a plain string with two values in practice, both of which the provider SDKs accept verbatim. `id` defaults to the empty string and is set in exactly one place: `ChatService.chat` creates the current turn as `ChatMessage("user", message, id=str(uuid.uuid4()))`, and the assistant reply as `ChatMessage("assistant", reply)` with no id. That one id is the provenance chain: it is passed as `source_message_id` to `memory.remember`, then to `brain.upsert_memory`, and stored on the new `Memory` by `brain._new_memory`. `SessionStore` keeps `ChatMessage` objects in a bounded deque, `select_context` places `[*recent, current]` in `LLMRequest.messages`, and both real providers map each to `{"role": m.role, "content": m.content}`; `id` never reaches the LLM.

### `UserProfile`

```python
@dataclass
class UserProfile:
    name: str | None = None
    date_of_birth: str | None = None  # ISO YYYY-MM-DD
    time_of_birth: str | None = None
    birth_place: str | None = None
    preferred_language: str | None = None
    sun_sign: str | None = None

    def fields(self) -> dict[str, str]:
        return {k: v for k, v in vars(self).items() if v}
```

Every field is an optional string. `fields()` returns the populated ones, dropping `None` and the empty string alike (`if v`). It is a read model: `SharedBrain.upsert_profile` takes a partial `dict[str, str]`, not a `UserProfile`, so writing `{"birth_place": "Delhi"}` leaves `name` and `sun_sign` untouched (`InMemoryBrain.upsert_profile` does `setattr` per key; `Neo4jBrain` runs `SET p += $fields`). Consumers: `brain._profile` and `InMemoryBrain` build it; `context.select_context` filters `profile.fields()` by the category's wanted set and renders `- date of birth: 1995-08-15` style lines into the system prompt; `main.upsert_user` returns `ProfileOut(user_id=req.user_id, **vars(profile))`.

`date_of_birth` is an ISO `YYYY-MM-DD` string by construction rather than by type: `brain._with_sun_sign` rewrites it to `dob.isoformat()` whenever the incoming value parses, and `main.upsert_user` converts the `date` it validated with `.isoformat()` before calling the brain. `time_of_birth` is free text; nothing in the codebase parses it.

### `MemoryCandidate`

```python
class MemoryCandidate(BaseModel):
    """What the extractor proposes. Pydantic because it doubles as the LLM structured-output schema."""

    key: str = Field(description="dotted <category>.<slug>, e.g. career.goal, language.preferred, profile.name")
    category: str = Field(description="one of: " + ", ".join(sorted(MEMORY_CATEGORIES)))
    type: str = Field(description="one of: fact, goal, preference, interest")
    value: str = Field(description="short normalized phrase; dates as YYYY-MM-DD")
    target_timeframe: str | None = Field(description="resolved year or period the user gave, else null")
    confidence: float = Field(description="0 to 1: certainty this is explicit and durable")
    reason: str = Field(description="one short phrase quoting or paraphrasing the user's statement")
```

| Field | Type | Required | Description the model sees | Persisted on `Memory` |
|---|---|---|---|---|
| `key` | `str` | yes | dotted `<category>.<slug>`, with examples | yes |
| `category` | `str` | yes | the nine `MEMORY_CATEGORIES`, sorted, generated at import | yes |
| `type` | `str` | yes | the four types, as a literal string | yes |
| `value` | `str` | yes | short normalized phrase; dates as `YYYY-MM-DD` | yes |
| `target_timeframe` | `str \| None` | yes (the key must be present; `null` is allowed) | resolved year or period, else null | yes |
| `confidence` | `float` | yes | 0 to 1, as prose; no numeric bound on the field | yes |
| `reason` | `str` | yes | one short phrase quoting the user | no |

All seven fields are required in the generated JSON schema (`MemoryCandidate.model_json_schema()["required"]` lists all of them) because none has a default; `target_timeframe` being `str | None` makes `null` a legal value, not the key optional. Every `Field(description=...)` string is emitted into that schema, which is how the field descriptions "feed the model".

The schema reaches the LLM by two routes in `app/llm.py`. `_Extraction` wraps it:

```python
class _Extraction(BaseModel):
    memories: list[MemoryCandidate]
```

`AnthropicLLM.extract_memories` passes `output_format=_Extraction` to `messages.parse`, so the provider constrains generation to the schema and the SDK returns `resp.parsed_output.memories` already validated. `OpenAICompatibleLLM.extract_memories` requests `json_object` mode, spells the shape out in `_JSON_SHAPE`, strips code fences and calls `_Extraction.model_validate_json(raw)`; a `ValidationError` becomes `LLMError`. `MockLLM` builds candidates directly through `llm._cand`. Downstream, `memory.validate` normalizes candidates in place (`c.key, c.category, c.type, c.value = c.key.strip().lower(), ...`), which works because Pydantic models are mutable by default; `brain.upsert_memory` reads `category`, `key`, `value`, `confidence`; `brain._new_memory` copies everything except `reason` into a `Memory`. Tests use `model_copy(update=...)` to derive variants.

Why numeric constraints are kept out. `CLAUDE.md`: "Keep numeric constraints out of its fields; `memory.validate` enforces them." The only numeric rule in the system is the `MIN_CONFIDENCE` threshold (`c.confidence < min_confidence` in `validate`), applied per candidate so that one weak candidate is dropped while the rest of the batch survives. A `ge=0, le=1` bound on the field would instead fail validation of the whole `_Extraction` payload and, on the OpenAI-compatible path, turn one out-of-range number into an `LLMError` that discards every candidate from that message (inferred from the code paths; the rationale is not recorded beyond the `CLAUDE.md` instruction). The same reasoning explains `category: str` rather than `category: Category`: `validate` lower-cases before the membership test, and `test_validate_filters_and_normalizes` requires `"CAREER"` to be accepted and normalized, which an enum-typed field would have rejected at parse time (inferred).

### `Memory`

```python
@dataclass
class Memory:
    id: str
    key: str
    category: str
    type: str
    value: str
    target_timeframe: str | None
    confidence: float
    status: str  # ACTIVE | SUPERSEDED
    source_message_id: str | None
    created_at: datetime
    updated_at: datetime
```

A `Memory` is constructed in two places, both in `app/brain.py`: `_new_memory(cand, source_message_id)` copies the candidate, assigns `id=str(uuid.uuid4())`, `status="ACTIVE"` and `created_at = updated_at = datetime.now(timezone.utc)`; `_memory(node)` rebuilds one from a Neo4j node, calling `.to_native()` on the two `neo4j.time.DateTime` properties (a `CLAUDE.md` gotcha). When `Neo4jBrain` persists a new memory it takes `vars(mem)` minus the two `_at` fields and lets Cypher set `created_at`/`updated_at` with `datetime()` server-side. `InMemoryBrain` mutates the dataclass directly on supersede (`old.status, old.updated_at = "SUPERSEDED", _now()`) and on duplicate (`old.confidence = max(old.confidence, cand.confidence)`).

`status` is a plain string with two values, `ACTIVE` and `SUPERSEDED`, which appear as literals in the Cypher constants `SEARCH_MEMORIES`, `FIND_ACTIVE` and `SUPERSEDE`. Provenance is `source_message_id`; the `SUPERSEDES` relationship between a new and an old memory exists only in the graph (`CREATE_MEMORY`), not as a field on the dataclass and not at all in `InMemoryBrain`. Readers: `context.select_context` (`m.key` into `context_used`), `context._render` (`category`, `key`, `value`, `target_timeframe`), `main.list_memories` (`MemoryOut(**vars(m))`).

### `LLMRequest`

```python
@dataclass
class LLMRequest:
    system_prompt: str  # fixed role text + rendered profile/memory context
    messages: list[ChatMessage]  # recent turns followed by the current user message
```

Built once, in `context.select_context`, as `LLMRequest(system, [*recent, current])` where `system` is `SYSTEM_PROMPT` plus a rendered `Context:` block. Consumed by the three `generate` implementations: `AnthropicLLM` sends `system_prompt` as the `system` parameter, `OpenAICompatibleLLM` prepends it as a `{"role": "system"}` message, and `MockLLM` records the whole request in `self.requests` and echoes the part after `"Context:"`, so tests can assert on the exact prompt (`llm.requests[-1].system_prompt` in `tests/test_chat.py`). TDD §27 gives the reason for the two-field shape: "Context rendered once in the prompt builder; providers stay thin and the prompt is testable."

### `sun_sign()`

```python
_SIGN_ENDS = [
    (1, 19, "Capricorn"), (2, 18, "Aquarius"), (3, 20, "Pisces"), (4, 19, "Aries"),
    (5, 20, "Taurus"), (6, 20, "Gemini"), (7, 22, "Cancer"), (8, 22, "Leo"),
    (9, 22, "Virgo"), (10, 22, "Libra"), (11, 21, "Scorpio"), (12, 21, "Sagittarius"),
    (12, 31, "Capricorn"),
]


def sun_sign(dob: date) -> str:
    return next(sign for month, day, sign in _SIGN_ENDS if (dob.month, dob.day) <= (month, day))
```

Each row is the last day, inclusive, on which a sign applies; the rows are in ascending date order, and the generator returns the first row whose end date is on or after `(dob.month, dob.day)`. Python compares the tuples lexicographically, so `(8, 23) <= (8, 22)` is false and 23 August falls through to Virgo. The year is never read.

| Sign | Range covered (tropical) | Row(s) |
|---|---|---|
| Capricorn | 22 Dec to 19 Jan | `(1, 19)` for 1 to 19 Jan; `(12, 31)` for 22 to 31 Dec |
| Aquarius | 20 Jan to 18 Feb | `(2, 18)` |
| Pisces | 19 Feb to 20 Mar | `(3, 20)` |
| Aries | 21 Mar to 19 Apr | `(4, 19)` |
| Taurus | 20 Apr to 20 May | `(5, 20)` |
| Gemini | 21 May to 20 Jun | `(6, 20)` |
| Cancer | 21 Jun to 22 Jul | `(7, 22)` |
| Leo | 23 Jul to 22 Aug | `(8, 22)` |
| Virgo | 23 Aug to 22 Sep | `(9, 22)` |
| Libra | 23 Sep to 22 Oct | `(10, 22)` |
| Scorpio | 23 Oct to 21 Nov | `(11, 21)` |
| Sagittarius | 22 Nov to 21 Dec | `(12, 21)` |

Capricorn appears twice because it straddles the year boundary: the first row catches January dates before any other sign, and the last row is the catch-all for late December, which also guarantees `next()` always finds a row and never raises `StopIteration` for a real `date`. 29 February satisfies `(2, 29) <= (3, 20)` and is Pisces.

Consumer: `brain._with_sun_sign`, called by both `upsert_profile` implementations.

```python
def _with_sun_sign(fields: dict[str, str]) -> dict[str, str]:
    """Normalize date_of_birth to ISO and recompute sun_sign whenever it is set."""
    dob = parse_date(fields["date_of_birth"]) if fields.get("date_of_birth") else None
    if dob:
        fields = {**fields, "date_of_birth": dob.isoformat(), "sun_sign": sun_sign(dob)}
    return fields
```

### `parse_date()`

```python
_DATE_FORMATS = ("%Y-%m-%d", "%d %B %Y", "%d %b %Y", "%B %d, %Y", "%B %d %Y", "%d/%m/%Y")


def parse_date(text: str) -> date | None:
    text = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", text.strip())
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None
```

Three steps. First, surrounding whitespace is stripped. Second, an English ordinal suffix that directly follows a digit and ends at a word boundary is removed (`15th` → `15`, `1st` → `1`, `2nd` → `2`, `3rd` → `3`); the `\b` keeps the substitution from touching letters inside words. Third, the six formats are tried in order and the first `strptime` that succeeds wins; if all raise `ValueError` the result is `None`. The function never raises for a string input.

| Format | Accepts | Notes |
|---|---|---|
| `%Y-%m-%d` | `1995-08-15`, `1995-8-5` | ISO; `strptime` tolerates unpadded month and day |
| `%d %B %Y` | `15 August 1995`, `1 August 1995` (from `1st August 1995`) | full month name, case-insensitive |
| `%d %b %Y` | `15 Aug 1995` (from `15th Aug 1995`) | abbreviated month, day first only |
| `%B %d, %Y` | `August 15, 1995` | US order with comma |
| `%B %d %Y` | `August 15 1995` | US order without comma |
| `%d/%m/%Y` | `15/08/1995` | day first; `08/15/1995` is rejected because 15 is not a month |

Not accepted, and therefore `None`: `Aug 15, 1995` (abbreviated month is only paired with day-first order), `15-08-1995`, `15th of August 1995`, `1995-08-15T00:00`, and anything without a full year such as `sometime in 1995`.

Consumers: `brain._with_sun_sign` (above) and `llm.extract_by_rules`, where a "born on ..." match is only emitted as a `profile.date_of_birth` candidate if it parses, with the ISO form as the value:

```python
if (m := _BORN_ON.search(message)) and (dob := parse_date(m[1])):
    out.append(_cand("profile.date_of_birth", "profile", "fact", dob.isoformat(), confidence=0.95))
```

### Walkthrough: from "born on 15 August 1995" to `sun_sign = Leo`

1. `ChatService.chat` classifies the PRD sentence and, after generating, calls `llm.extract_memories`. With the mock provider, `_BORN_ON` captures `15 August 1995`; `parse_date` matches `%d %B %Y`; the candidate is `MemoryCandidate(key="profile.date_of_birth", category="profile", type="fact", value="1995-08-15", ...)`. A real provider is instructed to emit `dates as YYYY-MM-DD` by both the prompt and the field description.
2. `memory.validate` keeps it (`profile` is in `MEMORY_CATEGORIES`, `fact` in `MEMORY_TYPES`, confidence 0.95 above the 0.6 default).
3. `memory.remember` sees `profile.date_of_birth` in `PROFILE_KEYS` and calls `brain.upsert_profile(user_id, {"name": "Rahul", "date_of_birth": "1995-08-15", "birth_place": "Delhi"})`.
4. `brain._with_sun_sign` parses the ISO string again, and returns the dict with `date_of_birth = "1995-08-15"` and `sun_sign = "Leo"`, because `(8, 15) <= (8, 22)`.
5. The `Profile` node (or the `UserProfile` in `InMemoryBrain`) now carries both. On the next non-follow-up query, `select_context` includes `sun_sign` for every category except `language` and `follow_up`, which is why `astrology` appears in `context_used` for a career question.

`POST /users` reaches step 4 by a shorter path: `UserUpsert.date_of_birth` is a Pydantic `date`, so malformed input is a `422` before the brain is called, and `main.upsert_user` passes `v.isoformat()`.

## Contracts and invariants

- `Category` has exactly the ten TDD §4.5 members; each member equals its string value; `MEMORY_CATEGORIES == {c.value for c in Category} - {"follow_up"}` and contains plain `str`, not enum members.
- `MEMORY_TYPES` is exactly `{"fact", "goal", "preference", "interest"}`; a candidate with any other `type` (after lower-casing) never reaches the brain.
- `PROFILE_KEYS.values()` is a strict subset of `PROFILE_FIELDS`: `preferred_language` and `sun_sign` are `Profile` fields that no chat-stated memory key routes to. Every `PROFILE_KEYS` key starts with `profile.`.
- `UserProfile` fields are all `str | None`; `fields()` never contains `None` or `""`. `date_of_birth` is `YYYY-MM-DD` whenever it was written through `brain.upsert_profile` with a value `parse_date` accepts (see Failure modes for the other case). `sun_sign` is one of the twelve names in `_SIGN_ENDS` or `None`; no API or chat path sets it directly, and it is rewritten whenever `date_of_birth` is written with a parseable value.
- `MemoryCandidate`: all seven fields are required; `target_timeframe` may be `null` but the key must be present; `category` and `type` are free strings at parse time and are only constrained by `memory.validate`; `confidence` has no bound in the schema and only a lower bound (`MIN_CONFIDENCE`) anywhere; `reason` is never persisted.
- `Memory.status` is `"ACTIVE"` or `"SUPERSEDED"`, and at most one `ACTIVE` memory exists per `(user_id, category, key)`; both are enforced by `brain.upsert_memory`, not by the dataclass (CLAUDE.md, "Invariants the tests enforce"). `Memory.id` is a UUID4 string. `created_at`/`updated_at` are timezone-aware in `InMemoryBrain` (`timezone.utc`) and driver-native `datetime` from Neo4j. A memory's `confidence` only ever rises while it stays `ACTIVE` (`max` on duplicate).
- `ChatMessage.role` is `"user"` or `"assistant"`; only the current user turn created in `ChatService.chat` has a non-empty `id`, so a `Memory.source_message_id` always names a user message.
- `LLMRequest.messages` ends with the current user message; `MockLLM.generate` depends on this (`request.messages[-1]`). `system_prompt` always contains the `Context:` marker that `select_context` appends, and `MockLLM.generate` splits on its first occurrence (`split("Context:", 1)`).
- `sun_sign` is a pure, total function of `(month, day)` over valid dates. `parse_date` returns a `date` or `None` and raises nothing for `str` input.

## Design decisions and alternatives rejected

**One taxonomy for queries and memories, not two.** Recorded in the `Category` docstring and TDD §5.1: `category` "uses the same taxonomy as query classification (§4.5), so retrieval is a direct equality match." The alternative, a query-intent enum and a separate memory-category enum joined by a mapping table, would have added a translation step between `classify` and `SEARCH_MEMORIES` for no gain, since TDD §4.5 says "categories exist to narrow the graph search" and nothing else. The accepted cost is one member (`follow_up`) that is meaningless for memory, handled by the `MEMORY_CATEGORIES` carve-out and the `FOLLOW_UP → GENERAL` remap in `extract_by_rules`, and one member (`general`) that means "retrieve everything" as a query and "no specific life area" as a memory.

**Generic `Memory` nodes with `type`/`category` properties, not typed `Goal`/`Preference`/`Interest` nodes.** TDD §5.1 defers typed nodes "until a query needs to traverse them; adding them is additive (a label plus a relationship per memory) and does not change the API." TDD §27 records the reduction: "§6 already recommended generic memories; the typed-node list contradicted it." README, Trade-offs: "typed nodes add nothing until a traversal needs them." For this layer the consequence is a single `Memory` dataclass with a `type` field instead of a class per kind, and a single `MemoryCandidate` schema for the extractor.

**Dataclasses for internal types, Pydantic only for `MemoryCandidate`.** `CLAUDE.md`: "`MemoryCandidate` is Pydantic because it is the structured-output schema for the real extractor; everything else is a dataclass." `AnthropicLLM` needs a `BaseModel` for `messages.parse(..., output_format=_Extraction)` and `OpenAICompatibleLLM` for `model_validate_json`, and `Field(description=...)` is the mechanism that puts guidance in front of the model. `ChatMessage`, `UserProfile`, `Memory` and `LLMRequest` are constructed by this codebase, never parsed from untrusted input, so validation would cost a schema and a parse for nothing (inferred). The API-boundary schemas in `app/main.py` are Pydantic for the opposite reason: they are parsed from client input.

**Schema fields as loose `str`/`float`, constraints in `memory.validate`.** `CLAUDE.md` fixes the rule; the effect is that a single malformed candidate is dropped with a log line rather than aborting the batch, and that `validate` can normalize case before checking membership (inferred, see How it works).

**ISO strings for dates on `UserProfile`, not `date` objects.** TDD §5.2 types the `Profile` properties as `date | null` and `time | null`; the code stores strings. No rationale is recorded. Inferred: the value already exists as a string at every boundary it crosses (the candidate `value`, the `SET p += $fields` Cypher parameter, the `ProfileOut.date_of_birth: str | None` response), so a `date` field would have meant converting in `brain._profile`, `brain._with_sun_sign`, `main.upsert_user` and `context.select_context`, while `_with_sun_sign` already normalizes the stored form to ISO. `time_of_birth` is never parsed because nothing consumes it yet.

**`sun_sign` on `UserProfile`, not a separate `AstrologyProfile`.** TDD §11 sketches an `AstrologyProfile` dataclass with one field; the implementation puts `sun_sign` on the `Profile` node and `UserProfile` (README, Graph schema). Inferred: one field does not justify a type, and keeping it on the profile is what lets `context.select_context` treat it as just another profile field with its own `astrology` tag.

**`Memory.status` as a string, not an enum.** Inferred: the two literals are embedded in Cypher constants (`m.status = 'ACTIVE'`, `SET m.status = 'SUPERSEDED'`) and compared as strings in `InMemoryBrain`; an enum would need `.value` at each site and buy nothing the tests do not already pin.

**`reason` on the candidate but not on `Memory`.** TDD §7.1's extraction example includes `reason` ("Explicitly stated by user"). Nothing reads it after extraction and `brain._new_memory` does not copy it. Inferred: it exists to make the model justify each memory at extraction time, not as a stored property.

**`LLMRequest` reduced to two fields.** TDD §27, row §4.7: "`LLMRequest` reduced to `system_prompt` + `messages`. Context rendered once in the prompt builder; providers stay thin and the prompt is testable." The v1 shape is not preserved in the repo; TDD §4.7's rationale implies it carried context that each provider rendered itself (inferred).

## Failure modes and degraded behavior

| Situation | What happens | Where |
|---|---|---|
| `date_of_birth` stated in chat does not parse (for example `summer 1995` from a real provider) | The rule extractor never emits it (walrus guard). If a provider emits it, `remember` still routes it to `upsert_profile`; `_with_sun_sign` returns the fields unchanged, so the raw string is stored as `date_of_birth` and `sun_sign` is not recomputed; a `sun_sign` from an earlier valid date stays as it was | `llm.extract_by_rules`, `brain._with_sun_sign` |
| `date_of_birth` malformed on `POST /users` | `422` from Pydantic (`UserUpsert.date_of_birth: date`); the brain is never called | `main.UserUpsert`, `tests/test_chat.py::test_invalid_payload_is_422` |
| Candidate `category` outside `MEMORY_CATEGORIES` (including `follow_up`) or `type` outside `MEMORY_TYPES` | Dropped with `log.info("dropping off-taxonomy candidate ...")`; the rest of the batch proceeds; `memory_updates` is simply lower | `memory.validate` |
| Candidate below `MIN_CONFIDENCE`, or with empty `key` or `value` after stripping | Dropped silently | `memory.validate` |
| OpenAI-compatible provider returns JSON that does not fit `_Extraction`, including a candidate that omits the `target_timeframe` key instead of writing `null` | `ValidationError` becomes `LLMError`; `ChatService.chat` logs "memory extraction failed, response still returned"; the response is returned with zero writes | `llm.OpenAICompatibleLLM.extract_memories`, `chat.ChatService.chat` |
| Anthropic structured output returns no parsed body | `extract_memories` returns `[]`; nothing is written | `llm.AnthropicLLM.extract_memories` |
| No profile exists for the user | `get_profile` returns `None`; `select_context` uses `{}` for facts; no `user_profile` or `astrology` tag | `context.select_context`, `tests/test_chat.py::test_8_missing_profile_is_fine` |
| A `Profile` node lacks some property | `brain._profile` reads it as `None` via `node.get` | `brain._profile` |
| Extractor emits a `profile.*` key that is not in `PROFILE_KEYS` (for example `profile.preferred_language` or `profile.sun_sign`) | Passes `validate` (`profile` is a valid category) and is stored as a `Memory` node, not on the `Profile` | `memory.remember` |

`sun_sign()` has no failure mode for a `date`: the trailing `(12, 31, "Capricorn")` row guarantees a match. `parse_date()` degrades only to `None`.

## Configuration

None. Neither module reads the environment. The knobs that shape how these types are used live in `app/config.py` and are applied elsewhere: `MIN_CONFIDENCE` (default 0.6) in `memory.validate`, `MEMORY_LIMIT` (default 8) as the `LIMIT` on retrieval, `RECENT_LIMIT` (default 10) as the deque cap on `ChatMessage` history. See [Configuration and deployment](08-configuration-and-deployment.md).

## Tests that pin this layer

Run with `uv run pytest -q`; no services are needed (48 passed, 1 skipped without `NEO4J_TEST_URI`).

| Test | What it asserts about this layer |
|---|---|
| `tests/test_units.py::test_sun_sign` | Six boundary dates: 15 Aug → Leo; 23 Aug → Virgo (Leo's row ends 22 Aug); 22 Dec → Capricorn (the `(12, 31)` row); 19 Jan → Capricorn and 20 Jan → Aquarius (the `(1, 19)` row is inclusive); 21 Mar → Aries |
| `tests/test_units.py::test_parse_date_formats` | `15 August 1995`, `1995-08-15` and `August 15, 1995` parse to the same date; `15th Aug 1995` exercises ordinal stripping plus `%d %b %Y`; `sometime in 1995` is `None` |
| `tests/test_units.py::test_validate_filters_and_normalizes` | `MEMORY_CATEGORIES` (`weather` dropped) and `MEMORY_TYPES` (`rumor` dropped) are enforced; `Career.Goal`/`CAREER`/`Goal` are lower-cased and ` switch jobs ` stripped; confidence 0.3 and an all-whitespace value are dropped |
| `tests/test_units.py::test_upsert_outcomes` | `Memory.status` moves `ACTIVE → SUPERSEDED`; `source_message_id` on the active memory is the newest message's id; an unchanged value raises `confidence` to the max; `MemoryCandidate.model_copy` produces variants |
| `tests/test_units.py::test_upsert_profile_derives_sun_sign` | `_with_sun_sign` turns `15 August 1995` into `1995-08-15` and `Leo`; a later partial update (`birth_place`) keeps `name` and `sun_sign` |
| `tests/test_units.py::test_classify` | `classify` returns `Category` members (compared with `is`) for eleven messages, including the `FOLLOW_UP` and `GENERAL` fall-throughs |
| `tests/test_chat.py::test_2_first_message_creates_durable_memory_and_profile` | `PROFILE_KEYS` routing: the PRD sentence yields `memory_updates == 4` (three profile fields plus `career.goal`), the profile reads `("Rahul", "1995-08-15", "Delhi", "Leo")`, and the goal's `target_timeframe` is next year |
| `tests/test_chat.py::test_7_user_correction_supersedes` | `language.preferred` is a `Memory`, not a profile field: English becomes `SUPERSEDED`, Hindi `ACTIVE`, and a `language` query reports `context_used == ["language.preferred"]` |
| `tests/test_chat.py::test_profile_endpoint_feeds_astrology_context` | `POST /users` with `1995-08-15` returns `sun_sign == "Leo"` and an astrology question then reports `["user_profile", "astrology"]` |
| `tests/test_chat.py::test_rule_extractor_matches_prd_example` | The mock extractor's candidate keys and values for the PRD sentence, including the ISO `profile.date_of_birth` |
| `tests/test_chat.py::test_invalid_payload_is_422` | `POST /users` rejects `not-a-date` at the schema, so `parse_date` is never the API's date validator |
| `tests/test_openai_llm.py::test_generate_sends_system_then_turns` | `LLMRequest.system_prompt` becomes the first `system` message and `ChatMessage.role`/`content` pass through in order |
| `tests/test_openai_llm.py::test_extract_requests_json_mode_and_tolerates_fences`, `::test_extract_invalid_output_is_llm_error` | JSON that fits `_Extraction` yields `MemoryCandidate`s; JSON that does not is an `LLMError` |
| `tests/test_neo4j.py::test_neo4j_roundtrip` (skipped without `NEO4J_TEST_URI`) | The same profile-with-sun-sign and supersede assertions against a live Neo4j, through `brain._profile` and `brain._memory` |

See [Testing and verification](09-testing-and-verification.md) for the full suite.

## Known limits and future work

`ponytail:` markers in the covered files. `app/models.py` has none. `app/astrology.py` has one, at the top of the module:

```python
# ponytail: tropical sun sign only; a real engine (sidereal rashi, moon sign, nakshatra) replaces this module.
```

Upgrade path: TDD §11 ("a real astrology engine can replace it later without changing chat orchestration") and README, Production path ("a real astrology engine behind `astrology.py`"). The model already carries the inputs such an engine needs and the stub ignores: `UserProfile.time_of_birth` and `UserProfile.birth_place` are stored and rendered into the prompt on `profile` and `astrology` queries, but read by no computation. A replacement could keep `sun_sign(dob)` and `parse_date(text)` as the entry points `brain._with_sun_sign` calls, or extend `_with_sun_sign` to derive further `Profile` fields and add them to `PROFILE_FIELDS`.

Other limits, all observable in the code:

- `sun_sign` ignores the year; cusp dates that shift by a day between years and any non-tropical zodiac are not modelled.
- `parse_date` reads `dd/mm/yyyy` only, so `01/02/1995` is 1 February; it does not accept `Aug 15, 1995`, dashed `15-08-1995`, `15th of August 1995`, or a date with a time component.
- `MemoryCandidate.confidence` has no upper bound anywhere: a value above 1 passes both Pydantic and `validate` and is stored.
- `MEMORY_TYPES` is duplicated as literal text in `MemoryCandidate.type`'s description and in `llm.EXTRACT_PROMPT`; adding a type means editing three places.
- `PROFILE_KEYS` routes by key alone. Extractor keys `profile.preferred_language` or `profile.sun_sign` would become `Memory` nodes rather than updating the `Profile`.
- A `general` memory is only ever retrieved on `general` queries, because every other query filters on its own category.
- `Memory` has no `valid_from`/`valid_until` (PRD §7.1 optional, TDD §5.2 recommended). README, Production path names decay as future work.
- `Memory.source_message_id` points at a `ChatMessage.id` that exists only in the process-local, ten-message `SessionStore`; there is no `:Message` node (TDD §5.1 defers it), so provenance cannot be resolved back to text after the deque rolls over or the process restarts.
- The `SUPERSEDES` link is not represented on `Memory` and is not recorded by `InMemoryBrain`; only Neo4j holds it.
- `time_of_birth` is free text with no validation or normalization.
