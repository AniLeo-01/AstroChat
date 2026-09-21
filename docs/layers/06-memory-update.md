# Memory update (extraction, validation, persistence)

After the LLM has answered, this layer decides what the user's latest message taught the system that is worth keeping, and writes it into the Shared Brain. It is the last stage of the request flow `Chat -> Context Selection -> Shared Brain -> LLM -> Response -> Memory Update` and the implementation of the memory lifecycle drawn in TDD §7. The pipeline is: the provider's `extract_memories()` turns the user's message into `MemoryCandidate` objects (a structured-output call for the real providers, a regex extractor for the mock), `memory.validate()` normalizes them and drops anything low-confidence, malformed or off-taxonomy, and `memory.remember()` routes the survivors: the four structured profile facts go to the `Profile` node in one `upsert_profile` call, everything else becomes a `Memory` node through `upsert_memory`, whose create / refresh / supersede decision is made inside the brain. The count of writes that changed state is returned to the caller as `memory_updates`.

**Files:** `app/memory.py`, extraction parts of `app/llm.py` (`EXTRACT_PROMPT`, `_extract_system`, `_Extraction`, `_JSON_SHAPE`, `_FENCE`, the `extract_memories` methods, `extract_by_rules` and its regexes), `MemoryCandidate`, `MEMORY_CATEGORIES`, `MEMORY_TYPES` and `PROFILE_KEYS` in `app/models.py`.

**Depends on:** [Domain model](03-domain-model.md) (`MemoryCandidate`, `Category`, `PROFILE_KEYS`), [Shared Brain](05-shared-brain.md) (`upsert_profile`, `upsert_memory`, `BrainUnavailable`), [LLM providers](07-llm-providers.md) (`extract_memories`, `LLMError`), [Query understanding](04-query-understanding-and-context-selection.md) (`classify()` is reused by the rule extractor). **Used by:** [Orchestration](02-orchestration-and-short-term-context.md) (`ChatService.chat()` is the only caller of `remember()`), [API layer](01-api-layer.md) (surfaces the count as `memory_updates`). Tests are catalogued in [Testing and verification](09-testing-and-verification.md); configuration in [Configuration and deployment](08-configuration-and-deployment.md).

## What this layer is

The memory update layer is the write path of the Shared Brain. Where [context selection](04-query-understanding-and-context-selection.md) reads the graph before generation, this layer writes to it after generation. It has three parts that live in three modules but form one pipeline:

| Part | Where | Responsibility |
|---|---|---|
| Extraction | `LLMProvider.extract_memories(message, today)` in `app/llm.py`, one implementation per provider | Turn the user's message into zero or more `MemoryCandidate` proposals. `AnthropicLLM` and `OpenAICompatibleLLM` ask the model with `EXTRACT_PROMPT`; `MockLLM` delegates to `extract_by_rules()`. |
| Validation | `validate()` in `app/memory.py` | Normalize case and whitespace, then drop candidates below `min_confidence`, with an empty key or value, or with a category or type outside the taxonomy. |
| Persistence | `remember()` in `app/memory.py` | Route validated candidates: `PROFILE_KEYS` to `brain.upsert_profile`, the rest to `brain.upsert_memory` one by one. Return how many writes changed state. |

The candidate schema, `MemoryCandidate` in `app/models.py`, is the contract between the three parts. It is the one Pydantic model among otherwise dataclass domain types because it doubles as the structured-output schema handed to the Anthropic SDK (`messages.parse(..., output_format=_Extraction)`) and used to validate the OpenAI-compatible provider's JSON (`_Extraction.model_validate_json`). `CLAUDE.md` records the consequence: numeric constraints stay out of its fields so that `memory.validate` is the single place that enforces them.

The layer does not decide whether a candidate is new, a duplicate or a correction. That decision, and the `SUPERSEDES` link that preserves history, belongs to `upsert_memory` in the [Shared Brain](05-shared-brain.md). `remember()` only observes the brain's outcome (`"created" | "updated" | "unchanged"`) to count it.

## Why it exists

**The requirement.** PRD FR-7 ("Memory Update") says that after a response the system must evaluate whether the user's latest message contains durable information and be able to create a new memory, update an existing one, ignore transient or low-value information, associate memory with a category and entity, and preserve enough provenance to understand where the memory came from. PRD §2 frames the underlying problem: preserve useful information across sessions "without storing every message as permanent memory." PRD §7.1 lists the fields a durable memory carries (`type`, `value`, `category`, `confidence`, `source_message_id`, timestamps); §7.2 gives the quality rule ("High confidence + durable: store automatically. Medium confidence: store if the statement is explicit and actionable. Low confidence / speculative: do not store."); §7.3 requires that an explicit correction supersede the stale value while retaining provenance, with the English -> Hindi language example.

**The risk it mitigates.** PRD §13 names the risk "LLM invents memories" and the mitigation "Structured extraction schema + confidence threshold + user-only provenance". Each of the three is a concrete piece of this layer: the schema is `MemoryCandidate`/`_Extraction`, the threshold is `validate()`'s `min_confidence` check, and user-only provenance is `ChatService.chat()` passing only the user's message to `extract_memories` and stamping the resulting memories with the user message's id. The same section's "User corrections leave stale values" risk is closed by routing corrections through the brain's supersede semantics rather than overwriting.

**The design.** TDD §2 principle 3 states that "User statements are the source of truth for memory. Assistant-generated content must not become user memory by default." TDD §7 draws the lifecycle this layer implements (extraction -> normalize -> confidence and durability checks -> discard or find active memory by logical key -> create / refresh / supersede). TDD §7.0 adds the profile routing rule; §7.1 the extraction format; §7.2 and §7.3 what to remember and what not to. TDD §22 requires memory writes to be idempotent at the logical key `(user_id, category, key)` with corrections expressed as `old -> SUPERSEDED`, `new -> ACTIVE`, "so retrieval is deterministic and history/provenance is preserved." TDD §24 records the trade-off "Structured LLM + deterministic validation: better flexibility while protecting against obvious low-value memories."

**The problem in one sentence.** A raw chat transcript is a poor long-term memory: it is unbounded, full of filler, and mixes what the user said with what the assistant guessed. This layer converts a message into a small set of typed, keyed, confidence-scored facts that the retrieval side can filter by category and that a later correction can supersede cleanly.

## How it works

### The lifecycle, from user message to graph write

