from datetime import datetime, timedelta, timezone

from app.core.time import to_naive_utc, utc_now


def test_to_naive_utc_preserves_the_instant_across_offsets():
    source = datetime(
        2026,
        8,
        4,
        8,
        30,
        tzinfo=timezone(timedelta(hours=-7)),
    )

    assert to_naive_utc(source) == datetime(2026, 8, 4, 15, 30)


def test_naive_utc_values_remain_naive():
    source = datetime(2026, 8, 4, 15, 30)

    assert to_naive_utc(source) == source
    assert to_naive_utc(source).tzinfo is None
    assert utc_now().tzinfo is None
