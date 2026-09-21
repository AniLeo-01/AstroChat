"""Shared Brain: persistent, graph-shaped long-term memory.

Two implementations of the same protocol. InMemoryBrain is the readable reference for the
semantics; Neo4jBrain executes the same decisions in Cypher.
"""

import logging
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Literal, Protocol

from neo4j import AsyncGraphDatabase
from neo4j.exceptions import AuthError, ServiceUnavailable, SessionExpired

from .astrology import parse_date, sun_sign
from .models import PROFILE_FIELDS, Memory, MemoryCandidate, UserProfile

log = logging.getLogger(__name__)
Outcome = Literal["created", "updated", "unchanged"]
_CONNECTIVITY_ERRORS = (ServiceUnavailable, SessionExpired, AuthError, OSError)


class BrainUnavailable(Exception):
    """The Shared Brain could not be reached. Callers degrade; they never see driver exceptions."""


class SharedBrain(Protocol):
    async def get_profile(self, user_id: str) -> UserProfile | None: ...
    async def upsert_profile(self, user_id: str, fields: dict[str, str]) -> UserProfile: ...
    async def onboard_user(self, user_id: str, name: str, email: str | None,
                           preferred_language: str | None) -> UserProfile: ...
    async def search_memories(self, user_id: str, category: str | None, limit: int) -> list[Memory]: ...
    async def upsert_memory(self, user_id: str, cand: MemoryCandidate, source_message_id: str) -> Outcome: ...
    async def list_memories(self, user_id: str) -> list[Memory]: ...


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _with_sun_sign(fields: dict[str, str]) -> dict[str, str]:
    """Normalize date_of_birth to ISO and recompute sun_sign whenever it is set."""
    dob = parse_date(fields["date_of_birth"]) if fields.get("date_of_birth") else None
    if dob:
        fields = {**fields, "date_of_birth": dob.isoformat(), "sun_sign": sun_sign(dob)}
    elif fields.get("date_of_birth") and "sun_sign" not in fields:
        # If DOB was set but unparseable, keep it but clear stale sun_sign
        fields = {**fields, "sun_sign": None}
    return fields


def _new_memory(cand: MemoryCandidate, source_message_id: str) -> Memory:
    now = _now()
    return Memory(
        id=str(uuid.uuid4()), key=cand.key, category=cand.category, type=cand.type, value=cand.value,
        target_timeframe=cand.target_timeframe, confidence=cand.confidence, status="ACTIVE",
        source_message_id=source_message_id, created_at=now, updated_at=now,
    )