The numbered steps below follow `ChatService.chat()` in `app/chat.py` after the reply has been generated and appended to the session. Steps 1 to 3 are the caller's gate; 4 to 9 are this layer.

1. **Gate.** Extraction runs only when `not degraded and category is not Category.FOLLOW_UP`. `degraded` is set when the pre-response brain read failed (TDD §13.2: "Skip the post-response memory write when the pre-response read already failed: one logged failure per request, not two"). `follow_up` turns are skipped because, in the words of the comment in `chat.py`, there is "nothing durable to learn". `category` comes from `classify(message)` in [query understanding](04-query-understanding-and-context-selection.md).
2. **Source text.** The orchestrator calls `self.llm.extract_memories(message, date.today())`, where `message` is the raw string from the request body. The assistant's reply and the recent history are not passed. `date.today()` is the server's local date and is what lets the prompt resolve "next year".
3. **Provenance id.** Before generation the orchestrator built `current = ChatMessage("user", message, id=str(uuid.uuid4()))`; `current.id` is later passed to `remember()` as `source_message_id`.
4. **Extraction (provider-specific).** `AnthropicLLM.extract_memories` sends `_extract_system(today)` as the system prompt and the message as the single user turn to `messages.parse(..., output_format=_Extraction)`; the SDK returns candidates already conforming to the schema, or `parsed_output` is `None` and the method returns `[]`. `OpenAICompatibleLLM.extract_memories` sends the same system prompt with `_JSON_SHAPE` appended, requests `response_format={"type": "json_object"}`, strips a code fence with `_FENCE`, and validates with `_Extraction.model_validate_json`; a `ValidationError` becomes `LLMError`. `MockLLM.extract_memories` returns `extract_by_rules(message, today)`. All three return `list[MemoryCandidate]`.
5. **Validation.** `remember()` first calls `validate(candidates, min_confidence)`, which mutates each candidate in place (strip and lowercase `key`, `category`, `type`; strip `value`) and returns only those that pass the rule table below.
6. **Profile routing.** Every surviving candidate whose `key` is in `PROFILE_KEYS` is folded into one dict `{PROFILE_KEYS[c.key]: c.value}`; if that dict is non-empty, `brain.upsert_profile(user_id, profile_fields)` is called once and `len(profile_fields)` is added to the count. The brain's `_with_sun_sign` normalizes `date_of_birth` to ISO and recomputes `sun_sign` when a date of birth is present (see [Shared Brain](05-shared-brain.md)).
7. **Memory upserts.** Every surviving candidate whose key is not in `PROFILE_KEYS` goes to `brain.upsert_memory(user_id, c, source_message_id)`, one call per candidate. The brain looks up the active memory with the same `(user_id, category, key)` and returns `"created"` (none existed), `"unchanged"` (same value; it refreshes `updated_at` and keeps the higher confidence) or `"updated"` (different value; old marked `SUPERSEDED`, new `ACTIVE`, `(new)-[:SUPERSEDES]->(old)`). Outcomes other than `"unchanged"` add one to the count.
8. **Count.** `remember()` returns the integer; the orchestrator stores it in `ChatResult.memory_updates`, which the [API layer](01-api-layer.md) returns verbatim.
9. **Failure containment.** The orchestrator wraps steps 4 to 7 in `try` and catches exactly `LLMError` (logged, response still returned, count stays 0) and `BrainUnavailable` (logged, `degraded = True`, count stays 0). Details are in the failure modes section.

The whole of step 5 to 8 is the following function:

```python
async def remember(brain: SharedBrain, user_id: str, candidates: list[MemoryCandidate],
                   source_message_id: str, min_confidence: float) -> int:
    valid = validate(candidates, min_confidence)
    changed = 0
    profile_fields = {PROFILE_KEYS[c.key]: c.value for c in valid if c.key in PROFILE_KEYS}
    if profile_fields:
        await brain.upsert_profile(user_id, profile_fields)
        changed += len(profile_fields)
    for c in valid:
        if c.key not in PROFILE_KEYS and await brain.upsert_memory(user_id, c, source_message_id) != "unchanged":
            changed += 1
    return changed
```

### The candidate schema, field by field

`MemoryCandidate` in `app/models.py` is what every extractor produces and what `validate()` and the brain consume. Every field is required (Pydantic fields declared with `Field(description=...)` and no default are required even when their type admits `None`), so a provider that omits `target_timeframe` fails validation rather than defaulting silently.

| Field | Type | Purpose | What happens to it downstream |
|---|---|---|---|
| `key` | `str` | Logical identity of the fact within a category, in the form `<category>.<slug>`, e.g. `career.goal`, `language.preferred`, `interests.cricket`, `profile.name`. Two candidates with the same `(category, key)` refer to the same fact and the later one supersedes the earlier. | Lowercased and stripped by `validate()`. Decides routing in `remember()` (`PROFILE_KEYS` lookup). Stored on the `Memory` node and shown in `context_used`. |
| `category` | `str` | Which life area the fact belongs to; must be one of `MEMORY_CATEGORIES`, the query taxonomy minus `follow_up`. Sharing the taxonomy with `classify()` is what makes retrieval a plain equality filter (`models.Category` docstring). | Lowercased; off-taxonomy values are dropped with a log line. Part of the logical key used by `upsert_memory`. |
| `type` | `str` | What kind of memory: `fact`, `goal`, `preference` or `interest` (`MEMORY_TYPES`). TDD §5.1: "`type` answers 'what kind of memory'; `category` answers 'which life area'." | Lowercased; off-taxonomy values dropped. Stored on the node; not used for retrieval. |
| `value` | `str` | The fact itself as a "short normalized phrase; dates as YYYY-MM-DD". | Stripped only; case is preserved (`"Hindi"` stays `"Hindi"`). Empty after stripping means the candidate is dropped. Compared by exact string equality in `upsert_memory` to decide `unchanged` versus `updated`. |
| `target_timeframe` | `str \| None` | The resolved year or period the user gave, else `null`. Resolution is done by the extractor, using `today`, so "next year" arrives as a concrete year. | Passed through unchanged; stored on the node and rendered in the prompt as `(timeframe: 2027)` by `context._render`. |
| `confidence` | `float` | 0 to 1, "how certain you are that this is explicit and durable". This is where PRD §7.2's explicitness and durability judgement lives. | Compared against `min_confidence` in `validate()`. Stored on the node; the brain keeps the higher of old and new on an `unchanged` upsert; retrieval orders by it. |
| `reason` | `str` | "one short phrase quoting or paraphrasing the user's statement". | Not persisted: `brain._new_memory` copies every other field onto the `Memory` and the node schema (TDD §5.2) has no `reason` property. The rule extractor sets it to `"rule"`. Inferred purpose: it makes the model ground each candidate in the text and gives a human reading the raw extraction something to audit. |

