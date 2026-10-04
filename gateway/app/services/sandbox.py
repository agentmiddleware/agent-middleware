"""
Interactive Testing Sandboxes — Pillar 13
===========================================
Headless, dynamically changing puzzle environments for testing
agent-built tools' ability to generalize and adapt.

Inspired by ARC-AGI benchmarking: measure intelligence not by
static knowledge, but by the ability to learn new rules in
instruction-less environments.

Architecture:
  Builder Agent → POST /v1/sandbox/environments → SandboxEngine
    → Generates a headless environment with hidden rules
    → Agent-under-test interacts with the environment
    → Environment evaluates actions and returns feedback
    → Builder agent gets a generalization score

Environment Types:
1. PATTERN    — Discover input→output transformation rules
2. NAVIGATION — Navigate a state graph to reach a goal
3. API_MOCK   — Interact with a shifting mock API that changes schema
4. ADVERSARIAL — Environment actively tries to confuse the agent

Production wiring:
- Docker containers for isolated sandbox execution
- WebSocket streams for real-time environment state
- GPU scheduling for compute-intensive environments
"""

import uuid
import random
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol
from enum import Enum

logger = logging.getLogger(__name__)


class EnvironmentType(str, Enum):
    PATTERN = "pattern"
    NAVIGATION = "navigation"
    API_MOCK = "api_mock"
    ADVERSARIAL = "adversarial"


class Difficulty(str, Enum):
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"
    EXTREME = "extreme"


# ---------------------------------------------------------------------------
# Environment Models
# ---------------------------------------------------------------------------


@dataclass
class EnvironmentState:
    """Current state of a sandbox environment."""

    step: int = 0
    max_steps: int = 50
    grid: list[list[Any]] = field(default_factory=list)
    score: float = 0.0
    solved: bool = False
    feedback: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class SandboxEnvironment:
    """A headless testing environment."""

    env_id: str
    env_type: EnvironmentType
    difficulty: Difficulty
    description: str
    hidden_rules: list[str]  # The rules the agent must discover
    state: EnvironmentState
    action_history: list[dict] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
    final_score: float | None = None
    generalization_score: float | None = None
    # Wallet that created the environment; ``None`` when a bootstrap admin did,
    # which leaves it reachable by bootstrap admins only.
    owner_wallet_id: str | None = None


@dataclass
class ActionResult:
    """Result of an agent's action in the environment."""

    step: int
    action_accepted: bool
    state_changed: bool
    feedback: str
    reward: float
    done: bool
    new_state: dict


# ---------------------------------------------------------------------------
# Environment Generators
# ---------------------------------------------------------------------------


class PatternEnvironmentGenerator:
    """Generate pattern-discovery puzzles."""

    def generate(self, difficulty: Difficulty, seed: int) -> SandboxEnvironment:
        rng = random.Random(seed)
        size = {"easy": 3, "medium": 5, "hard": 7, "extreme": 9}[difficulty.value]

        # Generate a hidden transformation rule
        rules = []
        if difficulty in (Difficulty.EASY, Difficulty.MEDIUM):
            rule_type = rng.choice(["rotate", "mirror", "increment", "color_swap"])
            rules.append(f"Transform input grid by applying: {rule_type}")
        else:
            rules.append("Apply two-step transform: rotate then invert")
            rules.append("Odd rows are shifted right by 1")

        # Generate example pairs
        input_grid = [[rng.randint(0, 5) for _ in range(size)] for _ in range(size)]

        state = EnvironmentState(
            max_steps={
                "easy": 20,
                "medium": 30,
                "hard": 50,
                "extreme": 100,
            }[difficulty.value],
            grid=input_grid,
            metadata={
                "size": size,
                "examples_shown": 2,
                "rule_complexity": len(rules),
            },
        )

        return SandboxEnvironment(
            env_id=f"env-{uuid.uuid4().hex[:12]}",
            env_type=EnvironmentType.PATTERN,
            difficulty=difficulty,
            description=(
                f"Discover the hidden transformation rule. Grid size: {size}×{size}."
            ),
            hidden_rules=rules,
            state=state,
        )


