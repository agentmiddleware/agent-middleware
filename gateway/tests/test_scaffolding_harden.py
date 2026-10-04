"""Tests for P0 scaffolding hardenings: false-success stubs and auth footguns."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from app.core.auth import get_auth_context
from app.core.config import get_settings
from app.core.runtime_mode import require_simulation
from app.schemas.media import (
    AspectRatio,
    CaptionStyle,
    GeneratedClip,
    Platform,
)
from app.services.agent_comms import (
    AgentMessage,
    AgentRegistry,
    DeliveryStatus,
    MessagePriority,
    MessageRouter,
    MessageType,
    RegisteredAgent,
)
from app.services.media_engine import PlatformDistributor


@pytest.fixture(autouse=True)
def _restore_sim_and_auth_settings():
    settings = get_settings()
    saved = {
        "SIMULATION_MODE_MEDIA_ENGINE": settings.SIMULATION_MODE_MEDIA_ENGINE,
        "SIMULATION_MODE_AGENT_COMMS": settings.SIMULATION_MODE_AGENT_COMMS,
        "DEBUG": settings.DEBUG,
        "VALID_API_KEYS": settings.VALID_API_KEYS,
        "ENVIRONMENT": settings.ENVIRONMENT,
    }
    yield
    for key, value in saved.items():
        setattr(settings, key, value)


def _sample_clip() -> GeneratedClip:
    return GeneratedClip(
        clip_id="clip-1",
        video_id="vid-1",
        hook_id="hook-1",
        aspect_ratio=AspectRatio.PORTRAIT_9_16,
        duration_seconds=15.0,
        download_url="https://example.com/clip.mp4",
        caption_style=CaptionStyle.KARAOKE,
        generated_at=datetime.now(timezone.utc),
    )


@pytest.mark.anyio
async def test_media_distribute_requires_simulation_and_never_published():
    settings = get_settings()
    settings.SIMULATION_MODE_MEDIA_ENGINE = True
    distributor = PlatformDistributor()
    clip = _sample_clip()
    result = await distributor.distribute(
        clip=clip,
        platform=Platform.TIKTOK,
        title="demo",
        hashtags=[],
    )
    assert result.status == "simulated"
    assert result.status != "published"

    settings.SIMULATION_MODE_MEDIA_ENGINE = False
    with pytest.raises(NotImplementedError) as exc:
        await distributor.distribute(
            clip=clip,
            platform=Platform.TIKTOK,
            title="demo",
            hashtags=[],
        )
    message = str(exc.value)
    assert "frozen proof-surface" in message
    assert "docs/PROOF_SURFACES.md" in message


@pytest.mark.anyio
async def test_agent_webhook_delivery_requires_simulation():
    settings = get_settings()
    settings.SIMULATION_MODE_AGENT_COMMS = True
    router = MessageRouter(AgentRegistry())
    message = AgentMessage(
        message_id="msg-1",
        from_agent="a",
        to_agent="b",
        message_type=MessageType.EVENT,
        priority=MessagePriority.NORMAL,
        subject="hello",
        body={"hello": "world"},
        delivery_status=DeliveryStatus.QUEUED,
    )
    recipient = RegisteredAgent(
        agent_id="b",
        name="bob",
        capabilities=[],
        webhook_url="https://example.com/hook",
        api_key="recipient-key-1",
    )
    assert await router._deliver_webhook(message, recipient) is True

    settings.SIMULATION_MODE_AGENT_COMMS = False
    with pytest.raises(NotImplementedError) as exc:
        await router._deliver_webhook(message, recipient)
    message = str(exc.value)
    assert "frozen proof-surface" in message
    assert "docs/PROOF_SURFACES.md" in message


@pytest.mark.anyio
async def test_debug_bootstrap_rejected_in_production_like_environment():
    settings = get_settings()
    settings.DEBUG = True
    settings.VALID_API_KEYS = ""
    settings.ENVIRONMENT = "production"

    with pytest.raises(HTTPException) as exc:
        await get_auth_context(api_key="any-long-enough-key")
    assert exc.value.status_code == 403


@pytest.mark.anyio
async def test_proof_surfaces_flag_defaults_false_for_local():
    """Local/dev imports now match the production-safe default."""
    settings = get_settings()
    assert settings.ENABLE_PROOF_SURFACES is False
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
        assert resp.status_code == 200


def test_require_simulation_media_names_frozen_scope():
    settings = get_settings()
    settings.SIMULATION_MODE_MEDIA_ENGINE = False
    with pytest.raises(NotImplementedError) as exc:
        require_simulation("media_engine")
    message = str(exc.value)
    assert "SIMULATION_MODE_MEDIA_ENGINE" in message
    assert "frozen proof-surface" in message
