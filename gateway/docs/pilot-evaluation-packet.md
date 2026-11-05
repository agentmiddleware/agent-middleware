# Pilot evaluation packet

Use this working template to scope, run, and review one partner-owned staging
action. It turns the [design partner guide](../DESIGN_PARTNER_GUIDE.md) and
[first-tool runbook](partner-first-tool-runbook.md) into a record both parties
can complete by email.

The empty fields are intentional. This file contains no customer, execution,
payment, or test results. Copy it into an approved private workspace before
entering partner details. Keep the completed packet and its evidence out of the
public repository. Do not put credentials, private keys, or access links in it.

The runbook's [pass and fail criteria](partner-first-tool-runbook.md#pass--fail)
remain the technical and commercial gate. This packet records the evidence;
it does not replace deployment approval, security review, or a signed agreement.

## 1. Scope and decision

Record a specific unresolved problem before configuring a tool. If the
partner's existing controls already solve it adequately, record that finding
and decide whether further integration is worthwhile.

| Field | Agreed value or private evidence reference |
| --- | --- |
| Packet owner and version | |
| Partner and contracting parties | |
| Partner engineer and integration owner | |
| Budget owner and decision maker | |
| One agent and one consequential staging action | |
| Exact upstream MCP server and tool name | |
| Tool owner and permission to run the action | |
| Current approval, retry, and reconciliation controls | |
| Specific unresolved failure or blocked use | |
| Operational consequence and how it will be measured | |
| Authoritative downstream lookup and its owner | |
| Baseline implementation and version | |
| Data owner, permitted data, redaction, retention, and deletion rules | |
| Dedicated deployment and supported transport confirmed | |
| Scope exclusions and written change process | |
| Approved scope document and commercial version | |
| Agreed fee, payment milestones, and invoice counterpart | |
| Agreed duration and engineering effort limit | |
| Prerequisites confirmed by both parties, with date | |
| Evaluation start, end, and decision date, with timezone | |
| Definition of delivered work and customer acceptance | |

Commercial terms come from the one approved private proposal. Do not infer a
price or duration from another draft, an earlier pilot, or this repository.
The evaluation clock starts only when the agreed prerequisites are satisfied.
A proposed fee, an invoice, and a received payment are different states.

### Email intake

Ask the partner to answer these questions in writing, then put the actual
answers in the scope above:

1. Which action should we evaluate, and what happens if its result is unknown?
2. What already prevents duplicate effects, and what remains unresolved?
3. Who owns the staging endpoint and the authoritative execution record?
4. Who can supply integration time and approve a paid evaluation?
5. What measured result would justify continuing, and by what decision date?

An interested technical reviewer is not automatically a buyer. A meeting,
positive reply, or successful demo does not establish an approved budget.

## 2. Readiness before a test window

Complete this checklist for the exact deployment that will be exercised.
Repository tests, a historical public receipt, and an echo demonstration are
different evidence from a live partner integration.

- [ ] Record the deployed commit, image or build identifier, environment, and
      observation time. Confirm the running build rather than inferring it
      from the current main branch.
- [ ] Record the enforced configuration and its evidence. Confirm governed
      invocation, the exact allowed tool, authentication, and the applicable
      [security limitations](../SECURITY_LIMITATIONS.md).
- [ ] Record the applicable release review and any unresolved blocking issue.
      A blank review or an open blocker is not permission to proceed.
- [ ] Agree the test window, total requests, concurrency, call and internal
      credit limits, operator, stop condition, and recovery contact.
- [ ] Provision scoped test access through the existing
      [bootstrap procedure](partner-api-key-bootstrap.md). Store only the
      credential reference, expiry, and revocation owner in this packet.
- [ ] Agree the operation identity, who creates it, and where it is persisted
      before the first attempt. Preserve it across retries and restarts; see
      [request keys](concepts/request-keys.md).
