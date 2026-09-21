# Product Requirements Document (PRD)

## Personalized Astrology Chat — Shared Brain & Memory
**Product:** MyNaksh Personalized Astrology Chat
**Document:** Product Requirements Document
**Status:** Assignment-ready MVP
**Primary audience:** SDE2 / 3–5 years
**Implementation target:** Python + FastAPI + Neo4j + pluggable LLM provider

---

## 1. Product Overview

MyNaksh is building a personalized astrology conversational experience in which the assistant can use both the current conversation and useful long-term information about a user. The core product capability is a persistent **Shared Brain** that stores selected user information and retrieves only the context relevant to a new question.

The assignment explicitly prioritizes the end-to-end flow:

> **Chat → Context Selection → Shared Brain → LLM → Response → Memory Update**

The product is not expected to implement a complete astrology engine. Astrology attributes may be simplified or stubbed; the primary objective is personalized conversational context and memory.

## 2. Problem Statement

A generic chatbot treats each message too independently. For MyNaksh, responses should become more personalized over time by remembering useful information such as goals, preferences, interests, life areas, important memories, and astrology attributes.

The system must solve two distinct context problems:

1. **Short-term context:** understand follow-up questions using recent messages in the active conversation.
2. **Long-term memory:** preserve useful information across sessions without storing every message as permanent memory.

## 3. Goals

### 3.1 Primary goals

- Accept a user message together with `user_id` and `session_id`.
- Maintain recent conversation context for follow-up questions.
- Maintain a persistent graph-based Shared Brain for useful user information.
- Select only relevant context before sending a request to the LLM.
- Generate a personalized response through a provider-independent LLM interface.
- Extract and persist useful memories after a response.
- Handle missing context and component failures gracefully.
- Provide 5–10 automated scenarios covering the core behavior.

### 3.2 Secondary goals

- Keep the design production-oriented while remaining feasible within the assignment's three-hour limit.
- Make the LLM provider replaceable without changing the rest of the application.
- Represent memories in a way that supports future conflict resolution, confidence scoring, expiration/decay, and richer graph traversal.

## 4. Non-Goals

The following are explicitly outside the MVP scope:

- A complete astrology calculation engine.
- Complex astrological prediction logic.
- A full UI/frontend application.
- Advanced graph algorithms.
- Sophisticated evaluation infrastructure.
- Guaranteed semantic correctness of all extracted memories.
- Full-scale multi-region production infrastructure.

Optional bonus features may be added only after the core flow works.

## 5. Target User Experience

### 5.1 First conversation

A user provides personal information and a future goal:

> "My name is Rahul. I was born on 15 August 1995 in Delhi. I'm planning to switch jobs next year."

The system should identify persistent information, such as a career goal with target year 2027, and store it in the Shared Brain.

### 5.2 Follow-up within the same session

The user asks:

> "What should I focus on for my career?"

The assistant should use relevant profile and career context plus the recent conversation to personalize the response.

### 5.3 Conversational reference

The user asks:

> "Why do you say that?"

The assistant should primarily rely on recent short-term conversation context rather than retrieving the entire user graph.

### 5.4 New session

The user starts a new session and asks:

> "What do you remember about my career goals?"

The assistant should retrieve relevant persistent memories from the Shared Brain.

## 6. Core Product Requirements

### FR-1: Chat API

Expose a `POST /chat` endpoint.

**Input**

```json
{
  "user_id": "user-123",
  "session_id": "session-456",
  "message": "What should I focus on in my career?"
}
```

**Output**

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

The response may include additional diagnostic metadata where useful.

### FR-2: User Profile

Support at minimum:

- Name
- Date of birth
- Time of birth
- Birth place
- Preferred language
- Zodiac / Sun Sign

Profile information may be provided during user creation or by a separate endpoint. Astrology calculations may be simplified or stubbed.

### FR-3: Shared Brain

Persist useful long-term information such as:

- Profile attributes
- Goals
- Preferences
- Interests
- Important memories
- Life areas
- Astrology attributes

The Shared Brain must be persistent and graph-oriented. Neo4j is the preferred implementation.

### FR-4: Short-Term Conversation Context

Maintain enough recent conversation history to resolve follow-ups and conversational references.

MVP recommendation:

- Keep the latest 8–12 messages per session in the context window.
- Do not send the complete lifetime history to the LLM.

### FR-5: Context Selection

Before generating a response, the system must:

1. Understand/classify the user's query.
2. Retrieve candidate profile and memory information.
3. Select only relevant context.
4. Build a bounded LLM context.

Possible implementation approaches are rules, LLM selection, a classifier, or a hybrid approach. The MVP will use a lightweight hybrid strategy: deterministic signals for high-confidence categories and structured LLM extraction only where beneficial.

### FR-6: LLM Abstraction

Expose an interface similar to:

```python
class LLMProvider(Protocol):
    async def generate(self, request: LLMRequest) -> LLMResponse: ...
    async def extract_memories(self, request: MemoryExtractionRequest) -> list[MemoryCandidate]: ...
```

The application must not depend directly on a specific provider SDK outside the provider adapter.

### FR-7: Memory Update

After a response, evaluate whether the user's latest message contains durable information.

The system should be able to:

- Create a new memory.
- Update an existing memory.
- Ignore transient or low-value information.
- Associate memory with a category and entity.
- Preserve enough provenance to understand where the memory came from.

Examples of likely durable memories:

- Career goals
- Language preference
- Long-term interests
- Stable profile facts
- Significant life goals

Examples of information normally not stored as long-term memory:

- Greetings
- Temporary conversational filler
- One-off questions
- Assistant-generated statements
- Ephemeral details with no likely future utility

### FR-8: Error Handling

Handle at minimum:

- Invalid request payload
- Missing profile data
- Empty memory
- No relevant context
- LLM failure
- Neo4j failure

The API should return useful HTTP errors for client mistakes and safe fallback behavior for downstream failures.

## 7. Memory Model Requirements

### 7.1 Memory categories

Each durable memory should have:

- `type`
- `value`
- `category`
- `confidence`
- `source_message_id`
- `created_at`
- `updated_at`
- Optional `valid_from` / `valid_until`

### 7.2 Memory quality rules

A memory candidate should be persisted only when it has sufficient future usefulness and confidence. The initial score can be heuristic.

Suggested decision rule:

- **High confidence + durable:** store automatically.
- **Medium confidence:** store if the statement is explicit and actionable.
- **Low confidence / speculative:** do not store.

### 7.3 Memory correction

When a user explicitly corrects a previous fact, the latest explicit user statement should supersede the stale value while retaining provenance.

Example:

> Previous: preferred language = English
> New user statement: "Actually, I prefer Hindi."

The active preference becomes Hindi. The implementation may either update the existing memory node or mark the older memory inactive.

## 8. Context Selection Requirements

The context builder must keep the LLM prompt bounded and relevant.

Candidate context sources:

| Source | Purpose | Default priority |
|---|---|---:|
| Current user message | Immediate intent | Highest |
| Recent session messages | Follow-up understanding | High |
| Relevant long-term memories | Personalization | High |
| User profile | Stable personal context | Medium/High |
| Astrology attributes | Astrology-specific personalization | Medium |
| Irrelevant memories | No value | Exclude |

For a follow-up such as "Why do you say that?", recent session context should dominate. For a new-session question about career goals, long-term memories should dominate.

## 9. API Requirements

### POST /chat

Creates a response and triggers memory update.

### Recommended optional endpoint: POST /users

Creates or updates profile information.

### Recommended optional endpoint: GET /users/{user_id}/memories

Provides a debugging/inspection surface for the Shared Brain during development.

These optional endpoints are not required by the assignment but improve observability and demoability.

## 10. Acceptance Criteria

The MVP is complete when:

1. A valid `/chat` request returns a response with `user_id`, `session_id`, `response`, and `context_used`.
2. A first-session message can create a durable memory.
3. A follow-up question can be answered using recent context.
4. A new session can retrieve a memory from the previous session.
5. Irrelevant memories are excluded from the LLM context.
6. A corrected user fact is reflected in future retrievals.
7. Missing profile or empty memory does not crash the request.
8. LLM failure and graph failure have safe fallback behavior.
9. Automated tests cover 5–10 required scenarios.
10. README/TDD explains the architecture, schema, memory strategy, context-selection strategy, trade-offs, and production considerations.

## 11. Evaluation Plan

The product should be evaluated against a fixed scenario set that compares the assistant with and without Shared Brain context.

### Metrics

- **Memory accuracy:** fraction of stored memories that correctly represent the user's explicit information.
- **Context relevance:** fraction of injected context items that are useful for the query.
- **Personalization:** whether the answer correctly uses relevant personal facts.
- **Conversation consistency:** ability to resolve follow-ups consistently.
- **Irrelevant context rate:** amount of unrelated memory injected into a prompt.
- **Memory persistence:** whether a memory remains retrievable in a new session.

A simple rubric with expected facts per test case is sufficient for the assignment.

## 12. MVP Delivery Scope for the 3-Hour Constraint

### Must have

- FastAPI `/chat`
- User/profile handling
- Neo4j Shared Brain
- Recent-message context
- Context selection
- LLM abstraction + one provider implementation
- Memory extraction/update
- Automated tests
- README with architecture and design decisions

### Nice to have

- Confidence scores
- Memory correction workflow
- Debug endpoint for memory inspection
- Prompt/token budgets
- Conversation summarization

### Defer unless core flow is finished

- Memory decay
- Advanced graph traversal
- Model routing/fallback across providers
- Multilingual response support
- Sophisticated ranking/retrieval models

## 13. Risks and Mitigations

| Risk | Mitigation |
|---|---|
| LLM invents memories | Structured extraction schema + confidence threshold + user-only provenance |
| Too much context sent to LLM | Hard limits on recent messages and retrieved memories |
| Irrelevant memories pollute responses | Query-category filtering + graph predicates + post-retrieval relevance filter |
| User corrections leave stale values | Active/inactive memory status or latest-value update semantics |
| Graph unavailable | Continue with recent context/profile when possible; expose degraded mode |
| LLM unavailable | Return a deterministic fallback message and do not mutate memory from failed generations |
| Provider lock-in | Provider interface + adapter pattern |

## 14. Future Direction

The architecture should leave room for:

- Memory importance and confidence scoring
- Memory conflict resolution
- Memory expiration/decay
- Conversation summarization
- Model fallback/routing
- Token/context optimization
- Hindi and multilingual responses
- Advanced graph traversal

---

## Appendix A — Product-Level Conversation Flow

```text
Client
  |
  v
POST /chat
  |
  v
Validate request
  |
  +--> Load recent session context
  |
  +--> Classify/understand query
  |
  +--> Retrieve candidate Shared Brain facts
  |
  +--> Select relevant context
  |
  v
Build bounded LLM prompt
  |
  v
Generate personalized response
  |
  +--> Persist recent message(s)
  |
  +--> Extract durable memory candidates
  |
  +--> Validate/update Shared Brain
  |
  v
Return response + context_used
```

## Appendix B — Assignment Alignment

This PRD is derived from the supplied MyNaksh ML Machine Coding Assignment, including the required Chat API, User Profile, Shared Brain, short-term vs long-term memory, Context Selection, modular LLM layer, memory update flow, testing/evaluation, error handling, and the stated three-hour prioritization. See the source assignment for the authoritative wording. 
