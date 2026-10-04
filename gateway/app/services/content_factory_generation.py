"""
Durable LLM text generation for Content Factory (OpenAI-compatible chat API).
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

from app.core.time import utc_now

from ..core.config import get_settings
from ..core.runtime_mode import is_simulation
from ..db.database import get_session_factory, is_database_configured
from ..db.models import ContentFactoryGenerationModel

logger = logging.getLogger(__name__)

# Key under which the owning wallet is kept inside the stored provenance JSON.
# It is internal: ``get_record`` lifts it out into ``owner_wallet_id`` so the
# provenance a caller reads back is exactly the provenance it was issued.
_OWNER_WALLET_KEY = "owner_wallet_id"


def _sha256_canonical(data: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, default=str).encode()
    ).hexdigest()


class ContentGenerationStore:
    """Persist and fetch ``content_factory_generations`` rows."""

    @staticmethod
    def _require_db() -> None:
        if not is_database_configured():
            raise RuntimeError(
                "Content generation persistence requires DATABASE_URL to be set."
            )

    async def insert(
        self,
        *,
        content_id: str,
        prompt_hash: str,
        output_hash: str,
        model: str,
        provenance: dict[str, Any],
        output_text: str,
        owner_wallet_id: str | None = None,
    ) -> None:
        self._require_db()
        factory = get_session_factory()
        stored_provenance = dict(provenance)
        if owner_wallet_id is not None:
            stored_provenance[_OWNER_WALLET_KEY] = owner_wallet_id
        row = ContentFactoryGenerationModel(
            content_id=content_id,
            prompt_hash=prompt_hash,
            output_hash=output_hash,
            model=model,
            provenance_json=json.dumps(stored_provenance, sort_keys=True, default=str),
            output_text=output_text,
            created_at=utc_now(),
        )
        async with factory() as session:
            session.add(row)
            await session.commit()

    async def get_record(self, content_id: str) -> dict[str, Any] | None:
        self._require_db()
        factory = get_session_factory()
        async with factory() as session:
            row = await session.get(ContentFactoryGenerationModel, content_id)
        if row is None:
            return None
        provenance: dict[str, Any] = {}
        if row.provenance_json:
            try:
                provenance = json.loads(row.provenance_json)
            except json.JSONDecodeError:
                logger.warning("Invalid provenance_json for %s", content_id)
        if not isinstance(provenance, dict):
            provenance = {}
        # Rows written before owner tracking carry no owner and resolve to
        # ``None``, which only a bootstrap admin may read.
        owner = provenance.pop(_OWNER_WALLET_KEY, None)
        return {
            "content_id": row.content_id,
            "prompt_hash": row.prompt_hash,
            "output_hash": row.output_hash,
            "model": row.model,
            "provenance": provenance,
            "text": row.output_text or "",
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "owner_wallet_id": owner if isinstance(owner, str) else None,
        }


async def openai_compatible_chat_completion(
    prompt: str,
    model: str | None = None,
) -> tuple[str, str, str | None]:
    """POST /v1/chat/completions. Returns (text, model_used, request_id)."""
    settings = get_settings()
    api_key = (settings.LLM_API_KEY or "").strip()
    if not api_key:
        raise RuntimeError(
            "LLM_API_KEY is not configured; cannot call the model in real mode."
        )
    model_used = model or settings.LLM_MODEL
    base = (settings.LLM_BASE_URL or "").rstrip("/")
    if not base:
        raise RuntimeError("LLM_BASE_URL is not configured.")
    url = f"{base}/chat/completions"
    payload = {
        "model": model_used,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": min(2048, max(1, settings.LLM_MAX_TOKENS)),
        "temperature": float(settings.LLM_TEMPERATURE),
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    # Provider failures surface as RuntimeError so the caller's 502 + audit
    # path runs. Messages name only the failure class: the provider URL and
    # response body stay out of anything returned to the client.
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        logger.warning("LLM provider returned HTTP %s", status_code)
        raise RuntimeError(f"LLM provider returned HTTP {status_code}.") from exc
    except httpx.HTTPError as exc:
        logger.warning("LLM provider request failed: %s", type(exc).__name__)
        raise RuntimeError(
            f"LLM provider request failed ({type(exc).__name__})."
        ) from exc
    except ValueError as exc:
        logger.warning("LLM provider returned a non-JSON response")
        raise RuntimeError("LLM provider returned a non-JSON response.") from exc
    if not isinstance(data, dict):
        raise RuntimeError("LLM response is not a JSON object")
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("LLM response missing choices")
    msg = choices[0].get("message") or {}
    text = msg.get("content") or ""
    if not isinstance(text, str):
        text = str(text)
    rid = data.get("id")
    resp_model = data.get("model") or model_used
    return text, str(resp_model), rid if isinstance(rid, str) else None


async def generate_text(
    *,
    store: ContentGenerationStore,
    prompt: str,
    model: str | None = None,
    owner_wallet_id: str | None = None,
) -> dict[str, Any]:
    """
    Simulation: synthetic text, no DB, hashes/provenance null.

    Real: OpenAI-compatible completion, persist row with hashes + provenance JSON.
    ``owner_wallet_id`` is persisted with the row so reads can be scoped to
    the wallet that generated it (``None`` = bootstrap-admin only).
    """
    if is_simulation("content_factory"):
        cid = str(uuid.uuid4())
        snippet = prompt[:200] + ("…" if len(prompt) > 200 else "")
        text = f"[simulated] {snippet}"
        return {
            "content_id": cid,
            "text": text,
            "model": "synthetic",
            "prompt_hash": None,
            "output_hash": None,
            "provenance": None,
        }

    text, model_used, request_id = await openai_compatible_chat_completion(
        prompt, model=model
    )
    content_id = str(uuid.uuid4())
    prompt_hash = _sha256_canonical({"prompt": prompt})
    output_hash = _sha256_canonical({"text": text})
    now = datetime.now(timezone.utc).isoformat()
    provenance: dict[str, Any] = {
        "provider": get_settings().LLM_PROVIDER,
        "model": model_used,
        "request_id": request_id,
        "created_at": now,
        "excerpt": text[:200],
    }
    await store.insert(
        content_id=content_id,
        prompt_hash=prompt_hash,
        output_hash=output_hash,
        model=model_used,
        provenance=provenance,
        output_text=text,
        owner_wallet_id=owner_wallet_id,
    )
    return {
        "content_id": content_id,
        "text": text,
        "model": model_used,
        "prompt_hash": prompt_hash,
        "output_hash": output_hash,
        "provenance": provenance,
    }
