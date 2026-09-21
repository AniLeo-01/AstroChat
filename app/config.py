import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    brain: str = "neo4j"  # neo4j | memory
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "password"
    llm_provider: str = "anthropic"  # anthropic | openai | mock
    llm_model: str = ""  # required for openai (server-specific names); anthropic falls back to claude-opus-5
    classify_model: str = ""  # cheaper model for query routing; empty means use llm_model for everything
    llm_effort: str = "medium"  # effort / reasoning_effort for both providers; empty omits it
    openai_base_url: str = ""  # any OpenAI-compatible endpoint; empty means api.openai.com
    openai_api_key: str = ""
    recent_limit: int = 10
    memory_limit: int = 8
    min_confidence: float = 0.6

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ.get
        return cls(
            brain=env("BRAIN", cls.brain),
            neo4j_uri=env("NEO4J_URI", cls.neo4j_uri),
            neo4j_user=env("NEO4J_USER", cls.neo4j_user),
            neo4j_password=env("NEO4J_PASSWORD", cls.neo4j_password),
            llm_provider=env("LLM_PROVIDER", cls.llm_provider),
            llm_model=env("LLM_MODEL", cls.llm_model),
            classify_model=env("LLM_CLASSIFY_MODEL", cls.classify_model),
            llm_effort=env("LLM_EFFORT", cls.llm_effort),
            openai_base_url=env("OPENAI_BASE_URL", cls.openai_base_url),
            openai_api_key=env("OPENAI_API_KEY", cls.openai_api_key),
            recent_limit=int(env("RECENT_LIMIT", cls.recent_limit)),
            memory_limit=int(env("MEMORY_LIMIT", cls.memory_limit)),
            min_confidence=float(env("MIN_CONFIDENCE", cls.min_confidence)),
        )
