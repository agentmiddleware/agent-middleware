# Key Management

How the trust-plane signing key is loaded today, how it is intended to be
managed in production, and what specifically changes when moving from the
env-var-based loader to an external KMS.

This document is normative for the Ed25519 key that signs permits, receipts,
and audit-chain entries. It is not the only secret in the system, but it is
the secret that the trust-plane guarantees depend on. If this key is
compromised, every receipt and permit becomes forgeable from the moment of
compromise forward.

## Current Posture

The active key is loaded by `SigningKeyService._load_private_key` in
`app/services/signing_keys.py`. The loader has two modes:

1. **Configured key** — `TRUST_SIGNING_PRIVATE_KEY_B64` (set in
   `app/core/config.py`) must be strict standard base64 that decodes to exactly
   the 32 raw bytes accepted by `Ed25519PrivateKey.from_private_bytes`. PEM,
   hex, 64-byte seed-plus-public-key concatenations, and double-encoded base64
   are rejected. The decoded private bytes never leave the process.
2. **Ephemeral fallback** — if the env var is empty and `TRUST_MODE_ENABLED`
   is false, the loader generates a process-local `Ed25519PrivateKey` that
   exists only in memory and dies with the process. This path exists so local
   tests can exercise sign/verify without persisting key material to disk.

If `TRUST_MODE_ENABLED` is true and the env var is empty, the loader raises
`SigningKeyError("trust_signing_private_key_required")`. There is no silent
fallback in trust mode.

Public-key metadata (key ID, base64 public key, status, activation time) is
persisted in the `SigningKeyModel` table and is the basis for offline receipt
verification. The private key is never persisted.

### Why the current posture is acceptable for MVP

- The private key lives only in the process memory of API workers
- Hosting platforms (Railway, Fly, AWS) inject the env var from their own
  secret stores at deploy time