class InMemoryBrain:
    """Dict-backed SharedBrain for tests and `BRAIN=memory` demo runs. `fail=True` simulates an outage."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self._profiles: dict[str, UserProfile] = {}
        self._memories: dict[str, list[Memory]] = defaultdict(list)

    def _check(self) -> None:
        if self.fail:
            raise BrainUnavailable("in-memory brain set to fail")

    async def get_profile(self, user_id: str) -> UserProfile | None:
        self._check()
        return self._profiles.get(user_id)

    async def upsert_profile(self, user_id: str, fields: dict[str, str]) -> UserProfile:
        self._check()
        profile = self._profiles.setdefault(user_id, UserProfile())
        for k, v in _with_sun_sign(fields).items():
            setattr(profile, k, v)
        return profile

    async def onboard_user(self, user_id: str, name: str, email: str | None,
                           preferred_language: str | None) -> UserProfile:
        self._check()
        fields = {"name": name}
        if preferred_language:
            fields["preferred_language"] = preferred_language
        return await self.upsert_profile(user_id, fields)

    async def search_memories(self, user_id: str, category: str | None, limit: int) -> list[Memory]:
        self._check()
        rows = [m for m in self._memories[user_id] if m.status == "ACTIVE" and (category is None or m.category == category)]
        rows.sort(key=lambda m: (m.confidence, m.updated_at), reverse=True)
        return rows[:limit]

    async def upsert_memory(self, user_id: str, cand: MemoryCandidate, source_message_id: str) -> Outcome:
        self._check()
        rows = self._memories[user_id]
        old = next((m for m in rows if m.status == "ACTIVE" and m.category == cand.category and m.key == cand.key), None)
        if old and old.value == cand.value:
            old.updated_at, old.confidence = _now(), max(old.confidence, cand.confidence)
            return "unchanged"
        if old:
            old.status, old.updated_at = "SUPERSEDED", _now()
        rows.append(_new_memory(cand, source_message_id))
        return "updated" if old else "created"

    async def list_memories(self, user_id: str) -> list[Memory]:
        self._check()
        return list(self._memories[user_id])

# CYPHER QUERIES
# Community Edition has no multiple databases or partial (filtered) unique constraints, so each user is
# a partition: Memory and Profile nodes carry the owning user_id. "At most one ACTIVE memory per key" is
# DB-enforced via a derived single property `active_key` = "user_id|category|key" that is non-null only
# while ACTIVE; Neo4j does not index nulls, so any number of SUPERSEDED versions can coexist while the
# unique constraint still rejects a second ACTIVE for the same key.
SCHEMA = (
    # migration: the old composite (user_id, category, key, status) constraint cannot allow a second
    # SUPERSEDED version of a key, so drop it in favour of the derived active_key constraint below.
    "DROP CONSTRAINT memory_partition_unique IF EXISTS",
    "CREATE CONSTRAINT user_id_unique IF NOT EXISTS FOR (u:User) REQUIRE u.id IS UNIQUE",
    "CREATE CONSTRAINT user_email_unique IF NOT EXISTS FOR (u:User) REQUIRE u.email IS UNIQUE",
    "CREATE CONSTRAINT memory_id_unique IF NOT EXISTS FOR (m:Memory) REQUIRE m.id IS UNIQUE",
    "CREATE CONSTRAINT memory_active_unique IF NOT EXISTS FOR (m:Memory) REQUIRE m.active_key IS UNIQUE",
    "CREATE CONSTRAINT profile_user_unique IF NOT EXISTS FOR (p:Profile) REQUIRE p.user_id IS UNIQUE",
)
# One-time idempotent backfill: legacy nodes created before the partition key get it from their User,
# and pre-existing memory nodes get their derived active_key (ACTIVE only) before the constraint exists.
BACKFILL = """
MATCH (u:User)-[:HAS_PROFILE]->(p:Profile) WHERE p.user_id IS NULL SET p.user_id = u.id
WITH u
MATCH (u)-[:HAS_MEMORY]->(m:Memory) WHERE m.user_id IS NULL SET m.user_id = u.id"""
BACKFILL_ACTIVE_KEY = """
MATCH (m:Memory) WHERE m.active_key IS NULL
SET m.active_key = CASE WHEN m.status = 'ACTIVE'
                        THEN m.user_id + '|' + m.category + '|' + m.key ELSE NULL END"""
GET_PROFILE = "MATCH (p:Profile {user_id: $user_id}) RETURN p"
ONBOARD_USER = """
MERGE (u:User {id: $user_id}) ON CREATE SET u.created_at = datetime(), u.email = $email
ON MATCH SET u.updated_at = datetime()
MERGE (u)-[:HAS_PROFILE]->(p:Profile)
SET p.name = $name
SET p.preferred_language = coalesce($preferred_language, p.preferred_language)
SET p.user_id = $user_id
RETURN p"""
UPSERT_PROFILE = """
MERGE (u:User {id: $user_id}) ON CREATE SET u.created_at = datetime()
SET u.updated_at = datetime()
MERGE (u)-[:HAS_PROFILE]->(p:Profile)
SET p.user_id = $user_id
SET p += $fields
RETURN p"""
SEARCH_MEMORIES = """
MATCH (m:Memory {user_id: $user_id})
WHERE m.status = 'ACTIVE' AND ($category IS NULL OR m.category = $category)
RETURN m ORDER BY m.confidence DESC, m.updated_at DESC LIMIT $limit"""
LIST_MEMORIES = "MATCH (m:Memory {user_id: $user_id}) RETURN m ORDER BY m.created_at"
FIND_ACTIVE = """
MATCH (m:Memory {user_id: $user_id, category: $category, key: $key, status: 'ACTIVE'})
RETURN m.id AS id, m.value AS value, m.confidence AS confidence"""
TOUCH = "MATCH (m:Memory {id: $id}) SET m.updated_at = datetime(), m.confidence = $confidence"
SUPERSEDE = ("MATCH (m:Memory {id: $id}) SET m.status = 'SUPERSEDED', m.updated_at = datetime(),"
             " m.active_key = null")
CREATE_MEMORY = """
MERGE (u:User {id: $user_id}) ON CREATE SET u.created_at = datetime()
CREATE (u)-[:HAS_MEMORY]->(m:Memory $props)
SET m.created_at = datetime(), m.updated_at = datetime(), m.user_id = $user_id
SET m.active_key = CASE WHEN m.status = 'ACTIVE'
                        THEN m.user_id + '|' + m.category + '|' + m.key ELSE NULL END
