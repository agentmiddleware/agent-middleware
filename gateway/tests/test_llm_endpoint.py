"""The OpenAI-compatible API root must not gain a duplicate version prefix."""

from types import SimpleNamespace

import httpx
import pytest

from app.core.config import Settings
from app.services.llm import LLMService


@pytest.mark.parametrize(
    "base_url, expected",
    [
        ("https://api.openai.com/v1", "https://api.openai.com/v1/chat/completions"),
        ("https://api.openai.com/v1/", "https://api.openai.com/v1/chat/completions"),
        (
            "https://synthetic.test/custom/v2",
            "https://synthetic.test/custom/v2/chat/completions",
        ),
    ],
)
@pytest.mark.anyio
async def test_openai_uses_configured_api_root(base_url, expected):
    seen = []

    def respond(request):
        seen.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": "synthetic"}, "finish_reason": "stop"}
                ],
                "model": "synthetic",
                "usage": {},
            },
        )

    service = LLMService()
    service.settings = SimpleNamespace(
        LLM_PROVIDER="openai",
        LLM_BASE_URL=base_url,
        LLM_API_KEY="synthetic",
        LLM_MODEL="synthetic",
        LLM_MAX_TOKENS=10,
        LLM_TEMPERATURE=0,
    )
    service._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        response = await service.chat(messages=[{"role": "user", "content": "hello"}])
        assert response.content == "synthetic"
        assert seen == [expected]
    finally:
        await service.close()


def test_default_llm_base_url_is_versioned_api_root():
    assert Settings.model_fields["LLM_BASE_URL"].default == "https://api.openai.com/v1"
