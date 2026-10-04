"""Authentication request/response schemas."""

from __future__ import annotations

from pydantic import BaseModel, Field


class TokenExchangeRequest(BaseModel):
    api_key: str = Field(..., min_length=8, description="Wallet API key to exchange")
    scopes: list[str] | None = Field(
        default=None,
        description="Requested scopes (default: billing:charge tool:invoke)",
    )


class TokenExchangeResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int
    scope: str


class TokenRefreshRequest(BaseModel):
    refresh_token: str


class TokenRefreshResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int


class TokenRevokeRequest(BaseModel):
    refresh_token: str
