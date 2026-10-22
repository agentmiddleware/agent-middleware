# Pods

A pod is a name for a group of agent API keys that share one budget. It is
not a new wallet type, and it is not part of the supported one-tool pilot.

`POST /v1/pods` and `GET /v1/pods/{pod_id}` both call
`require_bootstrap_admin`. A wallet-scoped key is refused. The create call
writes one sponsor wallet, one agent wallet per member, and one key per
member, in one database transaction. If any step fails, that transaction
rolls back and no wallet or key from the call is left behind. The read call
returns budget status and does not return key secrets.

The router is in `DORMANT_TRUST_ROUTERS`. It is mounted only when
`ENABLE_PROOF_SURFACES=true`. With that flag off, production does not mount
or advertise `/v1/pods`.

Limits, matching `app/schemas/pods.py`:

- no department templates
- no on-demand provisioning trigger
- no cross-pod budget sharing
- at most 50 members (`MAX_POD_MEMBERS`)

Code: `app/routers/pods.py`, `app/services/pods.py`, `app/schemas/pods.py`.
