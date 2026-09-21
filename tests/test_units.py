from datetime import date

import pytest

from app.astrology import parse_date, sun_sign
from app.brain import BrainUnavailable, InMemoryBrain, Neo4jBrain
from app.context import classify
from app.memory import validate
from app.models import Category, MemoryCandidate


@pytest.mark.parametrize("message, expected", [
    ("What should I focus on in my career?", Category.CAREER),
    ("What do you remember about my career goals?", Category.CAREER),
    ("Why do you say that?", Category.FOLLOW_UP),
    ("Tell me more", Category.FOLLOW_UP),
    ("Is that good?", Category.FOLLOW_UP),
    ("Hello!", Category.GENERAL),
    ("What do you remember about me?", Category.GENERAL),
    ("Actually, I prefer Hindi.", Category.LANGUAGE),
    ("My name is Rahul.", Category.PROFILE),
    ("What does my horoscope say about money?", Category.ASTROLOGY),
    ("How is my health this month?", Category.HEALTH),
])
def test_classify(message, expected):
    assert classify(message) is expected


@pytest.mark.parametrize("dob, sign", [
    (date(1995, 8, 15), "Leo"), (date(2000, 8, 23), "Virgo"), (date(2000, 12, 22), "Capricorn"),
    (date(2000, 1, 19), "Capricorn"), (date(2000, 1, 20), "Aquarius"), (date(2000, 3, 21), "Aries"),
])
def test_sun_sign(dob, sign):
    assert sun_sign(dob) == sign


def test_parse_date_formats():
    assert parse_date("15 August 1995") == parse_date("1995-08-15") == parse_date("August 15, 1995") == date(1995, 8, 15)
    assert parse_date("15th Aug 1995") == date(1995, 8, 15)
    assert parse_date("sometime in 1995") is None


def test_validate_filters_and_normalizes():
    cands = [
        MemoryCandidate(key="Career.Goal", category="CAREER", type="Goal", value=" switch jobs ", target_timeframe=None, confidence=0.9, reason=""),
        MemoryCandidate(key="career.mood", category="career", type="fact", value="tired", target_timeframe=None, confidence=0.3, reason="low"),
        MemoryCandidate(key="x.y", category="weather", type="fact", value="rainy", target_timeframe=None, confidence=0.9, reason="bad category"),
        MemoryCandidate(key="x.y", category="career", type="rumor", value="x", target_timeframe=None, confidence=0.9, reason="bad type"),
        MemoryCandidate(key="x.y", category="career", type="fact", value="  ", target_timeframe=None, confidence=0.9, reason="empty"),
    ]
    kept = validate(cands, 0.6)
    assert [(c.key, c.category, c.type, c.value) for c in kept] == [("career.goal", "career", "goal", "switch jobs")]


async def test_upsert_outcomes():
    brain = InMemoryBrain()
    c = MemoryCandidate(key="language.preferred", category="language", type="preference", value="English", target_timeframe=None, confidence=0.8, reason="")
    assert await brain.upsert_memory("u", c, "m1") == "created"
    assert await brain.upsert_memory("u", c.model_copy(update={"confidence": 0.95}), "m2") == "unchanged"
    assert await brain.upsert_memory("u", c.model_copy(update={"value": "Hindi"}), "m3") == "updated"
    active = await brain.search_memories("u", "language", 8)
    assert [(m.value, m.source_message_id) for m in active] == [("Hindi", "m3")]
    assert sorted((m.value, m.status, m.confidence) for m in await brain.list_memories("u")) == [
        ("English", "SUPERSEDED", 0.95), ("Hindi", "ACTIVE", 0.8)]


async def test_upsert_profile_derives_sun_sign():
    brain = InMemoryBrain()
    p = await brain.upsert_profile("u", {"name": "Rahul", "date_of_birth": "15 August 1995"})
    assert (p.date_of_birth, p.sun_sign) == ("1995-08-15", "Leo")
    p = await brain.upsert_profile("u", {"birth_place": "Delhi"})
    assert (p.name, p.sun_sign, p.birth_place) == ("Rahul", "Leo", "Delhi")


async def test_neo4j_connection_failure_is_brain_unavailable():
    brain = Neo4jBrain("bolt://127.0.0.1:1", "neo4j", "x")
    try:
        with pytest.raises(BrainUnavailable):
            await brain.get_profile("u")
    finally:
        await brain.close()
