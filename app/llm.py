"""LLM provider boundary. Only this module imports provider SDKs."""

import logging
import re
from datetime import date
from typing import Protocol

import anthropic
import openai
from pydantic import BaseModel, ValidationError

from .astrology import parse_date
from .config import Settings
from .context import classify as rules_classify
from .models import MEMORY_CATEGORIES, Category, LLMRequest, MemoryCandidate

log = logging.getLogger(__name__)


class LLMError(Exception):
    """The provider failed or declined; nothing was generated."""


class LLMProvider(Protocol):
    async def generate(self, request: LLMRequest) -> str: ...
    async def classify(self, message: str) -> Category: ...
    async def extract_memories(self, message: str, today: date) -> list[MemoryCandidate]: ...


class _Extraction(BaseModel):
    memories: list[MemoryCandidate]


class _Classification(BaseModel):
    """The query router's structured output: one category, nothing else."""

    category: Category


EXTRACT_PROMPT = """Extract durable facts about the user from their message. Today is {today}.
Store only explicit, stable, future-useful information the user states about themselves: goals, preferences,
interests, life plans, and stable profile facts (name, date/time/place of birth).
Ignore greetings, filler, questions, hypotheticals, speculation, and anything about the assistant.
For each memory:
- key: dotted <category>.<slug>, e.g. career.goal, language.preferred, interests.cricket, profile.name,
  profile.date_of_birth, profile.time_of_birth, profile.birth_place
- category: one of {categories}
- type: fact | goal | preference | interest
- value: short normalized phrase; dates as YYYY-MM-DD
- target_timeframe: the resolved year or period when the user gives one ("next year" is {next_year}), else null
- confidence: 0 to 1, how certain you are that this is explicit and durable
Return an empty list when nothing qualifies."""


def _extract_system(today: date) -> str:
    return EXTRACT_PROMPT.format(today=today.isoformat(), next_year=today.year + 1,
                                 categories=", ".join(sorted(MEMORY_CATEGORIES)))


CLASSIFY_SYSTEM = """You are the query router for a personalized astrology chatbot. Read the user's latest message
and pick exactly one category from: profile, career, relationships, finance, health, interests, language,
astrology, general, follow_up.

Rules:
- follow_up: the user is continuing the previous turn — starts with "why", "how come", "tell me more",
  "what do you mean", "and then", "what about that", or refers back with "that"/"this"/"it" in a short message.
- profile: the user states or asks about their own facts (name, birthdate, birth time, birth place, age) or
  asks what you remember about them.
- language: the user talks about which language to use or states a language preference (Hindi, English, ...).
- astrology: horoscope, zodiac, sun/moon/rising sign, rashi, kundli, nakshatra, planets, birth chart,
  predictions, auspicious times, astrological advice. If astrology is mentioned alongside a life area, still choose astrology.
- career: jobs, work, promotion, business, interviews, bosses, career or study direction.
- relationships: love, marriage, partner, family, friends, dating, breakups.
- finance: money, income, savings, investments, wealth, loans, debt, property, stocks.
- health: body, energy, sleep, stress, diet, fitness, illness, well-being.
- interests: hobbies, likes, pastimes (music, sports, travel, reading, cooking, ...).
- general: everything else — greetings, small talk, questions about the assistant, anything not clearly above.
Return a single category."""


async def _guarded(call, sdk):
    """Await an SDK call, mapping outages to LLMError. Both SDKs expose the same Stainless exception names."""
    try:
        return await call
    except (sdk.APIConnectionError, sdk.RateLimitError) as e:
        raise LLMError(f"provider unavailable: {e}") from e
    except sdk.APIStatusError as e:
        if e.status_code >= 500:
            raise LLMError(f"provider error {e.status_code}") from e
        raise  # 4xx is our bug (bad request, auth): surface it as a 500, not a degraded 503


