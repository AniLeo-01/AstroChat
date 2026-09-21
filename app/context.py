"""Query understanding, context selection and prompt building (TDD §4.5 to §4.7)."""

import re
from dataclasses import dataclass

from .models import PROFILE_FIELDS, Category, ChatMessage, LLMRequest, Memory, UserProfile

SYSTEM_PROMPT = """You are MyNaksh, a warm and personalized astrology assistant.
- Use only the context below and the conversation. Never invent facts about the user.
- The user's explicit statements are authoritative over anything else.
- If the context lacks something the user asks about, say you do not have it yet and ask.
- Keep answers conversational, specific and brief. Astrology is guidance, not certainty.
- Do not mention memories, context, retrieval or how you know things; speak naturally."""

# ponytail: keyword classifier, first match wins; swap for an LLM/embedding classifier when evals show misroutes.
_KEYWORDS: list[tuple[Category, re.Pattern]] = [
    (cat, re.compile(r"\b(?:" + "|".join(map(re.escape, words)) + ")", re.I))
    for cat, words in {
        Category.ASTROLOGY: ["horoscope", "zodiac", "sun sign", "rashi", "kundli", "kundali", "nakshatra", "planet",
                             "saturn", "jupiter", "mercury", "venus", "mars", "retrograde", "birth chart", "astrolog",
                             "moon sign", "rising sign"],
        Category.LANGUAGE: ["language", "hindi", "english", "speak", "reply in", "respond in", "translate"],
        Category.PROFILE: ["my name", "born", "birthday", "date of birth", "birth place", "how old", "my age"],
        Category.CAREER: ["career", "job", "work", "promotion", "business", "profession", "interview", "boss",
                          "salary", "office", "startup", "employ"],
        Category.RELATIONSHIPS: ["relationship", "marriage", "marry", "partner", "love", "wife", "husband",
                                 "girlfriend", "boyfriend", "family", "friend", "dating", "breakup"],
        Category.FINANCE: ["money", "financ", "invest", "saving", "wealth", "debt", "loan", "income", "stock",
                           "property", "budget"],
        Category.HEALTH: ["health", "fitness", "sleep", "stress", "diet", "doctor", "anxiety", "exercise",
                          "wellness", "energy", "sick"],
        Category.INTERESTS: ["hobby", "hobbies", "interest", "enjoy", "passion", "music", "reading", "travel",
                             "sport", "cricket", "paint", "cook"],
    }.items()
]
_FOLLOW_UP = re.compile(
    r"^\W*(?:why|how come|really|what do you mean|tell me more|elaborate|explain|go on|say more|can you expand|"
    r"expand on|what about (?:that|this|it)|and then|are you sure|based on what)\b", re.I)
_PRONOUN = re.compile(r"\b(?:that|this|it|those|these)\b", re.I)

# Which Profile fields ride along per query category (TDD §4.6 table). Default: name + sun_sign.
_PROFILE_FIELDS_FOR = {
    Category.FOLLOW_UP: (),
    Category.PROFILE: PROFILE_FIELDS,
    Category.ASTROLOGY: PROFILE_FIELDS,
    Category.LANGUAGE: ("name", "preferred_language"),
}
_DEFAULT_PROFILE_FIELDS = ("name", "sun_sign")


def classify(message: str) -> Category:
    for cat, pattern in _KEYWORDS:
        if pattern.search(message):
            return cat
    if _FOLLOW_UP.search(message) or (len(message.split()) <= 6 and _PRONOUN.search(message)):
        return Category.FOLLOW_UP
    return Category.GENERAL


@dataclass
class Selection:
    request: LLMRequest
    context_used: list[str]


def select_context(
    category: Category, current: ChatMessage, recent: list[ChatMessage],
    profile: UserProfile | None, memories: list[Memory],
) -> Selection:
    """Bounded context: recent turns (already capped), relevant profile fields, retrieved memories."""
    wanted = _PROFILE_FIELDS_FOR.get(category, _DEFAULT_PROFILE_FIELDS)
    facts = {k: v for k, v in (profile.fields() if profile else {}).items() if k in wanted}

    used: list[str] = []
    if recent:
        used.append("recent_conversation")
    if any(k != "sun_sign" for k in facts):
        used.append("user_profile")
    if "sun_sign" in facts:
        used.append("astrology")
    used += [m.key for m in memories]

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
    return Selection(LLMRequest(system, [*recent, current]), used)


def _render(m: Memory) -> str:
    timeframe = f" (timeframe: {m.target_timeframe})" if m.target_timeframe else ""
    return f"- [{m.category}] {m.key} = {m.value}{timeframe}"
