# Operator receipt resolution

Use this path only for an existing `delivery_uncertain` receipt ID after
checking authoritative upstream state. It never dispatches a tool, changes the
original receipt, or changes the original idempotency replay. The charge stands
unless the operator records `confirmed_not_delivered` with evidence and
explicitly requests a refund. Only the bootstrap admin credential is accepted;
agent keys, wallet keys, demo keys, and permits cannot resolve receipts. This
route is absent from MCP tools and agent discovery.

## Collect and review evidence

Export the original portable receipt and retain the public trust keys. Find the
upstream invocation from its dispatch attempt and idempotency record. Check the
provider's own reference ids and observation time. Prepare a local JSON request
file with `outcome` (`confirmed_success` or `confirmed_not_delivered`), an
`evidence` object containing `source`, `upstream_reference_ids` (a list),
`observed_at` (an ISO timestamp with timezone), and `note`. Optionally include
`raw_lookup_answer`, with secrets removed. Evidence is readable by the wallet
and any authorized receipt counterparty, so include only information they may
see. Common credential-named fields are redacted automatically, but review
free text and provider-specific fields yourself. Canonical evidence is limited
to 16 KiB. Use actual provider references, never invented evidence.

Set `refund_credits` explicitly to true only to refund a confirmed non-delivery.
It defaults to false. Success cannot refund. Before manually asserting
non-delivery, stop every process that could hold the dispatch claim and ensure
no worker can resume sending. Document how this was established in the note.
A terminal database row alone does not fence a paused sender.

Run the thin CLI on the operator host with the bootstrap key already configured
in `VALID_API_KEYS`:

```bash
python gateway/scripts/amw_resolutions.py "$RECEIPT_ID" "$REQUEST_FILE" \
  --base-url "$AMW_GATEWAY_URL"
```

This calls `POST /v1/admin/receipts/{receipt_id}/resolution`. Keep the request
file: an exact replay returns the same signed resolution and finishes any
interrupted refund. Changing the evidence, outcome, lookup flag, or refund
intent conflicts with the existing record (HTTP 409). Never delete it to retry.
A signed `evidence.refund_entry_id` records refund intent; confirm the separate
ledger entry exists before treating the refund as complete. An interruption
may leave the signed intent before the money transaction finishes.

## Optional refund fixture status lookup

A deployment can explicitly register `RefundFixtureStatusLookup(origin, token)`
with `register_receipt_status_lookup` in `app.services.operator_resolution`
during startup. Nothing is registered by default. Supply the bearer token from
the deployment's configuration without logging it. Set `use_lookup` to true
in the reviewed request file. The adapter sends `POST {origin}/status` with
`tool`, `invocation_id`, and the forwarded `idempotency_key`. For action-permit
calls that key is unavailable and null; the fixture must match invocation id.
Redirects are disabled, response bytes are bounded, and the lookup has a
five-second bound. Only the status enum is retained as evidence, so credentials
reflected in the raw provider response are discarded.

`executed` proves `confirmed_success`. `not_executed` can prove non-delivery
only for a terminal pre-dispatch failure with no historical send or dispatch claim.
For the usual uncertain attempt with a historical claim it is inconclusive:
a paused holder could still send after the observation. `pending`, `unknown`,
timeout, malformed answers, errors, an unregistered origin, and an answer that
disagrees with the requested outcome return HTTP 409 `resolution_inconclusive`.
No resolution or refund is written. The audit records the attempt.

If the provider cannot answer, retain the uncertain receipt and the charge.
Do not redispatch automatically. Obtain authoritative evidence and establish
that all senders have stopped before an operator asserts a final outcome.

## Verify offline and inspect the chain

Read `GET /v1/receipts/{receipt_id}/resolution` and the wallet chain at
`GET /v1/receipts/resolutions/wallet/{wallet_id}` with an authorized credential.
Export the original `GET /v1/receipts/{receipt_id}/portable` bundle and public
`/.well-known/trust-keys.json` document. On an offline host, load these actual
files and call the SDK's `key_set_from_document`, then
`verify_resolution(resolution, receipt_bundle, keys)` from
`b2a_sdk.resolution_verifier` (also exported through `amw`). Require `.ok`.
Use `verify_resolution_chain` with the complete ordered chain for chain checks.
Verification covers original receipt bytes, resolution signature and hash,
and the v0.2 evidence hash. v0.1 records remain verifiable without evidence.
The verifier does not prove the truth of an operator's assertion or that the
refund ledger transaction completed; check the ledger separately.
