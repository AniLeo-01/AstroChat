import pytest
from fastapi.testclient import TestClient

from app.brain import InMemoryBrain
from app.config import Settings
from app.llm import MockLLM
from app.main import create_app


@pytest.fixture
def brain() -> InMemoryBrain:
    return InMemoryBrain()


@pytest.fixture
def llm() -> MockLLM:
    return MockLLM()


@pytest.fixture
def client(brain, llm):
    app = create_app(brain=brain, llm=llm, settings=Settings(brain="memory", llm_provider="mock"))
    with TestClient(app) as c:
        yield c

