"""Help with creating a coworker: AI-written job descriptions and AI-drawn avatars."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from backend.agents.assist import draw_avatars, write_job_description
from backend.api.deps import KeyStoreDep, SettingsDep
from backend.providers.base import ModelProvider, ProviderError
from backend.providers.registry import build_provider

router = APIRouter(prefix="/api/assist", tags=["assist"])


class ModelChoice(BaseModel):
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)


class JobDescriptionRequest(ModelChoice):
    name: str = Field(default="", max_length=64)
    role: str = Field(min_length=3, max_length=1_000)


class AvatarRequest(ModelChoice):
    description: str = Field(min_length=3, max_length=1_000)
    count: int = Field(default=3, ge=1, le=4)


def _provider(choice: ModelChoice, settings: SettingsDep, keys: KeyStoreDep) -> ModelProvider:
    config = settings.load_providers().get(choice.provider)
    if config is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Provider '{choice.provider}' is not configured.")
    try:
        return build_provider(choice.provider, config, choice.model, keys.get)
    except ProviderError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.post("/job-description")
async def job_description(payload: JobDescriptionRequest, settings: SettingsDep, keys: KeyStoreDep) -> dict[str, str]:
    """A suggested job description; the form shows it for the user to edit."""
    try:
        text = await write_job_description(_provider(payload, settings, keys), payload.name, payload.role)
    except ProviderError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return {"text": text}


@router.post("/avatar")
async def avatar(payload: AvatarRequest, settings: SettingsDep, keys: KeyStoreDep) -> dict[str, list[str]]:
    """Avatar drawings (sanitized SVG) to choose from; the chosen one is uploaded as a picture."""
    try:
        drawings = await draw_avatars(_provider(payload, settings, keys), payload.description, payload.count)
    except ProviderError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return {"candidates": drawings}