The corresponding wire shape is `_Extraction`, a one-field wrapper (`memories: list[MemoryCandidate]`) so the model returns an object rather than a bare array, matching TDD §7.1's `{"memories": [...]}` example. The implementation adds `type` to that example's fields because `Memory.type` is on the node (TDD §5.1).

### The extraction prompt

`EXTRACT_PROMPT` in `app/llm.py` is a template with three placeholders, filled by `_extract_system(today)`:

```python
def _extract_system(today: date) -> str:
    return EXTRACT_PROMPT.format(today=today.isoformat(), next_year=today.year + 1,
                                 categories=", ".join(sorted(MEMORY_CATEGORIES)))
```

Walking through the prompt text:

| Prompt line | What it does |
|---|---|
| "Extract durable facts about the user from their message. Today is {today}." | Sets the task and anchors the date. With `today = 2026-09-21` the line reads "Today is 2026-09-21". |
| "Store only explicit, stable, future-useful information the user states about themselves: goals, preferences, interests, life plans, and stable profile facts (name, date/time/place of birth)." | The positive list, taken from PRD FR-7 "Examples of likely durable memories" and TDD §7.2. |
| "Ignore greetings, filler, questions, hypotheticals, speculation, and anything about the assistant." | The negative list, from PRD FR-7 "information normally not stored" and TDD §7.3. "Anything about the assistant" enforces TDD §2 principle 3 even within the user's own text. |
| "key: dotted <category>.<slug>, e.g. career.goal, language.preferred, interests.cricket, profile.name, profile.date_of_birth, profile.time_of_birth, profile.birth_place" | Fixes the key format and lists the four profile keys verbatim so that they match `PROFILE_KEYS` exactly and route to the Profile node. |
| "category: one of {categories}" | Rendered as `astrology, career, finance, general, health, interests, language, profile, relationships` (sorted `MEMORY_CATEGORIES`). `follow_up` is deliberately absent. |
| "type: fact \| goal \| preference \| interest" | The `MEMORY_TYPES` set. |
| "value: short normalized phrase; dates as YYYY-MM-DD" | Keeps values comparable across turns (the brain compares values by equality) and lets `parse_date` handle a date of birth. |
| "target_timeframe: the resolved year or period when the user gives one ("next year" is {next_year}), else null" | Rendered as `("next year" is 2027)` for a 2026 date. This is how PRD §5.1's "career goal with target year 2027" is produced by the model rather than by post-processing. |
| "confidence: 0 to 1, how certain you are that this is explicit and durable" | Defines the score so that PRD §7.2's "explicit and actionable" test is folded into the number that `validate()` thresholds. |
| "Return an empty list when nothing qualifies." | Makes the no-memory case a normal, non-error result. |

For the OpenAI-compatible provider, `_JSON_SHAPE` is appended to the system prompt. It spells out the exact JSON object expected, with one fully populated example memory, and adds "Use null when there is no target_timeframe." This is needed because `json_object` mode guarantees syntactically valid JSON but not a particular schema; the schema is enforced on our side by `_Extraction.model_validate_json`. `_FENCE` (`^```(?:json)?\s*|\s*```$`) removes a leading and trailing Markdown fence because, as the code comment says, "smaller models fence their JSON"; plain JSON passes through untouched.

The Anthropic provider does not need `_JSON_SHAPE`: `messages.parse(..., output_format=_Extraction)` has the SDK derive the schema from the Pydantic model and the API enforces it. The two providers differ on `LLM_EFFORT`: `OpenAICompatibleLLM` forwards `reasoning_effort` on the extraction call (`**self._extra`), while `AnthropicLLM.extract_memories` sends no `output_config`. The Anthropic extraction call sets `max_tokens=16000`; the OpenAI-compatible one leaves the server default. Both go through `_guarded()` so outages surface as `LLMError`.

### `validate()` rules

```python
def validate(candidates: list[MemoryCandidate], min_confidence: float) -> list[MemoryCandidate]:
    kept: list[MemoryCandidate] = []
    for c in candidates:
        c.key, c.category, c.type, c.value = c.key.strip().lower(), c.category.strip().lower(), c.type.strip().lower(), c.value.strip()
        if c.confidence < min_confidence or not c.key or not c.value:
            continue
        if c.category not in MEMORY_CATEGORIES or c.type not in MEMORY_TYPES:
            log.info("dropping off-taxonomy candidate key=%s category=%s type=%s", c.key, c.category, c.type)
            continue
        kept.append(c)
    return kept
```

| Order | Rule | Effect | Logged? | Rationale |
|---:|---|---|---|---|
| 1 | Normalize | `key`, `category`, `type` are stripped and lowercased; `value` is stripped only. The candidate object is mutated in place. | No | Logical identity is `(category, key)`, so `Career.Goal` and `career.goal` must be the same fact (TDD §22 idempotency). Value case is preserved because it is displayed to the model and the user (inferred). |
| 2 | Confidence threshold | `confidence < min_confidence` drops the candidate. The comparison is strict, so exactly `0.6` is kept at the default. | No (silent `continue`) | PRD §7.2: low-confidence or speculative statements are not stored. PRD §13 mitigation "confidence threshold". |
| 3 | Empty key or value | After stripping, an empty `key` or `value` drops the candidate. | No | A memory with no identity or no content cannot be retrieved or rendered (inferred). |
| 4 | Taxonomy | `category` must be in `MEMORY_CATEGORIES` (every `Category` except `follow_up`) and `type` in `MEMORY_TYPES` (`fact`, `goal`, `preference`, `interest`). | Yes, `INFO` with key, category and type | Retrieval filters by `category` equality, so an off-taxonomy category would be stored but never retrieved by a category query; the log line exists because this is the drop that indicates a prompt or model problem rather than a legitimately weak statement (inferred). |

What `validate()` deliberately does not check: that the key's prefix matches the category (a candidate with `key="profile.name"` and `category="general"` passes and is still routed to the Profile node because routing looks only at the key), that `target_timeframe` is a plausible period, or that `value` follows the `YYYY-MM-DD` convention (for dates of birth, `brain._with_sun_sign` parses the value with `parse_date` and only rewrites it when parsing succeeds).

