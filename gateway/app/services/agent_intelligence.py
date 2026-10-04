"""
Agent Intelligence Service
==========================
Autonomous decision-making, self-healing, and natural language
interfaces powered by LLM integration.
"""

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..core.durable_state import get_durable_state
from .llm import get_llm_service

logger = logging.getLogger(__name__)


def _agent_key(agent_id: str, owner_wallet_id: str | None) -> str:
    """Storage key for one agent's decisions and memory.

    ``agent_id`` is a caller-asserted label, not an identity, so the same id
    sent by two wallets must never share state. Wallet-scoped callers are
    namespaced under their wallet id; ``None`` (bootstrap admin) keeps the
    legacy un-namespaced key. Wallet ids are system-generated
    (``spn-``/``agt-``/``chd-`` + hex) and never contain ``:``, so one
    wallet's namespaced key can never equal another wallet's.
    """
    if owner_wallet_id is None:
        return agent_id
    return f"{owner_wallet_id}:{agent_id}"


@dataclass
class AgentDecision:
    """A decision made by the AI agent."""

    decision_id: str
    agent_id: str
    context: dict[str, Any]
    reasoning: str
    action: str
    confidence: float
    timestamp: datetime


@dataclass
class SelfHealResult:
    """Result of a self-healing operation."""

    heal_id: str
    issue: str
    diagnosis: str
    fix_applied: bool
    code_change: str | None
    verification: str | None
    success: bool
    error: str | None
    # Wallet that requested the heal; None for bootstrap-admin heals, which
    # only bootstrap admins may read back.
    owner_wallet_id: str | None = None


