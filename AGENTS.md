# AstroChat — AGENTS.md

Compact guidance for future OpenCode sessions. Every line answers: "Would an agent likely miss this without help?"

## Developer commands (from CLAUDE.md)

- `uv sync` — install dependencies (creates .venv)
- `uv run pytest` — full suite, no services needed, ~3s
- `uv run pytest tests/test_chat.py -k correction` — run one scenario
- `NEO4J_TEST_URI=bolt://localhost:7687 NEO4J_PASSWORD=password uv run pytest` — adds live-Neo4j round trip
- `BRAIN=memory LLM_PROVIDER=mock uv run uvicorn app.main:app --reload` — run with no Neo4j and no API key
- `uv run --env-file .env uvicorn app.main:app --reload` — run against .env (see .env.example)
- `docker compose up --build` — Neo4j + app
- `docker run -d -p 7687:7687 -e NEO4J_AUTH=neo4j/password neo4j:5` — throwaway Neo4j for local runs

## Project structure (from CLAUDE.md, README.md)

One module per concern, all in `app/`, dependency order:

```
config → models → session → astrology → brain → context → llm → memory → chat → main
```

- `brain.py`: SharedBrain protocol, InMemoryBrain (tests, BRAIN=memory) and Neo4jBrain with all Cypher. Change both together; in-memory class is the readable spec for the Cypher.
- `context.py`: select_context() builds bounded LLMRequest and context_used list, and classify() is the deterministic rule reference used by MockLLM only. Production routing is the LLM: ChatService calls llm.classify() (structured output, see llm.CLASSIFY_SYSTEM); the rules are NOT used in the live path.
- `llm.py`: the only module that imports provider SDKs (anthropic, openai). build_llm(settings) selects provider; _guarded() maps both SDKs' outage errors to LLMError.
- `memory.py`: validate candidates, route profile keys to Profile node, upsert the rest.
- `chat.py`: ChatService.chat() orchestrates. Sees only BrainUnavailable and LLMError, never driver or SDK exceptions.
- `main.py`: create_app(brain=, llm=, settings=) factory; tests inject fakes. Routes validate and delegate.

## Critical invariants (tests enforce)

- Memory identity is `(user_id, category, key)`; at most one ACTIVE memory per key. Corrections supersede (old.status=SUPERSEDED, (new)-[:SUPERSEDES]->(old)), never overwrite in place.
- Community Edition = no multiple databases or partial (filtered) unique constraints. Each user is a graph partition: Memory/Profile nodes carry `user_id` and every Cypher query selects on it. DB-enforced: `Memory.active_key` unique — a derived per-memory property `"user_id|category|key"` set only while ACTIVE (null otherwise, and Neo4j does not index nulls), so at most one ACTIVE memory per user+key is enforced while any number of SUPERSEDED versions may coexist. Reader beware: an earlier design used a composite `Memory(user_id, category, key, status)` constraint; it rejected a SECOND SUPERSEDED node for a key (live 500 on double correction), so it was migrated away — `ensure_schema` now DROPs it. `Profile.user_id` remains unique. `ensure_schema` runs BACKFILL (fills `user_id` + `active_key` onto legacy nodes) before creating constraints.
- Only the user's message is extracted from; assistant output never becomes memory. Extraction skipped on follow_up turns.
- Profile facts stated in chat (profile.name, profile.date_of_birth, profile.time_of_birth, profile.birth_place) update Profile node and recompute sun_sign. language.preferred stays a Memory so the supersede flow is demonstrable.
- Context budgets: 10 recent messages (deque cap), 8 memories (query LIMIT), profile fields per query category (table at top of context.py). follow_up gets recent conversation only and no graph read.
- context_used is exactly recent_conversation / user_profile / astrology tags, then memory keys in retrieval order. Tests assert whole lists.
- LLM down → 503, nothing recorded (no session append, no memory write). Neo4j down → 200 with degraded: true, memory write skipped, driver fails in ~3s.
- Memory categories share the query taxonomy in models.Category; general is a valid memory category, follow_up is not.

