# Query understanding and context selection

This layer turns one incoming chat message into a bounded, provider-neutral LLM request. `classify()` assigns the message a single `Category` with an ordered, first-match keyword scan followed by two follow-up heuristics; the orchestrator uses that category to decide whether to read the Shared Brain at all and which memory category to filter on. `select_context()` then takes the already-capped recent turns, the profile fields that matter for that category, and the memories the brain returned, renders them once into a system prompt, and reports exactly what it injected in `context_used`. There is no LLM call and no scoring in this layer: it is pure, synchronous Python with no I/O and no settings.

> **Status update (query routing now uses the LLM).** `classify()` is no longer the production classifier. `ChatService.chat` calls `llm.classify(message)` instead — a structured-output LLM router (prompt `llm.CLASSIFY_SYSTEM`, one category per message). `context.classify()` survives as the deterministic rule reference that `MockLLM.classify` delegates to, keeping the offline tests and the mock pipeline deterministic and pinning the same taxonomy. Everything in this page about `select_context()`, `context_used`, the profile-field table and the follow-up/`general` semantics is unchanged; the category that reaches them now comes from the LLM router. "Follow-up isolation", "`general` retrieves everything rather than nothing" and the trade-off discussion below should be re-read with that substitution in mind.

**Files:** `app/context.py`

**Depends on:** [Domain model](03-domain-model.md) (`Category`, `ChatMessage`, `LLMRequest`, `Memory`, `UserProfile`, `PROFILE_FIELDS` from `app/models.py`). **Used by:** [Orchestration and short-term context](02-orchestration-and-short-term-context.md) (`ChatService.chat` in `app/chat.py` calls `classify` then `select_context`) and [LLM providers](07-llm-providers.md) (`extract_by_rules` in `app/llm.py` reuses `classify` to name the category of an extracted goal). The memories it renders come from the [Shared Brain](05-shared-brain.md); the prompt it builds is consumed by the providers documented in [LLM providers](07-llm-providers.md). Overview of all layer documents: [Layers overview](README.md). Citations of the form "README, Trade-offs" below refer to the repository-root `README.md`, not to that overview.

## What this layer is

`app/context.py` is one module of about 100 lines with six module-level constants, one dataclass and three callables:

| Name | Kind | Role |
|---|---|---|
| `SYSTEM_PROMPT` | `str` | Fixed role text and behavioural rules sent on every turn |
| `_KEYWORDS` | `list[tuple[Category, re.Pattern]]` | Ordered category-to-pattern list; first category whose pattern matches wins |
| `_FOLLOW_UP` | `re.Pattern` | Leading follow-up phrases ("why", "tell me more", ...) |
| `_PRONOUN` | `re.Pattern` | Bare demonstratives/pronouns used by the short-message follow-up rule |
| `_PROFILE_FIELDS_FOR` | `dict[Category, tuple[str, ...]]` | Which `UserProfile` fields ride along per query category |
| `_DEFAULT_PROFILE_FIELDS` | `tuple[str, str]` | `("name", "sun_sign")`, used for every category absent from the table |
| `classify(message)` | function | Message to `Category` |
| `Selection` | dataclass | `request: LLMRequest` plus `context_used: list[str]` |
| `select_context(category, current, recent, profile, memories)` | function | Builds the `Selection` |
| `_render(m)` | function | One memory to one prompt line |

It implements TDD §4.5 (Query Understanding), §4.6 (Context Selector) and §4.7 (Prompt / Context Builder), which the module docstring cites. Everything that involves I/O sits outside it: the session deque in `app/session.py`, the graph reads in `app/brain.py`, the provider call in `app/llm.py`. `ChatService.chat` in `app/chat.py` is the only place that wires the three together, so this document also describes that wiring where it defines the layer's inputs.

## Why it exists

The PRD requires that before generating a response the system "understand/classify the user's query", "retrieve candidate profile and memory information", "select only relevant context" and "build a bounded LLM context" (PRD FR-5). It also requires enough recent history to resolve follow-ups without sending lifetime history (PRD FR-4: "Keep the latest 8–12 messages per session in the context window. Do not send the complete lifetime history to the LLM."). PRD §8 ranks the candidate sources (current message highest; recent messages and relevant memories high; profile medium/high; astrology attributes medium; irrelevant memories excluded) and gives the two anchoring cases: for "Why do you say that?" recent session context should dominate, and for a new-session career question long-term memories should dominate.

The problem being solved is a bounded, relevant prompt. Two PRD §13 risks are addressed directly here: "Too much context sent to LLM" (mitigation: "Hard limits on recent messages and retrieved memories") and "Irrelevant memories pollute responses" (mitigation: "Query-category filtering + graph predicates + post-retrieval relevance filter"). The first two of those three mitigations are the retrieval query the orchestrator issues from the category chosen here; the third, a post-retrieval relevance filter, does not exist in the code, which is consistent with TDD §4.6 making the query itself the ranking and deferring any relevance term to Phase 3 (inferred: the filter went with the weighted score it would have fed). The TDD turns those into design principle 2, "Retrieve before generating. Never send the whole graph or entire conversation history to the LLM", principle 5, "Bounded context. Every source has a limit", and principle 7, "Debuggability. The system should expose what context categories were used without leaking hidden prompt internals" (TDD §2). TDD §21 fixes the budgets: recent messages <= 10, long-term memories <= 8, profile and astrology fields "only relevant fields", and adds that "Query classification should remain deterministic unless an ambiguous query benefits from model-based classification."

