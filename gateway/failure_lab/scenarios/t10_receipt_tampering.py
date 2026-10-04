"""Test 10 -- Receipt tampering."""

from __future__ import annotations

import base64
import json
from typing import Any

from failure_lab.configurations import GATEWAY_CONFIGURATIONS, Configuration, Target
from failure_lab.identity import OperationIdentity
from failure_lab.scenarios.base import ConfigurationResult, EventLog, Scenario, Verdict
from failure_lab.verifier import (
    ENVELOPE_ONLY_FIELDS,
    ClaimStatus,
    KeySource,
    VerificationReport,
    field_coverage,
    parse_key_document,
    tamper,
    verify,
)

#: One forged value per signed field named in the scenario specification. Each
#: is an edit an attacker would actually want: a different charge, a different
#: tool, a different outcome, a different subject.
_SIGNED_FORGERIES: dict[str, Any] = {
    "credits_charged": "999999",
    "tool": "tool.attacker.controlled",
    "outcome": "failed_refunded",
    "created_at": "2001-01-01T00:00:00+00:00",
    "permit_id": "permit_forged_by_t10",
    "kid": "kid-forged-by-t10",
    "receipt_id": "rcpt_forged_by_t10",
    "wallet_id": "wallet_forged_by_t10",
    "request_hash": "0" * 64,
    "response_hash": "f" * 64,
}

#: The envelope side of the same matrix. ``signature`` is filled in at runtime
#: with a well-formed but wrong 64-byte Ed25519 signature, so the verifier
#: exercises the signature check itself rather than a length check.
_ENVELOPE_FORGERIES: dict[str, Any] = {
    "issuer": "https://receipts.attacker.example",
    "kid": "kid-envelope-forged-by-t10",
    "canonicalization": "attacker-canonical-v0",
    "schema_version": "0.0-attacker",
    "keys_url": "https://attacker.example/.well-known/trust-keys.json",
    "receipt_id": "rcpt_envelope_forged_by_t10",
}

_CLAIM_NAMES = (
    "SIGNATURE_VALID",
    "ISSUER_TRUST_ESTABLISHED",
    "DOWNSTREAM_EXECUTION_ESTABLISHED",
)


def _claims(report: VerificationReport) -> dict[str, str]:
    return {
        "SIGNATURE_VALID": report.signature.status.value,
        "ISSUER_TRUST_ESTABLISHED": report.issuer_trust.status.value,
        "DOWNSTREAM_EXECUTION_ESTABLISHED": report.downstream_execution.status.value,
    }


def _failure_mode(report: VerificationReport) -> str | None:
    """Why the signature claim failed, so the report does not blur two things.

    An edit can be caught by the Ed25519 check itself, or earlier -- the
    verifier picks its key by the ``kid`` *inside* the signed bytes, so a
    forged kid names a key the plane does not publish and never reaches the
    cryptographic check. Both mean the edit did not survive, but only the
    first is evidence that the signature covers those bytes, so they are
    counted separately rather than reported as one number.
    """
    if report.signature.status is ClaimStatus.ESTABLISHED:
        return None
    reason = report.signature.reason
    if "does not verify over signing_input" in reason:
        return "signature_check"
    if (
        "no published key" in reason
        or "names no key" in reason
        or "Ed25519 key" in reason
    ):
        return "key_resolution"
    if "not a 64-byte Ed25519 signature" in reason:
        return "signature_decode"
    return "other"


def _distinct(original: Any, forged: Any) -> Any:
    """Guarantee the edit is an edit.

    A "tampered" value equal to the original would re-serialize to the same
    bytes and verify, which would read as a signed field surviving tampering.
    That would be a false FAIL, so the harness refuses to measure it.
    """
    if original != forged:
        return forged
    if isinstance(forged, str):
        return forged + "-tampered"
    return f"{forged}-tampered"


