"""The required scenarios (TDD §17) through POST /chat with InMemoryBrain + MockLLM."""

from datetime import date

from fastapi.testclient import TestClient

from app.config import Settings
from app.llm import MockLLM, extract_by_rules
from app.main import create_app
from app.models import Category, MemoryCandidate


def chat(client, user: str, session: str, message: str) -> dict:
    r = client.post("/chat", json={"user_id": user, "session_id": session, "message": message})
    assert r.status_code == 200, r.text
    return r.json()


NEXT_YEAR = str(date.today().year + 1)
INTRO = "My name is Rahul. I was born on 15 August 1995 in Delhi. I'm planning to switch jobs next year."


def test_1_new_user_succeeds_with_no_context(client):
    r = chat(client, "u1", "s1", "Hello there!")
    assert r["user_id"] == "u1" and r["session_id"] == "s1"
    assert r["response"] and r["context_used"] == [] and r["degraded"] is False


async def test_2_first_message_creates_durable_memory_and_profile(client, brain):
    r = chat(client, "u1", "s1", INTRO)
    assert r["memory_updates"] == 4  # name, dob, birth place -> Profile; career goal -> Memory
    goal, = await brain.search_memories("u1", "career", 8)
    assert (goal.key, goal.value, goal.target_timeframe, goal.status) == ("career.goal", "switch jobs", NEXT_YEAR, "ACTIVE")
    profile = await brain.get_profile("u1")
    assert (profile.name, profile.date_of_birth, profile.birth_place, profile.sun_sign) == ("Rahul", "1995-08-15", "Delhi", "Leo")


def test_3_new_session_retrieves_memory(client):
    chat(client, "u1", "s1", INTRO)
    r = chat(client, "u1", "s2", "What do you remember about my career goals?")
    assert "career.goal" in r["context_used"]
    assert "recent_conversation" not in r["context_used"]  # fresh session
    assert "switch jobs" in r["response"]


def test_4_follow_up_uses_recent_context_only(client, llm):
    chat(client, "u1", "s1", INTRO)
    chat(client, "u1", "s1", "What should I focus on for my career?")
    r = chat(client, "u1", "s1", "Why do you say that?")
    assert r["context_used"] == ["recent_conversation"]
    assert r["memory_updates"] == 0
    sent = llm.requests[-1]
    assert [m.role for m in sent.messages] == ["user", "assistant", "user", "assistant", "user"]
    assert "career.goal" not in sent.system_prompt


def test_5_memory_persists_across_sessions(client):
    chat(client, "u1", "s1", INTRO)
    memories = client.get("/users/u1/memories").json()
    assert [(m["key"], m["status"]) for m in memories] == [("career.goal", "ACTIVE")]
    for session in ("s2", "s3"):
        assert "career.goal" in chat(client, "u1", session, "Any advice for my job?")["context_used"]


async def test_6_irrelevant_memory_excluded(client, brain, llm):
    await brain.upsert_memory("u1", _cand("career.goal", "career", "switch jobs"), "m1")
    await brain.upsert_memory("u1", _cand("health.goal", "health", "sleep 8 hours"), "m2")
    r = chat(client, "u1", "s1", "What should I focus on in my career?")
    assert "career.goal" in r["context_used"] and "health.goal" not in r["context_used"]
    assert "sleep" not in llm.requests[-1].system_prompt


async def test_7_user_correction_supersedes(client, brain, llm):
    chat(client, "u1", "s1", "I prefer English.")
    r = chat(client, "u1", "s1", "Actually, I prefer Hindi.")
    assert r["memory_updates"] == 1
    active = await brain.search_memories("u1", "language", 8)
    assert [m.value for m in active] == ["Hindi"]
    statuses = {m.value: m.status for m in await brain.list_memories("u1")}
    assert statuses == {"English": "SUPERSEDED", "Hindi": "ACTIVE"}
    r = chat(client, "u1", "s9", "Which language should we use?")
    assert r["context_used"] == ["language.preferred"]
    assert "Hindi" in llm.requests[-1].system_prompt and "English" not in llm.requests[-1].system_prompt


def test_8_missing_profile_is_fine(client):
    r = chat(client, "nobody", "s1", "What should I focus on in my career?")
    assert r["response"] and "user_profile" not in r["context_used"] and "astrology" not in r["context_used"]


async def test_9_llm_failure_returns_503_and_mutates_nothing(client, brain, llm):
    llm.fail = True
    r = client.post("/chat", json={"user_id": "u1", "session_id": "s1", "message": INTRO})
    assert r.status_code == 503 and "LLM" in r.json()["detail"]
    assert await brain.list_memories("u1") == [] and await brain.get_profile("u1") is None
    llm.fail = False
    assert "recent_conversation" not in chat(client, "u1", "s1", "Hi")["context_used"]  # failed turn not recorded