TDD §4.5 defines the ten domains (`profile`, `career`, `relationships`, `finance`, `health`, `interests`, `language`, `astrology`, `general`, `follow_up`), states that "a strict taxonomy is not required; categories exist to narrow the graph search", and prescribes the MVP approach in four steps: detect obvious follow-up phrases, detect explicit category keywords, let ambiguous queries fall through to `general` (which retrieves all active memories, top 8), and always include the current message as the strongest signal. TDD §4.6 gives the per-category selection table that `_PROFILE_FIELDS_FOR` encodes, and TDD §4.7 requires a provider-neutral request in which "context is rendered to text once, by the prompt builder". TDD §8 describes the same thing as a four-step retrieval strategy (categorize, retrieve candidates, filter profile, merge and apply hard limits). The layer is the code form of those sections, with one ordering difference described under How it works: TDD §4.5 lists follow-up detection as step 1 and keyword detection as step 2, and `classify` runs them the other way round.

## How it works

### classify: ordered keyword scan, then follow-up heuristics, then `general`

```python
def classify(message: str) -> Category:
    for cat, pattern in _KEYWORDS:
        if pattern.search(message):
            return cat
    if _FOLLOW_UP.search(message) or (len(message.split()) <= 6 and _PRONOUN.search(message)):
        return Category.FOLLOW_UP
    return Category.GENERAL
```

`_KEYWORDS` is a list comprehension over a dict literal's `.items()`, so its order is the dict's insertion order. Each entry compiles one case-insensitive pattern of the form `\b(?:w1|w2|...)` where every word is passed through `re.escape` and there is deliberately no trailing `\b`. The leading boundary means a word must start at a word boundary ("job" does not match "adjob"); the missing trailing boundary means the listed words act as stems: "astrolog" matches "astrology", "astrologer" and "astrological"; "employ" matches "employer" and "employment"; "financ" matches "finance" and "financial"; "invest" matches "investment"; "paint" and "cook" match "painting" and "cooking". The same property produces stem false positives, listed under Known limits. Multi-word entries such as "sun sign" or "reply in" are matched as literal phrases: `re.escape` turns the space into `\ `, which still matches exactly one literal space.

`pattern.search` looks anywhere in the message, and the loop returns on the first *category* whose pattern hits, so priority is decided by the position of the category in the list, not by where words appear in the message. The categories, in list order, with their keyword lists copied from `_KEYWORDS`:

| Priority | Category | Keywords (stems, case-insensitive) |
|---:|---|---|
| 1 | `astrology` | horoscope, zodiac, sun sign, rashi, kundli, kundali, nakshatra, planet, saturn, jupiter, mercury, venus, mars, retrograde, birth chart, astrolog, moon sign, rising sign |
| 2 | `language` | language, hindi, english, speak, reply in, respond in, translate |
| 3 | `profile` | my name, born, birthday, date of birth, birth place, how old, my age |
| 4 | `career` | career, job, work, promotion, business, profession, interview, boss, salary, office, startup, employ |
| 5 | `relationships` | relationship, marriage, marry, partner, love, wife, husband, girlfriend, boyfriend, family, friend, dating, breakup |
| 6 | `finance` | money, financ, invest, saving, wealth, debt, loan, income, stock, property, budget |
| 7 | `health` | health, fitness, sleep, stress, diet, doctor, anxiety, exercise, wellness, energy, sick |
| 8 | `interests` | hobby, hobbies, interest, enjoy, passion, music, reading, travel, sport, cricket, paint, cook |

Why this order: the code records no comment on it, so the following is inferred from the table in TDD §4.6 and the test that pins it. `astrology`, `language` and `profile` come first because they change *which profile fields* are sent (all fields for `astrology` and `profile`, name plus preferred language for `language`), whereas the five life areas all share the default `name` + `sun_sign`. Putting the field-changing categories ahead means a mixed message such as "What does my horoscope say about money?" is routed to `astrology`, so the full birth data reaches the prompt rather than the finance memories. `tests/test_units.py::test_classify` pins this routing (`"What does my horoscope say about money?"` is `Category.ASTROLOGY`); `tests/test_chat.py::test_profile_endpoint_feeds_astrology_context` carries the comment "astrology wins over finance", but its own assertions (`["user_profile", "astrology"]` and "Leo" in the prompt) would pass under `finance` too, since both categories select the name and sun sign of that fixture. Among the life areas the relative order only matters for messages that mention two of them ("Should I marry him? He is in finance." resolves to `relationships`), and no rationale for that internal order is recorded.

Only when no keyword matched does the follow-up check run. `_FOLLOW_UP` is anchored: `^\W*` permits leading whitespace or punctuation, then one of the phrases, then `\b`. Because this pattern *does* end with `\b`, "explained" is not treated as "explain" (it falls to `general`), while "Explain." and "  ...tell me more" match. The phrases are: `why`, `how come`, `really`, `what do you mean`, `tell me more`, `elaborate`, `explain`, `go on`, `say more`, `can you expand`, `expand on`, `what about (that|this|it)`, `and then`, `are you sure`, `based on what`. The second rule catches short conversational references that do not start with a listed phrase: at most six whitespace-separated tokens (`len(message.split()) <= 6`) and at least one whole-word `that`, `this`, `it`, `those` or `these` (`_PRONOUN`, which has boundaries on both sides). Examples, each checked against the code:

| Message | Result | Rule |
|---|---|---|
| "Why do you say that?" | `follow_up` | leading phrase `why` (`tests/test_units.py::test_classify`) |
| "Tell me more" | `follow_up` | leading phrase (`test_classify`) |
| "Is that good?" | `follow_up` | 3 words, pronoun `that` (`test_classify`) |
| "What about that?" | `follow_up` | leading phrase `what about that` |
| "What about the weather?" | `general` | no phrase, no pronoun |
| "Why is my career stuck?" | `career` | keyword scan runs first; `career` matched |
| "Really? Are you sure about that career advice?" | `career` | same: a topical keyword beats a leading follow-up phrase |
| "Explain my kundli" | `astrology` | keyword `kundli` wins over `explain` |
| "What do you remember about me?" | `general` | 6 words but `me` is not in `_PRONOUN` (`test_classify`) |
| "Hello!" | `general` | fallthrough (`test_classify`) |
| "" | `general` | total function: no keyword, no phrase, `split()` is empty |

Why follow-up detection runs only after category keywords: TDD §4.5 lists the steps in the opposite order (detect follow-up phrases first, then category keywords), TDD §27 records no row for the swap, and no comment in the code explains it, so the rationale is inferred. A message that names a topic carries enough intent to retrieve on, and the retrieval it triggers is cheap and bounded, whereas the follow-up route skips the graph and (in `app/chat.py`) skips memory extraction for the turn. Ordering the checks this way makes the expensive-to-get-wrong decision (do not consult memory at all) apply only when there is no topical signal. The cost is the mirror case: a short message with a pronoun and no keyword, such as "This is my plan." (4 words, `this`), is classified `follow_up` and nothing is extracted from it.

The final `return Category.GENERAL` is the fallthrough the TDD prescribes: "Ambiguous queries fall through to `general`, which retrieves all active memories (top 8)" (TDD §4.5).

### How the orchestrator uses the category

`ChatService.chat` in `app/chat.py` calls `classify(message)` once and makes two decisions from it before this layer is called again:

```python
profile, memories, degraded = None, [], False
if category is not Category.FOLLOW_UP:
    try:
        profile = await self.brain.get_profile(user_id)
        search = None if category is Category.GENERAL else category
        memories = await self.brain.search_memories(user_id, search, self.memory_limit)
    except BrainUnavailable as e:
        ...
        degraded = True
```

For `follow_up` there is no graph read at all: `profile` stays `None` and `memories` stays empty. For `general` the brain is searched with `category=None`, which both brain implementations treat as "all categories" (`InMemoryBrain.search_memories` and the `SEARCH_MEMORIES` Cypher in `app/brain.py`: `$category IS NULL OR m.category = $category`). For every other category the search is an equality match on `m.category`, which is why `models.Category` is one taxonomy for both queries and memories ("so retrieval is an equality match", its docstring). The brain also applies `status = 'ACTIVE'`, `ORDER BY confidence DESC, updated_at DESC` and `LIMIT $limit`, so the `memories` list that reaches `select_context` is already filtered, ranked and capped; see [Shared Brain](05-shared-brain.md). The `search` variable is `Category | None`; `Category` is a `StrEnum`, a `str` subclass, so it compares equal to the plain string stored on each memory.

`extract_by_rules` in `app/llm.py` is the second caller. When the mock extractor finds a plan ("I'm planning to switch jobs next year") it calls `classify(goal)` on the goal fragment ("switch jobs") to build the memory key and category (`career.goal`, `career`); if that returns `FOLLOW_UP` (for example the fragment "do that") it is mapped to `GENERAL`, because `follow_up` is not a memory category (`MEMORY_CATEGORIES` in `app/models.py` excludes it). This reuse is why `tests/test_chat.py::test_rule_extractor_matches_prd_example` indirectly depends on the classifier.

### select_context: fields, tags, blocks, request

```python
wanted = _PROFILE_FIELDS_FOR.get(category, _DEFAULT_PROFILE_FIELDS)
facts = {k: v for k, v in (profile.fields() if profile else {}).items() if k in wanted}
```

**Profile fields per category.** `_PROFILE_FIELDS_FOR` is the TDD §4.6 table with the life areas and `general` left implicit:

| Query category | `wanted` | Source |
|---|---|---|
| `follow_up` | `()` nothing | `_PROFILE_FIELDS_FOR` |
| `profile` | all six `PROFILE_FIELDS`: name, date_of_birth, time_of_birth, birth_place, preferred_language, sun_sign | `_PROFILE_FIELDS_FOR` |
| `astrology` | all six `PROFILE_FIELDS` | `_PROFILE_FIELDS_FOR` |
| `language` | `("name", "preferred_language")` | `_PROFILE_FIELDS_FOR` |
| `general`, `career`, `relationships`, `finance`, `health`, `interests` | `("name", "sun_sign")` | `_DEFAULT_PROFILE_FIELDS` via `.get` default |

`follow_up` maps to an empty tuple rather than being absent, so it does not pick up the default; the TDD table says `follow_up` gets "none" for profile fields, matching the rule that follow-ups rely on recent turns only (TDD §4.6; PRD §8). In practice `profile` is also `None` on follow-up turns because `app/chat.py` never read it, so the empty tuple is belt and braces for direct callers of `select_context`. `UserProfile.fields()` (in `app/models.py`) returns only truthy fields and iterates in dataclass declaration order, so `facts` is ordered name, date_of_birth, time_of_birth, birth_place, preferred_language, sun_sign regardless of the order in `wanted`.