class ReceiptTampering(Scenario):
    """Edit a receipt field by field and report exactly what the signature covers.

    WHAT TO IMPLEMENT
    -----------------
    1. Run one clean governed call so a genuine receipt exists. Export it with
       ``gateway.portable_receipt(tenant, receipt_id)`` and fetch
       ``gateway.trust_keys()``.
    2. Verify the genuine bundle with ``failure_lab.verifier.verify`` -- the
       INDEPENDENT verifier, not ``b2a_sdk`` and not ``POST /v1/receipts/verify``.
       Assert all three claims come back with the right statuses: signature
       ESTABLISHED, issuer trust NOT_ESTABLISHED (the key came from the
       audited origin), downstream execution ESTABLISHED only when the effect
       ledger observation is passed in. Exercise the no-observation call too
       and confirm it reports NOT_ESTABLISHED.
    3. Tamper matrix, using ``verifier.tamper(bundle, path, value)``, one
       modification at a time, each independently:
       ``signing_input.credits_charged``, ``signing_input.tool``,
       ``signing_input.outcome``, ``signing_input.created_at``,
       ``signing_input.permit_id``, ``signing_input.kid``,
       ``signing_input.receipt_id``, ``signing_input.wallet_id``,
       ``signing_input.request_hash``, ``signing_input.response_hash``,
       and the envelope-side ``issuer``, ``kid``, ``canonicalization``,
       ``schema_version``, ``keys_url``, ``receipt_id``, ``signature``.
    4. For each, record: the field, whether the signature still verified,
       which of the three claims changed, any envelope disagreement the
       verifier flagged, and whether ``verifier.field_coverage`` classifies
       the field as signed or envelope-only.
    5. Also check the gateway's OWN verification surface for the two most
       important cases (a tampered signed field and a tampered envelope field)
       via ``POST /v1/receipts/verify`` and record whether the two verifiers
       agree. A disagreement is a finding.
    6. Verdict: ``PASS`` iff every modification to a field inside
       ``signing_input`` causes the signature to fail, AND the genuine receipt
       verifies. ``FAIL`` if any signed-field edit still verifies.
    7. The envelope-only fields WILL survive tampering with a valid
       signature. That is a property of the format, not a bug, and it must be
       reported: put the surviving fields in ``extra["outside_signature"]``
       and add a ``remaining_risks`` entry naming them. Also add the standing
       risk that key distribution is first-party, so a compromised origin can
       serve keys that validate forged receipts -- signature validity is not
       issuer trust.
    """

    test_id = "T10"
    title = "Receipt tampering"
    claim = (
        "Every field inside the signed payload is covered by the signature; "
        "the fields that are not covered are named rather than implied."
    )
    tier = "fast"
    configurations = GATEWAY_CONFIGURATIONS
    inapplicable_reason = "a direct integration issues no receipts"
    expected = {
        Configuration.DIRECT_NAIVE.value: Verdict.NOT_APPLICABLE.value,
        Configuration.DIRECT_NATIVE.value: Verdict.NOT_APPLICABLE.value,
        Configuration.GATEWAY_NATIVE.value: Verdict.PASS.value,
        Configuration.GATEWAY_NAIVE.value: Verdict.PASS.value,
    }
    limitations = (
        "A valid signature establishes that the signing key signed the "
        "statement. It does not establish issuer trust while keys are fetched "
        "from the audited origin, and it never establishes that the "
        "downstream business action occurred.",
        "Envelope fields outside signing_input can be edited without breaking "
        "the signature. They are enumerated in the result.",
    )

    async def run_configuration(
        self, target: Target, log: EventLog
    ) -> ConfigurationResult:
        gateway, tenant, permit = target.require_gateway()
        configuration = target.configuration.value

        def emit(step: str, message: str, **data: Any) -> None:
            log.emit(
                step,
                message,
                scenario=self.test_id,
                configuration=configuration,
                **data,
            )

        # -- 1. one clean governed call, so a genuine receipt exists --------
        operation_id, refund = self.refund("pay_t10_tamper")
        identity = OperationIdentity.first_attempt(operation_id)
        emit(
            "call.submit",
            "submitting one clean governed refund so a genuine receipt exists",
            operation_id=operation_id,
            idempotency_key=identity.idempotency_key,
            permit_id=permit["permit_id"],
        )
        outcome = await target.agent.submit(identity, refund)
        attempts = [outcome]
        executions = target.ledger.execution_count(operation_id=operation_id)
        emit(
            "call.complete",
            f"gateway returned {outcome.status}; effect ledger shows {executions} execution(s)",
            status=outcome.status,
            client_visible_state=outcome.client_visible_state,
            receipt_id=outcome.receipt_id,
            receipt_outcome=outcome.receipt.get("outcome") if outcome.receipt else None,
            downstream_executions=executions,
        )

        receipt_id = outcome.receipt_id
        if receipt_id is None:
            measurements = await self.measure(
                target, attempts, operation_ids=[operation_id]
            )
            emit(
                "receipt.missing",
                "the governed call returned no receipt, so there is nothing to tamper with",
                status=outcome.status,
                reason=outcome.reason,
            )
            return self.result(
                target,
                verdict=Verdict.FAIL,
                observation=(
                    f"the governed call terminated as {outcome.status} but carried no "
                    "receipt, so the signed-payload coverage could not be measured at all"
                ),
                measurements=measurements,
                attempts=attempts,
                remaining_risks=[
                    "A terminalising governed call produced no receipt. Every claim "
                    "this scenario makes about signature coverage is unmeasured for "
                    "that path.",
                ],
                extra={"receipt_id": None, "downstream_executions": executions},
            )

        # -- exported evidence and the published keys -----------------------
        bundle = await gateway.portable_receipt(tenant, receipt_id)
        key_document = await gateway.trust_keys()
        keys = parse_key_document(key_document)
        coverage = field_coverage(bundle)
        signed_payload = json.loads(bundle["signing_input"])
        emit(
            "evidence.exported",
            f"exported the portable bundle and {len(keys)} published key(s)",
            receipt_id=receipt_id,
            envelope_fields=sorted(bundle),
            signed_fields=sorted(signed_payload),
            key_ids=sorted(keys),
            keys_url=bundle.get("keys_url"),
            issuer=bundle.get("issuer"),
        )

        observation = {
            "executions": executions,
            "operation_id": operation_id,
            "source": "failure-lab effect ledger (the gateway cannot write to it)",
        }

        # -- 2. the genuine bundle, with and without the ledger observation --
        genuine = verify(
            bundle,
            keys,
            key_source=KeySource.ISSUER_ORIGIN,
            downstream_observation=observation,
        )
        genuine_no_observation = verify(
            bundle, keys, key_source=KeySource.ISSUER_ORIGIN
        )
        baseline_claims = _claims(genuine)
        genuine_assertions = {
            "signature_established": genuine.signature.status
            is ClaimStatus.ESTABLISHED,
            "issuer_trust_not_established": (
                genuine.issuer_trust.status is ClaimStatus.NOT_ESTABLISHED
            ),
            "downstream_established_with_observation": (
                genuine.downstream_execution.status is ClaimStatus.ESTABLISHED
            ),
            "downstream_not_established_without_observation": (
                genuine_no_observation.downstream_execution.status
                is ClaimStatus.NOT_ESTABLISHED
            ),
        }
        genuine_ok = all(genuine_assertions.values())
        emit(
            "verify.genuine",
            "independent verifier on the untouched bundle: "
            + ", ".join(f"{name}={status}" for name, status in baseline_claims.items()),
            claims=baseline_claims,
            claims_without_observation=_claims(genuine_no_observation),
            assertions=genuine_assertions,
            signature_reason=genuine.signature.reason,
            issuer_trust_reason=genuine.issuer_trust.reason,
            downstream_reason=genuine.downstream_execution.reason,
            downstream_reason_without_observation=(
                genuine_no_observation.downstream_execution.reason
            ),
        )

        # -- control: re-serializing without changing a value must still verify
        control_field = (
            "tool" if "tool" in signed_payload else sorted(signed_payload)[0]
        )
        control_bundle = tamper(
            bundle, f"signing_input.{control_field}", signed_payload[control_field]
        )
        control = verify(control_bundle, keys, downstream_observation=observation)
        control_verifies = control.signature.status is ClaimStatus.ESTABLISHED
        emit(
            "verify.reserialization_control",
            (
                "re-serializing signing_input without changing any value still verifies"
                if control_verifies
                else "re-serializing signing_input without changing a value BROKE the "
                "signature -- every signed-field result below is confounded"
            ),
            field=control_field,
            signature=control.signature.status.value,
            reason=control.signature.reason,
        )

        # -- 3/4. the tamper matrix, one independent edit at a time ---------
        rows: list[dict[str, Any]] = []
        applied: dict[str, Any] = {}
        for name in _SIGNED_FORGERIES:
            if name not in signed_payload:
                rows.append(
                    {
                        "case": f"signing_input.{name}",
                        "field": f"signing_input.{name}",
                        "side": "signed",
                        "edited": False,
                        "note": "field absent from this receipt's signed payload",
                    }
                )
                emit(
                    "tamper.skipped",
                    f"signing_input.{name} is not present in this receipt's signed payload",
                    field=f"signing_input.{name}",
                )
                continue
            path = f"signing_input.{name}"
            forged = _distinct(signed_payload[name], _SIGNED_FORGERIES[name])
            applied[path] = forged
            rows.append(
                _probe(
                    bundle,
                    keys,
                    path=path,
                    value=forged,
                    original=signed_payload[name],
                    coverage=coverage,
                    baseline=baseline_claims,
                    observation=observation,
                    emit=emit,
                )
            )

        envelope_forgeries = dict(_ENVELOPE_FORGERIES)
        envelope_forgeries["signature"] = base64.b64encode(bytes(64)).decode()
        for name, forged_value in envelope_forgeries.items():
            forged = _distinct(bundle.get(name), forged_value)
            applied[name] = forged
            rows.append(
                _probe(
                    bundle,
                    keys,
                    path=name,
                    value=forged,
                    original=bundle.get(name),
                    coverage=coverage,
                    baseline=baseline_claims,
                    observation=observation,
                    emit=emit,
                )
            )

        signed_rows = [row for row in rows if row.get("side") == "signed"]
        envelope_rows = [row for row in rows if row.get("side") == "envelope"]
        signed_measured = [row for row in signed_rows if row.get("edited")]
        signed_unmeasured = [
            row["field"] for row in signed_rows if not row.get("edited")
        ]
        signed_survivors = [
            row["field"] for row in signed_measured if row.get("signature_valid")
        ]
        detected_by_signature_check = [
            row["field"]
            for row in signed_measured
            if row.get("failure_mode") == "signature_check"
        ]
        detected_before_signature_check = [
            row["field"]
            for row in signed_measured
            if row.get("failure_mode") not in (None, "signature_check")
        ]
        survived = [row for row in envelope_rows if row.get("signature_valid")]
        survived_fields = [row["field"] for row in survived]
        silent_survivors = [
            row["field"] for row in survived if not row["envelope_disagreements"]
        ]

        # -- supplementary: reach the cryptographic check for kid -----------
        # The main kid probe stops at key resolution -- the verifier picks its
        # key by the kid inside the signed bytes, so a forged kid names a key
        # the plane does not publish. That proves the edit does not survive,
        # but not that the bytes are covered. Re-run it with the forged kid
        # mapped to the real public key, which forces the Ed25519 check.
        kid_probe: dict[str, Any] | None = None
        if "signing_input.kid" in applied:
            real_kid = signed_payload.get("kid")
            augmented = dict(keys)
            if isinstance(real_kid, str) and real_kid in keys:
                augmented[str(applied["signing_input.kid"])] = keys[real_kid]
            kid_probe = _probe(
                bundle,
                augmented,
                path="signing_input.kid",
                value=applied["signing_input.kid"],
                original=signed_payload.get("kid"),
                coverage=coverage,
                baseline=baseline_claims,
                observation=observation,
                emit=emit,
                side="signed_supplementary",
            )
            kid_probe["why"] = (
                "the forged kid is mapped to the genuine public key, so the "
                "verifier resolves a key and performs the Ed25519 check instead "
                "of stopping at key lookup. This key map is the harness's, not "
                "the plane's: it measures byte coverage of signing_input.kid, it "
                "does not describe a key the deployment publishes."
            )
            rows.append(kid_probe)
        kid_probe_survived = bool(kid_probe and kid_probe.get("signature_valid"))

        # -- 5. the gateway's own verification surface ----------------------
        signed_case_field = (
            "signing_input.outcome"
            if "signing_input.outcome" in applied
            else next(
                (path for path in applied if path.startswith("signing_input.")), None
            )
        )
        signed_tampered = (
            tamper(bundle, signed_case_field, applied[signed_case_field])
            if signed_case_field
            else tamper(bundle, "signature", applied["signature"])
        )
        envelope_tampered = tamper(bundle, "receipt_id", applied["receipt_id"])
        gateway_surface = await _gateway_verify_surface(
            gateway,
            tenant,
            receipt_id=receipt_id,
            signed_case_field=signed_case_field or "signature",
            signed_tampered=signed_tampered,
            envelope_tampered=envelope_tampered,
            rows=rows,
            emit=emit,
        )

        measurements = await self.measure(
            target, attempts, operation_ids=[operation_id]
        )

        # -- 6. verdict -----------------------------------------------------
        # Unmeasured coverage is not coverage: a field the specification names
        # that is absent from this receipt's signed payload leaves the claim
        # about that field unestablished, so it cannot be reported as PASS.
        verdict = (
            Verdict.PASS
            if genuine_ok
            and not signed_survivors
            and not kid_probe_survived
            and not signed_unmeasured
            else Verdict.FAIL
        )
        survivor_text = list(signed_survivors)
        if kid_probe_survived:
            survivor_text.append("signing_input.kid (under the key-resolution control)")
        if survivor_text:
            observation_text = (
                f"{len(survivor_text)} field(s) inside signing_input were edited and "
                f"the signature STILL VERIFIED: {', '.join(survivor_text)}"
            )
        elif not genuine_ok:
            failed = [name for name, ok in genuine_assertions.items() if not ok]
            observation_text = (
                "every signed-field edit broke the signature, but the genuine receipt "
                f"did not verify as documented: {', '.join(failed)}"
            )
        elif signed_unmeasured:
            observation_text = (
                "every edit that could be made inside signing_input broke the "
                f"signature, but {len(signed_unmeasured)} field(s) the specification "
                "names are absent from this receipt's signed payload, so the signature "
                "does not cover them and this run could not measure them: "
                f"{', '.join(signed_unmeasured)}"
            )
        else:
            observation_text = (
                f"genuine bundle: signature ESTABLISHED, issuer trust NOT_ESTABLISHED "
                f"(key served by the audited origin), downstream execution ESTABLISHED "
                f"only with the effect-ledger observation of {executions} execution(s). "
                f"All {len(signed_measured)} edits inside signing_input were caught: "
                f"{len(detected_by_signature_check)} by the Ed25519 check over the "
                f"signed bytes"
                + (
                    f", {len(detected_before_signature_check)} earlier than that "
                    f"({', '.join(detected_before_signature_check)}, where the forged "
                    "kid named a key the plane does not publish; a control that maps "
                    "the forged kid to the real key put that edit through the Ed25519 "
                    "check too, and it failed there)"
                    if detected_before_signature_check
                    else ""
                )
                + f". {len(survived_fields)} envelope field(s) were edited with the "
                f"signature still valid ({', '.join(survived_fields) or 'none'}); "
                f"{len(silent_survivors)} of those raised no envelope disagreement "
                f"({', '.join(silent_survivors) or 'none'})."
            )
        if not observation_text.endswith("."):
            observation_text += "."
        if gateway_surface.get("ignores_supplied_evidence"):
            observation_text += (
                " POST /v1/receipts/verify answered valid=true for the tampered bundle: "
                "it reads the receipt_id out of the body and re-checks the row it "
                "stores, never the evidence it was handed."
            )
        if not control_verifies:
            observation_text += (
                " CAVEAT: the re-serialization control did not verify, so signature "
                "failures below cannot be attributed to the edited value alone."
            )

        remaining_risks = [
            (
                "Fields outside signing_input are not authenticated: "
                + (", ".join(survived_fields) or "none observed")
                + " were edited and the signature still verified. Of those, "
                + (", ".join(silent_survivors) or "none")
                + " produced no disagreement the verifier could flag, so a holder "
                "reading issuer, keys_url, canonicalization or schema_version from a "
                "bundle is reading unauthenticated text. Only signing_input is attested."
            ),
            (
                "Key distribution is first-party: the verifying key was fetched from "
                f"{bundle.get('keys_url')!r} on the same origin that issued the receipt. "
                "An origin that can serve receipts can serve keys that validate forged "
                "ones, so a valid signature is not issuer trust. Out-of-band key "
                "pinning is not implemented."
            ),
            (
                "A verified signature never establishes that the downstream business "
                "action happened. Here it was established only by the effect ledger, an "
                "observer the gateway cannot write to; with no observation supplied the "
                "verifier correctly reported NOT_ESTABLISHED."
            ),
        ]
        if signed_unmeasured:
            remaining_risks.append(
                "The signature does not cover "
                + ", ".join(signed_unmeasured)
                + ": those fields the specification names are absent from this "
                "receipt's signed payload, so no edit to them could be measured and "
                "nothing in this run establishes that they are attested."
            )
        if gateway_surface.get("ignores_supplied_evidence"):
            remaining_risks.append(
                "POST /v1/receipts/verify reads only the receipt_id from its body and "
                "answers about the receipt the gateway stores. Handed the holder's "
                "tampered bundle -- broken signature and all -- it answered "
                "valid=true (http "
                f"{gateway_surface['evidence_probe']['http_status']}). A holder who "
                "reads that as 'the receipt in my hand is genuine' is reading an "
                "answer about a different object."
            )
        mcp_verifier = gateway_surface.get("public_bundle_verifier", {})
        if not mcp_verifier.get("reachable"):
            remaining_risks.append(
                "The product's bundle-oriented verifier -- the public MCP "
                "verify_receipt tool, which runs b2a_sdk's offline verifier over a "
                "caller-supplied bundle -- answered http "
                f"{mcp_verifier.get('http_status')} in this deployment, so it could "
                "not be compared against the independent verifier here. That surface "
                "exists in the product but is served only where "
                "ENABLE_PUBLIC_MCP_ENDPOINT is set (it is off by default); where it "
                "is off, a holder's only recourse is the offline signature check "
                "against the published keys."
            )
        if not control_verifies:
            remaining_risks.append(
                "The re-serialization control failed: rewriting signing_input with "
                "unchanged values already broke the signature, so this run cannot "
                "attribute the signed-field failures to the edited values."
            )
        envelope_issuer = bundle.get("issuer")
        envelope_keys_url = str(bundle.get("keys_url") or "")
        if not envelope_issuer or not envelope_keys_url.startswith("http"):
            remaining_risks.append(
                "The exported bundle in this run named its issuer as "
                f"{envelope_issuer!r} and its key location as {envelope_keys_url!r}. "
                "Neither field is signed, and an offline holder with no other context "
                "cannot resolve a relative or empty value into an origin to fetch keys "
                "from. This deployment has PUBLIC_URL unset; a deployment that sets it "
                "would fill both fields, but they would still sit outside the signature."
            )

        emit(
            "matrix.complete",
            (
                f"{len(signed_measured) - len(signed_survivors)}/{len(signed_measured)} "
                f"signed-field edits detected "
                f"({len(detected_by_signature_check)} by the Ed25519 check); "
                f"{len(signed_unmeasured)} named field(s) unmeasured; "
                f"{len(survived_fields)} envelope field(s) survived"
            ),
            signed_survivors=signed_survivors,
            signed_unmeasured=signed_unmeasured,
            detected_by_signature_check=detected_by_signature_check,
            detected_before_signature_check=detected_before_signature_check,
            kid_probe_survived=kid_probe_survived,
            envelope_survivors=survived_fields,
            silent_survivors=silent_survivors,
            verdict=verdict.value,
        )

        return self.result(
            target,
            verdict=verdict,
            observation=observation_text,
            measurements=measurements,
            attempts=attempts,
            remaining_risks=remaining_risks,
            extra={
                "receipt_id": receipt_id,
                "receipt_outcome": signed_payload.get("outcome"),
                "downstream_executions": executions,
                # -- handed to the runner's evidence harvester ---------------
                # `failure_lab.runner.harvest_evidence` reads exactly these
                # three keys off `ConfigurationResult.extra`, and nothing else
                # can collect them: the runner closes every target before it
                # builds the bundle, so a live gateway is gone by then. Until
                # a scenario set them the bundle wrote "no portable receipt
                # bundles were supplied" and "no independent verification
                # results were supplied" on every run -- true of the bundle,
                # and false about this scenario, which exported all three.
                # (keys spelled literally rather than imported from
                # failure_lab.runner, which imports the scenarios.)
                "portable_receipts": {receipt_id: bundle},
                "trust_keys": key_document,
                "verification_results": [
                    {
                        "receipt_id": receipt_id,
                        "subject": "the genuine exported bundle",
                        "verifier": "failure_lab.verifier (independent: imports "
                        "neither app nor b2a_sdk)",
                        "key_source": KeySource.ISSUER_ORIGIN.value,
                        "downstream_observation": observation,
                        **genuine.as_dict(),
                    },
                    {
                        "receipt_id": receipt_id,
                        "subject": (
                            "the same bundle with no downstream observation supplied"
                        ),
                        "verifier": "failure_lab.verifier (independent: imports "
                        "neither app nor b2a_sdk)",
                        "key_source": KeySource.ISSUER_ORIGIN.value,
                        "downstream_observation": None,
                        **genuine_no_observation.as_dict(),
                    },
                ],
                # Same list under both names: "cases" is the suite-wide key
                # for a per-sub-case table; "tamper_matrix" is this
                # scenario's own name for it.
                "cases": rows,
                "downstream_observation": observation,
                "genuine_verification": {
                    "claims": baseline_claims,
                    "claims_without_observation": _claims(genuine_no_observation),
                    "assertions": genuine_assertions,
                    "signature_reason": genuine.signature.reason,
                    "issuer_trust_reason": genuine.issuer_trust.reason,
                    "downstream_reason": genuine.downstream_execution.reason,
                    "downstream_reason_without_observation": (
                        genuine_no_observation.downstream_execution.reason
                    ),
                    "key_id": genuine.key_id,
                    "envelope_disagreements": genuine.envelope_disagreements,
                    "notes": genuine.notes,
                    "assertion_note": (
                        "issuer_trust_not_established follows from the key source the "
                        "harness declared (ISSUER_ORIGIN -- the key was fetched from "
                        "the audited origin's /.well-known/trust-keys.json). It records "
                        "what this run can honestly claim, and is not a measurement of "
                        "the product."
                    ),
                },
                "reserialization_control": {
                    "field": f"signing_input.{control_field}",
                    "value_unchanged": True,
                    "signature": control.signature.status.value,
                    "still_verifies": control_verifies,
                    "why_it_matters": (
                        "an edit is only evidence of coverage if rewriting the same "
                        "value leaves the signature intact"
                    ),
                },
                "tamper_matrix": rows,
                "signed_fields_tampered": [row["field"] for row in signed_measured],
                "signed_fields_that_survived": signed_survivors,
                "signed_fields_not_measured": signed_unmeasured,
                "detected_by_signature_check": detected_by_signature_check,
                "detected_before_signature_check": detected_before_signature_check,
                "kid_coverage_probe": kid_probe,
                "outside_signature": survived_fields,
                "outside_signature_undetectable": silent_survivors,
                "outside_signature_detail": survived,
                "field_coverage": coverage,
                "declared_envelope_only_fields": list(ENVELOPE_ONLY_FIELDS),
                "bundle_envelope": {
                    "issuer": bundle.get("issuer"),
                    "keys_url": bundle.get("keys_url"),
                    "canonicalization": bundle.get("canonicalization"),
                    "schema_version": bundle.get("schema_version"),
                    "alg": bundle.get("alg"),
                    "kid": bundle.get("kid"),
                },
                "published_key_ids": sorted(keys),
                "gateway_verify_surface": gateway_surface,
            },
        )


