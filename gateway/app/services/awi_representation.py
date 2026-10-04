"""
AWI Progressive Representation Engine — Phase 7
==============================================
Based on arXiv:2506.10953v1 - "Build the web for agents, not agents for the web"

Provides progressive information transfer: agents request exactly the
representation they need (summary, embedding, low-res, etc.) instead of
full DOM/screenshots. This reduces bandwidth and processing time.
"""

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from ..schemas.awi import AWIRepresentationType


class ProgressiveRepresentationEngine:
    """
    Engine for generating progressive representations of web page state.

    Based on the paper's principle: "Progressive information transfer" -
    agents should request exactly the representation they need instead of
    receiving full DOM or screenshots every time.
    """

    def __init__(self):
        self._representation_cache: dict[str, dict[str, Any]] = {}

    async def generate_representation(
        self,
        session_id: str,
        representation_type: AWIRepresentationType,
        page_state: dict[str, Any],
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Generate a specific representation from page state.

        Args:
            session_id: The AWI session ID
            representation_type: Type of representation to generate
            page_state: Current page state (DOM, etc.)
            options: Optional parameters for representation generation

        Returns:
            Representation content and metadata
        """
        options = options or {}

        generator_map = {
            AWIRepresentationType.FULL_DOM: self._generate_full_dom,
            AWIRepresentationType.SUMMARY: self._generate_summary,
            AWIRepresentationType.EMBEDDING: self._generate_embedding,
            AWIRepresentationType.LOW_RES_SCREENSHOT: self._generate_low_res_screenshot,
            AWIRepresentationType.ACCESSIBILITY_TREE: self._generate_accessibility_tree,
            AWIRepresentationType.JSON_STRUCTURE: self._generate_json_structure,
            AWIRepresentationType.TEXT_EXTRACTION: self._generate_text_extraction,
        }

        generator = generator_map.get(representation_type)
        if not generator:
            return {
                "error": f"Unsupported representation type: {representation_type}",
                "representation_type": representation_type.value,
            }

        representation_id = f"rep-{uuid.uuid4().hex[:12]}"
        start_time = datetime.now(timezone.utc)

        content = await generator(page_state, options)

        generation_time = (datetime.now(timezone.utc) - start_time).total_seconds()

        result = {
            "representation_id": representation_id,
            "session_id": session_id,
            "representation_type": representation_type.value,
            "content": content,
            "metadata": {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "generation_time_ms": int(generation_time * 1000),
                "size_bytes": self._estimate_size(content),
                "options": options,
            },
        }

        cache_key = f"{session_id}:{representation_type.value}"
        self._representation_cache[cache_key] = result

        return result

    async def _generate_full_dom(
        self, page_state: dict[str, Any], options: dict[str, Any]
    ) -> dict[str, Any]:
        """Generate full DOM representation."""
        return {
            "type": "full_dom",
            "html": page_state.get("html", ""),
            "title": page_state.get("title", ""),
            "url": page_state.get("url", ""),
            "elements": page_state.get("elements", []),
        }

    async def _generate_summary(
        self, page_state: dict[str, Any], options: dict[str, Any]
    ) -> dict[str, Any]:
        """Generate a concise summary of the page."""
        max_length = options.get("max_length", 500)
        html = page_state.get("html", "")

        text_content = self._extract_text(html)
        if len(text_content) > max_length:
            text_content = text_content[:max_length] + "..."

        return {
            "type": "summary",
            "title": page_state.get("title", ""),
            "url": page_state.get("url", ""),
            "summary": text_content,
            "element_count": len(page_state.get("elements", [])),
            "forms_count": page_state.get("forms_count", 0),
            "links_count": page_state.get("links_count", 0),
        }

    async def _generate_embedding(
        self, page_state: dict[str, Any], options: dict[str, Any]
    ) -> dict[str, Any]:
        """Generate a simulated embedding of the page content.

        Not a semantic model: each 10-character chunk maps to a float in
        [0, 1) taken from its SHA-256 digest, so the vector is stable across
        processes (unlike Python's per-process salted hash()) and labelled
        ``simulated``.
        """
        embedding_model = options.get("embedding_model", "default")
        html = page_state.get("html", "")

        text_content = self._extract_text(html)

        mock_embedding = [
            int.from_bytes(
                hashlib.sha256(text_content[i : i + 10].encode("utf-8")).digest()[:8],
                "big",
            )
            / 2**64
            for i in range(0, min(len(text_content), 1536), 10)
        ]

        return {
            "type": "embedding",
            "model": embedding_model,
            "simulated": True,
            "dimension": len(mock_embedding),
            "vector": mock_embedding[:768]
            if len(mock_embedding) > 768
            else mock_embedding,
            "text_length": len(text_content),
        }

    async def _generate_low_res_screenshot(
        self, page_state: dict[str, Any], options: dict[str, Any]
    ) -> dict[str, Any]:
        """Generate a low-resolution representation."""
        quality = options.get("quality", "low")
        max_width = options.get("max_width", 640)

        return {
            "type": "low_res_screenshot",
            "format": "base64_png",
            "quality": quality,
            "max_width": max_width,
            "placeholder": "[Screenshot data - base64 encoded]",
            "size_hint": "small",
            "text_overlay": self._extract_text(page_state.get("html", ""))[:200],
        }

    async def _generate_accessibility_tree(
        self, page_state: dict[str, Any], options: dict[str, Any]
    ) -> dict[str, Any]:
        """Generate an accessibility tree representation."""
        elements = page_state.get("elements", [])

        accessible_elements = []
        for el in elements:
            accessible_elements.append(
                {
                    "role": el.get("role", "unknown"),
                    "name": el.get("name", el.get("text", "")),
                    "state": el.get("state", {}),
                    "actions": el.get("actions", []),
                }
            )

        return {
            "type": "accessibility_tree",
            "element_count": len(accessible_elements),
            "tree": accessible_elements[:100],
            "focused_element": page_state.get("focused_element"),
        }

    async def _generate_json_structure(
        self, page_state: dict[str, Any], options: dict[str, Any]
    ) -> dict[str, Any]:
        """Generate a JSON-serializable structure of the page."""
        max_depth = options.get("max_depth", 5)

        def sanitize(obj: Any, depth: int = 0) -> Any:
            if depth > max_depth:
                return "..."
            if isinstance(obj, dict):
                return {k: sanitize(v, depth + 1) for k, v in list(obj.items())[:20]}
            if isinstance(obj, list):
                return [sanitize(item, depth + 1) for item in obj[:50]]
            if isinstance(obj, str) and len(obj) > 1000:
                return obj[:1000] + "..."
            return obj

        return {
            "type": "json_structure",
            "structure": sanitize(page_state),
            "depth": max_depth,
        }

    async def _generate_text_extraction(
        self, page_state: dict[str, Any], options: dict[str, Any]
    ) -> dict[str, Any]:
        """Extract plain text from the page."""
        html = page_state.get("html", "")
        text = self._extract_text(html)

        max_length = options.get("max_length", 10000)
        if len(text) > max_length:
            text = text[:max_length] + "..."

        return {
            "type": "text_extraction",
            "text": text,
            "character_count": len(text),
            "word_count": len(text.split()),
            "url": page_state.get("url", ""),
        }

    def _extract_text(self, html: str) -> str:
        """Extract readable text from HTML."""
        import re

        text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _estimate_size(self, content: Any) -> int:
        """Estimate the byte size of content."""
        if isinstance(content, str):
            return len(content.encode("utf-8"))
        return len(json.dumps(content).encode("utf-8"))

    def get_cached_representation(
        self, session_id: str, representation_type: AWIRepresentationType
    ) -> dict[str, Any] | None:
        """Get a cached representation if available."""
        cache_key = f"{session_id}:{representation_type.value}"
        return self._representation_cache.get(cache_key)


_awi_representation: ProgressiveRepresentationEngine | None = None


def get_awi_representation() -> ProgressiveRepresentationEngine:
    """Get singleton representation engine instance."""
    global _awi_representation
    if _awi_representation is None:
        _awi_representation = ProgressiveRepresentationEngine()
    return _awi_representation
