# OWASP Top 10 for Agentic Applications (2026) — Mapping

This maps each risk in the [OWASP Top 10 for Agentic Applications](https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/)
(ASI01–ASI10, published December 2025) to this trust plane's actual posture:
the enforcing code, the proof that exercises it, and the honest gap. It is
written for security reviewers who evaluate in that vocabulary.

Two framing rules keep this honest:

- **This plane is a containment and evidence layer at the agent→tool
  boundary.** It does not inspect prompts, model outputs, or agent reasoning.
  Several ASI risks live above or below that boundary; those rows say
  "contained, not prevented" or "out of scope" instead of stretching a claim.
- **Posture labels follow the repo's reality levels.** *Enforced* means the
  control runs on the governed path and a named proof attacks it. *Contained*
  means the risk is not prevented but its blast radius is bounded and
  evidenced. *Partially addressed* means part of the risk's surface is
  structurally absent here rather than defended, and the remainder is
  unmanaged. *Out of scope* means this layer deliberately does not address it.
  See [SECURITY_LIMITATIONS.md](../SECURITY_LIMITATIONS.md) and
  [threat-model.md](threat-model.md) for the boundary itself.

## Summary

| Risk | Posture here | Primary proof |
|---|---|---|
| ASI01 Agent Goal Hijack | Contained | Scope-escape attack HELD; signed denial receipts |
| ASI02 Tool Misuse & Exploitation | Enforced | Budget/scope invariant attacks; negative-path tests |
| ASI03 Agent Identity & Privilege Abuse | Enforced | Credential-misuse attack HELD; cross-tenant 403 |
| ASI04 Agentic Supply Chain | Partially addressed | Pinned single upstream origin; no registry surface |
| ASI05 Unexpected Code Execution | Out of scope (by design) | No code-execution surface on the governed path |
| ASI06 Memory & Context Poisoning | Out of scope / self-protected | Tamper attacks on receipts and audit chain HELD |
| ASI07 Insecure Inter-Agent Communication | Out of scope (dormant proof surface) | Simulation-gated; unmounted by default |
| ASI08 Cascading Failures | Contained | Crash-consistency proof; fail-closed ambiguity handling |
| ASI09 Human-Agent Trust Exploitation | Contained | Offline receipt verification; approval gate |
| ASI10 Rogue Agents | Contained | Budget-cap race fixed and HELD; revocation |

## ASI01 — Agent Goal Hijack

**Posture: contained, not prevented.** Prompt injection and goal manipulation
happen inside the agent, above this boundary; the gateway cannot see them. What
it guarantees instead: a hijacked agent still cannot act beyond its permit. A
call outside `allowed_tools`, over budget, or past expiry is refused, and the
refusal itself is a signed, replayable receipt — evidence of the attempted
action, not just a dropped request.

- Enforcement: permit validation and policy checks on the governed MCP path
  (`app/services/permits.py`, `app/routers/mcp.py`).
- Proof: `scripts/invariant_attacks/attack3_scope.py` (HELD); the denial
  receipt leg of `make prove-trust-plane`; `tests/test_trust_negative_security.py`.
- Gap: no prompt/content inspection, no anomaly detection on call patterns.

## ASI02 — Tool Misuse & Exploitation

**Posture: enforced.** A tool call requires a signed permit naming the tool,
with a spend cap, expiry, and use count. The reservation is atomic under
concurrency (`authorize_and_reserve`, `app/services/permits.py`), and an
accepted idempotency key is bound to the request's content hash — the same key
with a different payload fails closed.

- Proof: `scripts/invariant_attacks/attack2_budget.py` — the campaign's
  headline finding: ten concurrent calls overspent a 7-credit cap ~3× on
  SQLite because SQLAlchemy silently drops `FOR UPDATE` there; fixed, re-run,
  now HELD on both engines, and SQLite is refused in production
  ([invariant-attack-report.md](invariant-attack-report.md)).
  Also `tests/test_constant_loop_permit_budget.py`,
  `tests/test_governed_persistence.py`.
- Gap: the pilot governs one operator-configured upstream origin and one exact
  tool; argument-level semantic validation is the tool's job, not the plane's.

## ASI03 — Agent Identity & Privilege Abuse

**Posture: enforced for DB-backed wallet keys.** A DB-issued key (stored as a
SHA-256 hash, with fixed-length digests compared constant-time by
`APIKeyService.validate_key` in `app/services/api_key_service.py`) is scoped to
exactly one wallet: cross-tenant reads and invokes return 403, and
rotation/replacement keys inherit their parents' bounds rather than
escalating. Bootstrap-admin keys (`VALID_API_KEYS`, `STATIC_DEV_API_KEYS`)
are the intentional exception: `AuthContext.require_wallet_access`
(`app/core/auth.py`) grants them every wallet by design, so this claim does
not extend to admin-shaped credentials. `STATIC_DEV_API_KEYS` is refused at
boot on a production-like deployment (`app/core/trust_mode.py`);
`VALID_API_KEYS` is not — it is the intended production bootstrap mechanism.

- Proof: `scripts/invariant_attacks/attack6_key_misuse.py` (invalid, revoked,
  confused-deputy, and cross-tenant cases — HELD, exercised with DB-backed
  keys); `tests/test_tool_interface_authority.py`; the tenant-isolation leg
  of `make prove-trust-plane`.