# -- helpers ---------------------------------------------------------------


def _probe(
    bundle: dict[str, Any],
    keys: dict[str, bytes],
    *,
    path: str,
    value: Any,
    original: Any,
    coverage: dict[str, str],
    baseline: dict[str, str],
    observation: dict[str, Any],
    emit: Any,
    side: str | None = None,
) -> dict[str, Any]:
    """Apply one independent edit and record what the verifier said."""
    edited = tamper(bundle, path, value)
    report = verify(edited, keys, downstream_observation=observation)
    claims = _claims(report)
    changed = [name for name in _CLAIM_NAMES if claims[name] != baseline[name]]
    classification = coverage.get(path, "absent_from_bundle")
    signature_valid = report.signature.status is ClaimStatus.ESTABLISHED
    row = {
        # ``case`` is the suite-wide name for a sub-case row; ``field`` is
        # this scenario's own and is kept.
        "case": path,
        "field": path,
        "side": side or ("signed" if path.startswith("signing_input.") else "envelope"),
        "coverage": classification,
        "edited": True,
        "original": original,
        "forged": value,
        "signature_valid": signature_valid,
        "signature_status": report.signature.status.value,
        "signature_reason": report.signature.reason,
        "failure_mode": _failure_mode(report),
        "claims": claims,
        "claims_changed": changed,
        "envelope_disagreements": list(report.envelope_disagreements),
        "notes": list(report.notes),
        "detected": signature_valid is False or bool(report.envelope_disagreements),
    }
    emit(
        "tamper.probe",
        (
            f"{path} edited -> signature {report.signature.status.value}"
            + (
                f", disagreements {row['envelope_disagreements']}"
                if row["envelope_disagreements"]
                else ""
            )
        ),
        field=path,
        side=row["side"],
        coverage=classification,
        signature=report.signature.status.value,
        failure_mode=row["failure_mode"],
        claims_changed=changed,
        envelope_disagreements=row["envelope_disagreements"],
        detected=row["detected"],
    )
    return row


