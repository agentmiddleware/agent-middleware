"""
PROOF SURFACE — frozen. Do not expand; see docs/PROOF_SURFACES.md.
LLM Integration Service for AI Agent Intelligence
==================================================
Provides unified access to OpenAI, Azure OpenAI, Anthropic, and Ollama.
Enables autonomous decision-making, natural language understanding,
and self-healing capabilities for agents.
"""

import json
import logging
from typing import Any
from dataclasses import dataclass

import httpx

from ..core.config import get_settings
from ..core.resilience import retry_with_backoff, CircuitBreaker

logger = logging.getLogger(__name__)


@dataclass
class LLMResponse:
    """Structured response from LLM provider."""

    content: str
    model: str
    usage: dict[str, int]
    finish_reason: str | None = None


class LLMService:
    """
    Unified LLM client supporting multiple providers.

    Providers:
    - openai: Standard OpenAI API
    - azure: Azure OpenAI Service
    - anthropic: Anthropic Claude (via OpenAI-compatible API)
    - ollama: Local Ollama server
    """

    def __init__(self):
        self.settings = get_settings()
        self._client: httpx.AsyncClient | None = None
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=5,
            recovery_timeout=30.0,
        )

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=120.0)
        return self._client

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None

    def _get_provider(self) -> str:
        return str(self.settings.LLM_PROVIDER.lower().strip())

    def _build_headers(self) -> dict[str, str]:
        """Build request headers based on provider."""
        provider = self._get_provider()

        if provider in ("openai", "azure", "anthropic"):
            return {
                "Authorization": f"Bearer {self.settings.LLM_API_KEY}",
                "Content-Type": "application/json",
            }

        if provider == "ollama":
            return {"Content-Type": "application/json"}

        return {"Content-Type": "application/json"}

    def _build_endpoint(self, endpoint: str = "/chat/completions") -> str:
        """Build the API endpoint URL."""
        provider = self._get_provider()

        if provider == "openai":
            # LLM_BASE_URL is the API root, including any version prefix.
            # This matches the content-generation client and the /v1 default.
            base = self.settings.LLM_BASE_URL.rstrip("/")
            return f"{base}/{endpoint.lstrip('/')}"

        if provider == "azure":
            base = self.settings.AZURE_OPENAI_ENDPOINT.rstrip("/")
            deployment = self.settings.AZURE_OPENAI_DEPLOYMENT
            return (
                f"{base}/openai/deployments/{deployment}"
                f"{endpoint}?api-version=2024-02-01"
            )

        if provider == "anthropic":
            base = self.settings.LLM_BASE_URL.rstrip("/")
            return f"{base}/v1{endpoint}"

        if provider == "ollama":
            base = self.settings.OLLAMA_BASE_URL.rstrip("/")
            return f"{base}/api{endpoint}"

        base = self.settings.LLM_BASE_URL.rstrip("/")
        return f"{base}/v1{endpoint}"

    def _build_payload(
        self,
        messages: list[dict[str, str]],
        system: str | None = None,
        tools: list[dict] | None = None,
        tool_choice: str | None = None,
    ) -> dict[str, Any]:
        """Build request payload based on provider."""
        provider = self._get_provider()

        if system:
            messages = [{"role": "system", "content": system}] + messages

        if provider == "ollama":
            # Ollama uses a different format
            content = "\n".join(f"{m['role']}: {m['content']}" for m in messages)
            return {
                "model": self.settings.OLLAMA_MODEL,
                "prompt": content,
                "stream": False,
            }

        if provider == "anthropic":
            # Anthropic format
            anthropic_messages = []
            for m in messages:
                if m["role"] == "system":
                    continue
                anthropic_messages.append(
                    {
                        "role": m["role"],
                        "content": m["content"],
                    }
                )

            payload: dict[str, Any] = {
                "model": self.settings.LLM_MODEL,
                "messages": anthropic_messages,
                "max_tokens": self.settings.LLM_MAX_TOKENS,
                "temperature": self.settings.LLM_TEMPERATURE,
            }
            return payload

        # OpenAI / Azure format
        payload = {
            "model": self.settings.LLM_MODEL,
            "messages": messages,
            "max_tokens": self.settings.LLM_MAX_TOKENS,
            "temperature": self.settings.LLM_TEMPERATURE,
        }

        if tools:
            payload["tools"] = tools
            if tool_choice:
                payload["tool_choice"] = tool_choice

        return payload

    def _parse_response(self, response: dict, provider: str) -> LLMResponse:
        """Parse provider-specific response into LLMResponse."""
        if provider == "ollama":
            return LLMResponse(
                content=response.get("response", ""),
                model=response.get("model", self.settings.OLLAMA_MODEL),
                usage=response.get("usage", {}),
                finish_reason=response.get("done_reason"),
            )

        if provider == "anthropic":
            content = response["content"][0]["text"] if response.get("content") else ""
            return LLMResponse(
                content=content,
                model=response.get("model", self.settings.LLM_MODEL),
                usage=response.get("usage", {}),
                finish_reason=response.get("stop_reason"),
            )

        # OpenAI / Azure
        choice = response["choices"][0]
        content = choice.get("message", {}).get("content", "")

        # Handle tool calls
        if choice.get("finish_reason") == "tool_calls":
            tool_calls = choice.get("message", {}).get("tool_calls", [])
            if tool_calls:
                content = json.dumps({"tool_calls": tool_calls})

        return LLMResponse(
            content=content,
            model=response.get("model", self.settings.LLM_MODEL),
            usage=response.get("usage", {}),
            finish_reason=choice.get("finish_reason"),
        )

    @retry_with_backoff(max_attempts=3, base_delay=1.0, max_delay=10.0)
    async def chat(
        self,
        messages: list[dict[str, str]],
        system: str | None = None,
        tools: list[dict] | None = None,
        tool_choice: str | None = None,
    ) -> LLMResponse:
        """
        Send a chat completion request to the LLM.

        Args:
            messages: List of message dicts with 'role' and 'content'
            system: System prompt to prepend
            tools: Optional tool definitions for function calling
            tool_choice: Force a specific tool ('auto', 'none', or tool name)

        Returns:
            LLMResponse with content and metadata

        Retries with exponential backoff on transient failures.
        """
        if not self._circuit_breaker.is_allowed():
            logger.warning(
                "llm_circuit_breaker_open state=%s", self._circuit_breaker.state
            )
            raise Exception("LLM circuit breaker is open")

        if not self.settings.LLM_API_KEY and self._get_provider() not in ("ollama",):
            logger.warning("LLM_API_KEY not set, returning mock response")
            return LLMResponse(
                content="Mock response: API key not configured",
                model="mock",
                usage={"prompt_tokens": 0, "completion_tokens": 0},
            )

        payload = self._build_payload(messages, system, tools, tool_choice)
        endpoint = self._build_endpoint()
        headers = self._build_headers()

        try:
            response = await self.client.post(endpoint, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()

            self._circuit_breaker.record_success()
            logger.debug("llm_response_received model=%s", data.get("model", "unknown"))

            return self._parse_response(data, self._get_provider())

        except httpx.TimeoutException as e:
            self._circuit_breaker.record_failure()
            logger.warning("llm_timeout error=%s", e)
            raise
        except httpx.HTTPStatusError as e:
            self._circuit_breaker.record_failure()
            logger.error("llm_http_error status=%s error=%s", e.response.status_code, e)
            raise
        except Exception as e:
            self._circuit_breaker.record_failure()
            logger.exception("llm_request_failed error=%s", e)
            raise

    async def generate(
        self,
        prompt: str,
        system: str | None = None,
    ) -> str:
        """Simple text generation."""
        messages = [{"role": "user", "content": prompt}]
        response = await self.chat(messages, system)
        return response.content

    async def analyze(
        self,
        data: str,
        instruction: str,
        system: str | None = None,
    ) -> dict[str, Any]:
        """
        Analyze structured data with JSON output.

        Args:
            data: The data to analyze
            instruction: What to extract/analyze
            system: Optional system prompt

        Returns:
            Parsed JSON response
        """
        messages = [
            {
                "role": "user",
                "content": (
                    f"Data:\n{data}\n\n"
                    f"Task: {instruction}\n\n"
                    "Respond with valid JSON only."
                ),
            }
        ]

        response = await self.chat(messages, system)

        try:
            result: dict[str, Any] = json.loads(response.content)
            return result
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse JSON from LLM: {response.content[:200]}")
            return {"error": "parse_failed", "raw": response.content}


# Singleton instance
_llm_service: LLMService | None = None


def get_llm_service() -> LLMService:
    """Get or create the LLM service singleton."""
    global _llm_service
    if _llm_service is None:
        _llm_service = LLMService()
    return _llm_service


async def close_llm_service() -> None:
    """Close the LLM service."""
    global _llm_service
    if _llm_service:
        await _llm_service.close()
        _llm_service = None