class NavigationEnvironmentGenerator:
    """Generate state-graph navigation puzzles."""

    def generate(self, difficulty: Difficulty, seed: int) -> SandboxEnvironment:
        rng = random.Random(seed)
        num_nodes = {
            "easy": 5,
            "medium": 10,
            "hard": 20,
            "extreme": 40,
        }[difficulty.value]

        # Build a random directed graph
        nodes = list(range(num_nodes))
        edges = {}
        for node in nodes:
            num_edges = rng.randint(1, min(3, num_nodes - 1))
            targets = rng.sample(
                [n for n in nodes if n != node], min(num_edges, len(nodes) - 1)
            )
            edges[node] = targets

        goal = rng.choice(nodes[1:])  # Never start at goal
        rules = [
            f"Navigate from node 0 to node {goal}",
            "Some edges have hidden costs (discovered on traversal)",
        ]

        state = EnvironmentState(
            max_steps=num_nodes * 3,
            grid=[[0]],  # Current position
            metadata={
                "num_nodes": num_nodes,
                "goal_node": goal,
                "visible_edges": {str(k): v for k, v in edges.items()},
                "current_node": 0,
            },
        )

        return SandboxEnvironment(
            env_id=f"env-{uuid.uuid4().hex[:12]}",
            env_type=EnvironmentType.NAVIGATION,
            difficulty=difficulty,
            description=(
                f"Navigate a {num_nodes}-node graph to reach the goal. "
                "Edges may have hidden costs."
            ),
            hidden_rules=rules,
            state=state,
        )


class ApiMockEnvironmentGenerator:
    """Generate shifting mock API environments."""

    def generate(self, difficulty: Difficulty, seed: int) -> SandboxEnvironment:
        rng = random.Random(seed)
        num_endpoints = {
            "easy": 3,
            "medium": 6,
            "hard": 10,
            "extreme": 15,
        }[difficulty.value]

        endpoints = []
        for i in range(num_endpoints):
            endpoints.append(
                {
                    "path": f"/api/v1/resource_{i}",
                    "method": rng.choice(["GET", "POST", "PUT"]),
                    "schema_version": 1,
                    "fields": [f"field_{j}" for j in range(rng.randint(2, 5))],
                }
            )

        rules = [
            "API schema changes every 5 interactions",
            (
                f"Correct sequence: discover all {num_endpoints} endpoints, "
                "then call them in order"
            ),
        ]
        if difficulty in (Difficulty.HARD, Difficulty.EXTREME):
            rules.append(
                "Some endpoints return misleading 200 responses with wrong data"
            )

        state = EnvironmentState(
            max_steps=num_endpoints * 10,
            metadata={
                "num_endpoints": num_endpoints,
                "current_schema_version": 1,
                "interactions": 0,
                "endpoints_discovered": 0,
                "mock_api": endpoints,
            },
        )

        return SandboxEnvironment(
            env_id=f"env-{uuid.uuid4().hex[:12]}",
            env_type=EnvironmentType.API_MOCK,
            difficulty=difficulty,
            description=(
                f"Interact with a mock API that has {num_endpoints} shifting endpoints."
            ),
            hidden_rules=rules,
            state=state,
        )


class AdversarialEnvironmentGenerator:
    """Generate adversarial environments that try to confuse agents."""

    def generate(self, difficulty: Difficulty, seed: int) -> SandboxEnvironment:
        _rng = random.Random(seed)

        trap_count = {"easy": 1, "medium": 3, "hard": 5, "extreme": 8}[difficulty.value]

        rules = [
            f"Environment contains {trap_count} deceptive traps",
            "Correct action looks wrong; obvious action is a trap",
            "Agent must detect inconsistencies to avoid traps",
        ]

        state = EnvironmentState(
            max_steps=50,
            metadata={
                "traps_remaining": trap_count,
                "traps_triggered": 0,
                "correct_actions": 0,
                "deception_level": difficulty.value,
            },
        )

        return SandboxEnvironment(
            env_id=f"env-{uuid.uuid4().hex[:12]}",
            env_type=EnvironmentType.ADVERSARIAL,
            difficulty=difficulty,
            description=(
                f"Adversarial environment with {trap_count} deceptive traps. "
                "Think before you act."
            ),
            hidden_rules=rules,
            state=state,
        )


