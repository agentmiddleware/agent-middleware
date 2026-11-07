# API key usage reporting

`api_key_usage_events` is the call-count source for database-backed API keys.
Each row records the key ID, tenant label, HTTP method, registered route
template, final status, latency in milliseconds, and UTC time. It stores no
request or response body, raw URL, query, header, client address, or key value.
Bootstrap keys configured outside the database have no key ID and produce no
per-key event. Requests refused before a database key is resolved produce no
event; a resolved key denied on route authorization still produces one.

`api_keys.use_count` remains the bounded-key budget counter. It advances only
for keys with `max_uses` and must not be used as a general traffic count.
The event insert is best effort: a database write failure never changes the
API response, so a failed insert can leave a gap in reported usage.
The application only appends rows; database administrators can still alter
them. This is usage reporting, not signed audit evidence.

The read-only `api_key_usage_daily` SQL view groups by key ID and UTC day. It
reports calls, 4xx, 5xx, continuous p50/p95 latency, and first/last call.
`GET /v1/admin/api-keys/usage/daily` computes the same aggregates from events
within the requested UTC day window. `days` defaults to 7 and is capped at
90; `limit` defaults to 100 rows and is capped at 200. `key_id` filters one
key and is optional only for windows of 7 days or less; over 7 days, a missing
or empty `key_id` returns 422 `key_id_required_for_long_window`. A report that
hits the 5s statement timeout returns 503 `usage_report_timeout`. Only
bootstrap admins can access the report.

`API_KEY_USAGE_RETENTION_DAYS` defaults to disabled (`None` or `0`).
`prune_api_key_usage_events()` deletes at most one batch of 1,000 rows per
explicit call, using a `created_at` cutoff; it is never scheduled or called
automatically. Pruning production data requires the owner's explicit approval.
