"""Chat orchestrator: Chat -> Context Selection -> Shared Brain -> LLM -> Response -> Memory Update."""

import logging
import uuid
from dataclasses import dataclass
from datetime import date

from .brain import BrainUnavailable, SharedBrain
from .context import select_context
from .llm import LLMError, LLMProvider
from .memory import remember
from .models import Category, ChatMessage
from .session import SessionStore

log = logging.getLogger(__name__)


@dataclass
class ChatResult:
    response: str
    context_used: list[str]
    memory_updates: int = 0
    degraded: bool = False


class ChatService:
    def __init__(self, brain: SharedBrain, llm: LLMProvider, sessions: SessionStore,
                 memory_limit: int = 8, min_confidence: float = 0.6):
        self.brain, self.llm, self.sessions = brain, llm, sessions
        self.memory_limit, self.min_confidence = memory_limit, min_confidence

    async def chat(self, user_id: str, session_id: str, message: str) -> ChatResult:
        recent = self.sessions.recent(user_id, session_id)
        category = await self.llm.classify(message)

        # Retrieve: follow-ups rely on recent turns only; everything else consults the Shared Brain.
        profile, memories, degraded = None, [], False
        if category is not Category.FOLLOW_UP:
            try:
                profile = await self.brain.get_profile(user_id)
                search = None if category is Category.GENERAL else category
                memories = await self.brain.search_memories(user_id, search, self.memory_limit)
            except BrainUnavailable as e:
                log.warning("brain unavailable, answering from short-term context user=%s: %s", user_id, e)
                degraded = True

        current = ChatMessage("user", message, id=str(uuid.uuid4()))
        selection = select_context(category, current, recent, profile, memories)

        # Generate. LLMError propagates to the API as 503; the failed turn is not recorded anywhere.
        reply = await self.llm.generate(selection.request)
        self.sessions.append(user_id, session_id, current, ChatMessage("assistant", reply))

        # Memory update. Skipped for follow-ups (nothing durable to learn) and when the brain is already down.
        updates = 0
        if not degraded and category is not Category.FOLLOW_UP:
            try:
                candidates = await self.llm.extract_memories(message, date.today())
                updates = await remember(self.brain, user_id, candidates, current.id, self.min_confidence)
            except LLMError as e:
                log.warning("memory extraction failed, response still returned: %s", e)
            except BrainUnavailable as e:
                log.warning("memory write failed, response still returned: %s", e)
                degraded = True

        log.info("chat user=%s session=%s category=%s context_used=%s memory_updates=%d degraded=%s",
                 user_id, session_id, category, selection.context_used, updates, degraded)
        return ChatResult(reply, selection.context_used, updates, degraded)
