"""
PROOF SURFACE — frozen. Do not expand; see docs/PROOF_SURFACES.md.
AWI RAG Engine — Phase 9
=========================

Retrieval-Augmented Memory for AWI Sessions.

Based on arXiv:2506.10953v1 — NLP / Multimodality sections.
Enables semantic search over past AWI session states, helping agents
recall and build upon previous interactions.

Architecture:
1. Session completes → AWIRAGEngine.index_session()
2. Embeddings generated for session state
3. Stored in the in-memory proof backend (the ChromaDB dependency is suspended)
4. Agents query with natural language → AWIRAGEngine.search()
5. Returns relevant past sessions with similarity scores

Features:
- Semantic search over session histories
- Entity extraction from actions and page content
- Intent inference from action sequences
- Context augmentation for current sessions
"""

import logging
import math
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional
from uuid import uuid4

from app.core.time import utc_now

logger = logging.getLogger(__name__)


@dataclass
class SessionMemory:
    """A stored memory from an AWI session."""

    memory_id: str
    session_id: str
    session_type: str
    action_sequence: list[str]
    page_summaries: list[str]
    key_entities: list[str]
    user_intent: str
    raw_state: dict[str, Any]
    embedding: list[float]
    created_at: datetime
    accessed_at: datetime
    access_count: int = 0
    relevance_tags: list[str] = field(default_factory=list)
    # Wallet that owned the AWI session when it was indexed; None for an
    # ownerless (bootstrap-admin) session. Owner-scoped retrieval filters on it.
    owner_wallet_id: Optional[str] = None


@dataclass
class SearchResult:
    """A single search result."""

    memory_id: str
    session_id: str
    session_type: str
    user_intent: str
    action_sequence: list[str]
    key_entities: list[str]
    similarity_score: float
    created_at: datetime
    accessed_at: datetime
    access_count: int
    raw_state: Optional[dict[str, Any]] = None


