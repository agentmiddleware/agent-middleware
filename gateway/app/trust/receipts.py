"""Trust-plane facade: signed action receipts.

Re-exports the canonical receipt implementation from
:mod:`app.services.receipts`, plus an opt-in asyncio batching emitter for
callers that trade receipt-before-response durability for lower per-call
latency.
"""

from __future__ import annotations

import asyncio
import math
from typing import Any

from app.services.receipts import (
    ReceiptError,
    ReceiptService,
    ReceiptWriteContendedError,
    get_receipt_service,
)


class BatchingReceiptEmitter:
    """Opt-in background batcher over ``ReceiptService.create_receipt``.

    ``enqueue(**create_receipt_kwargs)`` returns an ``asyncio.Future`` that
    resolves to the ``ReceiptResponse`` once the item is flushed. A single
    background task drains the queue and emits each item **in arrival order**
    through ``create_receipt``; receipts are integrity-critical, so a failed
    item's exception is set on that item's future — never dropped silently —
    and the remaining items in the batch still flush. ``aclose()`` flushes
    everything already enqueued before stopping.

    DURABILITY TRADE, stated plainly: ``enqueue`` returns *before* the
    database write. A crash between enqueue and flush loses the receipt.
    Callers that need receipt-before-response semantics (every governed
    invoke path does) must keep calling ``ReceiptService.create_receipt``
    directly; this emitter is only for callers that deliberately accept the
    window in exchange for taking the receipt write off their latency path.
    Nothing else in this facade changes — the emitter is opt-in only.

    BACKPRESSURE: the queue is bounded at ``max_pending`` items. When the
    drain task cannot keep up (a stalled ``ReceiptService``), ``enqueue``
    raises ``ReceiptError("receipt_emitter_saturated")`` instead of
    retaining an unbounded backlog of payloads and futures. A rejected item
    is never enqueued, so ``aclose()`` still flushes exactly the accepted
    items and returns.
    """

    def __init__(
        self,
        service: ReceiptService | None = None,
        *,
        max_batch: int = 32,
        flush_interval: float = 0.05,
        max_pending: int = 1024,
    ) -> None:
        if max_batch < 1:
            raise ValueError("max_batch must be at least 1")
        if not math.isfinite(flush_interval):
            raise ValueError("flush_interval must be finite")
        if flush_interval < 0:
            raise ValueError("flush_interval must be non-negative")
        if max_pending < 1:
            raise ValueError("max_pending must be at least 1")
        self._service = service
        self._max_batch = max_batch
        self._flush_interval = flush_interval
        self._max_pending = max_pending
        self._queue: asyncio.Queue[tuple[dict[str, Any], asyncio.Future]] | None = None
        self._task: asyncio.Task | None = None
        self._closed = False

    def _resolve_service(self) -> ReceiptService:
        return self._service if self._service is not None else get_receipt_service()

    async def start(self) -> None:
        """Start the background drain task. Idempotent."""
        if self._closed:
            raise RuntimeError("receipt_emitter_closed")
        if self._task is None:
            self._queue = asyncio.Queue(maxsize=self._max_pending)
            self._task = asyncio.create_task(self._drain())

    async def enqueue(self, **create_receipt_kwargs: Any) -> asyncio.Future:
        """Queue one receipt for background emission.

        Accepts exactly the keyword arguments of
        ``ReceiptService.create_receipt`` and returns a future that resolves
        to the ``ReceiptResponse`` (or raises that item's emission error)
        once the batch containing it is flushed. Starts the drain task on
        first use so ``start()`` need not be called separately.

        Raises ``ReceiptError("receipt_emitter_saturated")`` when
        ``max_pending`` items are already waiting — backpressure instead of
        an unbounded backlog behind a stalled ``ReceiptService``. Nothing is
        queued for a rejected call.
        """
        if self._closed:
            raise RuntimeError("receipt_emitter_closed")
        await self.start()
        assert self._queue is not None
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        try:
            self._queue.put_nowait((create_receipt_kwargs, future))
        except asyncio.QueueFull:
            future.cancel()
            raise ReceiptError("receipt_emitter_saturated") from None
        return future

    async def _drain(self) -> None:
        assert self._queue is not None
        loop = asyncio.get_running_loop()
        while True:
            batch = [await self._queue.get()]
            # Opportunistic batching: after the first item arrives, wait up to
            # flush_interval for more, capped at max_batch, then flush the
            # whole batch sequentially in arrival order.
            deadline = loop.time() + self._flush_interval
            while len(batch) < self._max_batch:
                timeout = deadline - loop.time()
                if timeout <= 0:
                    break
                try:
                    batch.append(await asyncio.wait_for(self._queue.get(), timeout))
                except asyncio.TimeoutError:
                    break
            for kwargs, future in batch:
                try:
                    response = await self._resolve_service().create_receipt(**kwargs)
                except asyncio.CancelledError:
                    # Shutdown must not leave the item's caller hanging: the
                    # receipt was not written, so surface that on the future
                    # before propagating the cancellation.
                    if not future.done():
                        future.set_exception(ReceiptError("receipt_emitter_cancelled"))
                    self._queue.task_done()
                    raise
                except Exception as exc:
                    # A poisoned item rejects only its own future; the rest of
                    # the batch still flushes.
                    if not future.done():
                        future.set_exception(exc)
                else:
                    if not future.done():
                        future.set_result(response)
                self._queue.task_done()

    async def aclose(self) -> None:
        """Flush everything already enqueued, then stop the drain task."""
        if self._closed:
            return
        self._closed = True
        if self._task is None:
            return
        assert self._queue is not None
        # join() returns once task_done() has been called for every enqueued
        # item, i.e. once every pending future has been resolved.
        await self._queue.join()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None


__all__ = [
    "BatchingReceiptEmitter",
    "ReceiptError",
    "ReceiptService",
    "ReceiptWriteContendedError",
    "get_receipt_service",
]