class AnthropicLLM:
    """Claude via the official SDK. Extraction uses structured outputs, so candidates arrive validated."""

    def __init__(self, model: str = "claude-opus-5", effort: str = "medium",
                 classify_model: str | None = None, **client_kwargs):
        self._client = anthropic.AsyncAnthropic(**client_kwargs)  # key from ANTHROPIC_API_KEY or an `ant auth` profile
        self._model, self._effort = model, effort
        self._classify_model = classify_model or model  # cheap router model if configured

    async def generate(self, request: LLMRequest) -> str:
        resp = await _guarded(self._client.messages.create(
            model=self._model, max_tokens=16000, system=request.system_prompt,
            messages=[{"role": m.role, "content": m.content} for m in request.messages],
            **( {"output_config": {"effort": self._effort}} if self._effort else {} ),
        ), anthropic)
        if resp.stop_reason == "refusal":
            raise LLMError("model declined the request")
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        if not text:
            raise LLMError("empty response")
        return text

    async def extract_memories(self, message: str, today: date) -> list[MemoryCandidate]:
        resp = await _guarded(self._client.messages.parse(
            model=self._model, max_tokens=16000, system=_extract_system(today),
            messages=[{"role": "user", "content": message}], output_format=_Extraction,
        ), anthropic)
        return resp.parsed_output.memories if resp.parsed_output else []

    async def classify(self, message: str) -> Category:
        resp = await _guarded(self._client.messages.parse(
            model=self._classify_model, max_tokens=1200, system=CLASSIFY_SYSTEM,
            messages=[{"role": "user", "content": message}], output_format=_Classification,
        ), anthropic)
        if resp.parsed_output is None:
            raise LLMError("classification returned no category")
        return resp.parsed_output.category


_JSON_SHAPE = ('\n\nRespond with JSON only, exactly this shape: {"memories": [{"key": "career.goal", '
               '"category": "career", "type": "goal", "value": "switch jobs", "target_timeframe": "2027", '
               '"confidence": 0.95, "reason": "user said so"}]}. Use null when there is no target_timeframe.')
_CLASSIFY_JSON_SHAPE = ('\n\nRespond with JSON only, exactly this shape: {"category": "career"}. '
                        'Valid categories: profile, career, relationships, finance, health, interests, language, '
                        'astrology, general, follow_up.')
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


class OpenAICompatibleLLM:
    """Any server speaking the OpenAI chat-completions API: OpenAI, Ollama, vLLM, Groq, OpenRouter, LM Studio, gateways.

    Configured by OPENAI_BASE_URL, OPENAI_API_KEY and LLM_MODEL. Extraction asks for `json_object` mode with the
    shape spelled out in the prompt (the lowest common denominator every compatible server supports) and validates
    the result on our side.
    """

    def __init__(self, model: str, base_url: str | None = None, api_key: str | None = None,
                 effort: str | None = None, classify_model: str | None = None, **client_kwargs):
        if not model:
            raise ValueError("LLM_MODEL is required for LLM_PROVIDER=openai (model names are server-specific)")
        # Local servers such as Ollama ignore the key, but the SDK insists on a non-empty one.
        self._client = openai.AsyncOpenAI(base_url=base_url or None, api_key=api_key or "not-needed", **client_kwargs)
        self._model = model
        self._classify_model = classify_model or model  # cheap router model if configured
        # reasoning_effort is sent when LLM_EFFORT is set; leave it empty for models that reject the parameter.
        self._extra = {"reasoning_effort": effort} if effort else {}

    async def generate(self, request: LLMRequest) -> str:
        messages = [{"role": "system", "content": request.system_prompt},
                    *({"role": m.role, "content": m.content} for m in request.messages)]
        resp = await _guarded(
            self._client.chat.completions.create(model=self._model, messages=messages, **self._extra), openai)
        text = (resp.choices[0].message.content or "").strip()
        if not text:
            raise LLMError("empty response")
        return text

    async def extract_memories(self, message: str, today: date) -> list[MemoryCandidate]:
        resp = await _guarded(self._client.chat.completions.create(
            model=self._model, response_format={"type": "json_object"}, **self._extra,
            messages=[{"role": "system", "content": _extract_system(today) + _JSON_SHAPE},
                      {"role": "user", "content": message}],
        ), openai)
        raw = _FENCE.sub("", (resp.choices[0].message.content or "").strip())  # smaller models fence their JSON
        try:
            return _Extraction.model_validate_json(raw).memories
        except ValidationError as e:
            raise LLMError(f"extraction returned invalid JSON ({e.error_count()} error(s))") from e

    async def classify(self, message: str) -> Category:
        resp = await _guarded(self._client.chat.completions.create(
            model=self._classify_model, response_format={"type": "json_object"}, max_tokens=1200, **self._extra,
            messages=[{"role": "system", "content": CLASSIFY_SYSTEM + _CLASSIFY_JSON_SHAPE},
                      {"role": "user", "content": message}],
        ), openai)
        raw = _FENCE.sub("", (resp.choices[0].message.content or "").strip())
        try:
            return _Classification.model_validate_json(raw).category
        except ValidationError as e:
            raise LLMError(f"classification returned invalid JSON ({e.error_count()} error(s))") from e