**`context_used`.** Built in a fixed order that TDD §14 defines; TDD §27 records that `context_used` was "given an exact definition" because "Tests and the evaluation harness assert on it":

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

- `recent_conversation` appears iff `recent` is non-empty, i.e. at least one prior message of this `(user_id, session_id)` exists in the session deque. It describes the `messages` list, not the system prompt.
- `user_profile` appears iff at least one selected fact *other than* `sun_sign` was included (TDD §14: "at least one Profile field was included").
- `astrology` appears iff `sun_sign` was included (TDD §14: "the sun sign was included").
- then one entry per memory, `m.key`, in the order the brain returned them.

The two profile tags are deliberately disjoint in what they count. When the only *selected* field is `sun_sign`, the result is `["astrology"]` and not `["user_profile", "astrology"]`: for example a profile created by `POST /users` with just `date_of_birth` (which stores `date_of_birth` and the derived `sun_sign`) on a life-area or `general` query, where `wanted` is name plus sun sign and the profile has no name. The same profile on an `astrology` or `profile` query selects `date_of_birth` too and yields both tags. The tag reflects what was injected, not what the profile holds; PRD §8 lists "User profile" and "Astrology attributes" as separate context sources with separate priorities, and the tags mirror that split. Conversely name plus sun sign yields both tags. The rendered block, however, is always headed "User profile:" even in the sun-sign-only case; the split exists in the tags, not in the prompt text.

**Blocks and the three context texts.**

```python
blocks: list[str] = []
if facts:
    blocks.append("User profile:\n" + "\n".join(f"- {k.replace('_', ' ')}: {v}" for k, v in facts.items()))
if memories:
    blocks.append("What the user has told you before (most relevant first):\n" + "\n".join(map(_render, memories)))
if blocks:
    context = "\n\n".join(blocks)
elif category is Category.FOLLOW_UP:
    context = "not needed for this turn; answer from the conversation above."
else:
    context = "none yet; this may be a new user."
system = f"{SYSTEM_PROMPT}\n\nContext:\n{context}"
```

Profile keys are printed with underscores replaced by spaces ("sun sign", "date of birth"). Memories are rendered by `_render` as `- [<category>] <key> = <value>` with ` (timeframe: <target_timeframe>)` appended only when `target_timeframe` is set; the header "most relevant first" is true because the brain's ordering (confidence, then recency) is preserved. Memory `type`, `confidence`, `status`, ids and timestamps are not rendered. When neither block exists, the text distinguishes the two reasons: on a follow-up the brain was deliberately not consulted, so the model is told the context is "not needed for this turn"; otherwise nothing was found, so it is told there is "none yet". The `Context:` line is always present; `MockLLM.generate` in `app/llm.py` splits the system prompt on the literal `"Context:"` to echo what was injected, which is how `tests/test_chat.py::test_3_new_session_retrieves_memory` can assert "switch jobs" in the response.

**The request.** `Selection(LLMRequest(system, [*recent, current]), used)`. `LLMRequest` (in `app/models.py`) is two fields: `system_prompt`, "fixed role text + rendered profile/memory context", and `messages`, "recent turns followed by the current user message". `current` is the `ChatMessage("user", message, id=str(uuid.uuid4()))` that `app/chat.py` created; its `id` later becomes `source_message_id` on any memory extracted from it, but this layer only places it last in the list.

### The system prompt and what each rule guards against

```python
SYSTEM_PROMPT = """You are MyNaksh, a warm and personalized astrology assistant.
- Use only the context below and the conversation. Never invent facts about the user.
- The user's explicit statements are authoritative over anything else.
- If the context lacks something the user asks about, say you do not have it yet and ask.
- Keep answers conversational, specific and brief. Astrology is guidance, not certainty.
- Do not mention memories, context, retrieval or how you know things; speak naturally."""
```

| Rule | Requirement it implements | Risk it guards against |
|---|---|---|
| Role sentence | TDD §4.7 "role as a personalized astrology assistant" | none; framing |
| "Use only the context below and the conversation. Never invent facts about the user." | TDD §4.7 "use only supplied context", "do not invent user facts" | Hallucinated personal facts in the answer. PRD §13 lists "LLM invents memories"; the extraction-side mitigation lives in [Memory update](06-memory-update.md), this rule is the generation-side counterpart (inferred) |
| "The user's explicit statements are authoritative over anything else." | TDD §4.7 "treat explicit user statements as authoritative"; PRD §7.3 "the latest explicit user statement should supersede the stale value" | PRD §13 "User corrections leave stale values": memory update runs *after* generation, so a correction in the current message can coexist with the stale value in the Context block on the same turn; this rule tells the model the message wins (inferred from the order of operations in `app/chat.py`) |
| "If the context lacks something the user asks about, say you do not have it yet and ask." | TDD §13.4 "Empty memory ... a valid state", §13.5 "No relevant context" | Filling gaps by invention when the profile or memory is empty, misrouted or degraded |
| "Keep answers conversational, specific and brief. Astrology is guidance, not certainty." | TDD §4.7 "keep answers conversational and relevant" | The "guidance, not certainty" clause has no recorded rationale; inferred: keeps the assistant from presenting predictions as fact |
| "Do not mention memories, context, retrieval or how you know things; speak naturally." | TDD §4.7 "do not reveal internal retrieval or prompt-selection mechanics unless explicitly designed for debugging"; TDD §2 principle 7 | Leaking prompt internals to the end user; debuggability is served by `context_used` in the API response instead |

