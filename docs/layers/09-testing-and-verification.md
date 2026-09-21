# Testing and verification

AstroChat's test layer drives the real FastAPI application through `TestClient` with two fakes injected at the app factory (`InMemoryBrain` for the Shared Brain, `MockLLM` for the LLM), so the ten required scenarios of TDD §17 run against the complete HTTP stack with no Neo4j, no provider key and no network. Around that core sit per-layer unit tests, a fake-server test of the OpenAI-compatible adapter built on `httpx.MockTransport`, and one opt-in round trip against a live Neo4j. Running `uv run pytest -q -p no:warnings` in this worktree collected 49 tests: 48 passed and 1 skipped (the live Neo4j test, gated on `NEO4J_TEST_URI`) in 3.3 to 7.8 seconds across several runs, almost all of it spent in the one test that waits for the Neo4j driver to give up on a refused connection under its 3 second fail-fast settings.

**Files:** `tests/conftest.py`, `tests/test_chat.py`, `tests/test_units.py`, `tests/test_openai_llm.py`, `tests/test_neo4j.py`, pytest section of `pyproject.toml`

**Depends on:** every other layer, because the scenarios exercise the whole request path: [API layer](01-api-layer.md), [orchestration and short-term context](02-orchestration-and-short-term-context.md), [domain model](03-domain-model.md), [query understanding and context selection](04-query-understanding-and-context-selection.md), [Shared Brain](05-shared-brain.md), [memory update](06-memory-update.md), [LLM providers](07-llm-providers.md), [configuration and deployment](08-configuration-and-deployment.md). **Used by:** the [layer index](README.md); nothing in `app/` imports from `tests/`, and the repository has no CI workflow, so the suite is run by hand with the commands below.

## What this layer is

The layer is five files under `tests/` plus three lines of pytest configuration in `pyproject.toml`:

| File | Kind | Tests collected | Needs |
|---|---|--:|---|
| `tests/conftest.py` | Fixtures `brain`, `llm`, `client` | 0 | nothing |
| `tests/test_chat.py` | The ten TDD §17 scenarios plus three extra API-level tests, all through `POST /chat`, `POST /users` and `GET /users/{user_id}/memories` | 13 | nothing |
| `tests/test_units.py` | Unit tests for `classify`, `sun_sign`, `parse_date`, `memory.validate`, `InMemoryBrain.upsert_memory`, `InMemoryBrain.upsert_profile`, and the `Neo4jBrain` connection-refused translation | 22 | a loopback socket that refuses connections (`127.0.0.1:1`) |
| `tests/test_openai_llm.py` | `OpenAICompatibleLLM` and `build_llm` against an in-process fake chat-completions server | 13 | nothing |
| `tests/test_neo4j.py` | The brain semantics against a live Neo4j; skipped unless `NEO4J_TEST_URI` is set | 1 | a reachable Neo4j 5 |

Counts are from `uv run pytest --collect-only -q` (parametrized cases counted individually). The dev dependency group in `pyproject.toml` is `pytest`, `pytest-asyncio` and `httpx`; `httpx` serves both `fastapi.testclient.TestClient` and the `httpx.MockTransport` fake server.

There is no separate evaluation harness module. The scenario tests in `tests/test_chat.py` are the evaluation rubric of PRD §11 in executable form (README "Tests"): each asserts the expected `context_used` and the expected facts in the prompt the LLM received.

## Why it exists

The requirements this layer discharges are explicit in the spec:

- PRD §3.1 lists "Provide 5–10 automated scenarios covering the core behavior" as a primary goal, and PRD §12 lists "Automated tests" under "Must have".
- PRD §10 acceptance criteria 1 to 9 are each a behavior the scenarios pin: response fields (1), durable memory from a first message (2), follow-up from recent context (3), retrieval in a new session (4), exclusion of irrelevant memories (5), a corrected fact reflected in later retrieval (6), missing profile or empty memory not crashing (7), safe fallback for LLM and graph failure (8), and "Automated tests cover 5–10 required scenarios" (9).
- PRD §11 asks for evaluation "against a fixed scenario set that compares the assistant with and without Shared Brain context" and says "A simple rubric with expected facts per test case is sufficient for the assignment."
- TDD §16 sets the strategy in three parts: §16.1 unit tests for deterministic logic ("context selection, memory validation, memory upsert decisions, follow-up detection, request schema validation"), §16.2 integration tests where "The default test run uses `InMemoryBrain` and `MockLLM` and needs no services" and `tests/test_neo4j.py` "is skipped unless `NEO4J_TEST_URI` is set", and §16.3 API tests of "the complete `/chat` flow with mocked LLM and repository dependencies".
- TDD §17 enumerates the ten required scenarios with expected results; TDD §18 describes the evaluation harness shape (`expected_context`, `expected_facts`) and says "A simple pass/fail or 0–2 rubric is sufficient".
- TDD §26 Definition of Done includes "Tests cover the required scenarios."

The problem the layer solves is that the interesting behavior of this service is not in any single function but in the sequence Chat → Context Selection → Shared Brain → LLM → Response → Memory Update (CLAUDE.md). Whether a follow-up skips the graph, whether a failed LLM call leaves no trace, whether a correction supersedes rather than overwrites: these are properties of the orchestration plus the persistence semantics plus the prompt builder together. Testing them requires running the whole path with the two external dependencies replaced by deterministic stand-ins, which is why `create_app(brain=, llm=, settings=)` accepts injected implementations (CLAUDE.md: "tests inject fakes here") and why TDD §27 records `InMemoryBrain` being promoted to "a first-class implementation" for "Tests and no-Neo4j demo runs; it is also the executable reference for the Cypher semantics".

Two further recorded design choices exist specifically so the tests can be exact. TDD §14: "`context_used` is defined precisely so tests can assert on it", with a stable order (three source tags, then memory keys in retrieval order), and the TDD §27 row for §14 gives the reason "Tests and the evaluation harness assert on it". TDD §4.7: rendering context once into `LLMRequest.system_prompt` "makes the exact prompt testable without a provider", which is what lets a test look at `llm.requests[-1].system_prompt` and check that "Hindi" is present and "English" is not.

## How it works

### Fixtures and injection

`tests/conftest.py` is the whole fixture layer:

```python
@pytest.fixture
def brain() -> InMemoryBrain:
    return InMemoryBrain()


@pytest.fixture
def llm() -> MockLLM:
    return MockLLM()


@pytest.fixture
def client(brain, llm):
    app = create_app(brain=brain, llm=llm, settings=Settings(brain="memory", llm_provider="mock"))
    with TestClient(app) as c:
        yield c
```

How the pieces fit:

- `create_app` in `app/main.py` stores the injected `brain` and `llm` on `app.state` inside its `lifespan` context manager and builds `ChatService(app.state.brain, app.state.llm, SessionStore(settings.recent_limit), settings.memory_limit, settings.min_confidence)`. Only when `brain` or `llm` is `None` does it fall back to `InMemoryBrain()`/`Neo4jBrain(...)` and `build_llm(settings)`. Because the fixture always passes both, the `brain="memory"` and `llm_provider="mock"` values in the fixture's `Settings` are never consulted for construction; they document intent and would keep the app service-free if an injection were ever removed (inferred). The remaining `Settings` defaults do flow into the service: `recent_limit=10`, `memory_limit=8`, `min_confidence=0.6`.
- `TestClient(app)` is used as a context manager. Starlette runs the ASGI lifespan only inside `with TestClient(app) as c:`; without the `with`, `app.state.chat` would never be created and every route would fail. This is why the fixture yields from inside the `with` block.
- All three fixtures are function-scoped (pytest's default), so every test gets a fresh `InMemoryBrain`, a fresh `MockLLM` and, through the lifespan, a fresh `SessionStore`. Nothing leaks between tests.
- Tests that need to inspect state receive the same `brain` and `llm` objects the app holds, because pytest resolves the shared `brain`/`llm` fixtures once per test. A test can therefore `await brain.search_memories(...)` or read `llm.requests` after driving the API. `InMemoryBrain` is a plain dict store with no event-loop affinity, so awaiting it from the test's own loop while `TestClient` runs the app on its worker thread is safe (inferred from the implementation in `app/brain.py`).
- The fakes carry an outage switch: `InMemoryBrain(fail=...)` raises `BrainUnavailable("in-memory brain set to fail")` from every method via `_check()`, and `MockLLM(fail=...)` raises `LLMError("mock failure")` from both `generate` and `extract_memories`. Tests flip `brain.fail` and `llm.fail` at runtime.
- `MockLLM.generate` records every request in `self.requests` (extraction calls are not recorded) and returns a string that echoes the text after `Context:` in the system prompt. That echo is what makes "switch jobs" observable in `r["response"]` in scenario 3. `MockLLM.extract_memories` delegates to `extract_by_rules`, the regex extractor that "covers the PRD example sentences, nothing more" (comment in `app/llm.py`).

### The `chat()` helper

```python
def chat(client, user: str, session: str, message: str) -> dict:
    r = client.post("/chat", json={"user_id": user, "session_id": session, "message": message})
    assert r.status_code == 200, r.text
    return r.json()
```

Every scenario turn goes through this helper, which fails with the response body in the assertion message if the status is not 200. The one turn that must return 503 (scenario 9) posts directly with `client.post` instead.

Two module constants keep the scenarios anchored to the PRD without rotting: `INTRO` is the PRD §5.1 sentence verbatim ("My name is Rahul. I was born on 15 August 1995 in Delhi. I'm planning to switch jobs next year."), and `NEXT_YEAR = str(date.today().year + 1)` because `extract_by_rules` resolves "next year" against `today`, so a hard-coded "2027" would start failing on 1 January 2027.

### The ten scenarios

Each row maps a TDD §17 scenario to its test in `tests/test_chat.py`, the turns it sends, what it asserts, and why that assertion proves the requirement.

| # | TDD §17 scenario | Test | Turns | Asserts | Why this proves it |
|--:|---|---|---|---|---|
| 1 | New user: response succeeds; no prior memory required | `test_1_new_user_succeeds_with_no_context` | `u1/s1` "Hello there!" | 200; `user_id` and `session_id` echoed; `response` non-empty; `context_used == []`; `degraded is False` | PRD §10 #1 (response fields present). The empty list proves a `general` query on an empty brain injects nothing and there is no `recent_conversation` on a first turn. `degraded is False` proves the empty brain was read successfully rather than skipped: TDD §13.4 "Empty memory ... Treat as a valid state." |
| 2 | Durable memory creation | `test_2_first_message_creates_durable_memory_and_profile` | `u1/s1` `INTRO` | `memory_updates == 4`; `brain.search_memories("u1", "career", 8)` returns exactly one memory `("career.goal", "switch jobs", NEXT_YEAR, "ACTIVE")`; `brain.get_profile("u1")` is `("Rahul", "1995-08-15", "Delhi", "Leo")` | The 4 is `remember` in `app/memory.py` counting three Profile fields plus one `"created"` upsert, so it proves TDD §7.0 routing (name, date and place of birth go to the Profile node, the goal becomes a Memory). The ISO date and "Leo" prove `_with_sun_sign` normalized the date and derived the sign, the reason recorded in TDD §27 for §7.0. The `target_timeframe` equal to next year is PRD §5.1's "career goal with target year 2027". |
| 3 | Memory retrieval in a new session | `test_3_new_session_retrieves_memory` | `INTRO` in `s1`; then `s2` "What do you remember about my career goals?" | `"career.goal" in context_used`; `"recent_conversation" not in context_used`; `"switch jobs" in response` | PRD §5.4 and PRD §10 #4. The absent `recent_conversation` proves the session store is keyed by `(user_id, session_id)` so `s2` starts empty; the present key proves the fact came from the graph; "switch jobs" in the mock's echoed response proves the fact reached the prompt text, not only the tag list. This is the "with Shared Brain" half of the PRD §11 comparison; scenario 1 is the "without" half (README "Tests"). |
| 4 | Follow-up resolved from recent context | `test_4_follow_up_uses_recent_context_only` | `INTRO`; "What should I focus on for my career?"; "Why do you say that?" all in `s1` | `context_used == ["recent_conversation"]`; `memory_updates == 0`; roles of `llm.requests[-1].messages` are `["user", "assistant", "user", "assistant", "user"]`; `"career.goal" not in system_prompt` | PRD §5.3 says the follow-up "should primarily rely on recent short-term conversation context rather than retrieving the entire user graph". The exact one-element list proves no `user_profile`, no `astrology` and no memory key was injected, which can only happen if `ChatService.chat` skipped the brain for `Category.FOLLOW_UP`. `memory_updates == 0` proves extraction was skipped on the follow-up (CLAUDE.md invariant). The role sequence proves both previous turns were appended as alternating user/assistant pairs and sent as short-term context, the property TDD §27 (§13 row) calls "Keeps session history alternating". |
| 5 | Cross-session persistence | `test_5_memory_persists_across_sessions` | `INTRO` in `s1`; `GET /users/u1/memories`; then "Any advice for my job?" in `s2` and `s3` | The memories list is exactly `[("career.goal", "ACTIVE")]`; `"career.goal" in context_used` for both new sessions | The exact one-item list proves the three profile facts did not also become Memory nodes and that the debug endpoint exposes status. Two further sessions prove the memory outlives any session, the PRD §11 "memory persistence" metric. |
| 6 | Irrelevant memory excluded | `test_6_irrelevant_memory_excluded` | Seeds `career.goal` and `health.goal` directly with `brain.upsert_memory`; then "What should I focus on in my career?" | `"career.goal" in context_used and "health.goal" not in context_used`; `"sleep" not in llm.requests[-1].system_prompt` | PRD §10 #5 and the PRD §11 "irrelevant context rate" metric. Seeding through the brain rather than through chat isolates retrieval from extraction. Checking the prompt as well as the tag proves exclusion at the text the LLM saw, so a rendering bug that leaked a memory into the prompt while reporting it correctly in `context_used` would still fail. |
| 7 | User correction supersedes | `test_7_user_correction_supersedes` | "I prefer English." then "Actually, I prefer Hindi." in `s1`; then "Which language should we use?" in `s9` | `memory_updates == 1` on the correction; active language memories are `["Hindi"]`; `list_memories` statuses are `{"English": "SUPERSEDED", "Hindi": "ACTIVE"}`; in `s9`, `context_used == ["language.preferred"]`, `"Hindi" in system_prompt`, `"English" not in system_prompt` | This is the PRD §7.3 example verbatim. `memory_updates == 1` proves the upsert returned `"updated"` (one logical key changed) rather than creating a second active memory. The statuses prove the Appendix C decision (mark the existing memory SUPERSEDED, create a new ACTIVE one; restated in TDD §22), not an in-place overwrite. The `s9` turn is PRD §10 #6 "reflected in future retrievals": a new session, an exact one-key list, and the stale value absent from the prompt. |
| 8 | Missing profile | `test_8_missing_profile_is_fine` | user `nobody`, "What should I focus on in my career?" | `response` non-empty; `"user_profile" not in context_used and "astrology" not in context_used` | PRD FR-8 "Missing profile data" and PRD §10 #7. The absent tags follow README "Failure modes": "Missing profile / empty memory: Normal 200; those tags simply do not appear in `context_used`". |
| 9 | LLM failure: graceful error, no unsafe memory mutation | `test_9_llm_failure_returns_503_and_mutates_nothing` | `llm.fail = True`; raw `POST /chat` with `INTRO`; then `llm.fail = False`; "Hi" in the same `s1` | 503 and `"LLM" in detail`; `brain.list_memories("u1") == []` and `brain.get_profile("u1") is None`; the next turn's `context_used` lacks `recent_conversation` | TDD §13.3: "Return 503 ... Do not append the failed turn to session history and do not persist memories: nothing about the request is recorded." The empty brain proves no memory or profile write happened even though `INTRO` would normally produce four. The final turn is the only external way to observe `SessionStore`: if the failed user turn had been appended, "Hi" would have seen `recent_conversation`. The detail text comes from the `_llm_error` handler in `app/main.py`, which formats `"LLM unavailable: ..."`. |
| 10 | Graph failure: response may degrade to short-term/profile context | `test_10_graph_failure_degrades` | `brain.fail = True`; `INTRO` in `s1`; then "Why do you say that?" | First turn: 200 with `degraded is True`, `memory_updates == 0`, `context_used == []`; second turn: `context_used == ["recent_conversation"]` | TDD §13.2: continue with recent context, return `degraded: true`, and "Skip the post-response memory write when the pre-response read already failed". `memory_updates == 0` proves the skip; the second turn proves short-term context survives a dead brain. This test uses `InMemoryBrain.fail`; the real driver's error translation and its fail-fast settings are pinned separately by `test_neo4j_connection_failure_is_brain_unavailable`. |

What the code actually produces for scenario 3's `context_used` is `["user_profile", "astrology", "career.goal"]` (the README transcript shows the same list); the test asserts membership rather than the whole list. Scenarios 1, 4, 7 (the `s9` turn), 10 and `test_profile_endpoint_feeds_astrology_context` assert whole lists.

### The extra API tests in `tests/test_chat.py`

| Test | Asserts | Requirement |
|---|---|---|
| `test_invalid_payload_is_422` | `POST /chat` without `session_id` is 422; with `message: ""` is 422 (`ChatRequest.message` has `min_length=1`); `POST /users` with `date_of_birth: "not-a-date"` is 422 (`UserUpsert.date_of_birth` is a Pydantic `date`) | TDD §13.1 and PRD FR-8 "Invalid request payload"; TDD §16.1 "request schema validation" |
| `test_profile_endpoint_feeds_astrology_context` | `POST /users` with name and `1995-08-15` returns 200 and `sun_sign == "Leo"`; then "What does my horoscope say about money?" yields exactly `["user_profile", "astrology"]` and `"Leo" in system_prompt` | Pins the TDD §4.6 row for `astrology` (all profile fields ride along) and the tag definitions in TDD §14: `user_profile` because `name` and `date_of_birth` were injected, `astrology` because `sun_sign` was. The comment "astrology wins over finance" records that "horoscope" matches `Category.ASTROLOGY` before "money" would match `Category.FINANCE`, because `_KEYWORDS` in `app/context.py` is ordered and first match wins. No memory keys appear because the brain has none. |
| `test_rule_extractor_matches_prd_example` | `extract_by_rules(INTRO, date.today())` yields exactly `profile.name = Rahul`, `profile.date_of_birth = 1995-08-15`, `profile.birth_place = Delhi`, `career.goal = ("switch jobs", NEXT_YEAR)` | Pins the mock extractor to the PRD §5.1 sentence directly, so a regex regression fails here with a dict diff naming the changed candidate, alongside the `memory_updates == 4` failure in scenario 2 (inferred). |

### Unit tests by layer (`tests/test_units.py`)

| Layer | Test | Cases | What it pins |
|---|---|--:|---|
| Query understanding (`app/context.py`) | `test_classify` | 11 | Keyword hits: "career" twice for `CAREER`, "hindi" for `LANGUAGE`, "my name" for `PROFILE`, "horoscope" for `ASTROLOGY` (over "money"), "health" for `HEALTH`. Follow-up cues: "Why do you say that?" and "Tell me more" match `_FOLLOW_UP`; "Is that good?" has no cue but is at most 6 words with a bare pronoun, the second rule in `classify`. Fall-through to `GENERAL`: "Hello!" and "What do you remember about me?" (no keyword, no cue, and "me" is not in `_PRONOUN`). This is TDD §16.1 "follow-up detection". |
| Astrology stub (`app/astrology.py`) | `test_sun_sign` | 6 | Boundary days of `_SIGN_ENDS`: 15 Aug Leo; 23 Aug Virgo (Leo ends 22 Aug); 22 Dec Capricorn (Sagittarius ends 21 Dec, hitting the `(12, 31, "Capricorn")` wrap entry); 19 Jan Capricorn (last day); 20 Jan Aquarius; 21 Mar Aries (Pisces ends 20 Mar). |
| Astrology stub | `test_parse_date_formats` | 1 | "15 August 1995", "1995-08-15" and "August 15, 1995" parse to the same date; "15th Aug 1995" parses after the ordinal suffix is stripped by the regex in `parse_date`; "sometime in 1995" is `None`. The `"%B %d %Y"` and `"%d/%m/%Y"` formats accepted by `_DATE_FORMATS` are not pinned. |
| Memory update (`app/memory.py`) | `test_validate_filters_and_normalizes` | 1 | Of five candidates only one survives `validate(cands, 0.6)`: key, category and type are lower-cased and the value stripped (`"Career.Goal"` becomes `"career.goal"`, `" switch jobs "` becomes `"switch jobs"`); dropped are confidence 0.3 below the threshold, category `weather` outside `MEMORY_CATEGORIES`, type `rumor` outside `MEMORY_TYPES`, and a whitespace-only value. TDD §16.1 "memory validation". |
| Shared Brain reference implementation (`app/brain.py`) | `test_upsert_outcomes` | 1 | Appendix C in `InMemoryBrain`: first upsert `"created"`; same value with higher confidence `"unchanged"` (confidence raised to 0.95, no new node); different value `"updated"`; afterwards `search_memories` returns only `("Hindi", "m3")` and `list_memories` shows `("English", "SUPERSEDED", 0.95)` and `("Hindi", "ACTIVE", 0.8)`. TDD §16.1 "memory upsert decisions". CLAUDE.md calls this class "the readable spec for the Cypher". |
| Shared Brain reference implementation | `test_upsert_profile_derives_sun_sign` | 1 | `upsert_profile` normalizes "15 August 1995" to `1995-08-15` and sets `sun_sign` Leo; a second partial upsert with only `birth_place` merges into the existing profile (name and sign kept). |
| Shared Brain driver boundary | `test_neo4j_connection_failure_is_brain_unavailable` | 1 | `Neo4jBrain("bolt://127.0.0.1:1", "neo4j", "x").get_profile("u")` raises `BrainUnavailable`, proving `_query` translates the driver's connectivity errors (`_CONNECTIVITY_ERRORS` in `app/brain.py`) so `ChatService` "sees only `BrainUnavailable` and `LLMError`, never driver or SDK exceptions" (CLAUDE.md). The `finally: await brain.close()` releases the driver. It is the slowest test in the suite by two orders of magnitude (3.33 s and 6.18 s in two runs here, see `--durations`; the next slowest is 0.04 s) because the driver keeps retrying the refused connection under `connection_timeout=3.0` and `max_transaction_retry_time=3.0` before one of the `_CONNECTIVITY_ERRORS` surfaces; TDD §13.2 records the 3 second settings as deliberate, "rather than the driver's 30s default". |

There is no direct unit test of `select_context`; its behavior is pinned through the `context_used` lists and prompt contents asserted in `tests/test_chat.py`.

### The fake-server technique (`tests/test_openai_llm.py`)

`OpenAICompatibleLLM` wraps `openai.AsyncOpenAI`, an SDK that speaks HTTP through `httpx`. Rather than mock the SDK's classes, the tests give the SDK a real `httpx.AsyncClient` whose transport is `httpx.MockTransport(handler)`: every request the SDK builds is serialized exactly as it would be on the wire and handed to a Python function that returns an `httpx.Response`. The SDK then parses that response through its normal code path. The result is a test of the adapter plus the real SDK request/response machinery with no socket opened and no key required. CLAUDE.md records this as the house technique: "`OpenAICompatibleLLM` is tested against a fake server via `httpx.MockTransport` passed as `http_client` ...; use the same trick for any provider change."

```python
def fake(handler, effort: str | None = "medium") -> OpenAICompatibleLLM:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenAICompatibleLLM("local-model", base_url="http://fake/v1", api_key="k", effort=effort,
                               http_client=client, max_retries=0)
```

`http_client` and `max_retries` travel through `OpenAICompatibleLLM.__init__`'s `**client_kwargs` into `openai.AsyncOpenAI`. `max_retries=0` matters for the error tests: with the SDK default the 5xx, 429 and connection-error cases would sleep through the SDK's retry back-off before surfacing (inferred from the SDK's retry behavior). The `completion(content)` helper builds a minimal chat-completion JSON (`id`, `object`, `created`, `model`, one `choices` entry) that the SDK parses.

| Test | Cases | What it pins |
|---|--:|---|
| `test_generate_sends_system_then_turns` | 1 | Request shape: URL `http://fake/v1/chat/completions` (so the `base_url` argument, which `build_llm` fills from `OPENAI_BASE_URL`, is honored), header `authorization: Bearer k`, body `model == "local-model"`, `reasoning_effort == "medium"`, and messages `[system SYS, user, assistant, user]` in that order, meaning the system prompt is prepended and the turns preserved. The returned text is stripped of surrounding whitespace. |
| `test_empty_effort_omits_reasoning_effort` | 1 | With `effort=""` the body has no `reasoning_effort` key at all, which is the "leave it empty for models that reject the parameter" behavior described in README "Run it" and `.env.example`. |
| `test_extract_requests_json_mode_and_tolerates_fences` | 1 | Extraction sends `response_format == {"type": "json_object"}` and `reasoning_effort`; the system content contains `"Respond with JSON only"` (the `_JSON_SHAPE` suffix) and `"2027"` (`next_year` computed from `today = 2026-09-21`). A response wrapped in a ```` ```json ```` fence is stripped by `_FENCE` and validated into one `MemoryCandidate` with `("career.goal", "switch jobs", "2027", 0.95)`. TDD §10: `json_object` mode is "the lowest common denominator across OpenAI, Ollama, vLLM, Groq, OpenRouter and LM Studio". |
| `test_extract_invalid_output_is_llm_error` | 3 | `"not json"`, a JSON object missing required fields, and `None` content each raise `LLMError`, because `_Extraction.model_validate_json` fails and the `ValidationError` is re-raised as `LLMError`. TDD §10: "invalid output is an `LLMError`, so the response is still returned and only the memory write is skipped." |
| `test_generate_empty_content_is_llm_error` | 1 | `None` content on generation raises `LLMError` matching "empty". |
| `test_status_mapping` | 4 | HTTP 500 and 503 become `LLMError` (`_guarded`: `APIStatusError` with `status_code >= 500`); 429 becomes `LLMError` (`RateLimitError`); 400 propagates as `openai.BadRequestError`, pinning TDD §10 "4xx responses propagate, because they indicate a bug on our side rather than an outage" and CLAUDE.md "4xx errors deliberately propagate". |
| `test_connection_error_is_llm_error` | 1 | A handler that raises `httpx.ConnectError` surfaces as `LLMError` matching "unavailable" (`APIConnectionError` branch of `_guarded`). |
| `test_build_llm_selects_provider` | 1 | Provider selection from `Settings`: `mock` gives `MockLLM`, `anthropic` gives `AnthropicLLM`, `openai` with `llm_model` and `openai_base_url` gives `OpenAICompatibleLLM`; `openai` without a model raises `ValueError` matching "LLM_MODEL" (CLAUDE.md: "`build_llm` raises at startup if it is missing"); `gemini` raises `ValueError` matching "unknown". Constructing `AnthropicLLM` here does not need a key: the selection test passed in this worktree with `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` unset. |

### The live Neo4j round trip (`tests/test_neo4j.py`)

The module reads `URI = os.getenv("NEO4J_TEST_URI")` at import and applies `pytestmark = pytest.mark.skipif(not URI, reason="set NEO4J_TEST_URI (and NEO4J_USER/NEO4J_PASSWORD) to run")`, so without the variable the single test is collected and reported as skipped; that is the `s` in the default run. With it set, `test_neo4j_roundtrip`:

1. Builds `Neo4jBrain(URI, NEO4J_USER or "neo4j", NEO4J_PASSWORD or "password")` and a unique user id `f"test-{uuid.uuid4()}"`, so repeated runs and other data in the same database cannot collide.
2. Calls `ensure_schema()` (the constraints and index in `SCHEMA`), then checks `get_profile` is `None` for the new user, `upsert_profile` with name and `1995-08-15` returns `("Rahul", "Leo")`, and a re-read shows `birth_place is None`.
3. Repeats the Appendix C outcomes against Cypher: `"created"`, `"unchanged"` for the same value, `"updated"` for "Hindi", and `"created"` for a `career.goal` with confidence 0.95.
4. Checks retrieval: the language search returns only `["Hindi"]`; the `None` (all categories) search returns `["career.goal", "language.preferred"]`, which pins `ORDER BY m.confidence DESC` (0.95 before 0.9); the health search is empty; `list_memories` shows `("English", "SUPERSEDED")`, `("Hindi", "ACTIVE")`, `("switch jobs", "ACTIVE")`; and `created_at.tzinfo is not None`, pinning the CLAUDE.md gotcha that `neo4j.time.DateTime` must be converted with `.to_native()` to a timezone-aware datetime.
5. Runs one raw Cypher query through `brain._driver.execute_query` to prove the edge the protocol does not expose: `MATCH (:User {id: $id})-[:HAS_MEMORY]->(n:Memory {value: 'Hindi'})-[:SUPERSEDES]->(o:Memory) RETURN o.value` returns exactly `["English"]`. Anchoring the match on the test user's `HAS_MEMORY` edge scopes it to this test's data.
6. In `finally`, deletes the user and every node it points to (`MATCH (u:User {id: $id}) OPTIONAL MATCH (u)-->(n) DETACH DELETE u, n`) and closes the driver, so a failing assertion does not leave test data behind.

This is TDD §16.2's integration test: "`tests/test_neo4j.py` runs the same upsert, search and supersede assertions against a real Neo4j". CLAUDE.md: "The in-memory class is the readable spec for the Cypher; change both together and run `test_neo4j.py`."

### Running each subset

| Purpose | Command |
|---|---|
| Whole default suite (no services, no keys) | `uv run pytest` or `uv run pytest -q -p no:warnings` |
| Exact inventory | `uv run pytest --collect-only -q` |
| Only the ten scenarios and extra API tests | `uv run pytest tests/test_chat.py` |
| One scenario by keyword (CLAUDE.md example) | `uv run pytest tests/test_chat.py -k correction` |
| Unit tests | `uv run pytest tests/test_units.py` |
| OpenAI-compatible adapter | `uv run pytest tests/test_openai_llm.py` |
| See the slow test | `uv run pytest --durations=5` |
| Start a throwaway Neo4j (CLAUDE.md) | `docker run -d -p 7687:7687 -e NEO4J_AUTH=neo4j/password neo4j:5` |
| Add the live round trip | `NEO4J_TEST_URI=bolt://localhost:7687 NEO4J_PASSWORD=password uv run pytest` |
| Only the live round trip | `NEO4J_TEST_URI=bolt://localhost:7687 NEO4J_PASSWORD=password uv run pytest tests/test_neo4j.py` |

`uv run` creates `.venv` from `uv.lock` on first use (CLAUDE.md: `uv sync` "creates .venv"). `-p no:warnings` hides one `DeprecationWarning` emitted by Starlette's `testclient` module about an `anyio` alias; it is not from this codebase. The run behind this document used CPython 3.13.1 (the project requires `>=3.12`), pytest 9.1.1, pytest-asyncio 1.4.0, httpx 0.28.1, FastAPI 0.141.1, neo4j 6.3.1, openai 3.16.2 and anthropic 1.7.0; the package versions are the ones pinned in `uv.lock`.

## Contracts and invariants

- **No network by default.** The 48 tests that run without `NEO4J_TEST_URI` use `InMemoryBrain`, `MockLLM` and `httpx.MockTransport`; no traffic leaves the host and no API key is read. The single socket operation is `test_neo4j_connection_failure_is_brain_unavailable` connecting to `127.0.0.1:1`, which is refused locally. Verified in this worktree with `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` and `NEO4J_TEST_URI` unset.
- **The real application, not a copy.** Every scenario goes through `create_app` in `app/main.py`, the real routes, exception handlers, `ChatService`, `select_context`, `remember` and `SessionStore`. Only the two ports to external systems are replaced, and they are replaced with the same classes the service ships for `BRAIN=memory` and `LLM_PROVIDER=mock` runs.
- **`context_used` is asserted as data, not as prose.** Five tests compare the whole list with `==`, which pins the TDD §14 definition including its order: `test_1_new_user_succeeds_with_no_context` (`[]`), `test_4_follow_up_uses_recent_context_only` (`["recent_conversation"]`), `test_7_user_correction_supersedes` (`["language.preferred"]` on the `s9` turn), `test_10_graph_failure_degrades` (`[]` then `["recent_conversation"]`) and `test_profile_endpoint_feeds_astrology_context` (`["user_profile", "astrology"]`). Five assert membership or absence with `in` / `not in`: `test_3_new_session_retrieves_memory`, `test_5_memory_persists_across_sessions`, `test_6_irrelevant_memory_excluded`, `test_8_missing_profile_is_fine` and `test_9_llm_failure_returns_503_and_mutates_nothing`. In either form, a change to the tag names, the order, or the rule that a follow-up reads no graph breaks a test.
- **Expected facts are checked in the prompt.** Scenarios 4, 6 and 7 and the profile-endpoint test read `llm.requests[-1].system_prompt`; scenario 3 reads the mock's echoed response. This is the TDD §18 `expected_facts` check, and it depends on `MockLLM.generate` echoing the `Context:` section and recording each request.
- **Failure scenarios verify absence of side effects, not only status codes.** Scenario 9 checks the brain is empty and the session did not grow; scenario 10 checks `memory_updates == 0`. TDD §17 phrases scenario 9 as "no unsafe memory mutation".
- **Per-test isolation.** Function-scoped fixtures give each test its own brain, LLM, app and session store. `tests/test_units.py` constructs its own `InMemoryBrain` instances and never touches the fixtures.
- **Live-test data isolation.** `test_neo4j_roundtrip` uses a `uuid4` user id, anchors every query including the `SUPERSEDES` check on that user, and deletes only that user's subgraph in `finally`. It never truncates the database.
- **Test-only knobs live on the fakes, not in production code paths.** `InMemoryBrain.fail`, `MockLLM.fail` and `MockLLM.requests` are attributes of the test doubles; `ChatService` and the routes have no test hooks.
- **The unit tests pin the reference implementation.** `test_upsert_outcomes` asserts Appendix C against `InMemoryBrain`, and `test_neo4j_roundtrip` asserts the same outcomes against `Neo4jBrain`, so the two implementations are held to one contract (TDD §27, §4.4/§16 row).

## Design decisions and alternatives rejected

| Decision | What was rejected | Why (source) |
|---|---|---|
| Inject `InMemoryBrain` and `MockLLM` through `create_app(brain=, llm=, settings=)` | Patching `Neo4jBrain`/`AnthropicLLM` with `unittest.mock` inside the app | The two doubles are real implementations of the `SharedBrain` and `LLMProvider` protocols that also serve the `BRAIN=memory` / `LLM_PROVIDER=mock` demo modes, and `InMemoryBrain` is "the executable reference for the Cypher semantics" (TDD §27). A `MagicMock` would have to be scripted per test with the expected return values and would encode nothing about upsert or retrieval semantics; the fakes carry those semantics once and let scenarios 2, 5, 6 and 7 observe real state. Rejection reasoning inferred; the choice itself is recorded in TDD §16.2, §16.3 and CLAUDE.md ("tests inject fakes here"). |
| API-level scenario tests as the primary layer | Unit tests only, one per module | TDD §16.3 requires testing "the complete `/chat` flow"; the invariants worth protecting (no graph read on follow-up, nothing recorded after a 503, supersede not overwrite) are properties of the orchestration plus persistence plus prompt builder together (see "Why it exists"). Unit tests remain for the deterministic pieces TDD §16.1 lists. |
| A fake HTTP server via `httpx.MockTransport` for the OpenAI adapter | Recorded HTTP cassettes (VCR-style) or patching `openai.AsyncOpenAI` | Recording cassettes would need a key and a network at least once, and re-recording on SDK upgrades; the handler approach lets each test assert the request body the SDK produced (`reasoning_effort`, `response_format`, message order) and inject any status or connection error, with zero fixtures on disk. Patching the SDK client would skip the SDK's own serialization and error classes, which is exactly what `_guarded` depends on. CLAUDE.md records the technique as the standard for "any provider change"; the rejection reasoning is inferred. |
| Opt-in live Neo4j test gated on `NEO4J_TEST_URI` | Testcontainers or a Docker fixture that starts Neo4j inside pytest | The default run must "need no services" (TDD §16.2) and finish in seconds (CLAUDE.md: "~3s"); a container start costs tens of seconds and a Docker daemon. The gate keeps the default suite hermetic while CLAUDE.md gives a one-line `docker run` for the local run. Rejection reasoning inferred. |
| `asyncio_mode = "auto"` in `pyproject.toml` | Strict mode with `@pytest.mark.asyncio` on each of the async tests | CLAUDE.md: "`asyncio_mode = "auto"` (async tests need no decorator)". The suite mixes sync tests (driven through `TestClient`) and async tests (awaiting the fakes directly) in the same files; auto mode makes the `async def` itself the marker. Rejection reasoning inferred. |
| `pythonpath = ["."]` in `pyproject.toml` | Installing the project as a package or adding `conftest.py` path hacks | CLAUDE.md: "`pythonpath = ["."]` (the project is not installed as a package)". `app` is a plain directory imported by `uvicorn app.main:app` and by the tests alike. |
| Hand-written scenario functions | A data-driven harness loading TDD §18 JSON cases | TDD §18 says "A simple pass/fail or 0–2 rubric is sufficient for the assignment" and PRD §11 says "A simple rubric with expected facts per test case is sufficient". Each test function is one case with its `expected_context` and `expected_facts` inline. Inferred. |
| Comparing "with" and "without" Shared Brain across scenarios | A single test that runs each query twice with the brain on and off | README "Tests": "the 'with vs without Shared Brain' comparison is scenario 3 versus scenario 1". Scenario 10 (brain down) is a second "without" case. Inferred. |
| Test the mock extractor directly (`test_rule_extractor_matches_prd_example`) | Rely on scenario 2 alone | Gives a candidate-level diff on regex regressions, where scenario 2 only reports a wrong `memory_updates` count. Inferred. |

## Failure modes and degraded behavior

What a red test indicates, by file:

| File | A failure here means |
|---|---|
| `tests/conftest.py` (reported as fixture `ERROR`, not `FAILED`) | `create_app` or the lifespan raised: the factory signature changed, `ChatService`'s constructor changed, or the app now needs something the fixture does not provide. Every test using `client` errors at setup. A collection error naming `app/main.py` and `app/config.py` instead means `Settings.from_env()` failed at import because a numeric environment variable (`RECENT_LIMIT`, `MEMORY_LIMIT`, `MIN_CONFIDENCE`) does not parse; see "Configuration". |
| `tests/test_chat.py` | A behavioral invariant of the request path changed: classification (`context_used` tags shift), context selection or its tag order, session append rules (scenario 4 roles, scenario 9 last turn), extraction routing (scenario 2 count of 4), supersede semantics (scenario 7 statuses), or the 503/degraded contracts. Because the mock extractor is regex-based, a wording change to `INTRO` or to `extract_by_rules` also fails `test_rule_extractor_matches_prd_example`, whose dict comparison shows exactly which candidate changed. |
| `tests/test_units.py` | A single deterministic function changed: a keyword or follow-up rule in `classify`, a boundary in `_SIGN_ENDS`, a format in `_DATE_FORMATS`, a rule in `validate`, or Appendix C in `InMemoryBrain`. A failure of `test_neo4j_connection_failure_is_brain_unavailable` means `_CONNECTIVITY_ERRORS` no longer covers what the driver raises (a driver upgrade is the likely cause; CLAUDE.md pins 6.x) or something is actually listening on `127.0.0.1:1`. |
| `tests/test_openai_llm.py` | The adapter's request shape or error mapping changed, or the openai SDK changed how it serializes requests, classifies status codes, or accepts `http_client`/`max_retries`. |
| `tests/test_neo4j.py` | The Cypher diverged from `InMemoryBrain`, the Neo4j server version changed a behavior (CLAUDE.md: tested against 5.26), the driver changed its temporal types, or the database in `NEO4J_TEST_URI` is unreachable. |

Timing and bounds:

- The suite's wall time is essentially the duration of `test_neo4j_connection_failure_is_brain_unavailable`, which took between 3.3 s and 6.2 s in runs here; every other test runs in tens of milliseconds (`--durations` shows the next slowest at 0.04 s). The duration comes from the `timeout=3.0` default of `Neo4jBrain.__init__`, applied as both `connection_timeout` and `max_transaction_retry_time`; the run-to-run spread is the driver's retry schedule (inferred). README "Tests" describes the whole run as "a few seconds" and CLAUDE.md as "~3s". It carries the marker `# ponytail: fixed timeout, no circuit breaker; add one when outages are long enough to matter per request.` (`app/brain.py`). If that default grows, the suite grows with it.
- If `NEO4J_TEST_URI` is set but the database is down, `test_neo4j_roundtrip` fails after the same fail-fast window, at `ensure_schema`. The `finally` block then runs its cleanup query against the same unreachable driver, so the exception pytest reports is the raw driver error from the cleanup rather than the `BrainUnavailable` from `ensure_schema` (inferred from the code; the test does not guard the cleanup).
- The first `uv run pytest` after a fresh clone additionally pays for `uv` creating the virtual environment (38 packages installed here) before pytest starts; that time is outside pytest's own reported duration.
- Skipped is the expected state for `tests/test_neo4j.py` in the default run; a `1 skipped` in the summary is not a degradation.

## Configuration

Environment variables read by the tests:

| Variable | Read by | Default in the test | Effect |
|---|---|---|---|
| `NEO4J_TEST_URI` | `tests/test_neo4j.py` at import | unset | Unset: the module's single test is skipped with reason "set NEO4J_TEST_URI (and NEO4J_USER/NEO4J_PASSWORD) to run". Set: `Neo4jBrain` connects to it. Deliberately distinct from the runtime `NEO4J_URI` so a developer's `.env` never makes the suite hit a database by accident (inferred). |
| `NEO4J_USER` | `tests/test_neo4j.py` | `"neo4j"` | Bolt username for the live test. Same variable as runtime (`.env.example`). |
| `NEO4J_PASSWORD` | `tests/test_neo4j.py` | `"password"` | Bolt password for the live test. Matches the `NEO4J_AUTH=neo4j/password` in the CLAUDE.md `docker run` line and `docker-compose.yml`. |

Provider variables do not change any test's behavior: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `LLM_MODEL` and `LLM_EFFORT` are ignored because the `client` fixture passes an explicit `Settings(...)` and the adapter tests construct `OpenAICompatibleLLM` with literal arguments.

One indirect dependency on the runtime environment remains. `tests/conftest.py` does `from app.main import create_app`, and `app/main.py` ends with the module-level `app = create_app()` for `uvicorn`; `create_app` begins with `settings = settings or Settings.from_env()`, so `Settings.from_env()` runs once at import time, during collection, before the fixture's explicit `Settings(...)` is ever reached. `from_env` converts `RECENT_LIMIT` and `MEMORY_LIMIT` with `int()` and `MIN_CONFIDENCE` with `float()`; a malformed value such as `RECENT_LIMIT=abc` raises `ValueError: invalid literal for int() with base 10: 'abc'` from `app/config.py` and collection fails for the whole suite (verified in this worktree). The module-level app never runs its lifespan under pytest, so `BRAIN`, `NEO4J_URI` and the provider variables are only read into strings at that point and nothing is connected or constructed from them.

pytest settings in `pyproject.toml`:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
pythonpath = ["."]
```

- `asyncio_mode = "auto"`: pytest-asyncio treats every `async def` test as an asyncio test without a marker (CLAUDE.md). The suite has async tests in every file except `tests/conftest.py`.
- `testpaths = ["tests"]`: a bare `uv run pytest` collects only `tests/`, so `docs/` and `app/` are not scanned.
- `pythonpath = ["."]`: puts the repository root on `sys.path` so `from app.main import create_app` resolves; the project is not installed as a package (CLAUDE.md). Running a script outside pytest needs the same, e.g. `PYTHONPATH=. uv run python ...`.

Fixture-level configuration: `Settings(brain="memory", llm_provider="mock")` in `tests/conftest.py`, with the other fields at their `app/config.py` defaults (`recent_limit=10`, `memory_limit=8`, `min_confidence=0.6`). `test_validate_filters_and_normalizes` passes `0.6` to `validate` explicitly.

## Tests that pin this layer

The full inventory, as collected by `uv run pytest --collect-only -q` (49 items; parametrized functions listed once with their case count).

| Test | Cases | The one thing it asserts |
|---|--:|---|
| `tests/test_chat.py::test_1_new_user_succeeds_with_no_context` | 1 | A first message from an unknown user returns 200 with `context_used == []` and `degraded is False`. |
| `tests/test_chat.py::test_2_first_message_creates_durable_memory_and_profile` | 1 | The PRD intro sentence yields `memory_updates == 4`: one ACTIVE `career.goal` memory with next year's timeframe plus a Profile with ISO date and sun sign Leo. |
| `tests/test_chat.py::test_3_new_session_retrieves_memory` | 1 | A new session gets `career.goal` from the graph, no `recent_conversation`, and the fact text in the response. |
| `tests/test_chat.py::test_4_follow_up_uses_recent_context_only` | 1 | A follow-up yields exactly `["recent_conversation"]`, zero memory updates, five alternating turns to the LLM, and no memory key in the prompt. |
| `tests/test_chat.py::test_5_memory_persists_across_sessions` | 1 | Only `career.goal` is a Memory (profile facts are not), and it is retrieved in two further sessions. |
| `tests/test_chat.py::test_6_irrelevant_memory_excluded` | 1 | A career query includes `career.goal` and excludes `health.goal` from both `context_used` and the prompt. |
| `tests/test_chat.py::test_7_user_correction_supersedes` | 1 | "Actually, I prefer Hindi." supersedes English (statuses SUPERSEDED/ACTIVE), and a new session sees only Hindi. |
| `tests/test_chat.py::test_8_missing_profile_is_fine` | 1 | A user with no profile gets a response without `user_profile` or `astrology` tags. |
| `tests/test_chat.py::test_9_llm_failure_returns_503_and_mutates_nothing` | 1 | LLM failure returns 503 with "LLM" in the detail, writes nothing to the brain, and does not append the turn to the session. |
| `tests/test_chat.py::test_10_graph_failure_degrades` | 1 | Brain failure returns 200 with `degraded is True`, no memory writes, empty context, and short-term context still works on the next turn. |
| `tests/test_chat.py::test_invalid_payload_is_422` | 1 | Missing `session_id`, empty `message`, and a non-date `date_of_birth` are all 422. |
| `tests/test_chat.py::test_profile_endpoint_feeds_astrology_context` | 1 | `POST /users` derives Leo, and an astrology query then yields exactly `["user_profile", "astrology"]` with "Leo" in the prompt. |
| `tests/test_chat.py::test_rule_extractor_matches_prd_example` | 1 | `extract_by_rules` on the PRD sentence yields exactly the four expected candidates. |
| `tests/test_neo4j.py::test_neo4j_roundtrip` | 1 (skipped without `NEO4J_TEST_URI`) | Profile upsert, the three upsert outcomes, category and all-category search order, statuses, timezone-aware timestamps, and a user-scoped `SUPERSEDES` edge, all against a live Neo4j. |
| `tests/test_openai_llm.py::test_generate_sends_system_then_turns` | 1 | The chat-completions request has the right URL, bearer header, model, `reasoning_effort`, and system-then-turns message order. |
| `tests/test_openai_llm.py::test_empty_effort_omits_reasoning_effort` | 1 | An empty effort omits the `reasoning_effort` key. |
| `tests/test_openai_llm.py::test_extract_requests_json_mode_and_tolerates_fences` | 1 | Extraction requests `json_object` mode with the shape and next year in the prompt, and parses fenced JSON. |
| `tests/test_openai_llm.py::test_extract_invalid_output_is_llm_error` | 3 | Non-JSON, schema-violating JSON, and `None` content each raise `LLMError`. |
| `tests/test_openai_llm.py::test_generate_empty_content_is_llm_error` | 1 | `None` generation content raises `LLMError` matching "empty". |
| `tests/test_openai_llm.py::test_status_mapping` | 4 | 500, 503 and 429 map to `LLMError`; 400 propagates as `openai.BadRequestError`. |
| `tests/test_openai_llm.py::test_connection_error_is_llm_error` | 1 | An `httpx.ConnectError` maps to `LLMError` matching "unavailable". |
| `tests/test_openai_llm.py::test_build_llm_selects_provider` | 1 | `build_llm` returns the class for `mock`, `anthropic`, `openai`; raises on a missing `LLM_MODEL` and on an unknown provider. |
| `tests/test_units.py::test_classify` | 11 | Each sample message classifies to the expected `Category`. |
| `tests/test_units.py::test_sun_sign` | 6 | Six boundary dates map to the expected sign, including the Capricorn year wrap. |
| `tests/test_units.py::test_parse_date_formats` | 1 | Four accepted date spellings parse to 15 August 1995 and free text returns `None`. |
| `tests/test_units.py::test_validate_filters_and_normalizes` | 1 | `validate` keeps one normalized candidate and drops low-confidence, off-taxonomy and empty ones. |
| `tests/test_units.py::test_upsert_outcomes` | 1 | `InMemoryBrain.upsert_memory` returns created/unchanged/updated and leaves one ACTIVE and one SUPERSEDED memory. |
| `tests/test_units.py::test_upsert_profile_derives_sun_sign` | 1 | `InMemoryBrain.upsert_profile` normalizes the date, derives the sign, and merges partial updates. |
| `tests/test_units.py::test_neo4j_connection_failure_is_brain_unavailable` | 1 | A refused Bolt connection surfaces as `BrainUnavailable` once the driver's 3 s fail-fast settings give up. |

Totals: 13 + 1 + 13 + 22 = 49 collected; 48 passed and 1 skipped in this worktree.

## Known limits and future work

- **`AnthropicLLM` has no automated test.** CLAUDE.md: "`AnthropicLLM` has no automated test (needs a key): keep `messages.create(..., output_config={"effort": ...})` for generation and `messages.parse(..., output_format=_Extraction)` for extraction." A fake-server test is possible with the same technique as `tests/test_openai_llm.py`, with one difference: the anthropic SDK pinned in `uv.lock` (1.7.0) is built on the `httpx2` package, not `httpx`, and rejects an `httpx.AsyncClient` with a `TypeError` naming `httpx2`. The recipe is therefore `AnthropicLLM("m", http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)), api_key="k", max_retries=0)` (all three keyword arguments pass through `**client_kwargs` to `anthropic.AsyncAnthropic`), with a handler that answers `POST https://api.anthropic.com/v1/messages` with a Messages-API body (`"type": "message"`, `"content": [{"type": "text", "text": ...}]`, `"stop_reason"`, `"usage"`). Checked ad hoc while writing this document, not committed: with such a fake, `generate` returns the text and the request body carries `model`, `max_tokens`, `system`, `messages` and `output_config == {"effort": "medium"}`; `extract_memories` sends the structured-output configuration inside `output_config` (there is no top-level `output_format` key on the wire), and a text block whose text is the JSON of `_Extraction` parses into `MemoryCandidate`s. Things worth pinning, mirroring the OpenAI file: `x-api-key` header and `output_config.effort`; `stop_reason == "refusal"` raising `LLMError("model declined the request")`; empty content raising `LLMError("empty response")`; 5xx, 429 and connection errors mapping to `LLMError` via `_guarded`; 4xx propagating.
- **The Docker build is not tested.** Nothing exercises `Dockerfile` or `docker-compose.yml`. README "Run it" documents `docker compose up --build` as the run path, and README "The PRD conversation, end to end" records a manual run of the PRD conversation "from the mock LLM against a real Neo4j" with the observed `context_used` and `memory_updates` values and the resulting graph. That transcript is the recorded manual verification; whether it was produced through Compose or a local `uv run` is not recorded.
- **PRD §11 metrics are not computed as numbers.** Memory accuracy, context relevance, personalization, conversation consistency, irrelevant-context rate and memory persistence are each covered by pass/fail assertions in specific scenarios (2, 6, 3 and 7, 4, 6, 5 respectively) rather than by a harness that scores a case set, and there is no separate baseline run "without Shared Brain retrieval" as TDD §18 step 1 describes; the comparison is between scenarios (README "Tests"). Both specs state the rubric form is sufficient for the assignment.
- **Coverage gaps that are true of the current suite.** No test sends more than ten messages to a session (the `deque(maxlen=...)` cap in `app/session.py`) or creates more than eight memories in one category (the `LIMIT` in `SEARCH_MEMORIES` and the `[:limit]` in `InMemoryBrain.search_memories`). No test covers the TDD §13.3 third bullet, generation succeeding and extraction then failing, because `MockLLM.fail` gates both methods together; nor the `BrainUnavailable` raised during the memory write after a successful read (the second `except` in `ChatService.chat`). `extract_by_rules`'s `_LIKE` (interests) and explicit-year `_YEAR` paths, `Category.INTERESTS`, `RELATIONSHIPS` and `FINANCE` classification, `time_of_birth` and `preferred_language` profile fields, and the `"%B %d %Y"` and `"%d/%m/%Y"` date formats are not exercised. `select_context` has no direct unit test.
- **`ponytail:` markers.** There are none in `tests/`. The four in `app/` and their relevance to this layer:
  - `app/brain.py`, `Neo4jBrain.__init__`: "fixed timeout, no circuit breaker; add one when outages are long enough to matter per request." This 3 s timeout is what sets the duration of `test_neo4j_connection_failure_is_brain_unavailable`, and with it the whole default suite; a circuit breaker would need its own tests for open/half-open behavior.
  - `app/context.py`, `_KEYWORDS`: "keyword classifier, first match wins; swap for an LLM/embedding classifier when evals show misroutes." The parametrized `test_classify` cases are the only classification eval today; the swap would need a larger labeled set and would make the `context_used` assertions non-deterministic unless the classifier stayed injectable.
  - `app/session.py`, `SessionStore`: "process-local dict; swap for Redis when running more than one replica." Scenarios 4, 9 and 10 observe the store only through `context_used`; a Redis store would need the same tests plus a fake or a gated live test like `tests/test_neo4j.py`.
  - `app/astrology.py`: "tropical sun sign only; a real engine (sidereal rashi, moon sign, nakshatra) replaces this module." `test_sun_sign` pins the tropical boundaries and would be replaced along with the module.
