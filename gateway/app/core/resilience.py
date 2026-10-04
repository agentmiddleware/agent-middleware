"""
Resilience utilities for retry with backoff and circuit breakers.
"""

import asyncio
import logging
import time
from functools import wraps
from typing import Any, Callable, TypeVar

from sqlalchemy.exc import OperationalError

logger = logging.getLogger(__name__)

T = TypeVar("T")


def retry_with_backoff(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 10.0,
    exponential_base: float = 2.0,
    retriable_exceptions: tuple = (Exception,),
):
    """
    Decorator for async functions with exponential backoff retry.

    Args:
        max_attempts: Maximum number of retry attempts.
        base_delay: Initial delay in seconds.
        max_delay: Maximum delay between retries.
        exponential_base: Base for exponential backoff.
        retriable_exceptions: Tuple of exception types to retry on.

    Usage:
        @retry_with_backoff(max_attempts=3, base_delay=1.0)
        async def call_api():
            ...
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exception: Exception | None = None

            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except retriable_exceptions as e:
                    last_exception = e
                    if attempt == max_attempts:
                        logger.warning(
                            "retry_exhausted: %s failed after %d attempts: %s",
                            func.__name__,
                            max_attempts,
                            str(e),
                        )
                        raise

                    delay = min(
                        base_delay * (exponential_base ** (attempt - 1)), max_delay
                    )
                    logger.info(
                        "retry_attempt: %s attempt %d/%d, delay=%.2fs: %s",
                        func.__name__,
                        attempt,
                        max_attempts,
                        delay,
                        str(e),
                    )
                    await asyncio.sleep(delay)

            if last_exception:
                raise last_exception

        return wrapper

    return decorator


class CircuitBreaker:
    """
    Simple circuit breaker implementation.

    States:
    - CLOSED: Normal operation, requests pass through
    - OPEN: Circuit is tripped, requests fail fast
    - HALF_OPEN: Testing if service recovered
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max_calls: int = 1,
    ):
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._half_open_max_calls = half_open_max_calls

        self._state = self.CLOSED
        self._failure_count = 0
        self._last_failure_time: float | None = None
        self._half_open_calls = 0

    @property
    def state(self) -> str:
        if self._state == self.OPEN:
            if (
                self._last_failure_time
                and time.monotonic() - self._last_failure_time >= self._recovery_timeout
            ):
                self._state = self.HALF_OPEN
                self._half_open_calls = 0
        return self._state

    def record_success(self):
        """Record a successful call."""
        if self._state == self.HALF_OPEN:
            self._half_open_calls += 1
            if self._half_open_calls >= self._half_open_max_calls:
                self._state = self.CLOSED
                self._failure_count = 0
                logger.info("circuit_breaker_closed: recovery_successful")
        elif self._state == self.CLOSED:
            self._failure_count = max(0, self._failure_count - 1)

    def record_failure(self):
        """Record a failed call."""
        self._failure_count += 1
        self._last_failure_time = time.monotonic()

        if self._state == self.HALF_OPEN:
            self._state = self.OPEN
            logger.warning("circuit_breaker_reopened: half_open_failure")
        elif self._failure_count >= self._failure_threshold:
            self._state = self.OPEN
            logger.warning(
                "circuit_breaker_opened: threshold_exceeded, failures=%d",
                self._failure_count,
            )

    def is_allowed(self) -> bool:
        """Check if a call is allowed through the circuit breaker."""
        state = self.state
        if state == self.CLOSED:
            return True
        if state == self.HALF_OPEN:
            return self._half_open_calls < self._half_open_max_calls
        return False  # OPEN

    async def call(self, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Execute a function through the circuit breaker."""
        if not self.is_allowed():
            raise CircuitBreakerOpen(f"Circuit breaker is {self.state}")

        try:
            if asyncio.iscoroutinefunction(func):
                result = await func(*args, **kwargs)
            else:
                result = func(*args, **kwargs)
            self.record_success()
            return result
        except Exception:
            self.record_failure()
            raise


class CircuitBreakerOpen(Exception):
    """Raised when circuit breaker is open."""

    pass


# --------------------------------------------------------------------------- #
# SQLite write-conflict restart
#
# On PostgreSQL a contended row lock blocks until it is free, so a writer simply
# waits and the helpers below run their operation exactly once. SQLite in WAL
# mode cannot do that for a transaction that has already read: the read pins a
# snapshot, and if anything committed since, upgrading that snapshot to a write
# returns SQLITE_BUSY_SNAPSHOT *without consulting the busy handler* — waiting
# could only deadlock, so `PRAGMA busy_timeout` is deliberately not applied. The
# single cure the engine offers is to abandon the snapshot and run the whole
# transaction again, which is what these two functions exist to do.
#
# That is why this is a transaction-restart helper and not a generic retry: the
# operation handed in must own and rebuild its own transaction, because the one
# that raised is already poisoned and nothing inside it can be replayed.
# --------------------------------------------------------------------------- #

#: Attempts before a contended writer gives up. Chosen with the ~20ms ceiling
#: below for roughly 0.4s of worst-case contention, which is long enough to
#: outlast a burst of concurrent writers and short enough that a caller blocked
#: on it still gets an answer rather than a timeout.
WRITE_CONFLICT_MAX_ATTEMPTS = 40


def is_retryable_write_conflict(exc: BaseException) -> bool:
    """Whether an ``OperationalError`` is a transient SQLite write conflict.

    Both tokens matter: SQLITE_BUSY surfaces as "database is locked", while
    SQLITE_BUSY_SNAPSHOT surfaces as one or the other depending on the build,
    so matching both keeps the classifier independent of that difference. A
    substantive fault — "disk I/O error", "no such table", a constraint — does
    not match and must propagate on the first attempt.
    """
    message = str(exc).lower()
    return "locked" in message or "snapshot" in message


async def run_with_write_conflict_retry(
    operation: Callable[[], Any],
    *,
    on_exhausted: Callable[[BaseException], BaseException],
    max_attempts: int = WRITE_CONFLICT_MAX_ATTEMPTS,
    restart_on: Callable[[BaseException], bool] | None = None,
) -> Any:
    """Run a full-transaction operation, restarting it on write conflicts.

    ``operation`` must open and commit its own transaction: it is re-invoked
    from the top, so anything it leaves committed from an earlier attempt is
    its own to make idempotent.

    ``on_exhausted`` builds the exception raised when the attempts run out. It
    is a callable rather than a fixed class so each caller keeps its own reason
    code — the contended permit and the contended ledger debit are different
    facts about the system and a client can act differently on each.

    ``restart_on`` lets the caller name one more failure as a restart. The
    classifier only knows the driver's own conflicts, but a transaction can
    also be invalidated by a commit the driver never objects to: a row the
    caller resolved before opening the transaction and re-reads under lock
    inside it may have changed in between, and the check that notices raises
    the caller's own type. It is a predicate rather than an exception type so
    the caller can restart on the one reason a restart cures and let every
    other reason of the same type fail on the first attempt. A restart taken
    this way spends the same budget and ends in the same ``on_exhausted``.
    """
    last_exc: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            return await operation()
        except OperationalError as exc:
            if not is_retryable_write_conflict(exc):
                raise
            last_exc = exc
        except Exception as exc:
            if restart_on is None or not restart_on(exc):
                raise
            last_exc = exc
        # Linear, capped: enough jitter-free spacing to let the winning
        # writer commit, without a backoff long enough to hold the caller.
        await asyncio.sleep(min(0.02, 0.002 * (attempt + 1)))
    assert last_exc is not None  # unreachable: max_attempts >= 1
    raise on_exhausted(last_exc)
