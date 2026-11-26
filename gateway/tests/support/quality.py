"""Exact-value assertion helpers for the test suite (Pattern 9).

A test that cannot fail certifies nothing: ``assert total >= 0`` passes when
the endpoint is broken, ``assert status in (422, 500)`` passes when the
server crashes, and key-existence asserts pass when the values are wrong.
Every helper here pins one exact expected value, so a regression fails
loudly instead of staying green. Use them for every new count, status, and
list/total pair; see the module docstring examples.
"""

from typing import Any, Mapping


def assert_exact_status(response: Any, expected: int) -> Any:
    """Assert one exact HTTP status, with the body in the failure message.

    A tuple of acceptable statuses (``in (200, 404)``) hides a regression
    behind the alternative: if two outcomes are genuinely possible, branch
    on the status explicitly and assert the exact body contract of each
    branch instead of passing one tuple here.
    """
    actual = response.status_code
    assert actual == expected, (
        f"expected HTTP {expected}, got {actual}: "
        f"{getattr(response, 'text', response)!r}"[:2000]
    )
    return response


def assert_exact_total(
    payload: Mapping[str, Any], expected: int, key: str = "total"
) -> int:
    """Assert a response count equals one exact integer.

    Rejects the always-true ``>= 0`` shape: the value must be present, must
    be an int (a missing key or a string total fails instead of passing),
    and must equal ``expected``.
    """
    assert key in payload, f"response has no {key!r} count: {sorted(payload)}"
    actual = payload[key]
    assert isinstance(actual, int) and not isinstance(actual, bool), (
        f"{key!r} must be an int, got {actual!r}"
    )
    assert actual == expected, f"expected {key} == {expected}, got {actual}"
    return actual


def assert_counts_agree(
    payload: Mapping[str, Any],
    total_key: str = "total",
    items_key: str = "vulnerabilities",
    count_keys: tuple[str, ...] = (),
) -> int:
    """Assert a list/total pair is self-consistent.

    ``total`` must equal ``len(items)``, and every named severity/count key
    must be a non-negative int that fits inside the total. A scanner that
    returns an empty list with a nonzero total (or the reverse) fails here
    instead of passing a key-existence check.
    """
    assert total_key in payload, f"response has no {total_key!r} count"
    assert items_key in payload, f"response has no {items_key!r} list"
    total = payload[total_key]
    items = payload[items_key]
    assert isinstance(total, int) and not isinstance(total, bool), (
        f"{total_key!r} must be an int, got {total!r}"
    )
    assert isinstance(items, list), f"{items_key!r} must be a list, got {items!r}"
    assert total == len(items), (
        f"{total_key}={total} disagrees with len({items_key})={len(items)}"
    )
    running = 0
    for name in count_keys:
        assert name in payload, f"response has no {name!r} count"
        value = payload[name]
        assert isinstance(value, int) and not isinstance(value, bool) and value >= 0, (
            f"{name!r} must be a non-negative int, got {value!r}"
        )
        running += value
    assert running <= total, f"severity counts sum to {running} but {total_key}={total}"
    return total