- [ ] Establish an authorized downstream lookup that can distinguish accepted,
      completed, and unresolved work. Record lookup consistency and visibility
      delays. A missing row is not proof of no effect.
- [ ] Verify that the baseline uses its native idempotency and approval controls
      correctly. Agree comparable but separate operations for baseline and AMW
      runs so one run does not change the other's result.
- [ ] Agree a safe fault-injection and cleanup procedure. Do not perform a
      second consequential action merely to discover whether the first worked.
- [ ] Have the partner record the expected verification key and how it was
      obtained. Downloading a key from the receipt issuer alone does not
      independently establish the issuer's identity.

Record readiness as **ready**, **blocked**, or **not assessed**, with the owner,
reason, and next evidence needed. This is readiness for the agreed test, not a
claim of general production readiness.

## 3. Evaluation matrix

Before each case, record the expected behavior for the deployed version and
the partner's tool. Use the existing
[failure semantics](failure-semantics.md),
[failure lab](failure-lab.md), and first-tool runbook as implementation
references. A local failure-lab result is not a partner result.

Use **not run**, **blocked**, **pass**, **fail**, or **unknown** for each result.
Missing evidence means unknown or blocked, never zero, success, or an inferred
pass. Record the actual reason codes rather than inventing expected responses.

| Case | Procedure and expected boundary | Evidence required | Initial status |
| --- | --- | --- | --- |
| Normal action | Perform one approved staging action and correlate the records. | Accepted request identity, gateway dispatch, internal debit, receipt, and authoritative downstream lookup. | Not run |
| Native baseline | Run the agreed case using the partner's correctly configured existing controls. | Baseline version, operation identity, result, effect lookup, latency, and operator work. | Not run |
| Same-key concurrency | Retry unchanged arguments under the same accepted identity within the approved concurrency bound. Expect at most one gateway dispatch and debit. After finalization, replay returns the recorded outcome and receipt where available. | Every attempt and response, authoritative dispatch and ledger records, replay correlation, and separate downstream effect count. | Not run |
| Changed arguments | Reuse that accepted identity with different arguments. Expect a conflict and no additional dispatch or debit for the changed request. | Original and changed request hashes, actual refusal, and before/after dispatch and ledger records. | Not run |
| New operation key | Repeat the argument set with a new key only where the test plan authorizes another operation. A new key is a new request, subject to explicit permit and budget controls. | Both identities, configured constraints, and separate dispatch, debit, and effect records. Do not assume semantic duplicate suppression. | Not run |
| Authority refusal | Exercise missing, expired, revoked, or out-of-scope authority within the approved test plan. Expect refusal before charge or dispatch on the supported governed path. | Actual reason, policy and scope, ledger and dispatch observations. Do not require a receipt on paths that do not issue one. | Not run |
| Concurrent budget limit | Race distinct approved operations against an agreed credit or call cap. Expect the applicable configured limit to hold. | Starting allowance, accepted and denied operations, reservations where applicable, and final authoritative accounting. Credits are internal metering units, not money. | Not run |
| Lost response after dispatch | Inject the agreed failure after dispatch. Preserve the original key. Where no trustworthy result can be established, record the documented uncertain outcome and reconcile; do not automatically redispatch. | Fault timing, retained accounting, dispatch evidence, receipt or recovery state, downstream lookup, and the operator's resolution. | Not run |
| Restart and recovery | Restart only within the approved window, then recover the persisted original identity. Follow the applicable recovery procedure. | Build/configuration before and after, original identity, durable record, recovery result, and any unresolved state. | Not run |
| Independent verification | The partner verifies the exported receipt in its own environment. Verify a modified copy fails and an untrusted key does not become a verified result. Preserve the original artifact. | Exact bundle and key-set hashes, verifier version, commands, exit status, output, key provenance, and partner engineer's result. | Not run |

