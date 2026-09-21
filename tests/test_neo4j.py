"""Integration: the same brain semantics against a live Neo4j. Set NEO4J_TEST_URI to run."""

import os
import uuid

import neo4j
import pytest

from app.brain import Neo4jBrain
from app.models import MemoryCandidate

URI = os.getenv("NEO4J_TEST_URI")
pytestmark = pytest.mark.skipif(not URI, reason="set NEO4J_TEST_URI (and NEO4J_USER/NEO4J_PASSWORD) to run")


async def test_neo4j_roundtrip():
    brain = Neo4jBrain(URI, os.getenv("NEO4J_USER", "neo4j"), os.getenv("NEO4J_PASSWORD", "password"))
    uid = f"test-{uuid.uuid4()}"
    email_uid = f"onboard-{uuid.uuid4()}@x.com"
    uid2 = None
    try:
        await brain.ensure_schema()
        assert await brain.get_profile(uid) is None
        p = await brain.upsert_profile(uid, {"name": "Rahul", "date_of_birth": "1995-08-15"})
        assert (p.name, p.sun_sign) == ("Rahul", "Leo")
        assert (await brain.get_profile(uid)).birth_place is None

        email_uid = f"onboard-{uuid.uuid4()}@x.com"
        onboarded = await brain.onboard_user(email_uid, "Ani", email_uid, "Hindi")
        assert (onboarded.name, onboarded.preferred_language) == ("Ani", "Hindi")
        assert (await brain.get_profile(email_uid)).preferred_language == "Hindi"

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

        # A SECOND correction of the same key must not trip the schema: the old active_key unique
        # constraint (a composite on user_id/category/key/status) could not allow two SUPERSEDED
        # versions, so anything past the first supersede 500'd. Regression: Live bug, manu@gmail.com.
        assert await brain.upsert_memory(uid, c.model_copy(update={"value": "Marathi"}), "m5") == "updated"
        everything = await brain.list_memories(uid)
        assert sorted((m.value, m.status) for m in everything) == [
            ("English", "SUPERSEDED"), ("Hindi", "SUPERSEDED"), ("Marathi", "ACTIVE"), ("switch jobs", "ACTIVE")]
        assert [m.value for m in await brain.search_memories(uid, "language", 8)] == ["Marathi"]

        # n successive corrections are fine too: exactly one ACTIVE (the latest), n-1 SUPERSEDED
        # versions, and the SUPERSEDES edges form a single unbroken chain of depth n-1.
        n = 8
        for i in range(n):
            # the logical key already exists (Marathi is ACTIVE), so every write supersedes -> "updated"
            assert await brain.upsert_memory(uid, c.model_copy(update={"value": f"v{i}"}), f"n-{i}") == "updated"
        lang_rows = [m for m in await brain.list_memories(uid) if m.category == "language"]
        assert sum(1 for m in lang_rows if m.status == "ACTIVE") == 1
        assert sum(1 for m in lang_rows if m.status == "SUPERSEDED") == len(lang_rows) - 1
        assert [m.value for m in await brain.search_memories(uid, "language", 8)] == ["v7"]
        depth = (await brain._driver.execute_query(
            "MATCH p = (a:Memory {user_id: $uid, key: $key, status: 'ACTIVE'})-[:SUPERSEDES*]->(s) "
            "RETURN length(p) AS depth", uid=uid, key="language.preferred")).records
        # one unbroken chain: every prefix length appears exactly once, deepest = all prior versions
        assert sorted(r["depth"] for r in depth) == list(range(1, len(lang_rows)))

        # each user is an isolated partition: another user sees nothing of this one
        uid2 = f"test-{uuid.uuid4()}"
        await brain.onboard_user(uid2, "Bob", None, None)
        assert await brain.search_memories(uid2, None, 8) == []
        assert await brain.get_profile(uid2) is not None and (await brain.get_profile(uid2)).name == "Bob"

        # the active_key unique constraint rejects a second ACTIVE memory for the same (user, key)
        with pytest.raises(neo4j.exceptions.ConstraintError):
            await brain._driver.execute_query(
                "CREATE (:Memory {id: $id, user_id: $uid, category: 'language', key: 'language.preferred',"
                " status: 'ACTIVE', value: 'Hindi', active_key: $uid + '|language|language.preferred'})",
                id=f"dup-{uuid.uuid4()}", uid=uid)
    finally:
        await brain._driver.execute_query(
            "MATCH (u:User {id: $id}) OPTIONAL MATCH (u)-->(n) DETACH DELETE u, n", id=uid)
        await brain._driver.execute_query("MATCH (u:User {id: $id}) DETACH DELETE u", id=email_uid)
        if uid2 is not None:
            await brain._driver.execute_query("MATCH (u:User {id: $id}) DETACH DELETE u", id=uid2)
        await brain.close()
