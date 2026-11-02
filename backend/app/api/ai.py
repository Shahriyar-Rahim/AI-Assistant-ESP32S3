from __future__ import annotations

import logging
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.dependencies import require_admin, require_user
from app.models import AuditLog, Conversation, ConversationMessage, ProviderConfig, User
from app.services.ai.providers import PROVIDERS, ProviderRequestError, ProviderUnavailable

logger = logging.getLogger(__name__)
router = APIRouter()


class ProviderUpdate(BaseModel):
    model: str = Field(min_length=1, max_length=120)
    system_prompt: str = Field(default="You are a helpful voice assistant.", max_length=4000)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    conversation_id: str | None = None


def _active_provider(request: Request) -> tuple[str, str, str]:
    with request.app.state.db_factory() as db:
        config = db.query(ProviderConfig).filter(ProviderConfig.active.is_(True)).one_or_none()
        if config:
            return config.provider, config.model, config.settings.get("system_prompt", "You are a helpful voice assistant.")
    provider_name = request.app.state.settings.default_ai_provider
    model = request.app.state.settings.ai_model
    if not provider_name:
        raise HTTPException(503, "configure an AI provider before requesting a response")
    if provider_name not in PROVIDERS:
        raise HTTPException(503, "configured AI provider is unsupported")
    return provider_name, model, "You are a helpful voice assistant."


@router.get("/ai/providers")
def provider_status(request: Request, _: User = Depends(require_user)) -> dict[str, object]:
    with request.app.state.db_factory() as db:
        selected = db.query(ProviderConfig).filter(ProviderConfig.active.is_(True)).one_or_none()
        active = selected.provider if selected else request.app.state.settings.default_ai_provider
        model = selected.model if selected else request.app.state.settings.ai_model
    return {
        "providers": [
            {"provider": name, "configured": name == "ollama" or bool({
                "openai": request.app.state.settings.openai_api_key,
                "gemini": request.app.state.settings.gemini_api_key,
                "ollama": "local",
            }[name]), "active": name == active, "model": model if name == active else None}
            for name in PROVIDERS
        ]
    }


@router.put("/ai/providers/{provider_name}")
def configure_provider(
    provider_name: str,
    body: ProviderUpdate,
    request: Request,
    _: User = Depends(require_admin),
) -> dict[str, str]:
    if provider_name not in PROVIDERS:
        raise HTTPException(404, "unsupported provider")
    with request.app.state.db_factory() as db:
        db.query(ProviderConfig).update({ProviderConfig.active: False})
        config = db.get(ProviderConfig, provider_name)
        if config is None:
            config = ProviderConfig(provider=provider_name, model=body.model, settings={})
            db.add(config)
        config.model = body.model
        config.settings = {"system_prompt": body.system_prompt}
        config.active = True
        db.commit()
    return {"provider": provider_name, "model": body.model, "status": "active"}


@router.get("/ai/providers/{provider_name}/health")
async def provider_health(
    provider_name: str,
    request: Request,
    _: User = Depends(require_user),
) -> dict[str, object]:
    if provider_name not in PROVIDERS:
        raise HTTPException(404, "unsupported provider")
    with request.app.state.db_factory() as db:
        config = db.get(ProviderConfig, provider_name)
    model = config.model if config else request.app.state.settings.ai_model
    if not model:
        raise HTTPException(422, "configure a model before checking provider health")
    try:
        healthy = await PROVIDERS[provider_name].health(model)
    except ProviderUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except ProviderRequestError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"provider": provider_name, "model": model, "healthy": healthy}


@router.post("/ai/chat")
async def chat(body: ChatRequest, request: Request, user: User = Depends(require_user)) -> dict[str, object]:
    provider_name, model, system_prompt = _active_provider(request)
    with request.app.state.db_factory() as db:
        if body.conversation_id:
            conversation = db.get(Conversation, body.conversation_id)
            if conversation is None or conversation.user_id != user.id:
                raise HTTPException(404, "conversation not found")
        else:
            conversation = Conversation(id=str(uuid4()), user_id=user.id, title=body.message[:80])
            db.add(conversation)
            db.commit()
            db.refresh(conversation)
        history = db.execute(
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation.id)
            .order_by(ConversationMessage.created_at.desc())
            .limit(20)
        ).scalars().all()
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend({"role": row.role, "content": row.content} for row in reversed(history))
        messages.append({"role": "user", "content": body.message})
        conversation_id = conversation.id
    try:
        result = await PROVIDERS[provider_name].complete(messages, model)
    except ProviderUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except ProviderRequestError as exc:
        logger.warning("AI provider request failed: %s", exc)
        raise HTTPException(502, str(exc)) from exc
    with request.app.state.db_factory() as db:
        db.add_all([
            ConversationMessage(
                conversation_id=conversation_id,
                role="user",
                content=body.message,
                provider=provider_name,
                model=model,
            ),
            ConversationMessage(
                conversation_id=conversation_id,
                role="assistant",
                content=result.text,
                provider=provider_name,
                model=result.model,
                usage=result.usage,
            ),
        ])
        db.add(AuditLog(
            event_type="ai.request",
            source="dashboard",
            action="assistant completion",
            result="success",
            metadata_json={"user_id": user.id, "provider": provider_name, "model": result.model, "usage": result.usage},
        ))
        db.commit()
    return {
        "conversation_id": conversation_id,
        "provider": provider_name,
        "model": result.model,
        "usage": result.usage,
        "response": result.text,
    }


@router.get("/ai/conversations/{conversation_id}")
def conversation_history(
    conversation_id: str,
    request: Request,
    user: User = Depends(require_user),
) -> dict[str, object]:
    with request.app.state.db_factory() as db:
        conversation = db.get(Conversation, conversation_id)
        if conversation is None or conversation.user_id != user.id:
            raise HTTPException(404, "conversation not found")
        messages = db.execute(
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation.id)
            .order_by(ConversationMessage.created_at)
        ).scalars().all()
        return {
            "conversation_id": conversation.id,
            "title": conversation.title,
            "messages": [{
                "role": message.role,
                "content": message.content,
                "provider": message.provider,
                "model": message.model,
                "created_at": message.created_at.isoformat(),
            } for message in messages],
        }