**How this implements PRD §7.2.** The PRD's three-tier rule (high and durable: store; medium: store if explicit and actionable; low or speculative: do not store) is collapsed into a single number by having the prompt define `confidence` as certainty that the statement is "explicit and durable". A candidate the model considers explicit and durable scores high and passes; a hedged or hypothetical one scores low and is dropped. The threshold sits at `0.6` so that a candidate the model rates as moderately certain still passes (inferred; the PRD itself says "the initial score can be heuristic").

### `remember()` routing and the `memory_updates` count

Routing is by key alone, using the table in `app/models.py`:

```python
PROFILE_KEYS = {
    "profile.name": "name",
    "profile.date_of_birth": "date_of_birth",
    "profile.time_of_birth": "time_of_birth",
    "profile.birth_place": "birth_place",
}
```

| Candidate key | Destination | Brain call | Counted as |
|---|---|---|---|
| In `PROFILE_KEYS` | `Profile` node property named by the mapping | One `upsert_profile(user_id, fields)` for all such candidates together; `sun_sign` recomputed if `date_of_birth` is among them | `len(fields)`: one per distinct profile field, whether or not the stored value changed |
| Any other key (including `language.preferred` and `profile.*` keys not in the table) | `Memory` node | One `upsert_memory(user_id, candidate, source_message_id)` per candidate | 1 if the outcome is `"created"` or `"updated"`; 0 if `"unchanged"` |

The two counting rules differ because `upsert_profile` returns the profile, not an outcome; the brain's `SET p += $fields` (Neo4j) or `setattr` loop (in-memory) does not report whether anything changed. The memory path can be precise because `upsert_memory` returns an `Outcome`.

Worked examples, verified by running `ChatService.chat()` with `InMemoryBrain` and `MockLLM`:

| Turn | Candidates after validation | `memory_updates` | Why |
|---|---|---:|---|
| "My name is Rahul. I was born on 15 August 1995 in Delhi. I'm planning to switch jobs next year." (PRD §5.1) | `profile.name`, `profile.date_of_birth`, `profile.birth_place`, `career.goal` | 4 | 3 profile fields in one `upsert_profile` (also sets `sun_sign = Leo`) + `career.goal` created |
| Same sentence again | Same four | 3 | 3 profile fields count again; `career.goal` upsert returns `"unchanged"` |
| "I prefer English." | `language.preferred = English` | 1 | Created |
| "I prefer English." again | Same | 0 | Unchanged (confidence refreshed to the max, `updated_at` touched) |
| "Actually, I prefer Hindi." | `language.preferred = Hindi` | 1 | Updated: English superseded, Hindi active |
| "Hello there!" | none | 0 | Rule extractor returns `[]`; `remember()` makes no brain calls |
| "Why do you say that?" | not extracted | 0 | `follow_up`: the orchestrator never calls `extract_memories` |

The first row is the `assert r["memory_updates"] == 4` in `tests/test_chat.py::test_2_first_message_creates_durable_memory_and_profile`, with the comment "name, dob, birth place -> Profile; career goal -> Memory".

### The correction flow and history

PRD §7.3's example runs through this layer as follows. On "I prefer English." `classify()` returns `language` (keyword `english`), extraction yields `language.preferred = English`, `validate()` keeps it (confidence 0.9 from the rule extractor), and because the key is not in `PROFILE_KEYS`, `upsert_memory` creates an `ACTIVE` memory with `source_message_id` set to that turn's message id. On "Actually, I prefer Hindi." `classify()` returns `language` (keyword `hindi`), so this is not treated as a follow-up and extraction runs; the candidate `language.preferred = Hindi` has the same `(category, key)` as the active English memory but a different `value`, so the brain marks English `SUPERSEDED`, creates Hindi `ACTIVE`, and links `(Hindi)-[:SUPERSEDES]->(English)`. The outcome `"updated"` counts as one update. A later `language` query retrieves only Hindi because `search_memories` filters on `status = 'ACTIVE'`, while `GET /users/{id}/memories` still lists both. This layer contributes only the extraction and the routing; the supersede mechanics, the transaction boundary and the Cypher are documented in [Shared Brain](05-shared-brain.md). TDD §7.0 makes the point that `language.preferred` is kept as a `Memory` precisely so that this flow is demonstrable.

### Provenance

`source_message_id` is the id of the user's `ChatMessage` for the turn in which the memory was created, generated by `uuid.uuid4()` in `ChatService.chat()`. The brain copies it onto every new `Memory` node in `_new_memory`, so a superseding memory carries the id of the message that corrected the fact while the superseded one keeps the id of the message that first stated it. `tests/test_units.py::test_upsert_outcomes` pins that the active memory after a correction carries the correcting message's id (`("Hindi", "m3")`). This is the "user-only provenance" of PRD §13: the id always points at a user message because only user messages are extracted from. Note that the message itself lives only in the process-local `SessionStore` deque; nothing durable stores message bodies, so the id is a stable reference rather than a resolvable one (see known limits).

### The rule-based extractor (`extract_by_rules`)

`MockLLM.extract_memories` returns `extract_by_rules(message, today)`. The code comment states its scope: "Covers the PRD's example sentences, nothing more." Each rule is tried once with `re.search`, so it contributes at most one candidate; the rules are independent, so one sentence can yield several. All candidates are built by the `_cand` helper with `reason="rule"` and a default confidence of `0.9`. In the table below, `|` inside a pattern is written `\|` so that it survives the Markdown table; the source has plain pipes.

