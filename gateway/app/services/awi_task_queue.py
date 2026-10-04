"""
AWI Task Queue — Phase 7
=========================
Agentic task queue with concurrency limits and human pause/steer capabilities.

Based on arXiv:2506.10953v1 - "Build the web for agents, not agents for the web"

Provides:
- Priority-based task queuing
- Concurrency limits per agent/tenant
- Human pause/steer capabilities
- Resource spike prevention
- Task state persistence and recovery
"""

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

from ..core.durable_state import get_durable_state
from ..schemas.awi import (
    AWITask,
    AWITaskCreate,
    AWITaskQueueStatus,
    AWITaskStatus,
)


class AWITaskQueue:
    """
    Agentic task queue with concurrency limits and human intervention.

    Based on the paper's principle: "Agentic task queues + safety" -
    prevent resource spikes and give humans pause/steer capabilities.
    """

    def __init__(self, max_concurrent_tasks: int = 10):
        self.max_concurrent_tasks = max_concurrent_tasks
        self._tasks: dict[str, AWITask] = {}
        self._pending_queue: list[str] = []
        self._running_tasks: dict[str, asyncio.Task] = {}
        self._state = get_durable_state()
        self._global_pause = False
        self._pause_reason: str | None = None

    @staticmethod
    def _task_key(task_id: str) -> str:
        return f"awi.tasks.{task_id}"

    @staticmethod
    def _task_id_from_key(key: str) -> str:
        return key.removeprefix("awi.tasks.")

    async def _save_task(self, task_id: str) -> None:
        task = self._tasks.get(task_id)
        if task:
            await self._state.save_json(
                self._task_key(task_id), task.model_dump(mode="json")
            )

    async def _load_task(self, task_id: str) -> AWITask | None:
        task = self._tasks.get(task_id)
        if task:
            return task

        payload = await self._state.load_json(self._task_key(task_id))
        if not isinstance(payload, dict):
            return None

        task = AWITask.model_validate(payload)
        self._tasks[task_id] = task
        if task.status == AWITaskStatus.PENDING:
            self._insert_into_priority_queue(task_id)
        return task

    def _rebuild_pending_queue(self) -> None:
        pending = [
            task
            for task in self._tasks.values()
            if task.status == AWITaskStatus.PENDING
        ]
        pending.sort(key=lambda task: (task.priority, task.created_at, task.task_id))
        self._pending_queue = [task.task_id for task in pending]

    async def _load_all_tasks(self) -> None:
        meta = await self._state.load_json("awi.task_queue.meta")
        if isinstance(meta, dict):
            self._global_pause = bool(meta.get("global_pause", False))
            pause_reason = meta.get("pause_reason")
            self._pause_reason = pause_reason if isinstance(pause_reason, str) else None

        for key in await self._state.list_keys("awi.tasks."):
            task_id = self._task_id_from_key(key)
            payload = await self._state.load_json(key)
            if isinstance(payload, dict):
                self._tasks[task_id] = AWITask.model_validate(payload)

        self._rebuild_pending_queue()

    async def create_task(self, request: AWITaskCreate) -> AWITask:
        """Create a new AWI task and add to queue."""
        task_id = f"awi-task-{uuid.uuid4().hex[:12]}"

        task = AWITask(
            task_id=task_id,
            task_type=request.task_type,
            target_url=request.target_url,
            status=AWITaskStatus.PENDING,
            priority=request.priority,
            created_at=datetime.now(timezone.utc),
            total_actions=len(request.action_sequence),
            action_sequence=request.action_sequence,
        )

        self._tasks[task_id] = task
        self._insert_into_priority_queue(task_id)
        await self._save_task(task_id)

        return task

    def _insert_into_priority_queue(self, task_id: str):
        """Insert task into pending queue maintaining priority order."""
        task = self._tasks[task_id]
        priority = task.priority

        if task_id in self._pending_queue:
            self._pending_queue.remove(task_id)

        insert_pos = len(self._pending_queue)
        for i, existing_id in enumerate(self._pending_queue):
            existing_task = self._tasks[existing_id]
            if priority < existing_task.priority:
                insert_pos = i
                break

        self._pending_queue.insert(insert_pos, task_id)

    async def get_task(self, task_id: str) -> AWITask | None:
        """Get a task by ID."""
        return await self._load_task(task_id)

    async def get_queue_status(self) -> AWITaskQueueStatus:
        """Get current status of the task queue."""
        await self._load_all_tasks()
        pending = sum(
            1 for t in self._tasks.values() if t.status == AWITaskStatus.PENDING
        )
        running = sum(
            1 for t in self._tasks.values() if t.status == AWITaskStatus.RUNNING
        )
        completed = sum(
            1 for t in self._tasks.values() if t.status == AWITaskStatus.COMPLETED
        )
        failed = sum(
            1 for t in self._tasks.values() if t.status == AWITaskStatus.FAILED
        )

        completed_tasks = [
            t for t in self._tasks.values() if t.status == AWITaskStatus.COMPLETED
        ]
        avg_duration = 0.0
        if completed_tasks:
            durations = []
            for t in completed_tasks:
                if t.completed_at and t.started_at:
                    dur = (t.completed_at - t.started_at).total_seconds() * 1000
                    durations.append(dur)
            avg_duration = sum(durations) / len(durations) if durations else 0.0

        return AWITaskQueueStatus(
            total_pending=pending,
            total_running=running,
            total_completed=completed,
            total_failed=failed,
            current_throughput=running,
            avg_task_duration_ms=avg_duration,
            queue=[self._tasks[tid] for tid in self._pending_queue[:10]],
        )

    async def start_next_task(self) -> AWITask | None:
        """Start the next task in the queue if capacity allows."""
        await self._load_all_tasks()
        if self._global_pause:
            return None

        running_count = sum(
            1 for task in self._tasks.values() if task.status == AWITaskStatus.RUNNING
        )
        if running_count >= self.max_concurrent_tasks:
            return None

        if not self._pending_queue:
            return None

        task_id = self._pending_queue.pop(0)
        task = self._tasks[task_id]

        if task.status != AWITaskStatus.PENDING:
            return None

        task.status = AWITaskStatus.RUNNING
        task.started_at = datetime.now(timezone.utc)
        await self._save_task(task_id)

        return task

    async def complete_task(self, task_id: str, result: dict[str, Any] | None = None):
        """Mark a task as completed."""
        task = await self._load_task(task_id)
        if not task:
            return

        task.status = AWITaskStatus.COMPLETED
        task.completed_at = datetime.now(timezone.utc)
        task.result = result
        if task_id in self._pending_queue:
            self._pending_queue.remove(task_id)

        if task_id in self._running_tasks:
            del self._running_tasks[task_id]
        await self._save_task(task_id)

    async def fail_task(self, task_id: str, error: str):
        """Mark a task as failed."""
        task = await self._load_task(task_id)
        if not task:
            return

        task.status = AWITaskStatus.FAILED
        task.completed_at = datetime.now(timezone.utc)
        task.error = error
        if task_id in self._pending_queue:
            self._pending_queue.remove(task_id)

        if task_id in self._running_tasks:
            del self._running_tasks[task_id]
        await self._save_task(task_id)

    async def pause_task(self, task_id: str, reason: str | None = None):
        """Pause a running task."""
        task = await self._load_task(task_id)
        if not task or task.status != AWITaskStatus.RUNNING:
            return

        task.status = AWITaskStatus.PAUSED

        if task_id in self._running_tasks:
            task_task = self._running_tasks[task_id]
            task_task.cancel()
            del self._running_tasks[task_id]
        await self._save_task(task_id)

    async def cancel_task(self, task_id: str) -> bool:
        """Cancel a task."""
        task = await self._load_task(task_id)
        if not task:
            return False

        if task.status == AWITaskStatus.RUNNING:
            await self.pause_task(task_id)
        elif task_id in self._pending_queue:
            self._pending_queue.remove(task_id)

        task.status = AWITaskStatus.CANCELLED
        task.completed_at = datetime.now(timezone.utc)
        await self._save_task(task_id)

        return True

    async def global_pause(self, reason: str | None = None):
        """Pause all tasks globally (for human intervention)."""
        self._global_pause = True
        self._pause_reason = reason

        for task_id in list(self._running_tasks.keys()):
            await self.pause_task(task_id, reason)
        await self._state.save_json(
            "awi.task_queue.meta",
            {"global_pause": self._global_pause, "pause_reason": self._pause_reason},
        )

    async def global_resume(self):
        """Resume all tasks after global pause."""
        self._global_pause = False
        self._pause_reason = None
        await self._state.save_json(
            "awi.task_queue.meta",
            {"global_pause": self._global_pause, "pause_reason": self._pause_reason},
        )

    def is_global_paused(self) -> tuple[bool, str | None]:
        """Check if globally paused and get reason."""
        return self._global_pause, self._pause_reason

    async def steer_task(self, task_id: str, new_instructions: str) -> bool:
        """
        Steer a task with new instructions (human intervention).

        This allows humans to redirect an agent's task with new guidance.
        """
        task = await self._load_task(task_id)
        if not task:
            return False

        if task.status == AWITaskStatus.PAUSED:
            task.action_sequence = [{"type": "steer", "instructions": new_instructions}]
            task.total_actions = len(task.action_sequence)
            task.current_action_index = 0
            task.status = AWITaskStatus.PENDING
            self._insert_into_priority_queue(task_id)
            await self._save_task(task_id)
            return True

        return False

    async def update_progress(self, task_id: str, action_index: int):
        """Update task progress."""
        task = await self._load_task(task_id)
        if task:
            task.current_action_index = action_index
            await self._save_task(task_id)


_awi_task_queue: AWITaskQueue | None = None


def get_awi_task_queue() -> AWITaskQueue:
    """Get singleton AWI task queue instance."""
    global _awi_task_queue
    if _awi_task_queue is None:
        _awi_task_queue = AWITaskQueue()
    return _awi_task_queue
