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
`GET /v1/admin/api-keys/usage/daily` reads the view; an optional `key_id`
query parameter filters one key. Only bootstrap admins can access it.
