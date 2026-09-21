"""Memory update: validate candidates, route profile facts, apply upserts (TDD §7)."""

import logging

from .brain import SharedBrain
from .models import MEMORY_CATEGORIES, MEMORY_TYPES, PROFILE_KEYS, MemoryCandidate

log = logging.getLogger(__name__)


def validate(candidates: list[MemoryCandidate], min_confidence: float) -> list[MemoryCandidate]:
    """Drop low-confidence, malformed or off-taxonomy candidates. Normalizes key/category/type case."""
    kept: list[MemoryCandidate] = []
    for c in candidates:
        c.key, c.category, c.type, c.value = c.key.strip().lower(), c.category.strip().lower(), c.type.strip().lower(), c.value.strip()
        if c.confidence < min_confidence or c.confidence > 1.0 or not c.key or not c.value:
            continue
        if c.category not in MEMORY_CATEGORIES or c.type not in MEMORY_TYPES:
            log.info("dropping off-taxonomy candidate key=%s category=%s type=%s", c.key, c.category, c.type)
            continue
        kept.append(c)
    return kept


async def remember(brain: SharedBrain, user_id: str, candidates: list[MemoryCandidate],
                   source_message_id: str, min_confidence: float) -> int:
    """Persist what qualifies. Profile keys go to the Profile node, the rest to Memory upserts.

    Returns the number of writes that changed state (duplicates of an active memory count as zero).
    """
    valid = validate(candidates, min_confidence)
    changed = 0
    profile_fields = {PROFILE_KEYS[c.key]: c.value for c in valid if c.key in PROFILE_KEYS}
    if profile_fields:
        existing = await brain.get_profile(user_id)
        existing_fields = existing.fields() if existing else {}
        new_fields = {k: v for k, v in profile_fields.items() if existing_fields.get(k) != v}
        if new_fields:
            await brain.upsert_profile(user_id, profile_fields)
            changed += len(new_fields)
    for c in valid:
        if c.key not in PROFILE_KEYS and await brain.upsert_memory(user_id, c, source_message_id) != "unchanged":
            changed += 1
    return changed
