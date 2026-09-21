"""Integration: the same brain semantics against a live Neo4j. Set NEO4J_TEST_URI to run."""

import os
import uuid

import pytest

from app.brain import Neo4jBrain
from app.models import MemoryCandidate

URI = os.getenv("NEO4J_TEST_URI")
pytestmark = pytest.mark.skipif(not URI, reason="set NEO4J_TEST_URI (and NEO4J_USER/NEO4J_PASSWORD) to run")


async def test_neo4j_roundtrip():
    brain = Neo4jBrain(URI, os.getenv("NEO4J_USER", "neo4j"), os.getenv("NEO4J_PASSWORD", "password"))
    uid = f"test-{uuid.uuid4()}"
    try:
        await brain.ensure_schema()
        assert await brain.get_profile(uid) is None
        p = await brain.upsert_profile(uid, {"name": "Rahul", "date_of_birth": "1995-08-15"})
        assert (p.name, p.sun_sign) == ("Rahul", "Leo")
        assert (await brain.get_profile(uid)).birth_place is None

        c = MemoryCandidate(key="language.preferred", category="language", type="preference", value="English",
                            target_timeframe=None, confidence=0.9, reason="t")
        assert await brain.upsert_memory(uid, c, "m1") == "created"
        assert await brain.upsert_memory(uid, c, "m2") == "unchanged"
        assert await brain.upsert_memory(uid, c.model_copy(update={"value": "Hindi"}), "m3") == "updated"
        goal = MemoryCandidate(key="career.goal", category="career", type="goal", value="switch jobs",
                               target_timeframe="2027", confidence=0.95, reason="t")
        assert await brain.upsert_memory(uid, goal, "m4") == "created"

        assert [m.value for m in await brain.search_memories(uid, "language", 8)] == ["Hindi"]
        assert [m.key for m in await brain.search_memories(uid, None, 8)] == ["career.goal", "language.preferred"]
        assert await brain.search_memories(uid, "health", 8) == []
        everything = await brain.list_memories(uid)
        assert sorted((m.value, m.status) for m in everything) == [("English", "SUPERSEDED"), ("Hindi", "ACTIVE"), ("switch jobs", "ACTIVE")]
        assert everything[0].created_at.tzinfo is not None

        rows = (await brain._driver.execute_query(
            "MATCH (:User {id: $id})-[:HAS_MEMORY]->(n:Memory {value: 'Hindi'})-[:SUPERSEDES]->(o:Memory) "
            "RETURN o.value AS old", id=uid)).records
        assert [r["old"] for r in rows] == ["English"]
    finally:
        await brain._driver.execute_query(
            "MATCH (u:User {id: $id}) OPTIONAL MATCH (u)-->(n) DETACH DELETE u, n", id=uid)
        await brain.close()
