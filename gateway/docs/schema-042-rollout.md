# Schema 042: single-action binding and rollback boundary

Migration `042_permit_action_binding` revises
`041_scrub_content_owner_keys`. It adds six nullable action-binding columns to
each of `permits` and `receipts`. Existing values and signatures remain
unchanged. Upstream 041 still scrubs non-wallet content owner keys; 042 neither
replaces that migration nor restores scrubbed values.

Action issuance remains frozen. The normal application does not mount or
advertise `/v1/action-permits`, including when proof surfaces or the configured
upstream are enabled. Only explicit test fixtures mount that router and provide
an `ActionToolBinding`; the configured upstream has no qualified binding.
Unfreezing issuance requires a separately reviewed binding and rollout. Existing
action verification, receipt reads and recovery remain available internally.

The feature's original local `041_permit_action_binding` was applied only to
disposable tests and was never an operational schema. Its unpublished revision
was renumbered for this integration. A retained database stamped with that old
local revision requires a separately reviewed graph repair; do not stamp it as
042 or run this plan against it.

## Release prerequisites and compatibility

This document records prerequisites, not permission to deploy or evidence of an
operational rollout. Staging and production state have not been inspected.
Before a separately authorized release, complete this record with observed
values and retained evidence. Placeholder values in the customer manifest do
not satisfy it.

| Required record | Status |
| --- | --- |
| Current database revision and current packaged head; private parity result | REQUIRED / UNVERIFIED |
| Current full build SHA, immutable image digest, deployment ID and worker IDs | REQUIRED / UNVERIFIED |
| Target full build SHA, immutable image digest, deployment ID and worker IDs | REQUIRED / UNVERIFIED |
| Target database revision and packaged head: `042_permit_action_binding`; private parity result | REQUIRED / UNVERIFIED |
| Qualified 042-capable recovery full SHA, immutable image digest and deployment ID | REQUIRED / UNVERIFIED |
| Recovery startup, authority verification and reconciliation rehearsal evidence | REQUIRED / UNVERIFIED |
| Issuance/admission stop procedure, isolation controls and responsible operator | REQUIRED / UNVERIFIED |

1. Verify the actual database is at `041_scrub_content_owner_keys` and the
   serving image has matching packaged schema. For a database at 040, separately
   review and rehearse the 041 scrub and its [rollback boundary](schema-041-rollout.md).
   Earlier revisions require their own review. Rehearse both 040 → 041 → 042 and
   041 → 042 using fresh disposable PostgreSQL, retaining legacy signatures,
   wallet ownership and action authority. Do not use operational credentials in
   local rehearsals.
2. Qualify an immutable 042-capable recovery image before changing the database.
   It must understand signed action bindings, owner tombstones, accounting and
   dispatch states, and capable reconciliation. A schema-only compatibility
   image that ignores action authority is insufficient. The recovery image must
   also retain the upstream owner-key scrub, authentication, nonce/cap validation
   and HTTP idempotency fixes. Record exact artifacts and proof above.
3. Keep new action issuance and admission stopped while installing the capable
   workers and migrating. There is no dedicated action rollout flag in this
   change: a separately verified operational procedure must block both new
   issuance and every execution surface without removing action-aware recovery
   workers. Do not invent an environment flag or treat the repeat-window
   issuance flag as an action control.
4. Route every executable surface for the selected protected tool to capable
   workers: standard `/mcp`, legacy `/mcp/messages`, and REST invocation. Old
   workers must have neither an executable route nor credentials/server-side
   authorization that can reach the protected upstream tool. Reject direct old
   origins and standard auto-permit bypasses. Routing alone is insufficient;
   demonstrate credential isolation and upstream refusal before admission.
   No mixed-version activation is allowed without this evidence.
5. Use a separately reviewed immutable exact-SHA operator process; this
   repository has no hosted deploy runbook or IaC graph. Apply 042 through the
   reviewed migration entrypoint, then verify private schema parity, the baked build SHA, worker
   identity, runtime posture, health and logs. Record the observations above.
   Preserve signing material, stable deployment authority and native upstream
   idempotency identity. Schema compatibility alone does not prove safe admission.
6. Enable issuance/admission only after all reachable workers and upstream
   credentials satisfy the isolation proof and the capable recovery path is
   available. A local fake partner proof is not partner or production acceptance.

## Rollback after activation

Stop new action issuance and admission using the qualified procedure. Keep
042-capable workers available to reconcile retained actions and provide
authorized receipt reads. Recover only with the recorded 042-capable immutable
image; never route an action permit to an older binary. A packaged 041 or 040
image cannot restart with database head 042 because schema parity is enforced.
Do not bypass parity or use a stamp-only downgrade to restart such an image.

Retain action permits, receipts, owner/tombstone records under `/mcp/action/v1`,
dispatch attempts, ledger/reservations and their linkage. Preserve signing
verification material, stable deployment authority and upstream native-key
retention. Expiry, revocation, refund or a stopped admission path does not erase
execution identity or authorize a second dispatch. Unresolved actions require
capable reconciliation; no automatic purge, reopening or redispatch is allowed.

The migration refuses downgrade if **any** action field is non-null in permits
or receipts, including partial bindings and unknown versions, or if any action
owner exists. The check and column removal are protected by database write
locks. Once action authority exists, retain schema 042. Do not delete authority
to make downgrade pass. A 042 → 041 roundtrip is tested only in disposable
databases with no retained action authority; it is not an operational rollback
procedure. Never downgrade through 041 to enable old credential-writing images:
the scrub restores no credentials.

The record remains REQUIRED / UNVERIFIED until the corresponding operational
checks are performed under separate authorization. Local integration and tests
do not establish current deployment parity, old-worker isolation, native-key
retention or a rehearsed operational stop/reconcile procedure.