class MockLLM:
    """Deterministic provider for tests and key-less demos. `fail=True` simulates an outage."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> str:
        if self.fail:
            raise LLMError("mock failure")
        self.requests.append(request)
        context = request.system_prompt.split("Context:", 1)[1].strip()
        return f"(mock) Replying to '{request.messages[-1].content}' using context: {context}"

    async def extract_memories(self, message: str, today: date) -> list[MemoryCandidate]:
        if self.fail:
            raise LLMError("mock failure")
        return extract_by_rules(message, today)

    async def classify(self, message: str) -> Category:
        if self.fail:
            raise LLMError("mock failure")
        return rules_classify(message)


# Rule-based extraction: what the mock uses. Covers the PRD's example sentences, nothing more.
_NAME = re.compile(r"\bmy name(?:'s| is) ([A-Za-z]+)", re.I)
_BORN_ON = re.compile(
    r"\bborn (?:on )?(\d{1,2}(?:st|nd|rd|th)? [A-Za-z]+,? \d{4}|\d{4}-\d{2}-\d{2}|[A-Za-z]+ \d{1,2},? \d{4})", re.I)
_BORN_IN = re.compile(r"\bborn\b[^.;!?]*?\bin ([A-Z]\w+(?: [A-Z]\w+)*)")
_PLAN = re.compile(
    r"\b(?:i(?:'m| am) (?:planning|going|hoping|aiming) to|i plan to|i want to|my goal is to|i intend to) ([^.;!?]+)",
    re.I)
_PREFER = re.compile(
    r"\bi(?:'d| would)? prefer (?:to (?:speak|chat|talk|converse) in |(?:replies|responses|answers) in |speaking )?"
    r"([A-Za-z]+)", re.I)
_LIKE = re.compile(r"\bi (?:love|enjoy|like) ([a-z][a-z ]{2,30}?)(?=[.,;!?]|$)", re.I)
_NEXT_YEAR = re.compile(r"\bnext year\b", re.I)
_YEAR = re.compile(r"\b(?:in |by )?(20\d{2})\b")
_LANGUAGES = {"hindi", "english", "tamil", "telugu", "bengali", "marathi", "gujarati", "kannada", "malayalam",
              "punjabi", "urdu", "odia"}


def _cand(key: str, category: str, type_: str, value: str, timeframe: str | None = None,
          confidence: float = 0.9) -> MemoryCandidate:
    return MemoryCandidate(key=key, category=category, type=type_, value=value, target_timeframe=timeframe,
                           confidence=confidence, reason="rule")


def extract_by_rules(message: str, today: date) -> list[MemoryCandidate]:
    out: list[MemoryCandidate] = []
    if m := _NAME.search(message):
        out.append(_cand("profile.name", "profile", "fact", m[1].title(), confidence=0.95))
    if (m := _BORN_ON.search(message)) and (dob := parse_date(m[1])):
        out.append(_cand("profile.date_of_birth", "profile", "fact", dob.isoformat(), confidence=0.95))
    if m := _BORN_IN.search(message):
        out.append(_cand("profile.birth_place", "profile", "fact", m[1], confidence=0.95))
    if m := _PLAN.search(message):
        goal, timeframe = m[1].strip(), None
        if _NEXT_YEAR.search(goal):
            goal, timeframe = _NEXT_YEAR.sub("", goal).strip(), str(today.year + 1)
        elif y := _YEAR.search(goal):
            goal, timeframe = _YEAR.sub("", goal).strip(), y[1]
        category = rules_classify(goal)
        if category is Category.FOLLOW_UP:
            category = Category.GENERAL
        out.append(_cand(f"{category}.goal", category, "goal", goal, timeframe))
    if (m := _PREFER.search(message)) and m[1].lower() in _LANGUAGES:
        out.append(_cand("language.preferred", "language", "preference", m[1].title()))
    if m := _LIKE.search(message):
        thing = m[1].strip()
        slug = re.sub(r"[^a-z0-9]+", "_", thing.lower()).strip("_")
        out.append(_cand(f"interests.{slug}", "interests", "interest", thing, confidence=0.75))
    return out


def build_llm(settings: Settings) -> LLMProvider:
    match settings.llm_provider:
        case "mock":
            return MockLLM()
        case "anthropic":
            return AnthropicLLM(settings.llm_model or "claude-opus-5", settings.llm_effort,
                                settings.classify_model)
        case "openai":
            return OpenAICompatibleLLM(settings.llm_model, settings.openai_base_url, settings.openai_api_key,
                                       settings.llm_effort, settings.classify_model)
    raise ValueError(f"unknown LLM_PROVIDER {settings.llm_provider!r}; use anthropic, openai or mock")
