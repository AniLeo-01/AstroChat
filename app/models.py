from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


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


MEMORY_CATEGORIES = {c.value for c in Category if c is not Category.FOLLOW_UP}
MEMORY_TYPES = {"fact", "goal", "preference", "interest"}
PROFILE_FIELDS = ("name", "date_of_birth", "time_of_birth", "birth_place", "preferred_language", "sun_sign")
# Memory keys that are structured profile facts and therefore live on the Profile node (TDD §7.0).
PROFILE_KEYS = {
    "profile.name": "name",
    "profile.date_of_birth": "date_of_birth",
    "profile.time_of_birth": "time_of_birth",
    "profile.birth_place": "birth_place",
}


@dataclass
class ChatMessage:
    role: str  # "user" | "assistant"
    content: str
    id: str = ""


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


class MemoryCandidate(BaseModel):
    """What the extractor proposes. Pydantic because it doubles as the LLM structured-output schema."""

    key: str = Field(description="dotted <category>.<slug>, e.g. career.goal, language.preferred, profile.name")
    category: str = Field(description="one of: " + ", ".join(sorted(MEMORY_CATEGORIES)))
    type: str = Field(description="one of: fact, goal, preference, interest")
    value: str = Field(description="short normalized phrase; dates as YYYY-MM-DD")
    target_timeframe: str | None = Field(description="resolved year or period the user gave, else null")
    confidence: float = Field(description="0 to 1: certainty this is explicit and durable")
    reason: str = Field(description="one short phrase quoting or paraphrasing the user's statement")


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


@dataclass
class LLMRequest:
    system_prompt: str  # fixed role text + rendered profile/memory context
    messages: list[ChatMessage]  # recent turns followed by the current user message
