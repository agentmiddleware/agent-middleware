# Schema 041: content owner-key scrub and rollback boundary

Migration `041_scrub_content_owner_keys` revises `040_permit_repeat_window`.
It is data-only. It blanks `owner_key` in `content_pipelines` and
`content_campaigns` wherever the value is not a known wallet id; those values
were raw API keys stored by the content factory before it recorded wallet ids.
No column changes shape. The downgrade is a no-op: the scrubbed values were
credentials and are not restored.

## Release

No compatibility release is needed, because no schema shape changes. Workers
already running a 040 image keep serving while 041 is applied; the schema
check runs only at startup.

1. Confirm the production database is at `040_permit_repeat_window`. An earlier
   revision needs its own migration review first.
2. Deploy the reviewed main SHA with a separately reviewed exact-SHA operator
   process. This repository has no hosted deploy runbook or IaC graph. Set
   `RUN_MIGRATIONS_ON_START=true` so the entrypoint applies 041 before uvicorn
   starts, and verify the image digest and runtime settings privately.
3. Record the deployed SHA, deployment id, and the result of the private schema
   parity check. That SHA is now the earliest valid recovery image.

## Rollback after activation

Once the database is at 041, an image whose packaged Alembic head is 040 cannot
start again. Two independent checks stop it:

- The entrypoint's `alembic upgrade head` fails with
  `Can't locate revision identified by '041_scrub_content_owner_keys'` and
  exits non-zero, so uvicorn never starts.
- Without migration-on-start, the boot schema check refuses the database. Its
  message reads `Database Alembic revision is behind packaged head:
  current=['041_scrub_content_owner_keys'] heads=['040_permit_repeat_window']`,
  although the database is ahead of the image, not behind it.

Both were reproduced against a database migrated to 041, using the images at
`34fbdb9` (merged main serving first-party production when 041 merged) and
`e18b0df` (the schema-040 compatibility release recorded in
[schema-040-rollout.md](schema-040-rollout.md)). Neither, nor `8c95229`, is a
valid recovery image after 041 is applied.

Roll back only to an image whose packaged head is 041 or later. Do not run
`alembic downgrade` to 040 as a rollback. It restores no data and only moves
the stamp, which would let a 040 image start and resume writing raw API keys
into `owner_key` on any deployment that mounts the content factory.