class AgentIntelligence:
    """
    AI-powered agent intelligence layer.

    Capabilities:
    - Autonomous decision-making based on context
    - Self-healing: diagnose and fix issues automatically
    - Natural language query interface
    - Agent memory and learning
    """

    def __init__(self):
        self._state = get_durable_state()
        self._llm = get_llm_service()
        self._decisions: dict[str, list[AgentDecision]] = {}
        self._heals: dict[str, SelfHealResult] = {}
        self._memory: dict[str, list[dict]] = {}

    async def initialize(self) -> None:
        """Load persisted state from durable store."""
        await self._state._ensure_ready()

        # Load decisions
        decisions_data = await self._state.load_json("agent_intelligence.decisions")
        if decisions_data:
            self._decisions = {
                key: [
                    item
                    if isinstance(item, AgentDecision)
                    else AgentDecision(
                        **{
                            **item,
                            "timestamp": datetime.fromisoformat(item["timestamp"]),
                        }
                    )
                    for item in items
                ]
                for key, items in decisions_data.items()
            }

        # Load heals
        heals_data = await self._state.load_json("agent_intelligence.heals")
        if heals_data:
            self._heals = {
                key: item
                if isinstance(item, SelfHealResult)
                else SelfHealResult(**item)
                for key, item in heals_data.items()
            }

        # Load memory
        memory_data = await self._state.load_json("agent_intelligence.memory")
        if memory_data:
            self._memory = memory_data

    async def _persist(self) -> None:
        """Save state to durable store."""
        await self._state.save_json("agent_intelligence.decisions", self._decisions)
        await self._state.save_json("agent_intelligence.heals", self._heals)
        await self._state.save_json("agent_intelligence.memory", self._memory)

    async def decide(
        self,
        agent_id: str,
        context: dict[str, Any],
        options: list[str] | None = None,
        owner_wallet_id: str | None = None,
    ) -> AgentDecision:
        """
        Make an autonomous decision based on context.

        The LLM analyzes the situation and recommends an action.
        """
        # Build context summary for the LLM
        context_json = json.dumps(context, indent=2, default=str)

        system_prompt = """You are an AI agent orchestrator. Analyze the current context
and make the best decision. Be concise and actionable."""

        if options:
            options_text = "\n".join(f"- {o}" for o in options)
            user_message = f"""Context:
{context_json}

Available actions:
{options_text}

Respond with:
1. reasoning: Why you chose this action
2. action: The chosen action (one of the options)
3. confidence: 0.0-1.0 confidence in this decision"""
        else:
            user_message = f"""Context:
{context_json}

Analyze this context and decide what action to take.
Respond with:
1. reasoning: Your analysis and reasoning
2. action: The recommended action
3. confidence: 0.0-1.0 confidence in this decision"""

        response = await self._llm.chat(
            messages=[{"role": "user", "content": user_message}],
            system=system_prompt,
        )

        # Parse the response
        try:
            result = json.loads(response.content)
            reasoning = result.get("reasoning", "")
            action = result.get("action", "unknown")
            confidence = float(result.get("confidence", 0.5))
        except (json.JSONDecodeError, ValueError):
            # Fallback: treat entire response as the action
            reasoning = response.content
            action = response.content.strip().split("\n")[0][:100]
            confidence = 0.5

        decision = AgentDecision(
            decision_id=str(uuid.uuid4())[:12],
            agent_id=agent_id,
            context=context,
            reasoning=reasoning,
            action=action,
            confidence=confidence,
            timestamp=datetime.now(timezone.utc),
        )

        # Store decision
        store_key = _agent_key(agent_id, owner_wallet_id)
        if store_key not in self._decisions:
            self._decisions[store_key] = []
        self._decisions[store_key].append(decision)

        # Keep only last 100 decisions per agent
        if len(self._decisions[store_key]) > 100:
            self._decisions[store_key] = self._decisions[store_key][-100:]

        await self._persist()

        logger.info(f"Agent {agent_id} decision: {action} (confidence: {confidence})")
        return decision

    async def diagnose_and_heal(
        self,
        issue: str,
        context: dict[str, Any],
        owner_wallet_id: str | None = None,
    ) -> SelfHealResult:
        """
        Automatically diagnose and attempt to heal an issue.

        The LLM analyzes error logs and suggests/produces fixes.
        """
        heal_id = str(uuid.uuid4())[:12]

        system_prompt = """You are an expert debugging AI. Analyze the issue and
either explain how to fix it or provide the exact code change needed.
Be precise and actionable."""

        context_json = json.dumps(context, indent=2, default=str)
        user_message = f"""Issue: {issue}

Context:
{context_json}

Analyze this issue and:
1. diagnosis: What's causing the problem
2. fix: The exact fix needed (code, config change, etc.)
3. verification: How to verify the fix works"""

        response = await self._llm.chat(
            messages=[{"role": "user", "content": user_message}],
            system=system_prompt,
        )

        try:
            result = json.loads(response.content)
            diagnosis = result.get("diagnosis", "")
            fix = result.get("fix", "")
        except json.JSONDecodeError:
            diagnosis = "Could not parse LLM response"
            fix = response.content

        result_obj = SelfHealResult(
            heal_id=heal_id,
            issue=issue,
            diagnosis=diagnosis,
            fix_applied=False,  # Human approval required for safety
            code_change=fix if fix else None,
            verification=None,
            success=False,
            error=None,
            owner_wallet_id=owner_wallet_id,
        )

        self._heals[heal_id] = result_obj
        await self._persist()

        logger.info(f"Self-heal {heal_id}: {diagnosis[:100]}")
        return result_obj

    async def query(
        self,
        question: str,
        data_context: dict[str, Any] | None = None,
    ) -> str:
        """
        Answer a natural language question about the system.

        The LLM uses the provided data context to answer.
        """
        system_prompt = """You are a helpful AI assistant for the Agent Middleware API.
Answer questions based on the provided context. Be concise and informative.
If you don't know something, say so."""

        messages = []

        if data_context:
            context_json = json.dumps(data_context, indent=2, default=str)
            messages.append(
                {
                    "role": "system",
                    "content": f"System data context:\n{context_json}",
                }
            )

        messages.append({"role": "user", "content": question})

        response = await self._llm.chat(messages=messages, system=system_prompt)
        return str(response.content)

    async def remember(
        self,
        agent_id: str,
        key: str,
        value: Any,
        owner_wallet_id: str | None = None,
    ) -> None:
        """Store a memory for an agent."""
        store_key = _agent_key(agent_id, owner_wallet_id)
        if store_key not in self._memory:
            self._memory[store_key] = []

        self._memory[store_key].append(
            {
                "key": key,
                "value": value,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )

        # Keep only last 1000 memories
        if len(self._memory[store_key]) > 1000:
            self._memory[store_key] = self._memory[store_key][-1000:]

        await self._persist()

    async def recall(
        self,
        agent_id: str,
        key: str | None = None,
        limit: int = 10,
        owner_wallet_id: str | None = None,
    ) -> list[dict]:
        """Recall memories for an agent."""
        store_key = _agent_key(agent_id, owner_wallet_id)
        if store_key not in self._memory:
            return []

        memories = self._memory[store_key]

        if key:
            memories = [m for m in memories if m.get("key") == key]

        return memories[-limit:]

    async def learn(
        self,
        agent_id: str,
        experience: dict[str, Any],
        owner_wallet_id: str | None = None,
    ) -> str:
        """
        Learn from an experience.

        The LLM extracts patterns and updates agent behavior.
        """
        system_prompt = """You are an AI learning system. Analyze this experience
and extract key patterns or lessons. Respond with a brief summary."""

        experience_json = json.dumps(experience, indent=2, default=str)
        response = await self._llm.chat(
            messages=[{"role": "user", "content": experience_json}],
            system=system_prompt,
        )

        # Store the learned pattern
        await self.remember(
            agent_id=agent_id,
            key="learned_pattern",
            value={
                "experience": experience,
                "insight": response.content,
                "learned_at": datetime.now(timezone.utc).isoformat(),
            },
            owner_wallet_id=owner_wallet_id,
        )

        return str(response.content)

    def get_decisions(
        self,
        agent_id: str,
        limit: int = 20,
        owner_wallet_id: str | None = None,
    ) -> list[AgentDecision]:
        """Get recent decisions for an agent."""
        store_key = _agent_key(agent_id, owner_wallet_id)
        if store_key not in self._decisions:
            return []
        return self._decisions[store_key][-limit:]

    def get_heal(self, heal_id: str) -> SelfHealResult | None:
        """Get a specific self-heal result.

        Unscoped lookup: callers must check ``owner_wallet_id`` before
        exposing the result to a non-admin caller.
        """
        return self._heals.get(heal_id)


# Singleton
_agent_intelligence: AgentIntelligence | None = None


def get_agent_intelligence() -> AgentIntelligence:
    """Get or create the agent intelligence singleton."""
    global _agent_intelligence
    if _agent_intelligence is None:
        _agent_intelligence = AgentIntelligence()
    return _agent_intelligence