# ---------------------------------------------------------------------------
# Sandbox Engine
# ---------------------------------------------------------------------------


class EnvironmentGenerator(Protocol):
    """Structural type shared by all environment generators registered on
    SandboxEngine, so `self._generators` has a precise value type instead of
    inferring the join of unrelated classes as `object`."""

    def generate(self, difficulty: Difficulty, seed: int) -> SandboxEnvironment: ...


class SandboxEngine:
    """
    Headless environment manager for testing agent generalization.

    Operations:
    1. create_environment()  — Spin up a new puzzle
    2. submit_action()       — Agent interacts with the environment
    3. evaluate()            — Get final generalization score
    """

    def __init__(self):
        self._environments: dict[str, SandboxEnvironment] = {}
        self._generators: dict[EnvironmentType, EnvironmentGenerator] = {
            EnvironmentType.PATTERN: PatternEnvironmentGenerator(),
            EnvironmentType.NAVIGATION: NavigationEnvironmentGenerator(),
            EnvironmentType.API_MOCK: ApiMockEnvironmentGenerator(),
            EnvironmentType.ADVERSARIAL: AdversarialEnvironmentGenerator(),
        }

    async def create_environment(
        self,
        env_type: str = "pattern",
        difficulty: str = "medium",
        seed: int | None = None,
        owner_wallet_id: str | None = None,
    ) -> SandboxEnvironment:
        """Create a new sandbox environment."""
        env_type_enum = EnvironmentType(env_type)
        diff_enum = Difficulty(difficulty)
        actual_seed = seed if seed is not None else random.randint(0, 2**32)

        generator = self._generators[env_type_enum]
        env = generator.generate(diff_enum, actual_seed)
        env.owner_wallet_id = owner_wallet_id

        self._environments[env.env_id] = env
        logger.info(f"Created sandbox {env.env_id}: {env_type} / {difficulty}")
        return env

    async def submit_action(
        self,
        env_id: str,
        action: dict,
    ) -> ActionResult:
        """Submit an action to the environment and get feedback."""
        env = self._environments.get(env_id)
        if not env:
            raise ValueError(f"Environment {env_id} not found")
        if env.state.solved or env.state.step >= env.state.max_steps:
            return ActionResult(
                step=env.state.step,
                action_accepted=False,
                state_changed=False,
                feedback="Environment is complete. Call evaluate() for final score.",
                reward=0,
                done=True,
                new_state=self._safe_state(env),
            )

        env.state.step += 1

        # Evaluate action based on environment type
        reward, feedback, solved = self._evaluate_action(env, action)

        env.state.score += reward
        env.state.feedback = feedback
        env.state.solved = solved
        env.action_history.append(
            {
                "step": env.state.step,
                "action": action,
                "reward": reward,
                "feedback": feedback,
            }
        )

        done = solved or env.state.step >= env.state.max_steps

        return ActionResult(
            step=env.state.step,
            action_accepted=True,
            state_changed=True,
            feedback=feedback,
            reward=reward,
            done=done,
            new_state=self._safe_state(env),
        )

    def _evaluate_action(
        self, env: SandboxEnvironment, action: dict
    ) -> tuple[float, str, bool]:
        """Evaluate an action against the environment's hidden rules."""
        action_type = action.get("type", "unknown")
        action_value = action.get("value", "")

        if env.env_type == EnvironmentType.PATTERN:
            if action_type == "submit_transform":
                # Check if agent guessed the transformation rule
                guess = str(action_value).lower()
                for rule in env.hidden_rules:
                    if any(
                        keyword in guess
                        for keyword in [
                            "rotate",
                            "mirror",
                            "increment",
                            "invert",
                            "shift",
                        ]
                    ):
                        return 10.0, "Correct transformation detected!", True
                return -1.0, "Incorrect transformation. Try another approach.", False
            return 0.5, "Observation recorded.", False

        elif env.env_type == EnvironmentType.NAVIGATION:
            goal = env.state.metadata.get("goal_node", -1)
            current = env.state.metadata.get("current_node", 0)
            if action_type == "move":
                target = action_value
                edges = env.state.metadata.get("visible_edges", {})
                neighbors = edges.get(str(current), [])
                if target in neighbors:
                    env.state.metadata["current_node"] = target
                    if target == goal:
                        return 10.0, f"Reached goal node {goal}!", True
                    return 0.5, f"Moved to node {target}.", False
                return -0.5, "Invalid move. Node not reachable.", False
            return 0, "Unknown action type for navigation.", False

        elif env.env_type == EnvironmentType.API_MOCK:
            interactions = env.state.metadata.get("interactions", 0) + 1
            env.state.metadata["interactions"] = interactions
            if interactions % 5 == 0:
                env.state.metadata["current_schema_version"] += 1
                return 0, "API schema has changed! Re-discover endpoints.", False
            if action_type == "call_endpoint":
                env.state.metadata["endpoints_discovered"] = (
                    env.state.metadata.get("endpoints_discovered", 0) + 1
                )
                total = env.state.metadata["num_endpoints"]
                discovered = env.state.metadata["endpoints_discovered"]
                if discovered >= total:
                    return 10.0, "All endpoints discovered and called!", True
                return (
                    1.0,
                    (f"Endpoint called successfully. {discovered}/{total} discovered."),
                    False,
                )
            return 0, "Try calling an endpoint.", False

        elif env.env_type == EnvironmentType.ADVERSARIAL:
            traps = env.state.metadata.get("traps_remaining", 0)
            if action_type == "choose" and action_value == "careful":
                env.state.metadata["correct_actions"] = (
                    env.state.metadata.get("correct_actions", 0) + 1
                )
                if env.state.metadata["correct_actions"] >= traps + 3:
                    return 10.0, "Successfully navigated all traps!", True
                return 1.0, "Correct choice. Stay vigilant.", False
            elif action_type == "choose":
                env.state.metadata["traps_triggered"] = (
                    env.state.metadata.get("traps_triggered", 0) + 1
                )
                return -3.0, "TRAP! That was a deceptive option.", False
            return 0, "Make a choice.", False

        return 0, "Action processed.", False

    async def evaluate(self, env_id: str) -> dict:
        """Compute final generalization score for a completed environment."""
        env = self._environments.get(env_id)
        if not env:
            raise ValueError(f"Environment {env_id} not found")

        steps_used = env.state.step
        max_steps = env.state.max_steps
        efficiency = max(0, 1 - (steps_used / max_steps)) if max_steps > 0 else 0
        solved_bonus = 50 if env.state.solved else 0
        score_bonus = max(0, min(30, env.state.score * 3))
        gen_score = round(efficiency * 20 + solved_bonus + score_bonus, 1)

        env.final_score = env.state.score
        env.generalization_score = gen_score
        env.completed_at = datetime.now(timezone.utc)

        return {
            "env_id": env_id,
            "env_type": env.env_type.value,
            "difficulty": env.difficulty.value,
            "solved": env.state.solved,
            "steps_used": steps_used,
            "max_steps": max_steps,
            "efficiency": round(efficiency, 3),
            "raw_score": round(env.state.score, 2),
            "generalization_score": gen_score,
            "action_count": len(env.action_history),
        }

    async def get_environment(self, env_id: str) -> SandboxEnvironment | None:
        return self._environments.get(env_id)

    async def list_environments(self) -> list[SandboxEnvironment]:
        return list(self._environments.values())

    def _safe_state(self, env: SandboxEnvironment) -> dict:
        """Return state without revealing hidden rules."""
        return {
            "step": env.state.step,
            "max_steps": env.state.max_steps,
            "score": round(env.state.score, 2),
            "solved": env.state.solved,
            "feedback": env.state.feedback,
            "grid": env.state.grid,
            "metadata": {
                k: v
                for k, v in env.state.metadata.items()
                if k not in ("hidden_rules",)
            },
        }
