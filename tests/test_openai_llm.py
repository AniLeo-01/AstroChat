"""OpenAICompatibleLLM against an in-process fake server (httpx.MockTransport): no network, no key."""

import json
from datetime import date

import httpx
import openai
import pytest

from app.config import Settings
from app.llm import AnthropicLLM, LLMError, MockLLM, OpenAICompatibleLLM, build_llm
from app.models import Category, ChatMessage, LLMRequest


def fake(handler, effort: str | None = "medium", model: str = "local-model",
         classify_model: str | None = None) -> OpenAICompatibleLLM:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenAICompatibleLLM(model, base_url="http://fake/v1", api_key="k", effort=effort,
                               classify_model=classify_model, http_client=client, max_retries=0)


def completion(content: str | None) -> httpx.Response:
    return httpx.Response(200, json={
        "id": "x", "object": "chat.completion", "created": 0, "model": "local-model",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
    })


async def test_generate_sends_system_then_turns():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(url=str(req.url), auth=req.headers.get("authorization"), body=json.loads(req.content))
        return completion("  Focus on steady steps.  ")

    request = LLMRequest("SYS", [ChatMessage("user", "hi"), ChatMessage("assistant", "hello"), ChatMessage("user", "career?")])
    assert await fake(handler).generate(request) == "Focus on steady steps."
    assert seen["url"] == "http://fake/v1/chat/completions" and seen["auth"] == "Bearer k"
    assert seen["body"]["model"] == "local-model" and seen["body"]["reasoning_effort"] == "medium"
    assert [(m["role"], m["content"]) for m in seen["body"]["messages"]] == [
        ("system", "SYS"), ("user", "hi"), ("assistant", "hello"), ("user", "career?")]


async def test_empty_effort_omits_reasoning_effort():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return completion("ok")

    await fake(handler, effort="").generate(LLMRequest("SYS", [ChatMessage("user", "hi")]))
    assert "reasoning_effort" not in seen["body"]


async def test_extract_requests_json_mode_and_tolerates_fences():
    payload = {"memories": [{"key": "career.goal", "category": "career", "type": "goal", "value": "switch jobs",
                             "target_timeframe": "2027", "confidence": 0.95, "reason": "said so"}]}

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        assert body["response_format"] == {"type": "json_object"} and body["reasoning_effort"] == "medium"
        assert "Respond with JSON only" in body["messages"][0]["content"] and "2027" in body["messages"][0]["content"]
        return completion("```json\n" + json.dumps(payload) + "\n```")

    cands = await fake(handler).extract_memories("I'm planning to switch jobs next year.", date(2026, 9, 21))
    assert [(c.key, c.value, c.target_timeframe, c.confidence) for c in cands] == [("career.goal", "switch jobs", "2027", 0.95)]


@pytest.mark.parametrize("content", ["not json", '{"memories": [{"key": "x"}]}', None])
async def test_extract_invalid_output_is_llm_error(content):
    with pytest.raises(LLMError):
        await fake(lambda req: completion(content)).extract_memories("x", date.today())


async def test_classify_requests_json_and_parses_category():
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        assert body["response_format"] == {"type": "json_object"} and body["reasoning_effort"] == "medium"
        assert "follow_up" in body["messages"][0]["content"] and "astrology" in body["messages"][0]["content"]
        return completion('{"category": "career"}')

    assert await fake(handler).classify("I need job advice.") is Category.CAREER


async def test_classify_uses_a_cheaper_model_than_generate():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content)["model"])
        return completion('{"category": "finance"}')

    llm = fake(handler, classify_model="cheap-router")
    await llm.classify("how is my 401k")
    await llm.generate(LLMRequest("SYS", [ChatMessage("user", "hi")]))
    assert seen == ["cheap-router", "local-model"]
    assert llm._classify_model == "cheap-router"


async def test_classify_falls_back_to_main_model_when_unset():
    assert fake(lambda req: completion('{"category": "career"}'))._classify_model == "local-model"


@pytest.mark.parametrize("content", ["not json", '{"category": "bogus"}', None])
async def test_classify_invalid_output_is_llm_error(content):
    with pytest.raises(LLMError):
        await fake(lambda req: completion(content)).classify("hello")


async def test_generate_empty_content_is_llm_error():
    with pytest.raises(LLMError, match="empty"):
        await fake(lambda req: completion(None)).generate(LLMRequest("SYS", [ChatMessage("user", "hi")]))


@pytest.mark.parametrize("status, expected", [(500, LLMError), (503, LLMError), (429, LLMError), (400, openai.BadRequestError)])
async def test_status_mapping(status, expected):
    llm = fake(lambda req: httpx.Response(status, json={"error": {"message": "boom", "type": "x"}}))
    with pytest.raises(expected):
        await llm.generate(LLMRequest("SYS", [ChatMessage("user", "hi")]))


async def test_connection_error_is_llm_error():
    def handler(req):
        raise httpx.ConnectError("refused")

    with pytest.raises(LLMError, match="unavailable"):
        await fake(handler).generate(LLMRequest("SYS", [ChatMessage("user", "hi")]))


def test_build_llm_selects_provider():
    assert isinstance(build_llm(Settings(llm_provider="mock")), MockLLM)
    assert isinstance(build_llm(Settings(llm_provider="anthropic")), AnthropicLLM)
    assert isinstance(build_llm(Settings(llm_provider="openai", llm_model="m", openai_base_url="http://x/v1")), OpenAICompatibleLLM)
    with pytest.raises(ValueError, match="LLM_MODEL"):
        build_llm(Settings(llm_provider="openai"))
    with pytest.raises(ValueError, match="unknown"):
        build_llm(Settings(llm_provider="gemini"))
