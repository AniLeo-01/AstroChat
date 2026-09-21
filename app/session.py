from collections import defaultdict, deque

from .models import ChatMessage


class SessionStore:
    """Short-term context: the last N messages per (user_id, session_id).

    ponytail: process-local dict; swap for Redis when running more than one replica.
    """

    def __init__(self, limit: int = 10):
        self._sessions: dict[tuple[str, str], deque[ChatMessage]] = defaultdict(lambda: deque(maxlen=limit))

    def recent(self, user_id: str, session_id: str) -> list[ChatMessage]:
        return list(self._sessions.get((user_id, session_id), ()))

    def append(self, user_id: str, session_id: str, *messages: ChatMessage) -> None:
        self._sessions[(user_id, session_id)].extend(messages)
