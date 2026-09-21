# AstroChat UI

A minimal, single-page web interface for AstroChat. Served by the FastAPI app at the root URL (`/`); no separate build step, server, or dependencies.

## Run it

```bash
# No Neo4j, no API key needed (in-memory brain + mock LLM):
BRAIN=memory LLM_PROVIDER=mock uv run uvicorn app.main:app --reload

# With environment file:
uv run --env-file .env uvicorn app.main:app --reload

# Full stack (Neo4j + app):
docker compose up --build
```

The app container loads environment variables from `.env` via `env_file: .env` in `docker-compose.yml`. The `.env` file from the project root is used; `uv run --env-file .env` achieves the same locally.

Neo4j connection settings are *configurable with a bundled default*. If `NEO4J_URI` / `NEO4J_USER` / `NEO4J_PASSWORD` are absent from `.env`, a plain `docker compose up` uses the bundled Compose `neo4j` service at `bolt://neo4j:7687` (and local runs use `bolt://localhost:7687`). Set them in `.env` to point the container at an external database (e.g. Neo4j Aura). See `.env` for the commented example.

Open http://localhost:8000 in a browser. Swagger docs at http://localhost:8000/docs.

## What the UI does

On first load the UI asks who you are — a **lightweight onboarding**, not a password login (full auth is deliberately deferred). Afterwards there are three tabs:

| Element | Purpose |
|---|---|
| **Onboarding** | Name (required), email (optional), preferred language (optional). The server derives a stable `user_id` — the normalized email if provided, otherwise a slug of the name (e.g. `Rahul Sharma` → `rahul-sharma`) — and creates the User + Profile nodes. |
| **Chat** | Send messages against the logged-in identity, receive astrology responses, see which context sources were used (`context_used` badges), how many memories were written (`memory` badges), and whether the response degraded (`degraded` badge). |
| **Profile** | Shows the identity (name + email/ID, read-only) and profile fields (date of birth, time of birth, birth place, preferred language). Pre-fills from `GET /users/{user_id}` on open. On save the server recomputes the sun sign. |
| **Stored data** | One unified panel for the logged-in user: the Profile card (name, language, birth details, sun sign) plus every stored Memory (active and superseded). A friendly empty state appears when nothing is stored yet — e.g. only profile facts exist, which are Profile nodes, not Memories. |

## How it works

The UI is a single file — `app/static/index.html` — containing inline HTML, CSS, and JavaScript. FastAPI serves it via `StaticFiles` mounted at `/` in `app/main.py`:

```python
app.mount("/", StaticFiles(directory=str(Path(__file__).resolve().parent / "static"), html=True), name="static")
```

Identity and session state:

- `astrochat.identity` (`{user_id, name, email}`) is kept in `localStorage`; "Log out" clears it and returns to onboarding.
- `astrochat.session` is a per-tab `session_id` (`crypto.randomUUID()`) kept in `sessionStorage`, so a reload keeps the conversation context while each tab stays independent.

The JavaScript calls these API endpoints via `fetch()`:

| UI action | API call |
|---|---|
| Log in / create identity | `POST /onboard` with `name`, `email`, `preferred_language` |
| Send a chat message | `POST /chat` with `user_id`, `session_id`, `message` |
| Load profile (prefill / stored view) | `GET /users/{user_id}` |
| Save profile | `POST /users` with `user_id` and profile fields |
| Load memories | `GET /users/{user_id}/memories` |

Error handling:

- **422** — displayed inline in the relevant form (invalid payload, missing profile fields, malformed email).
- **404** — treated as "nothing stored yet": profile tab stays blank, stored view shows the empty state.
- **503** — displayed as an error message in the chat (LLM unavailable) or via the empty state (brain unavailable).
- **Network errors** — displayed as an error message.
- **`degraded: true`** — shown as a red `degraded` badge on the assistant's message, meaning the Shared Brain was unreachable and the response came from recent conversation only.

## Design

- **No frameworks.** One HTML file, no npm, no bundler, no external CSS or JS libraries. Zero runtime network requests to CDNs.
- **Dark theme.** Colors defined as CSS custom properties in `:root` — easy to rebrand by editing a few values.
- **Responsive.** Chat bubbles, profile card, and onboarding card adapt to narrow viewports.
- **Self-contained.** Session state is client-side; the server is stateless with respect to the UI. Values rendered from the server are HTML-escaped.

## File reference

| File | Description |
|---|---|
| `app/static/index.html` | The entire UI (HTML + CSS + JS) |
| `app/main.py` (line ~123) | `StaticFiles` mount serving the UI at `/` |
| `app/main.py` (`POST /onboard`) | Lightweight login; derives `user_id` from email or name-slug |

## API contract

The UI relies on the endpoints documented in [API layer](layers/01-api-layer.md). Key response fields the UI displays:

| Field | Source | Displayed as |
|---|---|---|
| `user_id`, `name`, `email` | `POST /onboard` | Stored identity, header greeting |
| `response` | `POST /chat` | Assistant chat bubble |
| `context_used` | `POST /chat` | `ctx: ...` badge(s) |
| `memory_updates` | `POST /chat` | `+N memory` badge (only when > 0) |
| `degraded` | `POST /chat` | Red `degraded` badge |
| `sun_sign` | `POST /users`, `GET /users/{user_id}` | Profile form confirmation, stored-data card |
| `name`, `date_of_birth`, `time_of_birth`, `birth_place`, `preferred_language`, `sun_sign` | `GET /users/{user_id}` | Profile form prefill, stored-data card |
| `key`, `value`, `category`, `type`, `confidence`, `status` | `GET /users/{user_id}/memories` | Memory cards |