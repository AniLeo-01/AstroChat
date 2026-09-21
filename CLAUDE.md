# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

MyNaksh personalized astrology chat service: Python 3.12, FastAPI, Neo4j Shared Brain, pluggable LLM selected by `LLM_PROVIDER`: `anthropic` (default, `claude-opus-5`), `openai` (any OpenAI-compatible server via `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `LLM_MODEL`), or `mock` (deterministic, used by tests). The product is the memory pipeline, not astrology; astrology is a sun-sign stub in `app/astrology.py`.

Spec lives in `docs/AstroChat_PRD.md` (requirements) and `docs/AstroChat_TDD.md` (design; §27 records the deliberate deviations from v1). Keep the TDD in sync when behavior changes.

Every `POST /chat` follows: Chat → Context Selection → Shared Brain → LLM → Response → Memory Update.

Layer-by-layer documentation lives in `docs/layers/` (start at `docs/layers/README.md`).

## Commands

```bash
uv sync                                                  # install (creates .venv)
uv run pytest                                            # full suite, no services needed, ~3s
uv run pytest tests/test_chat.py -k correction           # one scenario
NEO4J_TEST_URI=bolt://localhost:7687 NEO4J_PASSWORD=password uv run pytest   # adds the live-Neo4j round trip
BRAIN=memory LLM_PROVIDER=mock uv run uvicorn app.main:app --reload          # run with no Neo4j and no API key
uv run --env-file .env uvicorn app.main:app --reload     # run against .env (see .env.example)
docker compose up --build                                # Neo4j + app
docker run -d -p 7687:7687 -e NEO4J_AUTH=neo4j/password neo4j:5   # throwaway Neo4j for local runs
```

pytest config is in `pyproject.toml`: `asyncio_mode = "auto"` (async tests need no decorator) and `pythonpath = ["."]` (the project is not installed as a package). No linter is configured.

## Layout

One module per concern, all in `app/`, dependency order: `config`, `models` → `session`, `astrology`, `brain` → `context` → `llm` → `memory` → `chat` → `main`.

- `brain.py`: `SharedBrain` protocol, `InMemoryBrain` (tests, `BRAIN=memory`) and `Neo4jBrain` with all Cypher. The in-memory class is the readable spec for the Cypher; change both together and run `test_neo4j.py`.
- `context.py`: `classify()` keyword classifier; `select_context()` builds the bounded `LLMRequest` and the `context_used` list.
- `llm.py`: the only module that imports provider SDKs (`anthropic`, `openai`). `build_llm(settings)` selects the provider; `_guarded()` maps both SDKs' outage errors to `LLMError`. `MockLLM.extract_memories` is a regex extractor that covers the PRD example sentences.
- `memory.py`: validate candidates, route profile keys to the Profile node, upsert the rest.
- `chat.py`: `ChatService.chat()` orchestrates. It sees only `BrainUnavailable` and `LLMError`, never driver or SDK exceptions.
- `main.py`: `create_app(brain=, llm=, settings=)` factory; tests inject fakes here. Routes only validate and delegate.

## Invariants the tests enforce

- Logical memory identity is `(user_id, category, key)`; at most one `ACTIVE` memory per key. Corrections supersede (`old.status = SUPERSEDED`, `(new)-[:SUPERSEDES]->(old)`), never overwrite in place.
- Only the user's message is extracted from; assistant output never becomes memory. Extraction is skipped on `follow_up` turns.
- Profile facts stated in chat (`profile.name`, `profile.date_of_birth`, `profile.time_of_birth`, `profile.birth_place`) update the Profile node and recompute `sun_sign`. `language.preferred` stays a Memory so the supersede flow is demonstrable.
- Context budgets: 10 recent messages (deque cap), 8 memories (query `LIMIT`), profile fields per query category (table at the top of `context.py`). `follow_up` gets recent conversation only and no graph read.
- `context_used` is exactly `recent_conversation` / `user_profile` / `astrology` tags, then memory keys in retrieval order. Tests assert whole lists.
- LLM down → `503`, nothing recorded (no session append, no memory write). Neo4j down → `200` with `degraded: true`, memory write skipped, driver fails in ~3s.
- Memory categories share the query taxonomy in `models.Category`; `general` is a valid memory category, `follow_up` is not.

## Gotchas

- neo4j driver is 6.x; tested against Neo4j 5.26. Timestamps come back as `neo4j.time.DateTime`; call `.to_native()`.
- `MemoryCandidate` is Pydantic because it is the structured-output schema for the real extractor; everything else is a dataclass. Keep numeric constraints out of its fields; `memory.validate` enforces them.
- `OpenAICompatibleLLM` is tested against a fake server via `httpx.MockTransport` passed as `http_client` (see `tests/test_openai_llm.py`); use the same trick for any provider change. `AnthropicLLM` has no automated test (needs a key): keep `messages.create(..., output_config={"effort": ...})` for generation and `messages.parse(..., output_format=_Extraction)` for extraction. In both providers 4xx errors deliberately propagate (our bug, not an outage).
- `LLM_MODEL` is required for `openai` and `build_llm` raises at startup if it is missing; anthropic falls back to `claude-opus-5`.