| Regex (name in `app/llm.py`) | Pattern | Captures | Candidate produced | Built for |
|---|---|---|---|---|
| `_NAME` | `\bmy name(?:'s\| is) ([A-Za-z]+)`, case-insensitive | One alphabetic word after "my name is" / "my name's" | `profile.name` / `profile` / `fact`, value title-cased, confidence 0.95 | "My name is Rahul." (PRD §5.1) |
| `_BORN_ON` | `\bborn (?:on )?(<day month[,] year> \| <YYYY-MM-DD> \| <Month day[,] year>)`, case-insensitive; the capture must then be accepted by `astrology.parse_date` | A date in one of three shapes: `15 August 1995` / `15th Aug 1995`, `1995-08-15`, `August 15, 1995` | `profile.date_of_birth` / `profile` / `fact`, value `dob.isoformat()`, confidence 0.95. If `parse_date` returns `None` (a misspelled month, or a comma in the day-first form such as `15th August, 1995`, which the regex admits but no `_DATE_FORMATS` entry accepts) no candidate is produced. | "I was born on 15 August 1995" (PRD §5.1) |
| `_BORN_IN` | `\bborn\b[^.;!?]*?\bin ([A-Z]\w+(?: [A-Z]\w+)*)`, case-sensitive | Capitalized word(s) after "in", anywhere later in the same clause as "born" | `profile.birth_place` / `profile` / `fact`, value as written, confidence 0.95. Lowercase place names ("born in delhi") do not match. | "... in Delhi." (PRD §5.1) |
| `_PLAN` | `\b(?:i(?:'m\| am) (?:planning\|going\|hoping\|aiming) to\|i plan to\|i want to\|my goal is to\|i intend to) ([^.;!?]+)`, case-insensitive | The rest of the clause after a plan lead-in | A goal, see below | "I'm planning to switch jobs next year." (PRD §5.1) |
| `_NEXT_YEAR` | `\bnext year\b`, case-insensitive | Applied to the captured goal text | Removed from the value; `target_timeframe = str(today.year + 1)` | "next year" -> `2027` for a 2026 date (PRD §5.1 "target year 2027") |
| `_YEAR` | `\b(?:in \|by )?(20\d{2})\b` | A 21st-century year, optionally preceded by "in " or "by "; tried only when `_NEXT_YEAR` did not match | Removed from the value; `target_timeframe` = the year | "... by 2028", "... in 2030" |
| `_PREFER` | `\bi(?:'d\| would)? prefer (?:to (?:speak\|chat\|talk\|converse) in \|(?:replies\|responses\|answers) in \|speaking )?([A-Za-z]+)`, case-insensitive; the word must be in `_LANGUAGES` (lowercased) | One word after "prefer", with optional connective phrases | `language.preferred` / `language` / `preference`, value title-cased, confidence 0.9. "I prefer tea." yields nothing because `tea` is not a language. | "I prefer English." / "Actually, I prefer Hindi." (PRD §7.3) |
| `_LIKE` | `\bi (?:love\|enjoy\|like\|am into\|'m into) ([a-z][a-z ]{2,30}?)(?=[.,;!?]\|$)`, case-insensitive | A 3 to 31 character phrase of letters and spaces, lazily up to the first punctuation mark or end of string | `interests.<slug>` / `interests` / `interest`, value as written, confidence 0.75; slug = value lowercased with non-alphanumerics collapsed to `_` | Long-term interests (PRD FR-7). Not exercised by the PRD sentences. |

`_LANGUAGES` is a fixed set of twelve: `hindi, english, tamil, telugu, bengali, marathi, gujarati, kannada, malayalam, punjabi, urdu, odia`.

The goal rule reuses the query classifier to pick the category:

```python
    if m := _PLAN.search(message):
        goal, timeframe = m[1].strip(), None
        if _NEXT_YEAR.search(goal):
            goal, timeframe = _NEXT_YEAR.sub("", goal).strip(), str(today.year + 1)
        elif y := _YEAR.search(goal):
            goal, timeframe = _YEAR.sub("", goal).strip(), y[1]
        category = classify(goal)
        if category is Category.FOLLOW_UP:
            category = Category.GENERAL
        out.append(_cand(f"{category}.goal", category, "goal", goal, timeframe))
```

`classify()` is documented in [query understanding](04-query-understanding-and-context-selection.md); here it is applied to the goal text alone ("switch jobs" matches the `career` keyword `job`), so the key becomes `career.goal`. `classify()` can return `follow_up` for short goal text with a leading follow-up phrase or a bare pronoun (for example the goal text "explain it"); since `follow_up` is not a memory category, it is mapped to `general`, giving `general.goal`. Anything the classifier does not recognise also falls to `general`.

Inputs and outputs, from the tests and from running the function directly:

| Input | Output (`key = value`, timeframe) | Source |
|---|---|---|
| "My name is Rahul. I was born on 15 August 1995 in Delhi. I'm planning to switch jobs next year." | `profile.name = Rahul`; `profile.date_of_birth = 1995-08-15`; `profile.birth_place = Delhi`; `career.goal = switch jobs`, `2027` (today's year + 1) | `tests/test_chat.py::test_rule_extractor_matches_prd_example` |
| "I prefer English." | `language.preferred = English` | `tests/test_chat.py::test_7_user_correction_supersedes` |
| "Actually, I prefer Hindi." | `language.preferred = Hindi` | same test |
| "Hello there!" | nothing | `tests/test_chat.py::test_1_new_user_succeeds_with_no_context` (`memory_updates` is not asserted there; `context_used == []` is) |
| "I was born on August 15, 1995 in New Delhi." | `profile.date_of_birth = 1995-08-15`; `profile.birth_place = New Delhi` | run directly |
| "I want to buy a house by 2028." | `general.goal = buy a house`, `2028` | run directly |
| "I plan to invest in stocks in 2030" | `finance.goal = invest in stocks`, `2030` | run directly (`invest` is a `finance` keyword) |
| "I love cricket." | `interests.cricket = cricket` (confidence 0.75) | run directly |
| "I enjoy reading books, and cooking." | `interests.reading_books = reading books` (the lazy match stops at the first comma) | run directly |
| "I'd prefer to chat in Tamil" | `language.preferred = Tamil` | run directly |
| "I prefer tea." / "I was born in delhi" / "My name's Priya Sharma" | nothing / nothing / `profile.name = Priya` (single word) | run directly; illustrate the rule boundaries |

The rule extractor never produces `profile.time_of_birth`, never produces a `relationships`, `health` or `astrology` candidate except through `classify()` on a goal, and never produces a candidate below the `0.6` threshold, so with `MockLLM` `validate()` only ever drops candidates that a test constructs by hand.

## Contracts and invariants

- **Only the user's text is extracted from.** `ChatService.chat()` passes the request `message` and nothing else to `extract_memories`; the assistant reply is appended to the session but never reaches the extractor. TDD §2 principle 3; README "Memory strategy": "Assistant text never becomes memory." `tests/test_chat.py::test_5_memory_persists_across_sessions` asserts that after the PRD sentence, whose mock reply echoes the user's text, the memory list is exactly `[("career.goal", "ACTIVE")]`.
- **Extraction is skipped on `follow_up` turns and when the request is already degraded.** `tests/test_chat.py::test_4_follow_up_uses_recent_context_only` asserts `memory_updates == 0` on "Why do you say that?"; `test_10_graph_failure_degrades` asserts `memory_updates == 0` when the brain is down.
- **Validated candidates have lowercase, stripped `key`, `category` and `type` and a stripped, non-empty `value`; `value` case is preserved.** `tests/test_units.py::test_validate_filters_and_normalizes` asserts `("career.goal", "career", "goal", "switch jobs")` from `("Career.Goal", "CAREER", "Goal", " switch jobs ")`.
- **Every persisted candidate has `confidence >= min_confidence`, a category in `MEMORY_CATEGORIES` and a type in `MEMORY_TYPES`.** `general` is a valid memory category; `follow_up` is not (`CLAUDE.md` invariants).
- **Profile keys never become `Memory` nodes.** `remember()` excludes `PROFILE_KEYS` from the `upsert_memory` loop. `tests/test_chat.py::test_5_memory_persists_across_sessions` asserts that after the PRD sentence the memory list is exactly `[("career.goal", "ACTIVE")]`.
- **`memory_updates` = number of profile fields written + number of memory upserts whose outcome is not `"unchanged"`.** Re-stating a fact word for word yields 0 for memories and 1 per field for profile facts.
- **`source_message_id` on a new `Memory` is the id of the user message of the current turn.** It is never the assistant message's id, because the assistant `ChatMessage` is created with the default empty id and is not passed to `remember()`.
- **Extraction failure never blocks the response.** By the time `extract_memories` runs, the reply has been generated and both turns appended to the session; `LLMError` and `BrainUnavailable` raised in the memory block are caught, and the reply is returned with `memory_updates = 0`. TDD §13.3; README "Failure modes" row "Extraction fails after a good response".
- **A failed generation records nothing.** `LLMError` from `generate` propagates before the memory block is reached, so no extraction, no session append, no write. `tests/test_chat.py::test_9_llm_failure_returns_503_and_mutates_nothing`.
- **`validate()` mutates its input.** Callers holding references to the candidates see the normalized fields; `remember()` relies on this because it reads `c.key` after validation.

## Design decisions and alternatives rejected

| Decision | Chosen | Rejected | Why (source) |
|---|---|---|---|
| Extraction mechanism | Structured LLM output (`MemoryCandidate` schema) followed by deterministic `validate()` | Rules only; LLM only | TDD §24 / README "Trade-offs": "Flexible extraction, hard guardrails on what is persisted." TDD §23 Phase 1 lists "Rules + structured LLM memory extraction". Rules alone cannot cover open phrasing (the rule set's own comment scopes it to the PRD sentences, and it is kept only for the mock); LLM alone would leave the PRD §13 "LLM invents memories" risk unmitigated, since nothing would reject off-taxonomy or low-confidence output. |
| Where profile facts live | `profile.name`, `profile.date_of_birth`, `profile.time_of_birth`, `profile.birth_place` -> `Profile` node via `upsert_profile` | Storing them as `Memory` nodes like everything else | TDD §7.0 and §27: "Structured stable facts belong in a structured place, and this is what lets a date of birth stated in chat produce a sun sign." The v1 design implicitly kept them as memories; the revision routed them so the astrology hook works from chat input. |
| Where language preference lives | `language.preferred` `Memory` | `Profile.preferred_language` property (which exists and is settable through `POST /users`) | TDD §7.0: "Everything else, including `language.preferred`, is a Memory and follows the supersede lifecycle above, which is what makes PRD §7.3 (English then Hindi) demonstrable." `CLAUDE.md` invariant: "`language.preferred` stays a Memory so the supersede flow is demonstrable." |
| Correction semantics | Delegated to `upsert_memory`: supersede, never overwrite | Update the node in place (allowed by PRD §7.3); a separate `supersede_memory` call | TDD §22 and §24: "Preserves provenance and avoids ambiguous retrieval." TDD §27: folding supersede into the upsert keeps the write atomic. This layer therefore has no correction logic of its own. |
| When extraction runs | Synchronously, in the request, after the reply | Background queue / worker | TDD §12: "In the timed MVP, it may execute synchronously to simplify implementation and testing." TDD §24: "Simpler end-to-end demo; background worker later." README "Trade-offs": "a queue is a one-line move of the last block in `chat.py`." A synchronous update is also what lets `memory_updates` be reported in the same response. |
| Confidence rule | One threshold, `MIN_CONFIDENCE` default `0.6`, strict `<` | PRD §7.2's three-tier rule implemented as separate branches | PRD §7.2 permits a heuristic score. The prompt defines `confidence` as certainty that the fact is "explicit and durable", so the medium tier's "explicit and actionable" condition is folded into the score and a single cut suffices (inferred). |
| Skipping follow-ups | No extraction on `follow_up` | Extract from every turn | `chat.py` comment: "nothing durable to learn". Saves an LLM round trip on the turns least likely to contain facts (inferred from TDD §21 "avoid unnecessary LLM calls"). |
| Skipping when degraded | No extraction when the pre-response read failed | Attempt the write anyway | TDD §13.2 and §27: "one logged failure per request, not two." |
| Case handling in `validate()` | Lowercase `key`/`category`/`type`, preserve `value` | Lowercase everything; normalize nothing | Identity fields must be case-insensitive for TDD §22 idempotency; values are rendered into prompts and API responses as the user said them (inferred). |
| `reason` in the schema | Required field, not persisted | Omit it; persist it | Inferred: it grounds each candidate in the user's words and is useful when reading raw extractor output, while the node schema (TDD §5.2) keeps provenance as `source_message_id` only. |
| Counting profile writes | One per field written | Diff against the stored profile to count only changes | `upsert_profile` returns a `UserProfile`, not an outcome, so a change count would require a read before the write (inferred). |
| Extraction text | The current user message only | The recent conversation window | TDD §2 principle 3 and TDD §9 step 4: "Do not convert all short-term messages into long-term memory." Passing history would re-extract the same facts every turn and would expose assistant text to the extractor. |

## Failure modes and degraded behavior

All of these occur after the reply has been generated and both turns appended to the session, so the user always receives the reply.

| Failure | Where it surfaces | What happens | Response |
|---|---|---|---|
| Provider outage during extraction (connection error, rate limit, HTTP 5xx) | `_guarded()` in `app/llm.py` raises `LLMError` | `ChatService.chat()` catches `LLMError`, logs `"memory extraction failed, response still returned: ..."` at `WARNING`, leaves `updates = 0` | `200`, `memory_updates: 0`, `degraded: false` |
| Model returns invalid JSON or a wrong shape (OpenAI-compatible provider) | `_Extraction.model_validate_json` raises `ValidationError`; `extract_memories` converts it to `LLMError("extraction returned invalid JSON (N error(s))")` | Same as above | `200`, `memory_updates: 0`, `degraded: false`. Pinned by `tests/test_openai_llm.py::test_extract_invalid_output_is_llm_error` for `"not json"`, a memory missing required fields, and `null` content |
| Model returns no parsed output (Anthropic provider) | `resp.parsed_output` is `None` | `extract_memories` returns `[]`; `remember()` writes nothing | `200`, `memory_updates: 0` |
| Model declines or returns fenced JSON | Refusal is not checked on the extraction path; fences are stripped by `_FENCE` | Fenced JSON parses normally (`test_extract_requests_json_mode_and_tolerates_fences`) | Normal |
| Off-taxonomy or low-confidence candidates | `validate()` | Dropped; off-taxonomy drops logged at `INFO` with key, category and type; confidence and empty-field drops are silent | Normal; the count excludes them |
| Provider HTTP 4xx during extraction (bad request, auth) | `_guarded()` re-raises the SDK exception; it is not an `LLMError` | Not caught by the memory block (which catches only `LLMError` and `BrainUnavailable`); the exception propagates out of `chat()` after the session was already appended | `500` from FastAPI's default handler. `CLAUDE.md`: "4xx errors deliberately propagate (our bug, not an outage)"; TDD §10 |
| Brain unreachable during `upsert_profile` or `upsert_memory` | Brain raises `BrainUnavailable` | Caught, logged `"memory write failed, response still returned: ..."`, `degraded = True`, `updates` stays 0. Writes made before the failure (e.g. the profile upsert, or earlier memory upserts in the loop) are not rolled back | `200`, `memory_updates: 0`, `degraded: true` |
| Brain unreachable before the reply | Pre-response read fails, `degraded = True` | Memory block skipped entirely (TDD §13.2) | `200`, `memory_updates: 0`, `degraded: true`; `tests/test_chat.py::test_10_graph_failure_degrades` |
| LLM unavailable during generation | `generate` raises `LLMError` | Memory block never reached; nothing appended, nothing written | `503`; `tests/test_chat.py::test_9_llm_failure_returns_503_and_mutates_nothing` |
| `MockLLM(fail=True)` | `extract_memories` raises `LLMError("mock failure")` | As for a provider outage; in practice `generate` fails first on the same flag | `503` |

## Configuration

| Setting | Source | Default | Effect on this layer |
|---|---|---|---|
| `MIN_CONFIDENCE` | `Settings.min_confidence` (`float`), read in `Settings.from_env`, passed to `ChatService(min_confidence=...)`, then to `remember(..., min_confidence)` and `validate()` | `0.6` | The strict lower bound on `MemoryCandidate.confidence`. Raising it makes persistence more conservative; the rule extractor's candidates (0.75 to 0.95) all pass at the default. Documented in README "Configuration" as "Extraction threshold". |
| `LLM_PROVIDER` | `build_llm(settings)` | `anthropic` | Selects the extractor. `mock`: `extract_by_rules`, deterministic, no network. `anthropic`: `messages.parse` with the schema enforced by the API. `openai`: `json_object` mode with `_JSON_SHAPE` in the prompt and local validation; the strictness of the JSON depends on the server behind `OPENAI_BASE_URL`. |
| `LLM_MODEL` | `Settings.llm_model` | empty (`claude-opus-5` for anthropic; required for openai) | The same model serves generation and extraction; there is no separate extraction model. |
| `LLM_EFFORT` | `Settings.llm_effort` | `medium` | Forwarded as `reasoning_effort` on the OpenAI-compatible extraction call; not forwarded on the Anthropic extraction call. |
| `BRAIN` | `Settings.brain` | `neo4j` | Chooses which `SharedBrain` receives the writes; the routing and counting in `remember()` are identical for both. |

There is no switch to disable extraction; the mock provider with `BRAIN=memory` is the way to run the pipeline without external services. See [Configuration and deployment](08-configuration-and-deployment.md) for the full variable table.

## Tests that pin this layer

| Test | Asserts |
|---|---|
| `tests/test_units.py::test_validate_filters_and_normalizes` | From five hand-built candidates, `validate()` keeps exactly one, normalized to `("career.goal", "career", "goal", "switch jobs")`; the low-confidence, bad-category, bad-type and blank-value ones are dropped. |
| `tests/test_units.py::test_upsert_outcomes` | The brain outcomes `remember()` counts: `"created"`, then `"unchanged"` for the same value with a higher confidence (confidence raised to 0.95 on the stored node), then `"updated"` for a new value; the active memory carries the correcting message's `source_message_id`. |
| `tests/test_units.py::test_upsert_profile_derives_sun_sign` | The profile route: `upsert_profile` with a human-readable date stores ISO `1995-08-15` and `sun_sign = Leo`, and a later partial upsert keeps earlier fields. |
| `tests/test_chat.py::test_2_first_message_creates_durable_memory_and_profile` | The PRD §5.1 sentence yields `memory_updates == 4`; `career.goal = switch jobs` with `target_timeframe` = next year and `ACTIVE`; profile `name`, `date_of_birth`, `birth_place`, `sun_sign` set from chat. |
| `tests/test_chat.py::test_4_follow_up_uses_recent_context_only` | A `follow_up` turn reports `memory_updates == 0`. |
| `tests/test_chat.py::test_5_memory_persists_across_sessions` | After the PRD sentence the memory list is exactly `[("career.goal", "ACTIVE")]`: profile facts did not become `Memory` nodes. |
| `tests/test_chat.py::test_7_user_correction_supersedes` | "I prefer English." then "Actually, I prefer Hindi." gives `memory_updates == 1` on the correction, active `language` memories `["Hindi"]`, statuses `{"English": "SUPERSEDED", "Hindi": "ACTIVE"}`, and a later prompt containing Hindi but not English. |
| `tests/test_chat.py::test_9_llm_failure_returns_503_and_mutates_nothing` | With the LLM failing, no memories and no profile exist afterwards. |
| `tests/test_chat.py::test_10_graph_failure_degrades` | With the brain failing, `degraded` is `true` and `memory_updates == 0`. |
| `tests/test_chat.py::test_rule_extractor_matches_prd_example` | `extract_by_rules` on the PRD sentence returns exactly the four `(key, value, timeframe)` pairs, with the goal's timeframe equal to `date.today().year + 1`. |
| `tests/test_openai_llm.py::test_extract_requests_json_mode_and_tolerates_fences` | The extraction request uses `response_format = {"type": "json_object"}` and `reasoning_effort`; the system prompt contains "Respond with JSON only" and the resolved next year `2027` for `today = 2026-09-21`; a fenced JSON reply parses into one `career.goal` candidate with `target_timeframe = "2027"` and `confidence = 0.95`. |
| `tests/test_openai_llm.py::test_extract_invalid_output_is_llm_error` | `"not json"`, `{"memories": [{"key": "x"}]}` and `null` content each raise `LLMError`. |
| `tests/test_neo4j.py::test_neo4j_roundtrip` (skipped without `NEO4J_TEST_URI`) | The same outcomes, active-only retrieval and `SUPERSEDES` link against a live Neo4j, using `MemoryCandidate` objects directly. |

`AnthropicLLM.extract_memories` has no automated test (it needs a key); `CLAUDE.md` asks that `messages.parse(..., output_format=_Extraction)` be kept for extraction.

## Known limits and future work

**`ponytail:` markers.** There are none in `app/memory.py`, `app/llm.py` or `app/models.py`. Two markers in adjacent modules bound this layer's behavior:

- `app/context.py`: `# ponytail: keyword classifier, first match wins; swap for an LLM/embedding classifier when evals show misroutes.` The rule extractor uses this classifier to pick a goal's category, so a misroute there becomes a mis-keyed memory (`general.goal` instead of `career.goal`) under the mock provider.
- `app/brain.py`: `# ponytail: fixed timeout, no circuit breaker; add one when outages are long enough to matter per request.` The memory write inherits this: an outage costs the request one ~3s timeout in the write phase in addition to the read phase.

**Deferred by design (PRD §12, §14; TDD §23 Phase 3; README "Production path").**

- *Decay / expiration.* PRD §7.1 lists optional `valid_from` / `valid_until`; TDD §5.2 lists `valid_until` on the `Memory` node and README "Production path" refers to it as "already on the schema". The code does not implement it: `models.Memory` has no such field and `brain._new_memory` does not write one. Retrieval is by `status` only, so a 2027 goal stays `ACTIVE` in 2030 until the user restates it.
- *Conflict resolution.* The only rule is that the latest explicit user statement wins through supersede (PRD §7.3). There is no reconciliation of two contradictory candidates in one message (each is upserted in list order, so the second supersedes the first within the same request) and no merging of related facts. README: "conflict resolution beyond last-write-wins".
- *Importance scoring.* Only `confidence` exists; there is no separate importance or usage signal, and retrieval orders by confidence then recency (PRD §14 "Memory importance and confidence scoring").
- *Background extraction.* Extraction is a second LLM round trip inside every non-follow-up request (TDD §12; TDD §23 Phase 2 "Queue/background worker for memory extraction"; README: "a queue is a one-line move of the last block in `chat.py`").

**Observed limits of the current code (inferred from reading and running it).**

- `remember()` is not transactional across candidates: a `BrainUnavailable` midway leaves earlier writes in place and reports `memory_updates = 0` with `degraded = true`.
- Routing ignores `category`. TDD §7.0 phrases the rule as "candidates with `category = profile` and a key in {...}", but `remember()` tests only `c.key in PROFILE_KEYS`: a candidate whose key is in `PROFILE_KEYS` goes to the Profile regardless of its category, and a candidate with a mismatched key prefix (say `key = career.goal`, `category = health`) is stored under the logical identity `(health, career.goal)`. `validate()` does not check that the key's prefix equals the category.
- `validate()` has no upper bound on `confidence`: only `confidence < min_confidence` is checked, so a provider value above 1 passes and, because `search_memories` orders by confidence descending, sorts to the top of retrieval. The schema description says "0 to 1" but `MemoryCandidate` carries no numeric constraint (by design, per `CLAUDE.md`).
- `MEMORY_TYPES` is spelled out as a literal twice, in `MemoryCandidate.type`'s description ("one of: fact, goal, preference, interest") and in `EXTRACT_PROMPT` ("type: fact | goal | preference | interest"), whereas the category list is rendered from `MEMORY_CATEGORIES`. Adding a type means editing three places.
- Value comparison in the brain is exact, so `"hindi"` and `"Hindi"` would supersede rather than dedupe. The rule extractor title-cases names and languages to avoid this; LLM providers rely on the prompt's "short normalized phrase".
- Profile field writes count as updates even when the value is unchanged (`changed += len(profile_fields)`), so re-introducing yourself word for word reports `memory_updates = 3`.
- A `profile.date_of_birth` value that `parse_date` cannot read is stored raw: `brain._with_sun_sign` rewrites `date_of_birth` to ISO and sets `sun_sign` only when parsing succeeds, and otherwise passes the fields through unchanged, so an LLM value such as "sometime in 1995" lands on the Profile as-is with no sun sign. `validate()` does not check the `YYYY-MM-DD` convention the prompt asks for (see [Shared Brain](05-shared-brain.md)).
- `source_message_id` refers to a message stored only in the in-process `SessionStore`; after a restart the id cannot be resolved to text (TDD §23 Phase 2 mentions "Postgres or event log for message persistence if required").
- `reason` is required from the model but discarded before persistence.
- `date.today()` is the server's date, not the user's; near midnight the resolved "next year" may differ from the user's expectation.
- A provider 4xx during extraction turns an otherwise successful turn into a `500` after the session has been appended (see failure modes).
- Rule extractor coverage: one candidate per rule; `_NAME` captures a single word ("Priya Sharma" -> "Priya"); `_BORN_IN` requires a capitalized place; `_BORN_ON` admits a comma in the day-first form (`15th August, 1995`) but `astrology._DATE_FORMATS` has no `%d %B, %Y` entry, so that date is silently dropped while the birth place from the same sentence is still extracted; there is no rule for time of birth; `_PREFER` recognises twelve languages; `_LIKE` captures one interest per message and its `'m into` alternative is unreachable because the pattern requires a space after "i" (so "I'm into music" yields nothing while "I am into music" yields `interests.music`).
