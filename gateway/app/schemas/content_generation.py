"""HTTP schemas for /v1/content text generation."""

from typing import Any

from pydantic import BaseModel, Field


class ContentGenerateRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=32000)
    model: str | None = Field(
        None,
        description="Override default LLM model (OpenAI-compatible deployment name).",
    )


class ContentGenerateResponse(BaseModel):
    content_id: str
    text: str
    model: str
    prompt_hash: str | None = None
    output_hash: str | None = None
    provenance: dict[str, Any] | None = None


class ContentRecordResponse(BaseModel):
    content_id: str
    prompt_hash: str | None
    output_hash: str | None
    model: str | None
    provenance: dict[str, Any] = Field(default_factory=dict)
    text: str
    created_at: str
    updated_at: str | None = None