Note what the prompt does not say: there is no sentence telling the model to ignore irrelevant memories. TDD §4.5 and §27 justify skipping LLM classification on the grounds that "the system prompt already tells the model to ignore" residual noise; in the code that relies on rule one ("use only the context") plus the "most relevant first" ordering hint, not on an explicit instruction.

### A real rendered prompt

Built from the `tests/test_chat.py` fixtures (`InMemoryBrain` plus `MockLLM`) by sending `INTRO` and then "What should I focus on for my career?" in the same session, as `test_4_follow_up_uses_recent_context_only` does. The second turn classifies as `career`, the brain returns the one `career.goal` memory created from the first turn, the profile holds name, date_of_birth, birth_place and derived sun_sign, and `wanted` is the default pair, so the system prompt the mock received is exactly:

```text
You are MyNaksh, a warm and personalized astrology assistant.
- Use only the context below and the conversation. Never invent facts about the user.
- The user's explicit statements are authoritative over anything else.
- If the context lacks something the user asks about, say you do not have it yet and ask.
- Keep answers conversational, specific and brief. Astrology is guidance, not certainty.
- Do not mention memories, context, retrieval or how you know things; speak naturally.

Context:
User profile:
- name: Rahul
- sun sign: Leo

What the user has told you before (most relevant first):
- [career] career.goal = switch jobs (timeframe: 2027)
```

The timeframe is `date.today().year + 1` at extraction time (2027 when run in 2026). `messages` is three entries: the user `INTRO`, the mock's assistant reply, and the current user message; `context_used` is `["recent_conversation", "user_profile", "astrology", "career.goal"]`, the same list the README shows for this turn (README, "The PRD conversation, end to end"). The next turn in that test, "Why do you say that?", renders `Context:\nnot needed for this turn; answer from the conversation above.`, five messages (user, assistant, user, assistant, user) and `context_used == ["recent_conversation"]`. The very first turn, `INTRO` from a new user, renders `Context:\nnone yet; this may be a new user.` with `context_used == []`.

## Contracts and invariants

- **`classify` is total and single-valued.** Every `str`, including the empty string, returns exactly one of the ten `Category` members; there is no multi-label output, no confidence and no exception path. The API layer already rejects empty messages (`ChatRequest.message` has `min_length=1` in `app/main.py`), but the function does not depend on that.
- **`follow_up` is a query category, never a memory category.** `MEMORY_CATEGORIES` excludes it (`app/models.py`), `memory.validate` drops any candidate carrying it, and `extract_by_rules` remaps it to `general` before building a key. `general` is valid for both.
- **`context_used` ordering is stable:** `recent_conversation`, `user_profile`, `astrology` in that order when present, then memory keys in retrieval order (TDD §14). CLAUDE.md ("Invariants the tests enforce") says tests assert whole lists; `test_1`, `test_4`, `test_7`, `test_10` and `test_profile_endpoint_feeds_astrology_context` do, while `test_3`, `test_5`, `test_6`, `test_8` and `test_9` check membership of individual tags. Each tag appears iff the corresponding text was actually injected: `recent_conversation` iff `messages` has more than the current message, `user_profile` iff a non-sun-sign profile line was rendered, `astrology` iff the sun-sign line was rendered, and one key per rendered memory line.
- **`messages` ends with the current user message** and begins with the recent turns in session order (`[*recent, current]`). `MockLLM.generate` reads `request.messages[-1].content` as the current message and both real providers forward the list in order (`app/llm.py`). Because `app/chat.py` appends the user and assistant turns together only after a successful generation, `recent` alternates roles and `messages` alternates too (TDD §27, row §13; `tests/test_chat.py::test_4_follow_up_uses_recent_context_only` asserts `["user", "assistant", "user", "assistant", "user"]`).
- **No superseded memory is ever rendered**, and this layer does not check `status` to guarantee it. The guarantee comes from the brain: `InMemoryBrain.search_memories` filters `m.status == "ACTIVE"` and `SEARCH_MEMORIES` has `WHERE m.status = 'ACTIVE'` (`app/brain.py`). TDD §4.6: "Superseded memories are excluded by the query, never by post-filtering." `tests/test_chat.py::test_7_user_correction_supersedes` asserts "Hindi" is in the prompt and "English" is not.
- **Inputs arrive bounded, and the layer does not truncate.** `recent` has at most `RECENT_LIMIT` (default 10) entries because `SessionStore` is a `deque(maxlen=limit)`; `memories` has at most `MEMORY_LIMIT` (default 8) because `ChatService` passes `self.memory_limit` as the query `LIMIT`; a profile has at most six fields. `select_context` renders whatever it is handed and never slices (TDD §21 budgets are enforced upstream).
- **Follow-up turns never touch the brain**, in either direction: `app/chat.py` skips `get_profile`/`search_memories` and skips `extract_memories`/`remember` when `category is Category.FOLLOW_UP`.
- **Purity.** `select_context` and `classify` perform no I/O, take no `Settings`, and are deterministic for equal inputs, which is why the prompt is testable without a provider (TDD §4.7: "makes the exact prompt testable without a provider").
- **The literal `Context:` line is part of the prompt contract** relied on by `MockLLM.generate`, whose `split("Context:", 1)[1]` raises `IndexError` if the marker is absent; renaming it breaks every mock-backed chat test, not only the one that inspects the echoed context.