WITH m
OPTIONAL MATCH (old:Memory {id: $old_id})
FOREACH (o IN CASE WHEN old IS NULL THEN [] ELSE [old] END | CREATE (m)-[:SUPERSEDES]->(o))"""


def _profile(node) -> UserProfile:
    return UserProfile(**{k: node.get(k) for k in PROFILE_FIELDS})


def _memory(node) -> Memory:
    return Memory(
        id=node["id"], key=node["key"], category=node["category"], type=node["type"], value=node["value"],
        target_timeframe=node.get("target_timeframe"), confidence=node["confidence"], status=node["status"],
        source_message_id=node.get("source_message_id"),
        created_at=node["created_at"].to_native(), updated_at=node["updated_at"].to_native(),
    )


class Neo4jBrain:
    def __init__(self, uri: str, user: str, password: str, timeout: float = 3.0):
        # Fail fast so an outage degrades the request in ~timeout seconds instead of the driver's 30s default.
        # ponytail: fixed timeout, no circuit breaker; add one when outages are long enough to matter per request.
        self._driver = AsyncGraphDatabase.driver(
            uri, auth=(user, password), connection_timeout=timeout, max_transaction_retry_time=timeout,
            notifications_min_severity="OFF")  # server hints about empty labels are noise on a fresh graph

    async def ensure_schema(self) -> None:
        await self._query(BACKFILL)
        await self._query(BACKFILL_ACTIVE_KEY)
        for stmt in SCHEMA:
            await self._query(stmt)

    async def close(self) -> None:
        await self._driver.close()

    async def get_profile(self, user_id: str) -> UserProfile | None:
        rows = await self._query(GET_PROFILE, user_id=user_id)
        return _profile(rows[0]["p"]) if rows else None

    async def upsert_profile(self, user_id: str, fields: dict[str, str]) -> UserProfile:
        rows = await self._query(UPSERT_PROFILE, user_id=user_id, fields=_with_sun_sign(fields))
        return _profile(rows[0]["p"])

    async def onboard_user(self, user_id: str, name: str, email: str | None,
                           preferred_language: str | None) -> UserProfile:
        rows = await self._query(ONBOARD_USER, user_id=user_id, email=email,
                                 name=name, preferred_language=preferred_language)
        return _profile(rows[0]["p"])

    async def search_memories(self, user_id: str, category: str | None, limit: int) -> list[Memory]:
        rows = await self._query(SEARCH_MEMORIES, user_id=user_id, category=category, limit=limit)
        return [_memory(r["m"]) for r in rows]

    async def list_memories(self, user_id: str) -> list[Memory]:
        return [_memory(r["m"]) for r in await self._query(LIST_MEMORIES, user_id=user_id)]

    async def upsert_memory(self, user_id: str, cand: MemoryCandidate, source_message_id: str) -> Outcome:
        """Appendix C in one write transaction: find active by logical key, then create / touch / supersede."""

        async def tx_fn(tx) -> Outcome:
            result = await tx.run(FIND_ACTIVE, user_id=user_id, category=cand.category, key=cand.key)
            old = await result.single()
            if old and old["value"] == cand.value:
                await tx.run(TOUCH, id=old["id"], confidence=max(old["confidence"], cand.confidence))
                return "unchanged"
            if old:
                await tx.run(SUPERSEDE, id=old["id"])
            mem = _new_memory(cand, source_message_id)
            props = {k: v for k, v in vars(mem).items() if not k.endswith("_at")}
            await tx.run(CREATE_MEMORY, user_id=user_id, props=props, old_id=old["id"] if old else None)
            return "updated" if old else "created"

        try:
            async with self._driver.session() as session:
                return await session.execute_write(tx_fn)
        except _CONNECTIVITY_ERRORS as e:
            raise BrainUnavailable(str(e)) from e

    async def _query(self, cypher: str, **params):
        try:
            return (await self._driver.execute_query(cypher, parameters_=params)).records
        except _CONNECTIVITY_ERRORS as e:
            raise BrainUnavailable(str(e)) from e