- Public-key metadata is auditable (`GET /v1/signing-keys/active`,
  `GET /v1/signing-keys/{key_id}`); retiring or rotating it is service code
  only today, with no route or script (see [Status](#status))
- The trust boundary documented in `SECURITY_LIMITATIONS.md` is consistent
  with this loader

### Why it is not production-grade

- The private key is recoverable by anyone with access to the process env
  (sidecar, debugger, `/proc`, crash dump, container introspection)
- There is no hardware-rooted custody
- There is no separation between signing authority and the workload that
  invokes signing
- Rotation requires re-deploying with a new env var, which couples key
  rotation to release cadence

## Production Target: External KMS

In production, signing must happen inside an external KMS, and the API worker
must never hold raw private bytes. Three providers are supported as targets;
the choice is per-deployment.

### Provider Targets

| Provider | Key type | Sign API | Audit |
|---|---|---|---|
| AWS KMS | `ECC_NIST_P256` (until KMS adds Ed25519) or external-key import | `Sign` with `SigningAlgorithm=ECDSA_SHA_256` or imported-key Ed25519 | CloudTrail logs every `Sign` call |
| GCP KMS | `ASYMMETRIC_SIGN` Ed25519 | `AsymmetricSign` | Cloud Audit Logs every `AsymmetricSign` call |
| HashiCorp Vault Transit | `ed25519` | `transit/sign/:name` | Vault audit device |

The receipt format does not change. `alg` stays `Ed25519` for Vault and GCP
KMS. For AWS KMS until Ed25519 is native, the `alg` field becomes
`ECDSA-P256-SHA256`. Offline verifiers can check that algorithm using the
published key's `alg`, but receipt v0.1 still classifies a genuine P-256
receipt as `unsupported`. Enabling that KMS signing path also requires an
explicit receipt-format policy update.

### Concrete code changes

The integration points are narrow because the loader was designed for this:

1. **`app/services/signing_keys.py`** — replace `_load_private_key` with a
   provider strategy. The strategy returns an opaque key handle, not raw
   bytes. `sign_payload_with_key_id` calls `strategy.sign(message)` instead
   of `self._private_key.sign(message)`.

2. **`app/core/config.py`** — add:
   - `TRUST_KMS_PROVIDER` (`env` | `aws_kms` | `gcp_kms` | `vault_transit`)
   - `TRUST_KMS_KEY_REF` (ARN, resource name, or Vault key name)
   - `TRUST_KMS_REGION` (where relevant)
   - `TRUST_KMS_ROLE_ARN` / equivalent workload-identity binding

3. **`SigningKeyService.ensure_active_key`** — the public-key metadata path
   stays the same, but the public key is fetched from the KMS once at
   startup and cached, rather than derived from the local private key.

4. **`SigningKeyService.rotate_active_key_metadata`** — gains a real
   counterpart: a rotation flow that creates a new KMS key version, marks the
   old metadata row `retired`, inserts the new metadata row, and begins
   signing under the new `kid` without breaking historical verification.

5. **Health checks** — add a `/healthz/kms` endpoint that performs a no-op
   signing round-trip and reports KMS latency. This is the early-warning
   signal for KMS misconfiguration.

The signing call path stays a single function call; only the implementation
changes.

### Workload identity, not static credentials

The API worker must authenticate to the KMS using workload identity, not a
static credential:

- **AWS** — IAM role attached to the task/pod, scoped to `kms:Sign` on
  exactly the trust-plane key ARN. No `kms:GetPublicKey` is required after
  startup if the public key is cached.
- **GCP** — Workload Identity binding, scoped to `roles/cloudkms.signer` on
  exactly the trust-plane key resource.
- **Vault** — Kubernetes auth or AppRole with a policy that allows
  `update` on `transit/sign/<key>` and nothing else.

The KMS audit log becomes the canonical record of "who signed what when."
This is the property that closes the gap in `SECURITY_LIMITATIONS.md` between
"receipts are verifiable" and "receipts are non-repudiable."

## Rotation Flow Under KMS

> **Target state, not implemented.** This flow is the design for after the
> KMS integration ships. `POST /v1/admin/signing-keys/rotate` does not exist:
> the only signing-key routes today are the read-only
> `GET /v1/signing-keys/active` and `GET /v1/signing-keys/{key_id}`
> (`app/routers/keys.py`). For what rotation means now, see [Status](#status).
> This design also assumes a retired row stays retired. That is not true of
> the code today: see [Retirement is not durable today](#retirement-is-not-durable-today).

Rotation under KMS is metadata + key-version change, not a redeploy:

1. Operator creates a new key version in the KMS (`aws kms
   create-key` for a new key, or version rotation on an existing key).
2. Operator calls a new admin route (proposed:
   `POST /v1/admin/signing-keys/rotate`) with the new key reference and a new
   `kid`.
3. `rotate_active_key_metadata` marks the prior metadata row `retired`
   (retains it for verification of pre-rotation receipts), inserts the new
   metadata row, and flips the active `kid`.
4. Subsequent `sign_payload` calls produce receipts under the new `kid`.
5. Verification continues to work for receipts under the old `kid` because
   the retired metadata row is still queryable.
6. No worker restart is required.

The same rotation flow runs unattended on a schedule (e.g., quarterly) via a
cron job that calls the admin endpoint with a service identity.

## Compromise Response

If a signing key is suspected of compromise:

1. **Disable**, not retire, the affected `kid` — set `status="disabled"` so
   `verify_payload` rejects signatures bound to it.
2. Rotate to a new KMS key version immediately.
3. Re-issue any unconsumed permits under the new `kid`. Receipts already
   produced under the disabled key are no longer accepted by verification;
   downstream consumers must re-acquire authorization.
4. Publish the compromised `kid` to any external transparency log used by
   consumers.

The audit chain itself is signed and hash-linked. If the signing key is
rotated, the chain continues — each entry references its signing `kid`, so a
verifier can walk a chain that spans key rotations.

## What This Does Not Solve

This document covers the trust-plane signing key. It does not cover:

- API-key custody (`VALID_API_KEYS`, DB-backed API keys) — rotation for
  these is covered in `docs/api-key-rotation.md`
- Stripe webhook secrets
- Database credentials
- TLS material

Those secrets follow the standard host secret-manager pattern and are out of
scope for the trust-plane key-management story.

## Status

Until the KMS integration ships, the production posture is:

- `TRUST_MODE_ENABLED=true` and `ALLOW_LEGACY_UNPERMITTED_MCP=false` are the
  shipped defaults; nothing extra to configure
- `TRUST_SIGNING_PRIVATE_KEY_B64` injected at deploy time from the hosting
  platform secret manager
- Rotation by redeploy only: a new `TRUST_SIGNING_PRIVATE_KEY_B64` paired with
  a new `TRUST_SIGNING_KEY_ID`. The redeploy activates the new `kid` but does
  not retire the old one (`SigningKeyService.ensure_active_key`), so both stay
  `active`. Retiring the old metadata has no route or script yet:
  `retire_key_metadata` and `rotate_active_key_metadata` are service methods
  only, and no `POST /v1/admin/signing-keys/rotate` route exists. The
  post-rotation steps are in the hosted-service deploy runbook (not in this repository).
- Retirement is not durable while any replica still runs the old key id. Use
  the rollout order in the next section.

### Retirement is not durable today

What the code does, checked against `app/services/signing_keys.py`:

- `retire_key_metadata` sets a row to `status="retired"` and stamps
  `retired_at`. `list_public_keys` still publishes retired rows (only
  `disabled` rows are left out), so receipts signed under them keep verifying.
- `ensure_active_key` runs before receipt, audit event, permit and quote
  signing. When the row for the process's configured `TRUST_SIGNING_KEY_ID`
  is `retired`, it sets the row back to `active`, resets `activated_at` and
  clears `retired_at`, then signs. Only `disabled` is terminal: that row
  refuses with `signing_key_disabled`.
- The prepared-key path does not reactivate. `validate_prepared_signing_key`
  (used when a caller signs under a key it resolved earlier, such as the
  receipt write in the pending-refund path) refuses a key that is not
  `active` or has `retired_at` set, with `signing_key_not_active`. That is the
  existing behavior when a rotation lands in the middle of a call.
- So a replica that still runs the old key id and key material, and signs
  anything, silently un-retires the old row. Retirement holds only while no
  process configured with the old id signs. The spec wording
  (`spec/key-set-v0.1.md`) says a retired key no longer signs new objects; the
  runtime does not enforce that.
- `rotate_active_key_metadata` changes the configured id only on the instance
  that calls it. Other replicas keep their startup id.

Rollout order that avoids the problem:

1. Deploy every replica with the new `TRUST_SIGNING_KEY_ID` and the matching
   new `TRUST_SIGNING_PRIVATE_KEY_B64`. The new row is created `active` at
   startup in trust mode, or on the first signature. Nothing requires a single active row, and the
   public key set lists every row that is not `disabled`.
2. Confirm no replica or scheduled job still runs the old id.
3. Only then retire the old row with `retire_key_metadata`. It is a manual
   service call; no route calls it.
4. Check `GET /.well-known/trust-keys.json`: the old `kid` should show
   `retired` and the new `kid` `active`. Check again after normal traffic, since
   a stale replica un-retires the old row on its next signature.

How an operator can and cannot tell a replica is stale:

- Health does not show it. The signing-key health check
  (`validate_signing_key_configuration`) validates configuration only and
  never reads the key row status, so a stale replica reports `up`.
- The reactivation is silent. It writes no log line and returns no error.
- The only visible sign is the published key set: the old `kid` flipping from
  `retired` back to `active`.

Why the code is not changed: making a retired key refuse to sign
(`signing_key_retired`) would stop the reactivation, but the pending-refund
path relies on it. `RefundReconciliationService.create_pending` resolves the
signing key per attempt and restarts after a concurrent rotation, and the
restart currently succeeds because `ensure_active_key` reactivates the key. A
refusal there would escape as an internal error and lose both the receipt and
the refund work item for a charge that already happened. The maintainer chose
documentation only for now. The analysis and the parked follow-up changes are
in [issue 280](https://github.com/agentmiddleware/agent-middleware/issues/280).

This is documented as a known limitation in `SECURITY_LIMITATIONS.md` and
should be the default answer to "where do the private keys live?" until the
KMS work lands.