def test_10_graph_failure_degrades(client, brain):
    brain.fail = True
    r = chat(client, "u1", "s1", INTRO)
    assert r["degraded"] is True and r["memory_updates"] == 0 and r["context_used"] == []
    r = chat(client, "u1", "s1", "Why do you say that?")
    assert r["context_used"] == ["recent_conversation"]  # short-term context still works


def test_invalid_payload_is_422(client):
    assert client.post("/chat", json={"user_id": "u1", "message": "hi"}).status_code == 422
    assert client.post("/chat", json={"user_id": "u1", "session_id": "s1", "message": ""}).status_code == 422
    assert client.post("/users", json={"user_id": "u1", "date_of_birth": "not-a-date"}).status_code == 422


def test_onboard_with_email_derives_user_id(client):
    r = client.post("/onboard", json={"name": "Ani", "email": "ani@gmail.com"})
    assert r.status_code == 200
    assert r.json() == {"user_id": "ani@gmail.com", "name": "Ani", "email": "ani@gmail.com"}
    got = client.get("/users/ani@gmail.com").json()
    assert got["name"] == "Ani" and got["sun_sign"] is None


def test_onboard_without_email_uses_name_slug(client):
    r = client.post("/onboard", json={"name": "Rahul Sharma"})
    assert r.json() == {"user_id": "rahul-sharma", "name": "Rahul Sharma", "email": None}


def test_onboard_with_language_and_email_normalization(client):
    r = client.post("/onboard", json={"name": "Priya", "email": " PRIYA@X.com ", "preferred_language": "Hindi"})
    got = r.json()
    assert got["user_id"] == "priya@x.com" and got["email"] == "priya@x.com"
    assert client.get("/users/priya@x.com").json()["preferred_language"] == "Hindi"


def test_onboard_rejects_bad_email(client):
    assert client.post("/onboard", json={"name": "Ani", "email": "not-an-email"}).status_code == 422


def test_onboard_is_idempotent(client):
    client.post("/onboard", json={"name": "Ani", "email": "ani@gmail.com"})
    client.post("/onboard", json={"name": "Ani", "email": "ani@gmail.com"})
    assert client.get("/users/ani@gmail.com").status_code == 200


async def test_onboarded_user_chat_uses_profile(client, brain, llm):
    uid = client.post("/onboard", json={"name": "Rahul", "email": "r@x.com"}).json()["user_id"]
    await brain.upsert_profile(uid, {"date_of_birth": "1995-08-15"})
    r = chat(client, uid, "s1", "What does my horoscope say about money?")
    assert r["context_used"] == ["user_profile", "astrology"]
    assert "Leo" in llm.requests[-1].system_prompt


def test_get_user_404_for_unseen(client):
    assert client.get("/users/nobody").status_code == 404


async def test_classify_uses_llm_router_not_rules(brain):
    """The chat flow must route through llm.classify: a message the rules would call GENERAL
    lands in FINANCE when the provider's router says so, and retrieval follows that category."""

    class FinanceRouter(MockLLM):
        async def classify(self, message: str) -> Category:
            return Category.FINANCE

    await brain.upsert_memory("u1", _cand("finance.goal", "finance", "save 20% of income"), "m1")
    await brain.upsert_memory("u1", _cand("career.goal", "career", "switch jobs"), "m2")

    app = create_app(brain=brain, llm=FinanceRouter(), settings=Settings(brain="memory", llm_provider="mock"))
    with TestClient(app) as c:
        r = chat(c, "u1", "s1", "Hello there!")  # rules: GENERAL; router: FINANCE
    assert r["context_used"] == ["finance.goal"]  # career memory excluded
    assert "save 20%" in r["response"]


def test_profile_endpoint_feeds_astrology_context(client, llm):
    r = client.post("/users", json={"user_id": "u1", "name": "Rahul", "date_of_birth": "1995-08-15"})
    assert r.status_code == 200 and r.json()["sun_sign"] == "Leo"
    r = chat(client, "u1", "s1", "What does my horoscope say about money?")  # astrology wins over finance
    assert r["context_used"] == ["user_profile", "astrology"]
    assert "Leo" in llm.requests[-1].system_prompt


def _cand(key: str, category: str, value: str) -> MemoryCandidate:
    return MemoryCandidate(key=key, category=category, type="goal", value=value, target_timeframe=None,
                           confidence=0.9, reason="test")


def test_rule_extractor_matches_prd_example():
    got = {c.key: (c.value, c.target_timeframe) for c in extract_by_rules(INTRO, date.today())}
    assert got == {
        "profile.name": ("Rahul", None),
        "profile.date_of_birth": ("1995-08-15", None),
        "profile.birth_place": ("Delhi", None),
        "career.goal": ("switch jobs", NEXT_YEAR),
    }
