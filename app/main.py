"""FastAPI surface: app factory, schemas, routes, error handlers. No Neo4j or LLM calls here."""

import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .brain import BrainUnavailable, InMemoryBrain, Neo4jBrain, SharedBrain
from .chat import ChatService
from .config import Settings
from .llm import LLMError, LLMProvider, build_llm
from .session import SessionStore

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=4000)


class ChatResponse(BaseModel):
    response: str
    user_id: str
    session_id: str
    context_used: list[str]
    memory_updates: int
    degraded: bool


class UserUpsert(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)
    name: str | None = None
    date_of_birth: date | None = None
    time_of_birth: str | None = None
    birth_place: str | None = None
    preferred_language: str | None = None


class OnboardRequest(BaseModel):
    """Lightweight login: name required, email optional. user_id = normalized email or a slug of the name."""
    name: str = Field(min_length=1, max_length=128)
    email: str | None = Field(default=None, max_length=254)
    preferred_language: str | None = Field(default=None, max_length=64)


class OnboardResponse(BaseModel):
    user_id: str
    name: str
    email: str | None


class ProfileOut(BaseModel):
    user_id: str
    name: str | None
    date_of_birth: str | None
    time_of_birth: str | None
    birth_place: str | None
    preferred_language: str | None
    sun_sign: str | None


class MemoryOut(BaseModel):
    id: str
    key: str
    category: str
    type: str
    value: str
    target_timeframe: str | None
    confidence: float
    status: str
    source_message_id: str | None
    created_at: datetime
    updated_at: datetime


def create_app(brain: SharedBrain | None = None, llm: LLMProvider | None = None,
               settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.brain = brain or (
            InMemoryBrain() if settings.brain == "memory"
            else Neo4jBrain(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password))
        if isinstance(app.state.brain, Neo4jBrain):
            try:
                await app.state.brain.ensure_schema()
            except BrainUnavailable as e:
                log.warning("Neo4j unreachable at startup, serving degraded until it returns: %s", e)
        app.state.llm = llm or build_llm(settings)
        app.state.chat = ChatService(app.state.brain, app.state.llm, SessionStore(settings.recent_limit),
                                     settings.memory_limit, settings.min_confidence)
        log.info("ready brain=%s llm=%s", type(app.state.brain).__name__, type(app.state.llm).__name__)
        yield
        if close := getattr(app.state.brain, "close", None):
            await close()

    app = FastAPI(title="AstroChat", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(LLMError)
    async def _llm_error(_: Request, exc: LLMError):
        return JSONResponse({"detail": f"LLM unavailable: {exc}"}, status_code=503)

    @app.exception_handler(BrainUnavailable)
    async def _brain_error(_: Request, exc: BrainUnavailable):
        return JSONResponse({"detail": f"Shared Brain unavailable: {exc}"}, status_code=503)

    @app.post("/chat", response_model=ChatResponse)
    async def chat(req: ChatRequest, request: Request):
        result = await request.app.state.chat.chat(req.user_id, req.session_id, req.message)
        return ChatResponse(response=result.response, user_id=req.user_id, session_id=req.session_id,
                            context_used=result.context_used, memory_updates=result.memory_updates,
                            degraded=result.degraded)

    @app.post("/users", response_model=ProfileOut)
    async def upsert_user(req: UserUpsert, request: Request):
        fields = {k: (v.isoformat() if isinstance(v, date) else v)
                  for k, v in req.model_dump(exclude={"user_id"}, exclude_none=True).items()}
        if not fields:
            raise HTTPException(422, "no profile fields provided")
        profile = await request.app.state.brain.upsert_profile(req.user_id, fields)
        return ProfileOut(user_id=req.user_id, **vars(profile))

    @app.get("/users/{user_id}", response_model=ProfileOut)
    async def get_user(user_id: str, request: Request):
        profile = await request.app.state.brain.get_profile(user_id)
        if profile is None:
            raise HTTPException(404, "user not found")
        return ProfileOut(user_id=user_id, **vars(profile))

    @app.post("/onboard", response_model=OnboardResponse)
    async def onboard(req: OnboardRequest, request: Request):
        name = req.name.strip()
        email = (req.email or "").strip().lower() or None
        if email and ("@" not in email or "." not in email):
            raise HTTPException(422, "email must look like name@example.com")
        user_id = email or _slugify(name)
        around = (req.preferred_language or "").strip() or None
        await request.app.state.brain.onboard_user(user_id, name, email, around)
        return OnboardResponse(user_id=user_id, name=name, email=email)

    @app.get("/users/{user_id}/memories", response_model=list[MemoryOut])
    async def list_memories(user_id: str, request: Request):
        return [MemoryOut(**vars(m)) for m in await request.app.state.brain.list_memories(user_id)]

    app.mount("/", StaticFiles(directory=str(Path(__file__).resolve().parent / "static"), html=True), name="static")
    return app


def _slugify(name: str) -> str:
    """stable user_id from a name: 'Rahul Sharma' -> 'rahul-sharma'"""
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "user"


app = create_app()