- Gap: single-tenant vendor-managed pilot; application-layer isolation only —
  no row-level security, no external IdP/KMS integration. A bootstrap-admin
  key is a full-control credential by design, not a tenant boundary — treat
  it like a root key, not like proof that isolation holds against it.

## ASI04 — Agentic Supply Chain Compromise

**Posture: partially addressed.** There is no tool marketplace or dynamic
registry to poison: executable tools are local callables or the single
operator-configured upstream. Upstream connections validate the destination,
refuse unsafe redirects, and pin one resolved address per session
([SECURITY_LIMITATIONS.md](../SECURITY_LIMITATIONS.md)).

Agent-supplied URLs on the dormant AWI browser bridge and behavioral sandbox
proxy pass a separate outbound-URL guard (`app/core/url_guard.py`) that blocks
loopback, RFC1918, and link-local targets. Its local-development escape hatch,
`ALLOW_PRIVATE_NETWORK_TARGETS=true`, skips those checks; production-like boots
refuse to start with it set (`app/core/trust_mode.py`).

- Gap: dependency and artifact attestation for this repo itself follows normal
  CI practice, nothing stronger; multi-upstream registry governance is
  deliberately not implemented.

## ASI05 — Unexpected Code Execution

**Posture: out of scope, by design.** The governed path dispatches JSON-RPC
tool calls; it does not execute agent-supplied code. Request bodies are
size-capped before parsing. Sandbox-flavored modules in the tree are dormant
proof surfaces, refused at runtime unless simulation is explicitly enabled
(`require_simulation`, `app/core/runtime_mode.py`) — they are not isolation
boundaries and are not mounted by default.

- Gap: process-level execution control (shell, file writes) is a different
  enforcement point — complementary to a gateway, not provided by it.

## ASI06 — Memory & Context Poisoning

**Posture: out of scope for agent memory; enforced for the plane's own
record.** This layer holds no agent memory to poison. Its own durable state —
receipts and the audit chain — is the tamper target it defends: receipts are
Ed25519-signed, audit events are hash-chained and signed per event, and both
have independent verification endpoints.

- Proof: `scripts/invariant_attacks/attack4_forgery.py` (HELD); the tamper
  legs of `make prove-trust-plane` (`receipt_signature_invalid`,
  `audit_payload_hash_mismatch`).
- Gap: chains are tamper-*evident*, not immutable — a database administrator
  can delete rows; no external anchoring or transparency log yet.

## ASI07 — Insecure Inter-Agent Communication

**Posture: out of scope.** Agent-to-agent messaging exists in the tree only as
a dormant, simulation-gated proof surface (`app/services/agent_comms.py`),
unmounted and unadvertised by default. No inter-agent protocol claim is made.

## ASI08 — Cascading Failures

**Posture: contained.** The failure contract is explicit per outcome
([failure-semantics.md](failure-semantics.md)): a crash or timeout after the
durable dispatch checkpoint is signed `delivery_uncertain`, keeps its single
debit, and is never automatically redispatched — ambiguity fails closed to
manual reconciliation instead of fanning out retries.

- Proof: `make prove-crash-recovery` (two-process `kill -9` at durable commit
  boundaries against PostgreSQL); `scripts/invariant_attacks/attack5_crash_sqlite.py`.
- Gap: at-most-once dispatch at the gateway does not make the remote side
  effect happen once unless the upstream honors the forwarded idempotency key.

## ASI09 — Human-Agent Trust Exploitation

**Posture: contained.** A human never has to take the agent's (or the
operator's) word for what happened: any third party can verify a receipt
offline against the published keys (`/.well-known/trust-keys.json`,
`b2a-verify-receipt` / `verify_bundle` in `b2a_sdk`). High-risk calls can be
paused on an explicit human decision ([human-approval-gate.md](human-approval-gate.md)).

- Gap: a receipt proves what happened, never what did not; offline
  verification trusts the issuing origin for key distribution (no out-of-band
  pinning yet).

## ASI10 — Rogue Agents

**Posture: contained.** A rogue or runaway agent exhausts its permit and
stops: spend caps and use counts bound total damage, expiry bounds duration,
and every action, allowed or refused, is metered and receipted. API-key
revocation and rotation are operational today. Env bootstrap keys use
`scripts/rotate_api_keys.py` plus a worker restart
([api-key-rotation.md](api-key-rotation.md)). Wallet keys use
`DELETE /v1/api-keys/{wallet_id}/{key_id}`,
`POST /v1/api-keys/rotate` with `revoke_old` true, and
`POST /v1/api-keys/emergency-revoke`. There is no separate retirement
script in `scripts/`. Signing-key retirement is not operational: see
[key-management.md](key-management.md).

- Proof: the ASI02 budget-race fix (HELD on both engines);
  `tests/test_constant_loop_permit_budget.py`.
- Gap: no behavioral detection — the plane bounds a rogue agent's authority,
  it does not notice the rogue.

## What this mapping does not claim

The plane is one enforcement point — the network boundary between agent and
tool. Risks that live in the model (ASI01, ASI06), the process (ASI05), or
between agents (ASI07) need controls at those layers; this mapping marks them
contained or out of scope rather than claiming coverage. Attacks on the
documented gaps above are the most useful ones a reviewer can run — start from
[SECURITY_LIMITATIONS.md](../SECURITY_LIMITATIONS.md) and the rules of
engagement in [security-review-kit.md](security-review-kit.md).
