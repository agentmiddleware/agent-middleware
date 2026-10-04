# Schema 040: compatibility release and rollback

Migration `040_permit_repeat_window` adds a nullable signed permit constraint.
It preserves existing permits, but **does not support rollback to an image
whose packaged Alembic head is 039**, including `8c95229`. Such an image
cannot restart at 040 and cannot verify new non-null repeat-window permits.
Never downgrade 040 or remove its column as an application rollback: the
stored values are part of signatures and finalized-request replay.

## Compatibility release first

The `openclaw/amw-schema040-bridge` branch starts from the previously serving
`8c95229` and adds the minimum schema, signature, SDK, and enforcement support
needed for a schema-040 fallback. Its new issuance gate is disabled by default.
It does not bypass schema parity, signature validation, or duplicate policy.

This particular fallback is qualified for the first-party instance still serving
`8c95229` at schema 039. It is not a universal rollback for an instance that
already served `84df9e7`: that version stored windowless creation hashes with
an explicit null window. Such an instance needs a fallback that accepts both
hash shapes. The merged release accepts both shapes but writes new windowless
records in the pre-040 shape, keeping this first-party fallback compatible.

1. Record the exact compatibility commit as `BRIDGE_SHA` before integrating
   later main changes. Require its own successful **push** CI run; the CI
   workflow explicitly includes this one branch. A PR merge-ref run or a green
   descendant does not substitute for exact-commit push CI.
2. Integrate the compatibility commit into main with a **merge commit**, not
   squash/rebase. Preserve that commit as a main ancestor. Require green CI on
   the final merged main SHA as well. Review the resulting main diff so no
   already-merged behavior or fix is reverted.
3. Rehearse 039 to 040 on disposable PostgreSQL. Verify preserved legacy
   permits, schema-040 restart, old-style permit-creation replay, signed-window
   enforcement and tamper rejection with issuance disabled. Missing columns
   and mismatched revision stamps must still fail closed.
4. Use the canonical clean, detached exact-SHA `prepare_railway_release.py`
   archive and immutable `railway up` upload for both releases. Keep
   `ENABLE_PERMIT_REPEAT_WINDOW_ISSUANCE=false` (its default) while deploying
   `BRIDGE_SHA`. Verify this setting inside the selected live instance. Apply
   040 through the migration-on-start entrypoint only after confirming the
   existing database is already at 039. Earlier revisions need their own
   migration review, including 037's maintenance gate.
5. Wait until the compatibility deployment is successful and is the sole
   running, traffic-eligible worker. Require private schema parity at 040,
   runtime posture, an SSH success sentinel, healthy public dependencies, and
   both health routes reporting the exact baked SHA with provenance `stamped`.
   Inspect logs. Record deployment ID, instance ID, full SHA, and results.
   Only then is `BRIDGE_SHA` a live-verified rollback target.
6. Deploy the reviewed merged main SHA using the same immutable path. New
   issuance may be enabled only after every remaining worker is compatible.
   Stage only `ENABLE_PERMIT_REPEAT_WINDOW_ISSUANCE=true` without automatic
   redeployment, then upload the exact approved main context. Do not trigger a
   rebuild from stale Railway GitHub source metadata. Preserve all credentials
   and other configuration. Verify the flag privately in the final instance.

The first compatibility deployment crosses the old-image restart boundary.
If it fails after 040 is applied, recover using a qualified 040-compatible
artifact; `8c95229` is no longer a valid recovery image. Do not advertise zero
downtime. Qualification of a fallback is not proof that a production rollback
has actually been exercised.

## Rollback after activation

Stage issuance disabled, without triggering a source redeploy, and upload the
recorded `BRIDGE_SHA` from its clean detached archive. Leave database 040 and
all signing material intact. Verify sole-worker identity, schema, runtime
posture, public exact-SHA health, and logs again. Previously issued non-null
permits must still verify and retain their window; the flag controls only
**new issuance**, not verification, invocation, or completed creation replay.
Do not substitute an arbitrary older commit for the recorded fallback.

## API behavior during disabled issuance

`POST /v1/permits` rejects a new non-null `repeat_window_seconds` with HTTP 400
and `repeat_window_issuance_disabled`, before persisting a permit or new
idempotency record. Re-enabling permits the same previously refused key to be
used. Completed creation replays still return their original permit; changed
payloads and pending requests retain their conflict behavior. Omitted and
explicit-null windows keep the pre-040 request hash. No other null fields are
removed from the hash.

Use current main's public preflight for the bridge image if its bundled
preflight predates the locked-down catalog checks. Pin private checks to the
actual instance; old images can use the documented inline runtime check.
Public-key continuity across these releases is not independent qualification
of the production signing identity or customer acceptance.