The boundary is at most one gateway dispatch and debit per accepted request
identity. Downstream effects depend on the tool's behavior and must be checked
separately. A signed receipt establishes integrity of the signed bytes relative
to a trusted key; it does not establish the truth of an external effect. An
uncertain call can pass an uncertainty-handling test while its downstream
business outcome remains unresolved. Keep those two results separate.

## 4. Per-case evidence record

Copy this table for each executed case. Keep original artifacts immutable and
record their actual SHA-256 hashes. A hash identifies the captured bytes; it
does not make a source authoritative. If an artifact needs redaction, keep the
original in the approved restricted location and identify the redacted
derivative separately.

| Field | Observed value or private evidence reference |
| --- | --- |
| Case, tester, start and end time, timezone | |
| Deployed build and configuration evidence | |
| Baseline or AMW path; client and upstream version | |
| Approved operation identity and argument hash | |
| Generated permit, wallet, and request record references | |
| Fault location and actual injection method | |
| Attempt count and observed responses | |
| Gateway dispatch count and authoritative source | |
| Debit count, credit amount, and authoritative source | |
| Downstream effect count, lookup source, and observation window | |
| Receipt and verifier result, or reason no receipt exists | |
| Artifact locations, SHA-256 hashes, and capture times | |
| Latency samples and sample count | |
| Active operator time and integration/support time | |
| Technical result and remaining uncertainty | |
| Reviewer and exact evidence reviewed | |

Use product-generated identifiers, receipts, ledger entries, and signatures.
Do not hand-write substitute evidence or infer authoritative counts from
best-effort usage telemetry. If the exact count is unavailable, say so.

## 5. Baseline and value comparison

Agree the benefit threshold before the run. Compare equivalent conditions and
record differences in configuration, failure timing, sample size, and operator
experience. Do not conclude that AMW improves native idempotency merely because
both paths prevent duplicates.

| Measure | Existing controls | With AMW | Interpretation and source |
| --- | --- | --- | --- |
| Correctly identified completed and unresolved operations | | | |
| Manual steps after an uncertain result | | | |
| Active operator time per investigated operation | | | |
| Integration and support effort | | | |
| Added latency and sample count | | | |
| Evidence the partner can reproduce independently | | | |
| Remaining failure or blocked use | | | |
| Agreed benefit threshold achieved | | | |

Small staging samples do not establish production incident rates, savings, or
an availability guarantee. Record a qualitative finding where the evidence
does not support a numerical claim. Keep internal credit accounting separate
from customer payments and any downstream monetary effect.

## 6. Delivery and decision

The written scope defines when delivery is complete. Deliver an honest report
even when a case fails or cannot be resolved. Technical success, completed
delivery, invoice status, payment, and continued use are separate facts.

- [ ] Deliver the agreed integration and configuration instructions.
- [ ] Include the executed matrix, original evidence references, limitations,
      failures, and cases not run.
- [ ] Include the partner engineer's independent verification and rerun result.
- [ ] Explain the baseline comparison and the measured integration burden.
- [ ] Deliver the operator procedure for uncertain outcomes, escalation,
      access revocation, and offboarding.
- [ ] Record delivery acknowledgment and any unmet contracted deliverable.
- [ ] Record the buyer's decision, named owner, reason, and next decision date.
- [ ] Agree any continuation scope and price separately. Do not enroll the
      partner automatically or treat a useful test as a purchase commitment.
- [ ] Obtain explicit permission before publishing a name, quote, result, or
      case study. Permission to run the evaluation is not publication consent.

| Decision field | Actual response or private evidence reference |
| --- | --- |
| Delivered work and remaining obligations | |
| Technical conclusion and unresolved business outcomes | |
| Benefit relative to existing controls | |
| Buyer decision: continue, bounded follow-up, decline, or pending | |
| Reason, owner, and next action/date | |
| Contract and invoice state; payment evidence if applicable | |
| Access revocation and data-retention completion | |
| Separate continuation agreement, if any | |
| Publication permission, if any | |

If the buyer cannot identify a meaningful benefit, record that result before
adding integrations or widening the product scope.