## Design decisions and alternatives rejected

**Keyword classification instead of an LLM or embedding classifier.** PRD FR-5 allowed "rules, LLM selection, a classifier, or a hybrid approach" and named a "lightweight hybrid strategy: deterministic signals for high-confidence categories and structured LLM extraction only where beneficial". The TDD narrowed this before implementation: "Classification is deterministic only; ambiguous falls to `general`", because it "saves an LLM round-trip per request; the system prompt already handles residual noise" (TDD §27, row §4.5), and "An LLM classification call is deferred: it would add a round-trip to every ambiguous request to save a few irrelevant memories" (TDD §4.5). TDD §21 adds "The design should avoid unnecessary LLM calls." The README states the trade-off as "Zero extra LLM calls; misroutes fall to `general`, which still retrieves everything" (README, Trade-offs). Embedding-based semantic retrieval is Phase 3 (TDD §23); PRD §14 only asks that the architecture "leave room for" items such as "Token/context optimization". The `ponytail:` marker above `_KEYWORDS` records the upgrade trigger: "swap for an LLM/embedding classifier when evals show misroutes". Rejected: an LLM classification call (latency and cost on every request, and a second failure point in the path that already has one LLM call); an embedding classifier (needs an embedding provider and training or exemplar data that the MVP does not have).

**The retrieval query is the ranking; no weighted score.** The v1 TDD had a weighted score over relevance, recency, confidence and source priority. v2 replaced it: "MVP ranking is the retrieval query itself: filter `status = 'ACTIVE'` and `category = $category` (all categories for `general`), then `ORDER BY confidence DESC, updated_at DESC LIMIT 8`. A weighted score ... is Phase 3 and only worth building once embeddings exist to feed the relevance term" (TDD §4.6; §27 row §4.6: "The formula had no semantic signal to weigh until embeddings exist"). README, Trade-offs: "Explainable; a weighted score has no semantic signal to weigh without embeddings." As a consequence this layer contains no ranking code at all; `select_context` receives the ordered list and preserves it, and "most relevant first" in the block header is a statement about the query's `ORDER BY`. Rejected: post-retrieval re-ranking in Python (would duplicate the database's ordering with no extra signal).

**Sun sign on every life-area query.** TDD §4.6: "Sun sign rides along on every life-area query because this is an astrology assistant: it is the personalization hook, not noise." README, Context selection, step 3 repeats it. This is why `_DEFAULT_PROFILE_FIELDS` is `("name", "sun_sign")` and why the default applies to `general` as well. Rejected: the narrower reading in TDD §8 Step 3, whose example says a career query "may include name and preferred language but need not include birth place unless astrology is relevant"; the implemented table (TDD §4.6 and `_PROFILE_FIELDS_FOR`) sends name and sun sign, reserves `preferred_language` for `language` queries, and sends birth data only for `profile` and `astrology`. No rationale is recorded for leaving `preferred_language` out of life-area queries; inferred: a language preference stated in chat is stored as the `language.preferred` Memory, not the Profile field (CLAUDE.md, "Invariants the tests enforce"), so the Profile field is only populated through `POST /users`, and it changes how to answer rather than what to answer, which the `language` route covers.

**Follow-up isolation.** PRD §5.3: a conversational reference "should primarily rely on recent short-term conversation context rather than retrieving the entire user graph"; PRD §8: "recent session context should dominate". TDD §4.6 makes it absolute (`follow_up`: memories none, profile fields none) and README, Context selection says "Follow-ups skip the graph entirely." The implementation splits the rule across two places: `app/chat.py` does not read the brain, and `_PROFILE_FIELDS_FOR[Category.FOLLOW_UP] = ()` makes `select_context` send nothing even if handed a profile. Rejected (inferred): retrieving anyway and letting the model weigh recency, which costs the two brain calls every keyword-routed turn makes (`get_profile`, then `search_memories`) and reintroduces the noise PRD §13 wants excluded. Two sub-decisions have no recorded rationale and are inferred from the code: keyword matches take precedence over follow-up phrases so that "Why is my career stuck?" retrieves career memories; and the pronoun rule is capped at six words so that a long message which happens to contain "it" is not mistaken for a bare reference.

**Context rendered once, in one place.** TDD §4.7: "Context is rendered to text once, by the prompt builder, so every provider receives the same two things ... Rendering context in one place keeps providers thin and makes the exact prompt testable without a provider." TDD §27, row §4.7: "`LLMRequest` reduced to `system_prompt` + `messages`". Rejected: a structured request carrying profile and memory objects for each provider adapter to format, which was the v1 shape; it would have meant three renderings (Anthropic, OpenAI-compatible, mock) to keep in sync and no single prompt to assert on. The `generate` methods in `app/llm.py` consequently do nothing with the request beyond mapping `system_prompt` and `messages` onto their SDK's call; their remaining code handles errors and empty or refused responses.

**`general` retrieves everything rather than nothing.** TDD §4.5: `general` "retrieves all active memories (top 8)"; TDD §4.6 table: "all active, top 8". `app/chat.py` implements it as `search = None`. Rejected (inferred): retrieving nothing on an unclassified message, which would make every misroute lose personalization; retrieving everything makes a misroute to `general` cost at most eight extra memory lines, which is the bound PRD §13 asks for.