async def _gateway_verify_surface(
    gateway: Any,
    tenant: Any,
    *,
    receipt_id: str,
    signed_case_field: str,
    signed_tampered: dict[str, Any],
    envelope_tampered: dict[str, Any],
    rows: list[dict[str, Any]],
    emit: Any,
) -> dict[str, Any]:
    """Ask the product's own verification surfaces about the tampered evidence.

    Nothing here is asserted about the product: what the endpoint accepts and
    what it answers are measured by sending it the holder's tampered bundle
    and reading the response.
    """

    async def post(body: Any, *, label: str) -> dict[str, Any]:
        response = await gateway.client.post(
            "/v1/receipts/verify", json=body, headers=tenant.headers
        )
        try:
            parsed = response.json()
        except ValueError:
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}
        reason = parsed.get("reason") or parsed.get("detail")
        if isinstance(reason, list):
            # A FastAPI validation error echoes the whole body back; keep the
            # shape of the complaint, drop the copy of the bundle.
            reason = [
                {k: item.get(k) for k in ("loc", "msg", "type")}
                if isinstance(item, dict)
                else item
                for item in reason
            ]
        return {
            "sent": label,
            "http_status": response.status_code,
            "valid": parsed.get("valid"),
            "reason": reason,
        }

    async def ask(candidate_id: str, *, label: str) -> dict[str, Any]:
        result = await post({"receipt_id": candidate_id}, label=label)
        result["receipt_id_sent"] = candidate_id
        return result

    def independent(path: str) -> dict[str, Any] | None:
        for row in rows:
            if row["field"] == path:
                return {
                    "signature_status": row.get("signature_status"),
                    "envelope_disagreements": row.get("envelope_disagreements"),
                    "detected": row.get("detected"),
                }
        return None

    genuine = await ask(receipt_id, label="the genuine receipt_id")

    # What a holder with a tampered copy would actually do: read the
    # receipt_id out of the bundle in their hand and ask the issuer about it.
    holder_id = signed_tampered.get("receipt_id") or receipt_id
    signed_case = await ask(holder_id, label="the receipt_id in the tampered bundle")

    # Does the endpoint look at supplied evidence at all? Send the whole
    # tampered bundle -- broken signature, broken signing_input -- as the body.
    evidence_probe = await post(signed_tampered, label="the whole tampered bundle")
    ignores_supplied_evidence = (
        evidence_probe["http_status"] == 200 and evidence_probe["valid"] is True
    )

    # ... and is the id the only thing it will take? Same bundle, id removed.
    without_id = {k: v for k, v in signed_tampered.items() if k != "receipt_id"}
    no_id_probe = await post(
        without_id, label="the tampered bundle with receipt_id removed"
    )
    requires_receipt_id = no_id_probe["http_status"] >= 400

    envelope_case = await ask(
        str(envelope_tampered.get("receipt_id")),
        label="the forged envelope receipt_id",
    )

    # The product also ships a bundle-oriented verifier -- the public MCP
    # ``verify_receipt`` tool, which runs b2a_sdk's offline verifier over a
    # caller-supplied bundle. Measure whether it is reachable here rather than
    # asserting the product has no such surface.
    mcp_response = await gateway.client.post(
        "/mcp/public",
        json={
            "jsonrpc": "2.0",
            "id": "t10-verify",
            "method": "tools/call",
            "params": {
                "name": "verify_receipt",
                "arguments": {"bundle": signed_tampered},
            },
        },
        headers={"Accept": "application/json, text/event-stream"},
    )
    try:
        mcp_body: Any = mcp_response.json()
    except ValueError:
        mcp_body = {"raw": mcp_response.text[:400]}
    mcp_structured = None
    if isinstance(mcp_body, dict):
        result = mcp_body.get("result")
        if isinstance(result, dict):
            mcp_structured = result.get("structuredContent")
    public_bundle_verifier = {
        "endpoint": "POST /mcp/public (tools/call verify_receipt)",
        "http_status": mcp_response.status_code,
        "reachable": mcp_response.status_code == 200,
        "structured_result": mcp_structured,
        "body": None if mcp_structured else mcp_body,
        "note": (
            "this surface takes the holder's bundle and checks the signature with "
            "b2a_sdk's offline verifier, so it answers the tampering question "
            "directly -- when the deployment enables it "
            "(ENABLE_PUBLIC_MCP_ENDPOINT)."
        ),
    }

    emit(
        "gateway_verify.probe",
        (
            f"POST /v1/receipts/verify: genuine id -> valid={genuine['valid']}; "
            f"tampered holder copy's id -> valid={signed_case['valid']}; "
            f"whole tampered bundle as body -> http {evidence_probe['http_status']} "
            f"valid={evidence_probe['valid']}; bundle without receipt_id -> http "
            f"{no_id_probe['http_status']}; forged envelope id -> http "
            f"{envelope_case['http_status']}. Bundle-oriented public MCP verifier -> "
            f"http {public_bundle_verifier['http_status']}"
        ),
        genuine=genuine,
        signed_field_case=signed_case,
        evidence_probe=evidence_probe,
        no_id_probe=no_id_probe,
        envelope_field_case=envelope_case,
        public_bundle_verifier=public_bundle_verifier,
    )

    signed_independent = independent(signed_case_field)
    envelope_independent = independent("receipt_id")
    surface = {
        "endpoint": "POST /v1/receipts/verify",
        "input_shape": {"receipt_id": "str"},
        "requires_receipt_id": requires_receipt_id,
        "ignores_supplied_evidence": ignores_supplied_evidence,
        "accepts_only_receipt_id": requires_receipt_id and ignores_supplied_evidence,
        "genuine_receipt": genuine,
        "evidence_probe": evidence_probe,
        "no_receipt_id_probe": no_id_probe,
        "public_bundle_verifier": public_bundle_verifier,
        "cases": [
            {
                "case": f"signed field tampered ({signed_case_field})",
                "independent_verifier": signed_independent,
                "gateway_endpoint": signed_case,
                "same_question": False,
                "agree": None,
                "note": (
                    "the holder's copy carries an unedited envelope receipt_id, so "
                    "asking the endpoint about it is what a holder would do. It "
                    f"answered valid={signed_case['valid']!r} about the row the "
                    "gateway stores, while the independent verifier reports the copy "
                    f"in the holder's hand as "
                    f"{(signed_independent or {}).get('signature_status')}. Posting "
                    "the tampered bundle itself did not change that answer "
                    f"(http {evidence_probe['http_status']}, "
                    f"valid={evidence_probe['valid']!r}): the endpoint reads the id "
                    "out of the body and ignores signing_input and signature."
                ),
            },
            {
                "case": "envelope field tampered (receipt_id)",
                "independent_verifier": envelope_independent,
                "gateway_endpoint": envelope_case,
                "same_question": False,
                "agree": None,
                "note": (
                    "the forged envelope receipt_id names no stored receipt, so the "
                    f"endpoint answered http {envelope_case['http_status']} "
                    f"({envelope_case['reason']!r}) -- it is answering about lookup and "
                    "authorization, not about the bundle. The independent verifier keeps "
                    "a valid signature here and flags the envelope/payload disagreement "
                    "instead, which is the honest answer for a field outside the signature."
                ),
            },
        ],
        "finding": (
            "POST /v1/receipts/verify answers about the receipt the gateway stores, "
            "not about the bundle it is shown"
            + (
                " -- it read the id out of a body whose signature was broken and still "
                "answered valid=true"
                if ignores_supplied_evidence
                else ""
            )
            + ". The product does ship a bundle-oriented verifier (the public MCP "
            "verify_receipt tool, running b2a_sdk's offline verifier), but this "
            f"deployment answered http {public_bundle_verifier['http_status']} for it, "
            + (
                "so it could be compared directly."
                if public_bundle_verifier["reachable"]
                else "so it was not reachable here and could not be compared. In this "
                "deployment a holder's only recourse is the offline check against the "
                "published keys, which is what the independent verifier ran."
            )
        ),
    }
    return surface