class AWIRAGEngine:
    """
    Retrieval-Augmented Memory engine for AWI sessions.

    Stores embeddings of session states and enables semantic search
    to help agents recall relevant past interactions.

    Session Types:
    - shopping: E-commerce interactions (browse, search, cart, checkout)
    - form_filling: Form submission workflows
    - authentication: Login, logout, account management
    - content_consumption: Reading, watching, browsing
    - data_entry: Data input and processing tasks
    - research: Information gathering and comparison

    Storage Backend:
    - Supported: In-memory dict storage for the frozen proof surface
    - Suspended: ChromaDB while its Dependabot alert reports no patched release
    """

    SESSION_TYPE_PATTERNS: dict[str, list[str]] = {
        "shopping": [
            "shop",
            "store",
            "product",
            "cart",
            "basket",
            "checkout",
            "add.*cart",
            "buy",
            "purchase",
            "order",
            "item",
        ],
        "form_filling": [
            "form",
            "register",
            "signup",
            "sign-up",
            "application",
            "contact",
            "subscribe",
            "newsletter",
            "fill",
        ],
        "authentication": [
            "login",
            "signin",
            "sign-in",
            "logout",
            "signout",
            "sign-out",
            "password",
            "authenticate",
            "account",
            "session",
        ],
        "content_consumption": [
            "article",
            "blog",
            "news",
            "read",
            "watch",
            "video",
            "document",
            "pdf",
            "ebook",
            "content",
        ],
        "data_entry": [
            "spreadsheet",
            "database",
            "upload",
            "import",
            "export",
            "csv",
            "excel",
            "sheet",
            "entry",
            "input.*data",
        ],
        "research": [
            "search",
            "compare",
            "review",
            "rating",
            "specification",
            "features",
            "comparison",
            "vs",
            "alternative",
        ],
    }

    def __init__(
        self,
        vector_store_path: str = "./data/awi_vectors",
        embedding_model: str = "text-embedding-3-small",
        collection_name: str = "awi_sessions",
        embedding_dimension: int = 1536,
    ):
        """
        Initialize the RAG engine.

        Args:
            vector_store_path: Reserved path for a reviewed persistent backend.
            embedding_model: Model to use for embeddings.
            collection_name: Name of the collection in the vector store.
            embedding_dimension: Dimension of embedding vectors.
        """
        self._vector_store_path = vector_store_path
        self._embedding_model = embedding_model
        self._collection_name = collection_name
        self._embedding_dimension = embedding_dimension

        self._memories: dict[str, SessionMemory] = {}
        self._session_index: dict[str, list[str]] = {}
        self._type_index: dict[str, set[str]] = {}

        # Dormant legacy fields retained for proof-surface compatibility.
        # init_chroma() fails closed and leaves them unset.
        self._chroma_client: Any = None
        self._chroma_collection: Any = None
        self._use_chroma = False

    # ─────────────────────────────────────────────────────────────────────────
    # Session Indexing
    # ─────────────────────────────────────────────────────────────────────────

    async def index_session(
        self,
        session_id: str,
        session_type: str,
        action_history: list[dict[str, Any]],
        state_snapshots: list[dict[str, Any]],
        metadata: Optional[dict[str, Any]] = None,
        owner_wallet_id: Optional[str] = None,
    ) -> str:
        """
        Index a completed session for future retrieval.

        Args:
            session_id: Unique AWI session identifier.
            session_type: Type of session (shopping, form_filling, etc.).
            action_history: List of actions taken in the session.
            state_snapshots: State representations captured during session.
            metadata: Additional metadata to store.
            owner_wallet_id: Wallet owning the AWI session (None if ownerless).

        Returns:
            The memory_id for the indexed session.
        """
        memory_id = str(uuid4())

        action_sequence = [a.get("action", "") for a in action_history]

        page_summaries = []
        for snapshot in state_snapshots:
            if isinstance(snapshot, dict):
                summary = snapshot.get("summary", "")
                page_type = snapshot.get("page_type", "")
                text = snapshot.get("text", snapshot.get("main_content", ""))
                content = summary or page_type or text
                if content:
                    page_summaries.append(content[:500])
            elif isinstance(snapshot, str):
                page_summaries.append(snapshot[:500])

        key_entities = self._extract_entities(action_history, state_snapshots)

        inferred_type = self._infer_session_type(
            session_type, action_sequence, state_snapshots
        )
        final_type = session_type if session_type else inferred_type

        user_intent = self._infer_intent(
            action_sequence, state_snapshots, key_entities, final_type
        )

        text_for_embedding = self._prepare_embedding_text(
            final_type, action_sequence, page_summaries, key_entities, user_intent
        )

        embedding = await self._generate_embedding(text_for_embedding)

        memory = SessionMemory(
            memory_id=memory_id,
            session_id=session_id,
            session_type=final_type,
            action_sequence=action_sequence,
            page_summaries=page_summaries,
            key_entities=key_entities,
            user_intent=user_intent,
            raw_state={
                "action_history": action_history,
                "state_snapshots": state_snapshots,
                "metadata": metadata or {},
            },
            embedding=embedding,
            created_at=utc_now(),
            accessed_at=utc_now(),
            relevance_tags=[final_type]
            + self._generate_tags(action_sequence, key_entities),
            owner_wallet_id=owner_wallet_id,
        )

        self._memories[memory_id] = memory

        if session_id not in self._session_index:
            self._session_index[session_id] = []
        self._session_index[session_id].append(memory_id)

        if final_type not in self._type_index:
            self._type_index[final_type] = set()
        self._type_index[final_type].add(memory_id)

        if self._use_chroma and self._chroma_collection:
            try:
                await self._index_to_chroma(memory)
            except Exception as e:
                logger.warning(f"Failed to index to ChromaDB: {e}")

        logger.info(
            f"Indexed session {session_id} as memory {memory_id}, "
            f"type: {final_type}, entities: {len(key_entities)}"
        )

        return memory_id

    async def get_memory(self, memory_id: str) -> Optional[SessionMemory]:
        """Get a memory by ID."""
        return self._memories.get(memory_id)

    async def get_session_memories(self, session_id: str) -> list[SessionMemory]:
        """Get all memories for a session."""
        memory_ids = self._session_index.get(session_id, [])
        return [self._memories[mid] for mid in memory_ids if mid in self._memories]

    def _owned_ids(
        self,
        memory_ids: Iterable[str],
        owner_wallet_ids: Optional[Collection[Optional[str]]],
    ) -> set[str]:
        """Restrict ``memory_ids`` to memories whose owner is in the scope.

        Retrieval scopes by owner *before* scoring, so another tenant's
        memories are never compared, have their access counters bumped, or
        take a ``top_k`` slot that a later filter would empty. ``None`` means
        unscoped and is for bootstrap-admin and internal callers only.
        """
        if owner_wallet_ids is None:
            return set(memory_ids)
        return {
            mid
            for mid in memory_ids
            if mid in self._memories
            and self._memories[mid].owner_wallet_id in owner_wallet_ids
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Semantic Search
    # ─────────────────────────────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        session_type: Optional[str] = None,
        top_k: int = 5,
        similarity_threshold: float = 0.7,
        include_raw_state: bool = False,
        owner_wallet_ids: Optional[Collection[Optional[str]]] = None,
    ) -> list[SearchResult]:
        """
        Semantic search over session memories.

        Uses in-memory similarity search. The legacy ChromaDB branch remains
        dormant while init_chroma() fails closed.

        Args:
            query: Natural language query.
            session_type: Optional filter by session type.
            top_k: Number of results to return.
            similarity_threshold: Minimum similarity score (0.0-1.0).
            include_raw_state: Include full state data in results.
            owner_wallet_ids: Only search memories owned by these wallets
                (``None`` in the collection matches ownerless memories).
                ``None`` searches every tenant: bootstrap/internal use only.

        Returns:
            List of SearchResult objects sorted by similarity; empty for blank queries.
        """
        if not query.strip():
            return []

        query_embedding = await self._generate_embedding(query)

        # Dormant legacy branch; init_chroma() leaves _use_chroma false. The
        # Chroma index carries no owner, so scoped searches never use it.
        if self._use_chroma and self._chroma_collection and owner_wallet_ids is None:
            results = await self._search_chroma(query_embedding, session_type, top_k)
            # Add raw_state if requested
            if include_raw_state:
                for r in results:
                    memory = self._memories.get(r.memory_id)
                    if memory:
                        r.raw_state = memory.raw_state
            return results

        # Fallback to in-memory search
        candidate_ids = self._owned_ids(self._memories.keys(), owner_wallet_ids)

        if session_type:
            type_ids = self._type_index.get(session_type, set())
            candidate_ids &= type_ids

        results = []

        for memory_id in candidate_ids:
            memory = self._memories[memory_id]

            similarity = self._cosine_similarity(query_embedding, memory.embedding)

            if similarity >= similarity_threshold:
                memory.access_count += 1
                memory.accessed_at = utc_now()

                result = SearchResult(
                    memory_id=memory_id,
                    session_id=memory.session_id,
                    session_type=memory.session_type,
                    user_intent=memory.user_intent,
                    action_sequence=memory.action_sequence[:10],
                    key_entities=memory.key_entities[:20],
                    similarity_score=round(similarity, 4),
                    created_at=memory.created_at,
                    accessed_at=memory.accessed_at,
                    access_count=memory.access_count,
                    raw_state=memory.raw_state if include_raw_state else None,
                )

                results.append(result)

        results.sort(key=lambda x: x.similarity_score, reverse=True)

        return results[:top_k]

    async def search_by_entities(
        self,
        entities: list[str],
        session_type: Optional[str] = None,
        top_k: int = 5,
        owner_wallet_ids: Optional[Collection[Optional[str]]] = None,
    ) -> list[SearchResult]:
        """
        Search for sessions containing specific entities.

        Args:
            entities: List of entity names to search for.
            session_type: Optional filter by session type.
            top_k: Number of results to return.
            owner_wallet_ids: Owner scope, as for :meth:`search`.

        Returns:
            List of SearchResult objects.
        """
        entity_set = {e.lower() for e in entities}

        candidate_ids = self._owned_ids(self._memories.keys(), owner_wallet_ids)

        if session_type:
            type_ids = self._type_index.get(session_type, set())
            candidate_ids &= type_ids

        scored_memories = []

        for memory_id in candidate_ids:
            memory = self._memories[memory_id]

            memory_entity_set = {e.lower() for e in memory.key_entities}

            overlap = entity_set & memory_entity_set

            if overlap:
                score = len(overlap) / max(len(entity_set), len(memory_entity_set))
                score = round(score, 4)

                memory.access_count += 1
                memory.accessed_at = utc_now()

                result = SearchResult(
                    memory_id=memory_id,
                    session_id=memory.session_id,
                    session_type=memory.session_type,
                    user_intent=memory.user_intent,
                    action_sequence=memory.action_sequence[:10],
                    key_entities=memory.key_entities[:20],
                    similarity_score=score,
                    created_at=memory.created_at,
                    accessed_at=memory.accessed_at,
                    access_count=memory.access_count,
                )

                scored_memories.append(result)

        scored_memories.sort(key=lambda x: x.similarity_score, reverse=True)

        return scored_memories[:top_k]

    async def get_similar_sessions(
        self,
        session_id: str,
        top_k: int = 3,
        owner_wallet_ids: Optional[Collection[Optional[str]]] = None,
    ) -> list[SearchResult]:
        """
        Find sessions similar to a given session.

        Args:
            session_id: The session to find similar sessions for.
            top_k: Number of similar sessions to return.
            owner_wallet_ids: Owner scope, as for :meth:`search`.

        Returns:
            List of similar SearchResult objects.
        """
        memories = await self.get_session_memories(session_id)

        if not memories:
            return []

        query_embedding = memories[0].embedding

        own_ids = {m.memory_id for m in memories}
        scoped_ids = self._owned_ids(self._memories.keys(), owner_wallet_ids)
        other_ids = [
            mid for mid in self._memories if mid in scoped_ids and mid not in own_ids
        ]

        results = []

        for memory_id in other_ids:
            memory = self._memories[memory_id]
            similarity = self._cosine_similarity(query_embedding, memory.embedding)

            if similarity >= 0.5:
                memory.access_count += 1
                memory.accessed_at = utc_now()

                result = SearchResult(
                    memory_id=memory_id,
                    session_id=memory.session_id,
                    session_type=memory.session_type,
                    user_intent=memory.user_intent,
                    action_sequence=memory.action_sequence[:10],
                    key_entities=memory.key_entities[:20],
                    similarity_score=round(similarity, 4),
                    created_at=memory.created_at,
                    accessed_at=memory.accessed_at,
                    access_count=memory.access_count,
                )

                results.append(result)

        results.sort(key=lambda x: x.similarity_score, reverse=True)

        return results[:top_k]

    # ─────────────────────────────────────────────────────────────────────────
    # Context Augmentation
    # ─────────────────────────────────────────────────────────────────────────

    async def get_session_context(
        self,
        current_session_id: str,
        current_state: dict[str, Any],
        session_type: Optional[str] = None,
        top_k: int = 3,
        owner_wallet_ids: Optional[Collection[Optional[str]]] = None,
    ) -> dict[str, Any]:
        """
        Get relevant context from past sessions for the current session.

        This is the key method agents call to get "memories" before acting.

        Args:
            current_session_id: ID of the current session.
            current_state: Current session state (URL, goal, etc.).
            session_type: Inferred or specified session type.
            top_k: Number of similar sessions to consider.
            owner_wallet_ids: Owner scope, as for :meth:`search`. It also
                bounds the suggested actions and patterns derived from them.

        Returns:
            Dict with relevant past sessions and suggested actions.
        """
        inferred_type = self._classify_session(current_state)

        if session_type is None:
            session_type = inferred_type

        query = current_state.get("goal", "")

        if not query:
            query = f"{session_type} interaction"

        similar = await self.search(
            query=query,
            session_type=session_type,
            top_k=top_k,
            owner_wallet_ids=owner_wallet_ids,
        )

        context_memories = []

        for result in similar:
            memory = self._memories.get(result.memory_id)
            if memory:
                context_memories.append(
                    {
                        "memory_id": result.memory_id,
                        "session_id": result.session_id,
                        "session_type": result.session_type,
                        "actions_taken": memory.action_sequence,
                        "key_entities": memory.key_entities,
                        "user_intent": result.user_intent,
                        "relevance": result.similarity_score,
                        "age_minutes": int(
                            (utc_now() - result.created_at).total_seconds() / 60
                        ),
                    }
                )

        suggested_actions = self._suggest_actions(similar, current_state)

        common_patterns = self._extract_common_patterns(similar)

        return {
            "current_session_id": current_session_id,
            "current_session_type": session_type,
            "relevant_past_sessions": context_memories,
            "suggested_next_actions": suggested_actions,
            "common_patterns": common_patterns,
            "query_used": query,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Memory Management
    # ─────────────────────────────────────────────────────────────────────────

    async def delete_memory(self, memory_id: str) -> bool:
        """
        Delete a memory from the index.

        Args:
            memory_id: The memory to delete.

        Returns:
            True if deleted, False if not found.
        """
        memory = self._memories.get(memory_id)
        if not memory:
            return False

        del self._memories[memory_id]

        if memory.session_id in self._session_index:
            if memory_id in self._session_index[memory.session_id]:
                self._session_index[memory.session_id].remove(memory_id)

        if memory.session_type in self._type_index:
            self._type_index[memory.session_type].discard(memory_id)

        if self._chroma_collection:
            try:
                self._chroma_collection.delete(memory_id)
            except Exception as e:
                logger.warning(f"Failed to delete from ChromaDB: {e}")

        logger.info(f"Deleted memory {memory_id}")

        return True

    async def delete_session_memories(self, session_id: str) -> int:
        """
        Delete all memories for a session.

        Args:
            session_id: The session whose memories to delete.

        Returns:
            Number of memories deleted.
        """
        memory_ids = list(self._session_index.get(session_id, []))
        deleted = 0

        for memory_id in memory_ids:
            if await self.delete_memory(memory_id):
                deleted += 1

        if session_id in self._session_index:
            del self._session_index[session_id]

        return deleted

    async def get_stats(self) -> dict[str, Any]:
        """Get statistics about the memory store."""
        total_memories = len(self._memories)
        total_sessions = len(self._session_index)

        type_counts = {}
        for mem_type, memory_ids in self._type_index.items():
            type_counts[mem_type] = len(memory_ids)

        total_accesses = sum(m.access_count for m in self._memories.values())

        return {
            "total_memories": total_memories,
            "total_sessions": total_sessions,
            "type_counts": type_counts,
            "total_accesses": total_accesses,
            "embedding_model": self._embedding_model,
            "embedding_dimension": self._embedding_dimension,
            "vector_store": "chroma" if self._use_chroma else "in_memory",
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Private Methods
    # ─────────────────────────────────────────────────────────────────────────

    def _extract_entities(
        self,
        action_history: list[dict],
        state_snapshots: list[dict],
    ) -> list[str]:
        """Extract key entities from session."""
        entities = set()

        for action in action_history:
            params = action.get("parameters", {})

            for key in ["product", "item", "name", "title", "query", "category"]:
                if key in params:
                    value = params[key]
                    if isinstance(value, str) and len(value) > 1:
                        entities.add(value[:100])

            for key in ["products", "items", "selected"]:
                if key in params and isinstance(params[key], list):
                    for item in params[key]:
                        if isinstance(item, str):
                            entities.add(item[:100])
                        elif isinstance(item, dict):
                            for val in item.values():
                                if isinstance(val, str):
                                    entities.add(val[:100])

        for snapshot in state_snapshots:
            if isinstance(snapshot, dict):
                entities.update(
                    entity[:100]
                    for entity in snapshot.get("entities", [])
                    if isinstance(entity, str)
                )
                entities.update(
                    entity[:100]
                    for entity in snapshot.get("key_entities", [])
                    if isinstance(entity, str)
                )

        return sorted(list(entities))[:100]

    def _infer_session_type(
        self,
        provided_type: str,
        action_sequence: list[str],
        state_snapshots: list[dict],
    ) -> str:
        """Infer session type from content."""
        if provided_type and provided_type != "generic":
            return provided_type

        combined_text = " ".join(action_sequence).lower()

        for snapshot in state_snapshots:
            if isinstance(snapshot, dict):
                combined_text += " " + snapshot.get("summary", "").lower()
                combined_text += " " + snapshot.get("page_type", "").lower()

        for session_type, patterns in self.SESSION_TYPE_PATTERNS.items():
            for pattern in patterns:
                if pattern in combined_text:
                    return session_type

        return "generic"

    def _infer_intent(
        self,
        action_sequence: list[str],
        state_snapshots: list[dict],
        entities: list[str],
        session_type: str,
    ) -> str:
        """Infer user intent from session."""
        actions_set = set(action_sequence)

        if "checkout" in actions_set and "add_to_cart" in actions_set:
            return f"Complete purchase of {', '.join(entities[:3])}"
        elif "login" in actions_set or "logout" in actions_set:
            return "Account authentication task"
        elif "fill_form" in actions_set:
            return f"Submit form with {len(entities)} items of data"
        elif "search_and_sort" in actions_set:
            return f"Research and compare {', '.join(entities[:2]) if entities else 'products'}"
        elif "navigate_to" in actions_set:
            return "Browse and explore content"

        return f"{session_type.replace('_', ' ').title()} task"

    def _prepare_embedding_text(
        self,
        session_type: str,
        actions: list[str],
        summaries: list[str],
        entities: list[str],
        user_intent: str,
    ) -> str:
        """Prepare text for embedding generation."""
        parts = [
            f"Session type: {session_type}",
            f"User intent: {user_intent}",
            f"Actions: {', '.join(actions[:20])}",
            f"Content: {' '.join(summaries[:5])}",
            f"Entities: {', '.join(entities[:20])}",
        ]

        return " | ".join(parts)

    async def _generate_embedding(self, text: str) -> list[float]:
        """
        Generate embedding vector for text.

        In production, this calls OpenAI/Azure/etc embedding API.
        For now, generates a deterministic hash-based embedding.
        """
        from ..core.config import get_settings

        if (
            self._embedding_model.startswith("text-embedding")
            and get_settings().LLM_API_KEY
        ):
            try:
                return await self._generate_openai_embedding(text)
            except Exception as exc:
                # Name the failure only: the text and provider message may
                # carry caller content.
                logger.warning("awi_rag_embedding_fallback: %s", type(exc).__name__)

        return self._generate_mock_embedding(text)

    async def _generate_openai_embedding(self, text: str) -> list[float]:
        """Generate embedding using OpenAI API."""
        from openai import AsyncOpenAI
        from ..core.config import get_settings

        settings = get_settings()
        if not settings.LLM_API_KEY:
            raise ValueError("LLM_API_KEY is not configured")

        client = AsyncOpenAI(api_key=settings.LLM_API_KEY)
        try:
            response = await client.embeddings.create(
                model=self._embedding_model,
                input=text[:8192],
            )
            return response.data[0].embedding
        finally:
            await client.close()

    def _generate_mock_embedding(self, text: str) -> list[float]:
        """Generate deterministic mock embedding; empty text has zero similarity."""
        import struct

        if not text:
            return [0.0] * self._embedding_dimension

        text_bytes = text.encode("utf-8")

        embedding = []

        for i in range(self._embedding_dimension):
            start = (i * 4) % len(text_bytes)
            chunk = text_bytes[start : start + 4]

            if len(chunk) < 4:
                chunk = chunk + b"\x00" * (4 - len(chunk))

            try:
                value = struct.unpack(">I", chunk)[0]
            except Exception:
                value = sum(b << (j * 8) for j, b in enumerate(chunk))

            normalized = (value % 1000) / 1000.0
            embedding.append(normalized)

        return embedding

    def _cosine_similarity(self, a: list[float], b: list[float]) -> float:
        """Calculate cosine similarity between two vectors."""
        if len(a) != len(b):
            return 0.0

        dot_product = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))

        if norm_a == 0 or norm_b == 0:
            return 0.0

        return dot_product / (norm_a * norm_b)

    def _generate_tags(self, actions: list[str], entities: list[str]) -> list[str]:
        """Generate relevance tags from session content."""
        tags = []

        actions_lower = [a.lower() for a in actions]

        if any("checkout" in a for a in actions_lower):
            tags.append("purchase_intent")
        if any("search" in a for a in actions_lower):
            tags.append("searching")
        if any("login" in a or "auth" in a for a in actions_lower):
            tags.append("authenticated")

        if len(entities) > 5:
            tags.append("multi_item")

        return tags[:5]

    def _suggest_actions(
        self,
        similar_sessions: list[SearchResult],
        current_state: dict[str, Any],
    ) -> list[str]:
        """Suggest next actions based on similar sessions."""
        if not similar_sessions:
            return []

        action_counts: dict[str, float] = {}

        for session in similar_sessions:
            for action in session.action_sequence:
                weighted_count = session.similarity_score
                action_counts[action] = action_counts.get(action, 0) + weighted_count

        sorted_actions = sorted(
            action_counts.items(),
            key=lambda x: x[1],
            reverse=True,
        )

        suggested = []

        for action, score in sorted_actions[:5]:
            if action not in suggested:
                suggested.append(action)

        return suggested

    def _extract_common_patterns(
        self, similar_sessions: list[SearchResult]
    ) -> list[str]:
        """Extract common action patterns from similar sessions."""
        if len(similar_sessions) < 2:
            return []

        action_lists = [s.action_sequence for s in similar_sessions]

        common_patterns = []

        if all("search_and_sort" in actions for actions in action_lists):
            common_patterns.append("search before browse")
        if all("add_to_cart" in actions for actions in action_lists):
            common_patterns.append("add items to cart")
        if all("checkout" in actions for actions in action_lists):
            common_patterns.append("complete checkout")

        return common_patterns[:3]

    def _classify_session(self, state: dict[str, Any]) -> str:
        """Classify session type from current state."""
        url = state.get("url", "").lower()
        goal = state.get("goal", "").lower()
        combined = f"{url} {goal}"

        for session_type, patterns in self.SESSION_TYPE_PATTERNS.items():
            for pattern in patterns:
                if pattern in combined:
                    return session_type

        return "generic"

    def _build_searchable_text(self, memory: SessionMemory) -> str:
        """Build searchable text from a memory for vector storage."""
        parts = [
            f"Session type: {memory.session_type}",
            f"Intent: {memory.user_intent}",
            f"Entities: {', '.join(memory.key_entities)}",
            f"Actions: {' -> '.join(memory.action_sequence[:10])}",
            f"Summary: {' | '.join(memory.page_summaries[:5])}",
        ]
        return " | ".join(filter(None, parts))

    async def _search_chroma(
        self,
        query_embedding: list[float],
        session_type: Optional[str] = None,
        top_k: int = 5,
    ) -> list[SearchResult]:
        """Search using ChromaDB vector store."""
        if not self._chroma_collection:
            return []

        import json

        where_filter = {}
        if session_type:
            where_filter["session_type"] = session_type

        results = self._chroma_collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            where=where_filter if where_filter else None,
            include=["metadatas", "distances"],
        )

        search_results = []
        for i, memory_id in enumerate(results["ids"][0]):
            metadata = results["metadatas"][0][i]
            distance = results["distances"][0][i]

            similarity = max(0.0, 1.0 - distance / 2.0)

            memory = self._memories.get(memory_id)
            if not memory:
                continue

            memory.access_count += 1
            memory.accessed_at = utc_now()

            search_results.append(
                SearchResult(
                    memory_id=memory_id,
                    session_id=metadata.get("session_id", ""),
                    session_type=metadata.get("session_type", ""),
                    user_intent=metadata.get("user_intent", ""),
                    action_sequence=json.loads(metadata.get("action_sequence", "[]"))[
                        :10
                    ],
                    key_entities=json.loads(metadata.get("key_entities", "[]"))[:20],
                    similarity_score=round(similarity, 4),
                    created_at=datetime.fromisoformat(
                        metadata.get("created_at", utc_now().isoformat())
                    ),
                    accessed_at=memory.accessed_at,
                    access_count=memory.access_count,
                )
            )

        return search_results

    async def _index_to_chroma(self, memory: SessionMemory) -> None:
        """Index a memory to ChromaDB for persistent vector storage."""
        if not self._chroma_collection:
            return

        import json

        doc_text = self._build_searchable_text(memory)

        metadata = {
            "memory_id": memory.memory_id,
            "session_id": memory.session_id,
            "session_type": memory.session_type,
            "user_intent": memory.user_intent,
            "action_sequence": json.dumps(memory.action_sequence),
            "key_entities": json.dumps(memory.key_entities),
            "created_at": memory.created_at.isoformat(),
        }

        self._chroma_collection.upsert(
            ids=[memory.memory_id],
            embeddings=[memory.embedding],
            documents=[doc_text],
            metadatas=[metadata],
        )

        logger.debug(f"Indexed memory {memory.memory_id} to ChromaDB")

    async def init_chroma(self) -> bool:
        """Refuse to activate ChromaDB as a conservative advisory mitigation.

        The method remains for API compatibility with the frozen proof surface,
        but fails closed even when an ambient ChromaDB package is installed.
        """
        self._use_chroma = False
        self._chroma_client = None
        self._chroma_collection = None
        logger.warning(
            "ChromaDB persistence is disabled as a conservative mitigation for "
            "GHSA-f4j7-r4q5-qw2c. Using in-memory storage."
        )
        return False


_rag_engine: Optional[AWIRAGEngine] = None


def get_awi_rag_engine() -> AWIRAGEngine:
    """Get or create the AWIRAGEngine singleton."""
    global _rag_engine
    if _rag_engine is None:
        from ..core.config import get_settings

        settings = get_settings()

        _rag_engine = AWIRAGEngine(
            vector_store_path=settings.RAG_VECTOR_STORE_PATH,
            embedding_model=settings.RAG_EMBEDDING_MODEL,
        )

    return _rag_engine