## Gotchas

- neo4j driver is 6.x; tested against Neo4j 5.26. Timestamps come back as neo4j.time.DateTime; call `.to_native()`.
- MemoryCandidate is Pydantic because it is the structured-output schema for the real extractor; everything else is a dataclass. Keep numeric constraints out of its fields; memory.validate enforces them.
- OpenAICompatibleLLM is tested against a fake server via httpx.MockTransport (see tests/test_openai_llm.py); use same trick for any provider change. AnthropicLLM has no automated test (needs a key).
- LLM_MODEL is required for openai and build_llm raises at startup if it is missing; anthropic falls back to claude-opus-5.
- BRAIN env var: neo4j or memory (in-process, for demos).
- Identity: no auth. `POST /onboard` derives a stable user_id — the normalized email if provided, else a slug of the name (`Rahul Sharma` → `rahul-sharma`) — and creates User+Profile. Re-onboarding with the same email/name-slug returns the same user (idempotent). `User.email` is set at onboarding and has a unique constraint; full auth is deliberately deferred (TDD §20).
- `GET /users/{user_id}` returns ProfileOut, 404 when the user was never seen. The UI's "Stored data" tab loads it together with `GET /users/{user_id}/memories`; profile facts are Profile nodes, so a chat that only wrote profile facts shows no memories.
- MockLLM has `fail=True` to simulate outage; used by tests exclusively.

## Testing

- Default test run uses InMemoryBrain + MockLLM; needs no services.
- `tests/test_neo4j.py` repeats brain assertions against real Neo4j; skipped unless `NEO4J_TEST_URI` is set.
- test_units.py covers classifier, sun-sign boundaries, date parsing, candidate validation, upsert outcomes and driver-error translation.
- test_openai_llm.py drives the OpenAI-compatible adapter against an in-process fake server.
- To run a single scenario: `uv run pytest tests/test_chat.py -k correction`
- LLM failure → 503, nothing recorded (no session append, no memory write).
- Graph failure → response may degrade to short-term/profile context.

## Environment / configuration

- `.env` and `.env.example` for all settings.
- `BRAIN`: neo4j | memory
- `LLM_PROVIDER`: anthropic | openai | mock
- `LLM_MODEL`: required for openai; anthropic falls back to claude-opus-5
- `NEO4J_URI` / `NEO4J_USER` / `NEO4J_PASSWORD`: bolt://localhost:7687 / neo4j / password
- `LLM_EFFORT`: low | medium | high; empty omits the parameter for models that reject it
- `ANTHROPIC_API_KEY`: Anthropic provider
- `OPENAI_BASE_URL` / `OPENAI_API_KEY`: OpenAI-compatible provider; empty URL means api.openai.com, key may be empty for local servers
- `RECENT_LIMIT` / `MEMORY_LIMIT`: 10 / 8 — context budgets
- `MIN_CONFIDENCE`: 0.6 — extraction threshold

## Failure modes (from TDD §13, PRD §8)

| Failure | Behavior |
|---|---|
| Invalid payload | 422 from FastAPI validation |
| LLM unavailable | 503; turn not appended to history; no memory writes |
| Neo4j unavailable | 200 with `degraded: true`; answered from recent turns; memory write skipped |
| Extraction fails after good response | Response returned; write skipped and logged |
| Missing profile / empty memory | Normal 200; those tags simply do not appear in context_used |
| Irrelevant memory | LLM gets recent turns + profile only |

## Memory strategy (from TDD §7, PRD §7)

- Logical key `(user_id, category, key)` identifies a fact; at most one ACTIVE memory per key.
- Profile facts route to Profile node (name, date_of_birth, time_of_birth, birth_place). sun_sign recomputed when DOB changes.
- language.preferred stays a Memory so the supersede flow is demonstrable.
- What not to remember: greetings, filler, temporary conversational details, assistant-generated assertions, off-taxonomy categories/types, anything below MIN_CONFIDENCE (0.6).
- Extraction: structured LLM output + deterministic validation. If extraction fails, user still gets response; write is skipped and logged.