**One taxonomy for queries and memories.** `Category` is shared "so retrieval is an equality match" (`app/models.py` docstring) and TDD §4.5 says categories "exist to narrow the graph search". Rejected (inferred): separate query intents mapped onto memory categories through a table, which would add a mapping with nothing to express while the two sets are identical apart from `follow_up`.

**An exact `context_used`.** TDD §14 defines the tags precisely and §27 records why: "Tests and the evaluation harness assert on it." The README calls the scenario tests "the PRD §11 rubric in executable form" (README, Tests). One detail worth knowing when reading TDD §14: its sample JSON shows `"career_goal"` while the definition paragraph beneath it and the code use the memory key itself, `career.goal`.

## Failure modes and degraded behavior

This layer raises nothing and has no dependencies that can fail; its failure modes are wrong decisions, and each is bounded by the layers around it.

| Situation | What happens | Why it is bounded |
|---|---|---|
| Message routed to the wrong life area (for example "energy drinks" to `health`) | Memories of the true category are not retrieved; name and sun sign still ride along; recent turns are still sent | At most eight memories of the wrong category are added, never more (`MEMORY_LIMIT`); the prompt tells the model to say what it does not have rather than invent; the turn's memory extraction is unaffected because extraction works on the message text, not the query category |
| Message routed to `general` when it had a topic | All active memories, top 8 by confidence and recency, across categories | This is the designed fallback (TDD §4.5); the cost is up to eight lines, bounded by the query `LIMIT` |
| Short pronoun-bearing statement routed to `follow_up` ("This is my plan.", "I like it") | No profile, no memories, and `app/chat.py` skips extraction for the turn | Recent turns are still sent, so the reply is coherent; the consequence is a missed memory, not a wrong one; the user can restate it in a longer sentence or one with a keyword |
| Follow-up phrased with a topical keyword ("Are you sure about that career advice?") routed to `career` | Two brain calls (`get_profile`, `search_memories`) and up to eight career memories plus name and sun sign are added | The recent turns the follow-up refers to are still in `messages`, so the question is still answerable; cost is latency and prompt size within budget |
| Stem false positive ("borne" to `profile`, "workshop" to `career`) | Same as a wrong life area | Same bound |
| Profile missing (`profile is None`) | `facts` is empty; no `user_profile`/`astrology` tag; profile block omitted; if no memories either, "none yet; this may be a new user." | TDD §13.4 treats empty memory as a valid state; README, Failure modes: "those tags simply do not appear in `context_used`"; `tests/test_chat.py::test_8_missing_profile_is_fine` |
| Memories empty | Memory block omitted; only profile (if any) and recent turns are sent | TDD §13.5 "No relevant context: Call the LLM with the current message and recent conversation only" |
| Profile has `date_of_birth` and derived `sun_sign` but no `name` or `preferred_language`, on a `language` query that finds no `language` memories | `wanted` is name plus preferred language, so `facts` is empty, no block is rendered and the text is "none yet; this may be a new user." | Correct per the table; noted because a profile exists yet nothing is rendered. In the normal case a `language.preferred` memory is retrieved and renders a block (`tests/test_chat.py::test_7_user_correction_supersedes`) |
| Shared Brain unavailable | If `get_profile` raises, `app/chat.py` hands in `profile=None, memories=[]` and rendering is identical to a brand-new user, including the text "none yet; this may be a new user."; if only the later `search_memories` raises, the profile already fetched is still rendered and `memories` is empty (both assignments sit in one `try`, and `profile` is set first) | The request still returns 200 with `degraded: true` set by the caller (README, Failure modes); this layer is not told about the outage and the prompt does not distinguish "no data" from "data unreachable" (inferred limit); `tests/test_chat.py::test_10_graph_failure_degrades` |
| Follow-up as the first message of a session ("Why?") | `recent` is empty, so `context_used == []` and the prompt says "answer from the conversation above" although there is none | The model has only the current message to work from, and rule three of the prompt directs it to say what it lacks and ask |

## Configuration

Direct: **None.** `app/context.py` does not import `app/config.py` and has no tunable values; the keyword lists, phrase list, six-word cap, field table and prompt text are constants.

Indirect, applied by callers before this layer runs:

| Setting (env) | Default | Where it is applied | Effect seen here |
|---|---|---|---|
| `RECENT_LIMIT` (`Settings.recent_limit`) | 10 | `SessionStore(limit)` deque cap in `app/session.py`, constructed in `create_app` (`app/main.py`) | Upper bound on `len(recent)`, hence on `messages` (recent plus one) |
| `MEMORY_LIMIT` (`Settings.memory_limit`) | 8 | `ChatService.memory_limit`, passed as `limit` to `brain.search_memories` in `app/chat.py` | Upper bound on `len(memories)` and on the number of memory keys in `context_used` |

`MIN_CONFIDENCE`, `LLM_*`, `BRAIN` and `NEO4J_*` do not influence this layer. See [Configuration and deployment](08-configuration-and-deployment.md) for the full table.

## Tests that pin this layer

All run with `uv run pytest`, no services required (`tests/conftest.py` injects `InMemoryBrain` and `MockLLM` through `create_app`). The `tests/test_chat.py` scenario tests reach this layer through `POST /chat`; `test_classify` and `test_rule_extractor_matches_prd_example` call `classify` and `extract_by_rules` directly. The one thing each asserts about this layer:

| Test | Asserts |
|---|---|
| `tests/test_units.py::test_classify` | Eleven parametrized message-to-category cases: two `career`, three `follow_up` ("Why do you say that?", "Tell me more", "Is that good?"), two `general` ("Hello!", "What do you remember about me?"), and one each of `language`, `profile`, `astrology`, `health`, using `is` on the enum member |
| `tests/test_chat.py::test_1_new_user_succeeds_with_no_context` | A new user's first message yields `context_used == []` |
| `tests/test_chat.py::test_3_new_session_retrieves_memory` | In a fresh session `career.goal` is in `context_used`, `recent_conversation` is not, and the memory value reached the prompt (the mock echoes the `Context:` block into the response) |
| `tests/test_chat.py::test_4_follow_up_uses_recent_context_only` | "Why do you say that?" gives `context_used == ["recent_conversation"]`, `memory_updates == 0`, a five-message alternating `messages` list, and no `career.goal` in the system prompt |
| `tests/test_chat.py::test_5_memory_persists_across_sessions` | "Any advice for my job?" (keyword `job`) retrieves `career.goal` in two later sessions |
| `tests/test_chat.py::test_6_irrelevant_memory_excluded` | A `career` query includes `career.goal` and excludes `health.goal`; "sleep" is absent from the system prompt |
| `tests/test_chat.py::test_7_user_correction_supersedes` | After a correction, "Which language should we use?" yields `context_used == ["language.preferred"]` with "Hindi" and not "English" in the prompt, i.e. superseded values never reach the prompt |
| `tests/test_chat.py::test_8_missing_profile_is_fine` | With no profile, neither `user_profile` nor `astrology` appears |
| `tests/test_chat.py::test_9_llm_failure_returns_503_and_mutates_nothing` | After a failed turn, the next message has no `recent_conversation`, pinning that the tag reflects only successfully recorded turns |
| `tests/test_chat.py::test_10_graph_failure_degrades` | With the brain failing, `context_used == []` on a topical message and `["recent_conversation"]` on the follow-up |
| `tests/test_chat.py::test_profile_endpoint_feeds_astrology_context` | A profile stored through `POST /users` (name plus derived sun sign) reaches the prompt: "What does my horoscope say about money?" yields exactly `["user_profile", "astrology"]` and "Leo" is in the system prompt. Its comment says "astrology wins over finance", but the `astrology`-over-`finance` routing itself is pinned by `test_classify`, since these assertions hold under either category |
| `tests/test_chat.py::test_rule_extractor_matches_prd_example` | Indirectly: `classify("switch jobs")` is `career`, producing the key `career.goal` |

`tests/test_neo4j.py` and `tests/test_openai_llm.py` do not touch this layer. See [Testing and verification](09-testing-and-verification.md) for the suite as a whole.

## Known limits and future work

The one deliberate shortcut in the covered file, verbatim from `app/context.py`:

```python
# ponytail: keyword classifier, first match wins; swap for an LLM/embedding classifier when evals show misroutes.
```

Upgrade path as stated: replace `classify` with an LLM or embedding classifier once an evaluation shows misroutes that matter. The function signature (`str -> Category`) and the two call sites (`app/chat.py`, `app/llm.py`) would not change; the follow-up route and the `general` fallthrough would need to be preserved or re-expressed. The eval fixture this depends on is described in TDD §18 but is not in the repository; the scenario tests are the current stand-in (README, Tests).

Limits inferred from the code, none recorded as decisions:

- **Stem matching over-matches.** Because `_KEYWORDS` patterns have no trailing `\b`, "borne" routes to `profile` (via "born") and "workshop" to `career` (via "work").
- **Keywords carry one sense.** Whole-word hits route by the listed category regardless of meaning: "energy drinks" routes to `health` (via "energy") and "interest rate" to `interests` (via "interest"; `finance`, although earlier in the list, has no word that matches it).
- **English-only vocabulary.** Apart from the transliterated astrology terms (rashi, kundli, kundali, nakshatra) and the language names, there are no Hindi keywords; "Hindi mein batao" routes to `language` only because it contains "hindi". PRD §14 lists Hindi and multilingual responses as future direction.
- **Single label, fixed priority.** A message about two life areas is routed to whichever category appears earlier in `_KEYWORDS`, not to both, and the second topic's memories are not retrieved unless the message falls to `general`.
- **Follow-up heuristics are surface-level.** No negation or question-form analysis; a durable statement phrased in six words or fewer with `that`/`this`/`it` ("This is my plan.") is treated as a follow-up and skipped for extraction.
- **No "ignore irrelevant items" instruction** in `SYSTEM_PROMPT`; residual noise handling rests on "use only the context below" and the ordering hint.
- **Outage is indistinguishable from emptiness in the prompt.** During a Shared Brain outage the model is told "none yet; this may be a new user." for a returning user; only the API field `degraded` carries the difference.
- **`context_used` lists keys, not `(category, key)` pairs.** `memory.validate` does not require a key's prefix to equal its category, so under `general` two active memories with the same key in different categories would both be rendered and the key would appear twice.
- **Weighted ranking and semantic retrieval** remain Phase 3 (TDD §23, §4.6); when embeddings arrive, the relevance term would feed both the classifier upgrade above and a re-ranking step that this layer currently does not have.
