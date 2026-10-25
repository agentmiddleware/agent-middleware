"""Portable daily API-key usage view and bounded report query.

Timestamps in this database are naive UTC. Percentiles use continuous linear
interpolation, matching PostgreSQL's percentile_cont on both supported SQL
dialects. The SQLite development schema and Alembic migration share this SQL.
"""


def _percentile_expression(percent: int) -> str:
    return (
        "SUM(CASE "
        f"WHEN latency_rank = p{percent}_floor_rank "
        f"THEN latency_ms * (100 - p{percent}_remainder) / 100.0 "
        f"WHEN latency_rank = p{percent}_floor_rank + 1 "
        f"THEN latency_ms * p{percent}_remainder / 100.0 "
        f"ELSE 0 END) AS p{percent}_latency_ms"
    )


def _daily_usage_select_sql(event_filter: str = "") -> str:
    return f"""
WITH ranked AS (
    SELECT
        key_id,
        DATE(created_at) AS day,
        status_code,
        latency_ms,
        created_at,
        ROW_NUMBER() OVER (
            PARTITION BY key_id, DATE(created_at)
            ORDER BY latency_ms, id
        ) AS latency_rank,
        COUNT(*) OVER (
            PARTITION BY key_id, DATE(created_at)
        ) AS call_count
    FROM api_key_usage_events{event_filter}
), positioned AS (
    SELECT
        *,
        ((call_count - 1) * 50) / 100 + 1 AS p50_floor_rank,
        ((call_count - 1) * 50) % 100 AS p50_remainder,
        ((call_count - 1) * 95) / 100 + 1 AS p95_floor_rank,
        ((call_count - 1) * 95) % 100 AS p95_remainder
    FROM ranked
)
SELECT
    key_id,
    day,
    COUNT(*) AS calls,
    SUM(CASE WHEN status_code BETWEEN 400 AND 499 THEN 1 ELSE 0 END) AS calls_4xx,
    SUM(CASE WHEN status_code BETWEEN 500 AND 599 THEN 1 ELSE 0 END) AS calls_5xx,
    {_percentile_expression(50)},
    {_percentile_expression(95)},
    MIN(created_at) AS first_call_at,
    MAX(created_at) AS last_call_at
FROM positioned
GROUP BY key_id, day
"""


CREATE_API_KEY_USAGE_DAILY_VIEW_SQL = (
    "CREATE VIEW api_key_usage_daily AS" + _daily_usage_select_sql()
)


def bounded_daily_usage_sql(*, filter_key: bool) -> str:
    """Aggregate only events in the requested UTC window, then cap result rows."""
    event_filter = "\n    WHERE created_at >= :start_at AND created_at < :end_at"
    if filter_key:
        event_filter += " AND key_id = :key_id"
    return (
        _daily_usage_select_sql(event_filter)
        + " ORDER BY day DESC, key_id LIMIT :limit"
    )
