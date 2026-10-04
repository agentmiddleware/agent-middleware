"""The executable judge: it never asks a model anything.

An integration check whose verdict is "the AI said it worked" is worth
nothing, and it is worth less than nothing for a product whose entire pitch is
that an agent's account of its own actions is not evidence. This judge
therefore decides from instruments the candidate cannot reach:

* the **independent effect ledger** -- a SQLite file owned by the simulated
  refund tool, duplicate-tolerant on purpose, which the gateway has no handle
  to. "The customer was refunded twice" is a ``COUNT(*)`` over that file.
* the **fault layer** between the gateway and the tool, which counts every
  execution request that crossed it. "The gateway dispatched once" is observed
  from outside the gateway, never read back from its tables.
* the **gateway's own tables**, used only where the question is genuinely
  about gateway state (does a receipt exist, what outcome did it record), and
  labelled gateway-reported wherever it is rendered.
* the **candidate's source text and its HTTP traffic**, for the questions a
  runtime observation cannot answer: did it verify the receipt with the
  vendor's own verifier, did it reach for the endpoint owned by the party
  being audited.

Exactly one number in the output is taken on the candidate's word --
``documentation_gaps``, because where the docs ran out is only knowable from
inside the attempt -- and it is labelled self-reported in the schema, in the
provenance map and in the rendered report.

Configuration
-------------
The run uses ``D_gateway_naive_downstream``: the gateway in front of a tool
with **no** native idempotency. That choice is the point. Against a correctly
built downstream, an integration that retries under a fresh replay key is
rescued by the tool and looks correct; the naive downstream isolates the
gateway's own guarantee from the downstream's, so an integration that
mis-handles a lost response is caught rather than covered for.

What this does not establish
----------------------------
That a candidate passing here is safe in production. Six assertions over one
tool, one tenant, two injected failures and one deliberate refusal is a floor,
not a certificate.
The crash is a simulated in-process death at an instrumented durable
boundary, not a ``SIGKILL`` of a separate OS process. And a judge that has
never failed anything is not a judge, which is why
``broken_candidate.py`` ships alongside ``reference_candidate.py``.

It also does not establish that the gateway was needed. Only ``D`` is run, so
there is no baseline here at all: a downstream that honours ``operation_id``
as a durable idempotency key removes the same duplicate with no gateway in the
picture, which is configuration ``B_direct_native_idempotency`` in the main
suite and is where that comparison belongs. A pass here says the integration
survived; it says nothing about whether it had to be this integration. That
sentence is in :data:`LIMITATIONS` as well as here, because a caveat that
lives only in a docstring is a caveat nobody reading the result will see.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
from abc import ABC, abstractmethod
from collections.abc import Iterable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

import httpx

from failure_lab.gateway import GATEWAY_BASE_URL, GATEWAY_TOOL_ID, is_process_death
from failure_lab.integration_check.sample_app import RefundIntent

#: Schema id for the result document, whose ``metrics`` object carries exactly
#: the eight metrics and whose ``metric_provenance`` object says where each one
#: came from. Bump it when either set changes, so a published number cannot
#: silently come to mean something else.
RESULT_SCHEMA = "failure_lab.integration_check/result/1"

#: The argument the permit forbids. Naming the settlement currency is a
#: privileged choice, so an agent that sends it is asking for an operation its
#: authority does not cover -- an authorization denial, not a budget refusal.
FORBIDDEN_FIELD = "currency"

#: How long the candidate's client waits. Longer than the gateway's own
#: upstream timeout, so a lost response reaches the candidate as the gateway's
#: explicit uncertain answer rather than as the candidate's own timeout.
CLIENT_TIMEOUT_SECONDS = 15.0

#: The post-claim boundary the ambiguous operation dies at: the downstream has
#: executed and answered, and the gateway dies before recording the terminal
#: state. Neither side can tell from the response alone.
AMBIGUOUS_CRASH_BOUNDARY = "after_upstream_response"

#: Seconds the reconciler's idle window is skipped by. A live claim waits
#: hours before it is declared abandoned; the lab moves the clock instead and
#: says so rather than pretending it waited.
RECONCILE_BACKDATE_SECONDS = 20_000


# --------------------------------------------------------------------------- #
# The vocabulary the candidate speaks                                          #
# --------------------------------------------------------------------------- #


class Understanding(str, Enum):
    """What the integration is prepared to say about one business operation.

    The distinction that matters is between ``KNOWN_AMBIGUOUS`` and
    ``UNKNOWN``. Both mean the money's fate is not established. Only the first
    means the integration *knows* that and has stopped guessing, which is the
    terminal state a correct integration reaches after an interrupted call.
    """

    CONFIRMED_SUCCESS = "confirmed_success"
    CONFIRMED_REPLAY = "confirmed_replay"
    CONFIRMED_REFUSED = "confirmed_refused"
    KNOWN_AMBIGUOUS = "known_ambiguous"
    UNKNOWN = "unknown"


TERMINAL_UNDERSTANDINGS = frozenset(
    {
        Understanding.CONFIRMED_SUCCESS,
        Understanding.CONFIRMED_REPLAY,
        Understanding.CONFIRMED_REFUSED,
        Understanding.KNOWN_AMBIGUOUS,
    }
)

#: Understandings that assert the business action happened. Reporting one of
#: these for an operation the gateway recorded as uncertain is an overclaim.
CLAIMS_THE_ACTION_HAPPENED = frozenset(
    {Understanding.CONFIRMED_SUCCESS, Understanding.CONFIRMED_REPLAY}
)


class RecoveryStage(str, Enum):
    """Which half of the restart exercise a call is."""

    BEFORE_RESTART = "before_restart"
    AFTER_RESTART = "after_restart"


def _as_understanding(value: Any) -> Understanding:
    """Read a candidate's understanding, or ``UNKNOWN`` if it is unreadable.

    A candidate is free to hand back the bare string, because
    :class:`Understanding` is a str enum and nothing stops it. Taking the
    string at face value is not generosity -- it is reporting what the
    candidate actually said. Anything the vocabulary does not contain becomes
    ``UNKNOWN``, which is non-terminal and therefore fails.
    """
    if isinstance(value, Understanding):
        return value
    try:
        return Understanding(value)
    except (ValueError, TypeError):
        return Understanding.UNKNOWN


@dataclass(frozen=True)
class OperationReport:
    """What the candidate believes about one business operation."""

    understanding: Understanding
    receipt_id: str | None = None
    idempotency_key: str | None = None
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        # ``Understanding`` is a str enum, so a candidate that writes
        # ``understanding="confirmed_success"`` constructs successfully and
        # only explodes here. The judge's job is to fail that candidate, not
        # to die describing it, so the value is read defensively.
        return {
            "understanding": getattr(
                self.understanding, "value", str(self.understanding)
            ),
            "receipt_id": self.receipt_id,
            "idempotency_key": self.idempotency_key,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ReceiptVerificationReport:
    """Three separate claims, never one boolean.

    A signature establishes that the holder of a key signed a statement. It
    does not establish that the key belongs to who the envelope says, and it
    never establishes that the business action occurred. An integration that
    collapses these into ``verified: true`` has learned the wrong lesson, so
    they are three fields here and the judge checks each.
    """

    receipt_id: str
    signature_valid: bool
    issuer_trust_established: bool
    downstream_execution_established: bool
    verifier: str = ""
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "signature_valid": self.signature_valid,
            "issuer_trust_established": self.issuer_trust_established,
            "downstream_execution_established": self.downstream_execution_established,
            "verifier": self.verifier,
            "detail": self.detail,
        }


@dataclass
class IntegrationContext:
    """Everything the candidate is given, and deliberately nothing more.

    There is no effect ledger here, no fault injector and no gateway object.
    The candidate cannot see what the judge sees; if it could, the check would
    be measuring cooperation rather than integration.
    """

    #: An httpx client already pointed at the gateway. No auth headers are
    #: set: authenticating is part of the integration.
    gateway: httpx.AsyncClient
    gateway_base_url: str
    #: Wallet-scoped API key. Never written to disk by the judge.
    api_key: str
    wallet_id: str
    permit_id: str
    tool_id: str
    permit_allowed_tools: tuple[str, ...]
    permit_forbidden_fields: tuple[str, ...]
    credits_per_call: str
    #: The business operation this step is about.
    intent: RefundIntent
    #: Scratch the candidate may write to. It is the only thing that survives
    #: the simulated restart.
    work_dir: Path
    invoke_path: str = "/mcp/messages"
    rest_invoke_path: str = "/mcp/tools/{tool}/invoke"
    portable_receipt_path: str = "/v1/receipts/{receipt_id}/portable"
    trust_keys_path: str = "/.well-known/trust-keys.json"

    def auth_headers(self) -> dict[str, str]:
        return {"X-API-Key": self.api_key}


class CandidateIntegration(ABC):
    """The five operations a clean-room integration has to survive.

    Implementations take no constructor arguments: the judge builds a fresh
    instance to model the restart, and an instance that smuggled state through
    its constructor would be modelling a process that never died.
    """

    @abstractmethod
    async def execute_authorized_operation(
        self, ctx: IntegrationContext
    ) -> OperationReport:
        """Issue one refund through the gateway and end up with a receipt."""

    @abstractmethod
    async def retry_after_lost_response(
        self, ctx: IntegrationContext
    ) -> OperationReport:
        """Attempt, lose the response after it executed, and retry safely."""

    @abstractmethod
    async def attempt_unauthorized_operation(
        self, ctx: IntegrationContext
    ) -> OperationReport:
        """Ask for an operation the permit does not cover. Report the refusal."""

    @abstractmethod
    async def verify_receipt_independently(
        self, ctx: IntegrationContext, receipt_id: str
    ) -> ReceiptVerificationReport:
        """Verify the receipt without the issuer's own verifier or the SDK."""

    @abstractmethod
    async def restart_and_recover(
        self, ctx: IntegrationContext, *, stage: RecoveryStage
    ) -> OperationReport:
        """Make an interrupted attempt, then -- as a new process -- resolve it."""

    def documentation_gaps(self) -> list[str]:
        """Where the published material was not enough. Self-reported."""
        return []


# --------------------------------------------------------------------------- #
# Instruments the candidate cannot see                                         #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RequestRecord:
    """One HTTP request the candidate issued, observed at the transport.

    Headers are never recorded: the candidate's API key travels in one, and a
    judge that writes the credential into its own evidence file has broken the
    rule it exists to enforce.
    """

    sequence: int
    at: str
    method: str
    path: str
    http_status: int | None
    tool: str | None
    operation_id: str | None
    idempotency_key: str | None
    jsonrpc_error_code: int | None
    jsonrpc_error_message: str | None
    transport_error: str | None

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @property
    def carried_an_error(self) -> bool:
        return (
            self.transport_error is not None
            or self.jsonrpc_error_code is not None
            or (self.http_status is not None and self.http_status >= 400)
        )


class RequestLog:
    """Append-only record of everything the candidate sent at the gateway."""

    def __init__(self) -> None:
        self.records: list[RequestRecord] = []
        self._sequence = 0

    def add(self, *, at: datetime, **fields: Any) -> RequestRecord:
        """Record one crossing. ``at`` is when the request *left*, not when it
        came back: a metric measured from the answer would be measured from
        after the thing it is trying to time."""
        self._sequence += 1
        record = RequestRecord(
            sequence=self._sequence,
            at=at.isoformat(timespec="microseconds"),
            **fields,
        )
        self.records.append(record)
        return record

    def since(self, marker: int) -> list[RequestRecord]:
        return [r for r in self.records if r.sequence > marker]

    @property
    def marker(self) -> int:
        return self._sequence

    def first_request_at(self) -> datetime | None:
        if not self.records:
            return None
        return datetime.fromisoformat(self.records[0].at)

    def keys_by_operation(self) -> dict[str, list[str]]:
        """Distinct replay keys the candidate presented per business operation.

        More than one key for one operation is the classic wrong fix for a
        lost response: mint a new key and try again, which is a second
        authorization for the same intent.
        """
        grouped: dict[str, list[str]] = {}
        for record in self.records:
            if not record.operation_id or not record.idempotency_key:
                continue
            keys = grouped.setdefault(record.operation_id, [])
            if record.idempotency_key not in keys:
                keys.append(record.idempotency_key)
        return grouped


_MAX_SNIFFED_BODY = 256 * 1024


def _sniff_request(body: bytes) -> tuple[str | None, str | None, str | None]:
    """Pull (tool, operation_id, idempotency_key) out of a governed call body.

    Understands both governed transports: the JSON-RPC ``tools/call`` shape
    and the REST ``/mcp/tools/{tool}/invoke`` shape. Anything else yields
    three ``None``s and is recorded as a plain request.
    """
    if not body or len(body) > _MAX_SNIFFED_BODY:
        return None, None, None
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None, None, None
    if not isinstance(payload, dict):
        return None, None, None
    envelope = (
        payload.get("params") if isinstance(payload.get("params"), dict) else payload
    )
    if not isinstance(envelope, dict):
        return None, None, None
    tool = envelope.get("name")
    arguments = envelope.get("arguments")
    context = envelope.get("mcpContext")
    if not isinstance(context, dict):
        context = envelope.get("mcp_context")
    operation_id = (
        arguments.get("operation_id") if isinstance(arguments, dict) else None
    )
    key = context.get("idempotency_key") if isinstance(context, dict) else None
    return (
        tool if isinstance(tool, str) else None,
        operation_id if isinstance(operation_id, str) else None,
        key if isinstance(key, str) else None,
    )


def _sniff_response(body: bytes) -> tuple[int | None, str | None]:
    """Pull a JSON-RPC error code and message out of a response body.

    Denials arrive as HTTP 200 with a JSON-RPC error member, so status codes
    alone would report a refusal as a success.
    """
    if not body or len(body) > _MAX_SNIFFED_BODY:
        return None, None
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    error = payload.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        message = error.get("message")
        return (
            code if isinstance(code, int) else None,
            str(message)[:200] if message is not None else None,
        )
    detail = payload.get("detail")
    if isinstance(detail, dict):
        message = detail.get("error") or detail.get("message")
        if message is not None:
            return None, str(message)[:200]
    return None, None


class RecordingTransport(httpx.AsyncBaseTransport):
    """Wraps the ASGI transport so the judge sees every call the candidate made.

    This is the only honest way to answer "did the candidate reach for the
    issuer's own verification endpoint" and "how many replay keys did it
    present for one refund". Asking the candidate would be asking the
    defendant.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport, log: RequestLog) -> None:
        self._inner = inner
        self._log = log

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            body = request.content
        except Exception:  # noqa: BLE001 - a streaming body is simply not sniffed
            body = b""
        tool, operation_id, key = _sniff_request(body)
        sent_at = datetime.now(timezone.utc)
        try:
            response = await self._inner.handle_async_request(request)
        except BaseException as exc:
            self._log.add(
                at=sent_at,
                method=request.method,
                path=request.url.path,
                http_status=None,
                tool=tool,
                operation_id=operation_id,
                idempotency_key=key,
                jsonrpc_error_code=None,
                jsonrpc_error_message=None,
                transport_error=f"{type(exc).__name__}",
            )
            raise
        chunks = [chunk async for chunk in response.stream]
        await response.aclose()
        content = b"".join(chunks)
        code, message = _sniff_response(content)
        self._log.add(
            at=sent_at,
            method=request.method,
            path=request.url.path,
            http_status=response.status_code,
            tool=tool,
            operation_id=operation_id,
            idempotency_key=key,
            jsonrpc_error_code=code,
            jsonrpc_error_message=message,
            transport_error=None,
        )
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            content=content,
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


# --------------------------------------------------------------------------- #
# Loading the candidate, and reading what it is made of                        #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LoadedCandidate:
    factory: type[CandidateIntegration]
    path: Path
    sources: dict[str, str]

    def build(self) -> CandidateIntegration:
        return self.factory()


def _import_roots(text: str) -> set[str]:
    """Top-level names one module imports, as its source states them."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _static_sibling_sources(resolved: Path) -> dict[str, str]:
    """Candidate-directory modules the candidate imports, followed transitively.

    The runtime scan can only see what *this* import introduced into
    ``sys.modules``. Judge one candidate and then another in the same process
    and the siblings are already imported, so the second run's diff is empty
    and the cheat scan silently goes blind on exactly the files it exists to
    read. Resolving imports against the directory on disk does not depend on
    what happened earlier in the process.
    """
    collected: dict[str, str] = {}
    pending = [resolved]
    seen = {resolved}
    while pending:
        current = pending.pop()
        try:
            text = current.read_text(encoding="utf-8")
        except OSError:
            continue
        if current != resolved:
            collected[str(current)] = text
        for root in sorted(_import_roots(text)):
            if root == "failure_lab":
                continue
            sibling = (resolved.parent / f"{root}.py").resolve()
            if sibling in seen or not sibling.is_file():
                continue
            seen.add(sibling)
            pending.append(sibling)
    return collected


def load_candidate(path: Path) -> LoadedCandidate:
    """Import one candidate file and collect every source file it brought.

    Sibling modules the candidate imports from its own directory are collected
    too, so a cheat cannot be hidden one file away. Modules from
    ``failure_lab`` itself are excluded: the judge is not evidence about the
    candidate.
    """
    resolved = path.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"no candidate file at {resolved}")
    module_name = f"failure_lab_candidate_{uuid.uuid4().hex[:8]}"
    spec = importlib.util.spec_from_file_location(module_name, resolved)
    if spec is None or spec.loader is None:
        raise ImportError(f"{resolved} is not importable as a Python module")
    module = importlib.util.module_from_spec(spec)
    before = set(sys.modules)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    introduced = set(sys.modules) - before - {module_name}

    sources = {str(resolved): resolved.read_text(encoding="utf-8")}
    for name in sorted(introduced):
        sibling = sys.modules.get(name)
        origin = getattr(sibling, "__file__", None)
        if not origin or name.split(".")[0] == "failure_lab":
            continue
        origin_path = Path(origin).resolve()
        if origin_path.parent != resolved.parent or origin_path == resolved:
            continue
        try:
            sources[str(origin_path)] = origin_path.read_text(encoding="utf-8")
        except OSError:
            continue
    sources.update(_static_sibling_sources(resolved))

    factory = getattr(module, "CANDIDATE", None)
    if factory is None:
        found = [
            value
            for value in vars(module).values()
            if isinstance(value, type)
            and issubclass(value, CandidateIntegration)
            and value is not CandidateIntegration
        ]
        if len(found) != 1:
            raise AttributeError(
                f"{resolved} must define CANDIDATE, or exactly one "
                f"CandidateIntegration subclass (found {len(found)})"
            )
        factory = found[0]
    if not (isinstance(factory, type) and issubclass(factory, CandidateIntegration)):
        raise TypeError(f"{resolved}: CANDIDATE is not a CandidateIntegration subclass")
    return LoadedCandidate(factory=factory, path=resolved, sources=sources)


#: Import roots that mean the candidate verified a receipt with something the
#: audited party supplies. ``b2a_sdk`` and ``awi_sdk`` are the vendor SDKs;
#: ``app`` is the gateway itself.
_DISQUALIFYING_IMPORT_ROOTS = frozenset({"b2a_sdk", "awi_sdk", "app"})

#: The issuer's own verification endpoint, written as a pattern so the literal
#: path never appears in a candidate-scannable file.
_ISSUER_VERIFY_RE = re.compile(r"receipts\s*/\s*(?:[^/\s\"']+\s*/\s*)?verify")


@dataclass(frozen=True)
class CheatFinding:
    kind: str
    where: str
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def scan_candidate_sources(loaded: LoadedCandidate) -> list[CheatFinding]:
    """Read the candidate's source for the verification shortcuts.

    Static, mechanical, and deliberately blunt: an import of the vendor SDK or
    of the gateway's own modules, or any reference to the issuer's verify
    endpoint. A verifier supplied by the party being audited proves nothing
    about that party, so reaching for one is disqualifying rather than merely
    noted.
    """
    findings: list[CheatFinding] = []
    for where, text in sorted(loaded.sources.items()):
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            findings.append(CheatFinding("unparseable_source", where, str(exc)))
            continue
        for node in ast.walk(tree):
            roots: list[str] = []
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                roots = [node.module.split(".")[0]]
            for root in roots:
                if root in _DISQUALIFYING_IMPORT_ROOTS:
                    findings.append(
                        CheatFinding(
                            "disqualifying_import",
                            f"{where}:{node.lineno}",
                            f"imports {root!r}, which the audited party supplies",
                        )
                    )
        for number, line in enumerate(text.splitlines(), start=1):
            if _ISSUER_VERIFY_RE.search(line):
                findings.append(
                    CheatFinding(
                        "issuer_verify_endpoint",
                        f"{where}:{number}",
                        "references the issuer's own receipt verification endpoint",
                    )
                )
    return findings


# --------------------------------------------------------------------------- #
# Posture: what must be unchanged when the run ends                            #
# --------------------------------------------------------------------------- #

#: Settings that decide whether the gateway is enforcing anything. A candidate
#: that "integrates" by flipping one of these has not integrated.
TRUST_FLAGS = (
    "TRUST_MODE_ENABLED",
    "ALLOW_LEGACY_UNPERMITTED_MCP",
    "ENABLE_PROOF_SURFACES",
    "ENABLE_DEV_KEY_SELF_PROVISION",
    "ENABLE_DOGFOOD_TOOL",
    "ENABLE_STANDARD_MCP_ENDPOINT",
    "SIMULATION_MODE_HUMAN_APPROVAL",
    "ENVIRONMENT",
    "DEBUG",
    "RATE_LIMIT_PER_MINUTE",
)

#: Environment variables whose *value* is a secret. Only a digest is kept, so
#: a change is detectable without the judge writing the secret anywhere.
_DIGEST_ONLY_ENV = (
    "VALID_API_KEYS",
    "STATIC_DEV_API_KEYS",
    "TRUST_SIGNING_PRIVATE_KEY_B64",
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def trust_flag_snapshot() -> dict[str, Any]:
    """Read the enforcement posture from both settings and the environment.

    Settings are cached after boot, so a candidate could mutate the
    environment without the cached object moving. Both are captured; a
    disagreement between them is itself a finding.
    """
    from app.core.config import get_settings

    settings = get_settings()
    snapshot: dict[str, Any] = {}
    for name in TRUST_FLAGS:
        snapshot[f"settings.{name}"] = str(getattr(settings, name, "<absent>"))
        snapshot[f"env.{name}"] = os.environ.get(name, "<unset>")
    for name in _DIGEST_ONLY_ENV:
        raw = os.environ.get(name)
        snapshot[f"env.{name}"] = "<unset>" if raw is None else f"sha256:{_digest(raw)}"
    return snapshot


def source_tree_manifest(root: Path) -> dict[str, str]:
    """Fingerprint every file under a tree, so a write anywhere in it shows up.

    Size and mtime rather than content hashes: the question is whether the
    candidate touched the gateway at all, and a touch that preserves both is
    not a thing an integration does by accident.
    """
    manifest: dict[str, str] = {}
    if not root.is_dir():
        return manifest
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        manifest[str(path.relative_to(root.parent))] = (
            f"{stat.st_size}:{stat.st_mtime_ns}"
        )
    return manifest


def _manifest_changes(before: dict[str, str], after: dict[str, str]) -> list[str]:
    changes = [f"added {p}" for p in sorted(set(after) - set(before))]
    changes += [f"removed {p}" for p in sorted(set(before) - set(after))]
    changes += [
        f"modified {p}"
        for p in sorted(set(before) & set(after))
        if before[p] != after[p]
    ]
    return changes


def _flag_changes(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    changed: list[str] = []
    for name in sorted(set(before) | set(after)):
        if before.get(name) != after.get(name):
            changed.append(
                f"{name}: {before.get(name, '<absent>')!r} -> {after.get(name, '<absent>')!r}"
            )
    return changed


# --------------------------------------------------------------------------- #
# Results                                                                      #
# --------------------------------------------------------------------------- #


@dataclass
class Assertion:
    name: str
    passed: bool
    requirement: str
    observation: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "assertion": self.name,
            "passed": self.passed,
            "requirement": self.requirement,
            "observation": self.observation,
            "evidence": self.evidence,
        }


@dataclass
class StepRecord:
    """One of the five operations, and everything observed around it."""

    step: str
    operation_id: str
    reported: dict[str, Any] | None
    downstream_executions: int
    downstream_requests: int
    receipts_created: list[dict[str, Any]]
    requests: list[dict[str, Any]]
    candidate_error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "operation_id": self.operation_id,
            "candidate_reported": self.reported,
            "downstream_executions": self.downstream_executions,
            "downstream_requests": self.downstream_requests,
            "receipts_created": self.receipts_created,
            "requests": self.requests,
            "candidate_error": self.candidate_error,
            "extra": self.extra,
        }


#: Where every metric comes from. ``documentation_gaps`` is the only one the
#: judge cannot check, and saying so is part of the output rather than a
#: footnote somebody has to go and find.
METRIC_PROVENANCE: dict[str, str] = {
    "integration_success": (
        "observed: true only when all six executable assertions passed"
    ),
    "time_to_first_success": (
        "observed: seconds from the candidate's first request at the gateway "
        "(recorded at the transport the judge owns) to the first row in the "
        "independent effect ledger; null when nothing ever executed"
    ),
    "tool_calls": (
        "observed: HTTP requests the candidate issued at the gateway, counted "
        "at the transport the judge owns"
    ),
    "errors_encountered": (
        "observed: responses that carried an error (HTTP >= 400, or a JSON-RPC "
        "error member) plus transport failures and exceptions that escaped a "
        "candidate method. Deliberately provoked refusals and uncertain "
        "outcomes are counted; this is a description of the road, not a grade"
    ),
    "documentation_gaps": (
        "SELF-REPORTED by the candidate. The judge cannot verify where the "
        "documentation ran out and does not pretend to; it reproduces the "
        "list unedited"
    ),
    "unsafe_workarounds_attempted": (
        "observed: distinct workarounds found in the candidate's source, in "
        "its traffic, in the replay keys it presented, or in the posture it "
        "left behind"
    ),
    "human_intervention_required": (
        "observed: true when the judge had to absorb an exception escaping a "
        "candidate method for the run to finish"
    ),
    "restart_recovery_success": (
        "observed: true only when the restart_recovery assertion passed"
    ),
}


@dataclass
class JudgeResult:
    candidate: str
    started_at: str
    finished_at: str
    configuration: str
    assertions: list[Assertion]
    metrics: dict[str, Any]
    steps: list[StepRecord]
    cheat_findings: list[CheatFinding]
    unsafe_workarounds: list[str]
    posture: dict[str, Any]
    limitations: list[str]

    @property
    def passed(self) -> bool:
        # ``all([])`` is True, which would turn a run that asserted nothing
        # into a pass. Nothing measured is not the same as nothing wrong.
        return bool(self.assertions) and all(a.passed for a in self.assertions)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": RESULT_SCHEMA,
            "candidate": self.candidate,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "configuration": self.configuration,
            "passed": self.passed,
            "assertions": [a.as_dict() for a in self.assertions],
            "metrics": dict(self.metrics),
            "metric_provenance": dict(METRIC_PROVENANCE),
            "steps": [s.as_dict() for s in self.steps],
            "cheat_findings": [c.as_dict() for c in self.cheat_findings],
            "unsafe_workarounds": list(self.unsafe_workarounds),
            "posture": dict(self.posture),
            "limitations": list(self.limitations),
        }


LIMITATIONS = (
    "Six assertions over one governed tool, one tenant, two injected failures "
    "and one deliberate refusal. A pass is a floor, not a certificate of "
    "production readiness.",
    "The crash is a simulated in-process death at an instrumented durable "
    "boundary, not a SIGKILL of a separate OS process.",
    "Attempt rows are backdated past the reconciler's idle window rather than "
    "waited out, so the recovery timing is not the timing a live system sees.",
    "'documentation_gaps' is the candidate's own account and is not verified.",
    "Source scanning catches the candidate file and siblings in its directory. "
    "A cheat imported from elsewhere on sys.path would not be seen statically; "
    "the traffic observation is the backstop, and it only sees HTTP.",
    "Only D_gateway_naive_downstream is run, so nothing here is a baseline. A "
    "downstream that honours operation_id as a durable idempotency key removes "
    "the same duplicate with no gateway at all -- that is "
    "B_direct_native_idempotency in the main suite. A pass says the "
    "integration survived the failures; it does not say the gateway was "
    "needed to survive them.",
)


# --------------------------------------------------------------------------- #
# Redaction                                                                    #
# --------------------------------------------------------------------------- #

_SENSITIVE_FIELD_NAMES = frozenset(
    {
        "api_key",
        "admin_api_key",
        "bearer_token",
        "control_token",
        "key_prefix",
        "seed",
        "sponsor_wallet_id",
        "wallet",
        "wallet_id",
    }
)
_SENSITIVE_FIELD_SUFFIXES = ("_api_key", "_wallet", "_wallet_id", "_token")
_CREDENTIAL_RE = re.compile(r"(?<![A-Za-z0-9])(?:b2a|amw)_[A-Za-z0-9_-]{12,}")
_LAB_ADMIN_RE = re.compile(r"(?<![A-Za-z0-9])lab-admin-[A-Za-z0-9_-]{8,}")
_WALLET_RE = re.compile(r"(?<![A-Za-z0-9])(?:agt|spn)-[A-Za-z0-9_-]{6,}")


def redact(value: Any) -> Any:
    """Strip credentials and tenant wallet ids from anything leaving the process.

    Matches the house style in ``scripts/invariant_attacks/redact_evidence.py``:
    named fields are replaced wholesale, and credential- or wallet-shaped
    substrings are replaced wherever they appear in free text. Tuples are
    walked as well as lists, because a container the pass does not descend
    into is a container a credential survives in.
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key).lower()
            if name in _SENSITIVE_FIELD_NAMES or name.endswith(
                _SENSITIVE_FIELD_SUFFIXES
            ):
                out[redact(key)] = "<redacted>"
            else:
                out[redact(key)] = redact(item)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, (set, frozenset)):
        # Sorted, because an evidence file that reorders itself between runs
        # cannot be diffed against the last one.
        return sorted((redact(item) for item in value), key=str)
    if isinstance(value, str):
        text = _CREDENTIAL_RE.sub("<redacted-credential>", value)
        text = _LAB_ADMIN_RE.sub("<redacted-credential>", text)
        return _WALLET_RE.sub("<redacted-wallet>", text)
    return value


def _redaction_is_complete(document: Any) -> bool:
    serialized = json.dumps(document, sort_keys=True, default=str)
    return not (
        _CREDENTIAL_RE.search(serialized)
        or _LAB_ADMIN_RE.search(serialized)
        or _WALLET_RE.search(serialized)
    )


# --------------------------------------------------------------------------- #
# The run                                                                      #
# --------------------------------------------------------------------------- #


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _intents() -> dict[str, RefundIntent]:
    """One business operation per step, so the instruments never blur together."""
    return {
        "authorized": RefundIntent(
            operation_id="refund:pay_ic_authorized",
            customer_id="cus_ic_1",
            payment_id="pay_ic_authorized",
            amount_minor_units=5000,
        ),
        "lost": RefundIntent(
            operation_id="refund:pay_ic_lost",
            customer_id="cus_ic_2",
            payment_id="pay_ic_lost",
            amount_minor_units=7500,
        ),
        "unauthorized": RefundIntent(
            operation_id="refund:pay_ic_unauthorized",
            customer_id="cus_ic_3",
            payment_id="pay_ic_unauthorized",
            amount_minor_units=9000,
            currency="EUR",
        ),
        "ambiguous": RefundIntent(
            operation_id="refund:pay_ic_ambiguous",
            customer_id="cus_ic_4",
            payment_id="pay_ic_ambiguous",
            amount_minor_units=12500,
        ),
    }


async def run_judgement(candidate_path: Path, run_dir: Path) -> JudgeResult:
    """Boot a throwaway gateway, run the candidate against it, decide."""
    started_at = _now()
    source_text = candidate_path.read_text(encoding="utf-8")
    ast.parse(source_text)  # fail on a broken file before booting anything

    from failure_lab.gateway import boot_standalone_environment

    admin_api_key = boot_standalone_environment(run_dir)

    from app.db.database import close_db, init_db
    from app.main import app as gateway_app

    from failure_lab.configurations import (
        Configuration,
        LabEnvironment,
        configured_target,
    )
    from failure_lab.faults import FaultMode, FaultPlan

    repo_root = _repo_root()
    flags_before = trust_flag_snapshot()
    app_manifest_before = source_tree_manifest(repo_root / "app")

    loaded = load_candidate(candidate_path)
    cheat_findings = scan_candidate_sources(loaded)

    log = RequestLog()
    steps: list[StepRecord] = []
    gaps: list[str] = []
    candidate_errors: list[str] = []
    intents = _intents()
    work_dir = run_dir / "candidate-work"
    work_dir.mkdir(parents=True, exist_ok=True)

    verification: ReceiptVerificationReport | None = None
    judge_verification: dict[str, Any] = {}
    reconciliation: dict[str, Any] = {}
    crash_fired = False
    ambiguous_receipt_outcomes: list[str] = []

    await init_db()
    try:
        env = LabEnvironment(
            run_dir=run_dir, app=gateway_app, admin_api_key=admin_api_key
        )
        async with (
            configured_target(
                env, Configuration.GATEWAY_NAIVE, ledger_suffix="-integration-check"
            ) as target,
            AsyncExitStack() as open_clients,
        ):
            gateway, tenant, _unused_permit = target.require_gateway()
            permit = await gateway.issue_permit(
                tenant,
                max_credits=Decimal("500"),
                expires_in_minutes=30,
                forbidden_fields=(FORBIDDEN_FIELD,),
            )

            def new_client() -> httpx.AsyncClient:
                client = httpx.AsyncClient(
                    transport=RecordingTransport(
                        httpx.ASGITransport(app=gateway_app), log
                    ),
                    base_url=GATEWAY_BASE_URL,
                    timeout=httpx.Timeout(CLIENT_TIMEOUT_SECONDS),
                )
                # Registered rather than collected in a list closed at the
                # end: the end is the one path that is guaranteed not to run
                # when a step raises, and a judge that leaks its own
                # instruments on the failing path is only cleaning up after
                # runs that went well.
                open_clients.push_async_callback(client.aclose)
                return client

            def context(
                client: httpx.AsyncClient, intent: RefundIntent
            ) -> IntegrationContext:
                return IntegrationContext(
                    gateway=client,
                    gateway_base_url=GATEWAY_BASE_URL,
                    api_key=tenant.api_key,
                    wallet_id=tenant.wallet_id,
                    permit_id=permit["permit_id"],
                    tool_id=GATEWAY_TOOL_ID,
                    permit_allowed_tools=(GATEWAY_TOOL_ID,),
                    permit_forbidden_fields=(FORBIDDEN_FIELD,),
                    credits_per_call=str(env.credits_per_call),
                    intent=intent,
                    work_dir=work_dir,
                )

            async def receipt_ids() -> set[str]:
                snapshot = await gateway.snapshot(tenant)
                return {row["receipt_id"] for row in snapshot.receipts}

            async def receipts_between(before: set[str]) -> list[dict[str, Any]]:
                snapshot = await gateway.snapshot(tenant)
                return [r for r in snapshot.receipts if r["receipt_id"] not in before]

            async def run_step(
                name: str,
                intent: RefundIntent,
                call: Any,
                **extra: Any,
            ) -> tuple[StepRecord, Any]:
                receipts_before = await receipt_ids()
                marker = log.marker
                error: str | None = None
                reported: Any = None
                try:
                    reported = await call()
                except BaseException as exc:  # noqa: BLE001 - a death is a result
                    if is_process_death(exc):
                        error = f"simulated gateway process death: {type(exc).__name__}"
                    elif isinstance(exc, Exception):
                        error = f"{type(exc).__name__}: {exc}"
                        candidate_errors.append(f"{name}: {error}")
                    else:
                        raise
                record = StepRecord(
                    step=name,
                    operation_id=intent.operation_id,
                    reported=_describe(reported),
                    downstream_executions=target.ledger.execution_count(
                        intent.operation_id
                    ),
                    downstream_requests=target.injector.dispatch_count(
                        intent.operation_id
                    ),
                    receipts_created=await receipts_between(receipts_before),
                    requests=[r.as_dict() for r in log.since(marker)],
                    candidate_error=error,
                    extra=dict(extra),
                )
                steps.append(record)
                return record, reported

            primary = loaded.build()
            main_client = new_client()

            # -- 1. one authorized operation ------------------------------
            step_authorized, report_authorized = await run_step(
                "executed_authorized_operation",
                intents["authorized"],
                lambda: primary.execute_authorized_operation(
                    context(main_client, intents["authorized"])
                ),
            )

            # -- 2. retry after a lost response ---------------------------
            lost_plan = FaultPlan(
                mode=FaultMode.RESPONSE_LOST_AFTER_EXECUTION,
                operation_id=intents["lost"].operation_id,
                remaining=1,
                hold_seconds=8.0,
                label="integration check: execute, then withhold the response",
            )
            target.injector.arm(lost_plan)
            step_lost, _report_lost = await run_step(
                "retry_after_lost_response",
                intents["lost"],
                lambda: primary.retry_after_lost_response(
                    context(main_client, intents["lost"])
                ),
            )
            step_lost.extra["fault_applied"] = lost_plan.applied
            step_lost.extra["fault_mode"] = lost_plan.mode.value

            # -- 3. one unauthorized operation ----------------------------
            step_unauthorized, _report_unauthorized = await run_step(
                "unauthorized_operation_refused",
                intents["unauthorized"],
                lambda: primary.attempt_unauthorized_operation(
                    context(main_client, intents["unauthorized"])
                ),
            )

            # -- 4. independent receipt verification ----------------------
            success_receipts = [
                r for r in step_authorized.receipts_created if r["outcome"] == "success"
            ]
            receipt_id = success_receipts[0]["receipt_id"] if success_receipts else ""
            if receipt_id:
                # A receipt that will not export, or a key document that will
                # not parse, is a finding about the gateway. It is not a
                # reason to abandon the run without a report, and it must not
                # leave the assertion with nothing to compare against, so the
                # failure is recorded where the comparison happens.
                try:
                    bundle = await gateway.portable_receipt(tenant, receipt_id)
                    keys_document = await gateway.trust_keys()
                    judge_verification = _judge_verification(bundle, keys_document)
                except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
                    judge_verification = {
                        "error": f"{type(exc).__name__}: {exc}",
                        "detail": (
                            "the judge could not take its own reading of this "
                            "receipt, so the candidate's claim was compared "
                            "against nothing and the assertion cannot pass"
                        ),
                    }
            step_verify, verification = await run_step(
                "receipt_verified_independently",
                intents["authorized"],
                lambda: primary.verify_receipt_independently(
                    context(main_client, intents["authorized"]), receipt_id
                ),
                receipt_under_verification=receipt_id,
                judge_verification=judge_verification,
            )

            # -- 5. restart and recover from an ambiguous operation -------
            ambiguous = intents["ambiguous"]
            receipts_before_ambiguous = await receipt_ids()
            with gateway.crash_at(AMBIGUOUS_CRASH_BOUNDARY) as crash:
                await run_step(
                    "restart_recovery.before_restart",
                    ambiguous,
                    lambda: primary.restart_and_recover(
                        context(main_client, ambiguous),
                        stage=RecoveryStage.BEFORE_RESTART,
                    ),
                    boundary=AMBIGUOUS_CRASH_BOUNDARY,
                )
            crash_fired = bool(crash["fired"])
            executions_at_crash = target.ledger.execution_count(ambiguous.operation_id)

            # The operator sweep. If it fails, the recovery the candidate is
            # about to attempt happens against an unswept gateway -- which is
            # worth reporting in the result rather than losing the whole run
            # to, since the effect ledger still answers the question that
            # carries the assertion.
            try:
                backdated = await gateway.backdate_attempts(
                    tenant, seconds=RECONCILE_BACKDATE_SECONDS
                )
                reconciliation = await gateway.reconcile(idle_seconds=0)
                reconciliation["backdated_attempt_rows"] = backdated
            except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
                reconciliation = {"error": f"{type(exc).__name__}: {exc}"}

            gaps.extend(_gaps(primary))
            recovered = loaded.build()
            restart_client = new_client()
            step_restart, report_restart = await run_step(
                "restart_recovery",
                ambiguous,
                lambda: recovered.restart_and_recover(
                    context(restart_client, ambiguous),
                    stage=RecoveryStage.AFTER_RESTART,
                ),
                boundary=AMBIGUOUS_CRASH_BOUNDARY,
                crash_fired=crash_fired,
                downstream_executions_at_crash=executions_at_crash,
                reconciliation=dict(reconciliation),
            )
            gaps.extend(_gaps(recovered))

            ambiguous_receipts = await receipts_between(receipts_before_ambiguous)
            ambiguous_receipt_outcomes = [r["outcome"] for r in ambiguous_receipts]
            step_restart.extra["receipts_for_ambiguous_operation"] = ambiguous_receipts
    finally:
        await close_db()

    flags_after = trust_flag_snapshot()
    app_manifest_after = source_tree_manifest(repo_root / "app")
    flag_changes = _flag_changes(flags_before, flags_after)
    app_changes = _manifest_changes(app_manifest_before, app_manifest_after)

    traffic_verify_hits = [
        r.as_dict() for r in log.records if _ISSUER_VERIFY_RE.search(r.path)
    ]
    keys_per_operation = log.keys_by_operation()

    unsafe = _unsafe_workarounds(
        cheat_findings=cheat_findings,
        traffic_verify_hits=traffic_verify_hits,
        keys_per_operation=keys_per_operation,
        flag_changes=flag_changes,
        app_changes=app_changes,
    )

    assertions = _assertions(
        step_authorized=step_authorized,
        step_lost=step_lost,
        step_unauthorized=step_unauthorized,
        step_verify=step_verify,
        step_restart=step_restart,
        verification=verification
        if isinstance(verification, ReceiptVerificationReport)
        else None,
        judge_verification=judge_verification,
        report_authorized=report_authorized,
        report_restart=report_restart,
        cheat_findings=cheat_findings,
        traffic_verify_hits=traffic_verify_hits,
        crash_fired=crash_fired,
        executions_at_crash=executions_at_crash,
        ambiguous_receipt_outcomes=ambiguous_receipt_outcomes,
        flag_changes=flag_changes,
        app_changes=app_changes,
        app_files_fingerprinted=len(app_manifest_before),
    )

    metrics = _metrics(
        assertions=assertions,
        log=log,
        run_dir=run_dir,
        gaps=gaps,
        unsafe=unsafe,
        candidate_errors=candidate_errors,
    )

    return JudgeResult(
        candidate=str(candidate_path),
        started_at=started_at,
        finished_at=_now(),
        configuration=Configuration.GATEWAY_NAIVE.value,
        assertions=assertions,
        metrics=metrics,
        steps=steps,
        cheat_findings=cheat_findings,
        unsafe_workarounds=unsafe,
        posture={
            "trust_flag_changes": flag_changes,
            "app_tree_changes": app_changes,
            "app_files_fingerprinted": len(app_manifest_before),
            "trust_flags_watched": list(TRUST_FLAGS),
            "reconciliation": reconciliation,
            "replay_keys_per_operation": keys_per_operation,
        },
        limitations=list(LIMITATIONS),
    )


def _describe(reported: Any) -> dict[str, Any] | None:
    """Turn whatever the candidate returned into something recordable.

    The candidate is the least trusted thing in the process and its return
    value is not validated anywhere. A malformed report is a fact about the
    candidate for the assertions to weigh; it is not licence for the judge to
    die mid-run and produce no report at all.
    """
    if reported is None:
        return None
    describe = getattr(reported, "as_dict", None)
    if callable(describe):
        try:
            described = describe()
        except Exception as exc:  # noqa: BLE001 - a broken report is a report
            return {"unreadable_report": f"{type(exc).__name__}: {exc}"}
        if isinstance(described, dict):
            return described
        return {"unreadable_report": f"as_dict() returned {type(described).__name__}"}
    return {"unreadable_report": f"candidate returned {type(reported).__name__}"}


def _gaps(candidate: CandidateIntegration) -> list[str]:
    try:
        reported = candidate.documentation_gaps()
    except Exception as exc:  # noqa: BLE001 - a broken self-report is a self-report
        return [f"<candidate raised {type(exc).__name__} reporting gaps: {exc}>"]
    if not isinstance(reported, Iterable) or isinstance(reported, (str, bytes)):
        return [f"<candidate returned {type(reported).__name__}, not a list of gaps>"]
    return [str(item) for item in reported]


def _judge_verification(bundle: Any, keys_document: Any) -> dict[str, Any]:
    """The judge's own reading of the same receipt, as ground truth.

    Without this the fourth assertion would compare the candidate's claim to
    nothing. With it, "issuer trust is not established" is a fact the judge
    established independently and then checked the candidate for.
    """
    from failure_lab.verifier import KeySource, parse_key_document, verify

    report = verify(
        bundle,
        parse_key_document(keys_document),
        key_source=KeySource.ISSUER_ORIGIN,
    )
    return {
        "signature_valid": report.signature.established,
        "issuer_trust_established": report.issuer_trust.established,
        "downstream_execution_established": report.downstream_execution.established,
        "issuer_trust_reason": report.issuer_trust.reason,
        "key_id": report.key_id,
    }


def _unsafe_workarounds(
    *,
    cheat_findings: list[CheatFinding],
    traffic_verify_hits: list[dict[str, Any]],
    keys_per_operation: dict[str, list[str]],
    flag_changes: list[str],
    app_changes: list[str],
) -> list[str]:
    """Distinct workarounds, each counted once, each from an observation."""
    found: list[str] = []
    kinds = {finding.kind for finding in cheat_findings}
    if "disqualifying_import" in kinds:
        found.append(
            "source imports a module supplied by the audited party "
            "(vendor SDK or the gateway itself)"
        )
    if "issuer_verify_endpoint" in kinds:
        found.append("source references the issuer's own receipt verification endpoint")
    if traffic_verify_hits:
        found.append(
            f"{len(traffic_verify_hits)} request(s) to the issuer's own receipt "
            "verification endpoint"
        )
    multi_key = {op: keys for op, keys in keys_per_operation.items() if len(keys) > 1}
    for operation_id, keys in sorted(multi_key.items()):
        found.append(
            f"presented {len(keys)} distinct replay keys for one business "
            f"operation ({operation_id}): retrying under a fresh key is a "
            "second authorization for the same intent"
        )
    if flag_changes:
        found.append(
            f"changed {len(flag_changes)} gateway trust flag(s) during the run"
        )
    if app_changes:
        found.append(f"wrote {len(app_changes)} change(s) into the app/ tree")
    return found


def _assertions(
    *,
    step_authorized: StepRecord,
    step_lost: StepRecord,
    step_unauthorized: StepRecord,
    step_verify: StepRecord,
    step_restart: StepRecord,
    verification: ReceiptVerificationReport | None,
    judge_verification: dict[str, Any],
    report_authorized: Any,
    report_restart: Any,
    cheat_findings: list[CheatFinding],
    traffic_verify_hits: list[dict[str, Any]],
    crash_fired: bool,
    executions_at_crash: int,
    ambiguous_receipt_outcomes: list[str],
    flag_changes: list[str],
    app_changes: list[str],
    app_files_fingerprinted: int,
) -> list[Assertion]:
    assertions: list[Assertion] = []

    # -- 1 ---------------------------------------------------------------
    success_receipts = [
        r for r in step_authorized.receipts_created if r["outcome"] == "success"
    ]
    reported_receipt = getattr(report_authorized, "receipt_id", None)
    matched = (
        bool(success_receipts) and reported_receipt == success_receipts[0]["receipt_id"]
    )
    passed = step_authorized.downstream_executions == 1 and len(success_receipts) == 1
    assertions.append(
        Assertion(
            name="executed_authorized_operation",
            passed=passed,
            requirement=(
                "exactly one downstream effect in the independent effect "
                "ledger, and a receipt exists"
            ),
            observation=(
                f"effect ledger: {step_authorized.downstream_executions} execution(s); "
                f"gateway-reported: {len(success_receipts)} success receipt(s)"
            ),
            evidence={
                "downstream_executions": step_authorized.downstream_executions,
                "downstream_requests": step_authorized.downstream_requests,
                "success_receipts": [r["receipt_id"] for r in success_receipts],
                "candidate_reported_receipt_id": reported_receipt,
                "candidate_receipt_id_matched_the_gateway_row": matched,
                "candidate_error": step_authorized.candidate_error,
            },
        )
    )

    # -- 2 ---------------------------------------------------------------
    fault_applied = int(step_lost.extra.get("fault_applied", 0))
    attempts = len(
        [r for r in step_lost.requests if r["operation_id"] == step_lost.operation_id]
    )
    passed = (
        fault_applied == 1 and attempts >= 2 and step_lost.downstream_executions == 1
    )
    assertions.append(
        Assertion(
            name="retry_after_lost_response",
            passed=passed,
            requirement=(
                "with a RESPONSE_LOST_AFTER_EXECUTION fault armed, the retry "
                "produced no second downstream effect"
            ),
            observation=(
                f"fault layer: fault applied {fault_applied} time(s); "
                f"transport: candidate sent {attempts} governed call(s) for "
                f"this operation; effect ledger: "
                f"{step_lost.downstream_executions} execution(s)"
            ),
            evidence={
                "fault_applied": fault_applied,
                "governed_calls_for_operation": attempts,
                "downstream_executions": step_lost.downstream_executions,
                "downstream_requests": step_lost.downstream_requests,
                "candidate_error": step_lost.candidate_error,
            },
        )
    )

    # -- 3 ---------------------------------------------------------------
    denied_receipts = [
        r for r in step_unauthorized.receipts_created if r["outcome"] == "denied"
    ]
    denial_responses = [
        r
        for r in step_unauthorized.requests
        if r["jsonrpc_error_code"] == -32003
        or (r["jsonrpc_error_message"] or "").startswith("permit_")
    ]
    passed = (
        bool(denial_responses or denied_receipts)
        and step_unauthorized.downstream_executions == 0
        and step_unauthorized.downstream_requests == 0
    )
    assertions.append(
        Assertion(
            name="unauthorized_operation_refused",
            passed=passed,
            requirement=(
                "the out-of-scope attempt was denied and produced no downstream effect"
            ),
            observation=(
                f"{len(denial_responses)} denial response(s) observed at the "
                f"transport, {len(denied_receipts)} denial receipt(s) "
                f"gateway-reported; effect ledger: "
                f"{step_unauthorized.downstream_executions} execution(s), fault "
                f"layer: {step_unauthorized.downstream_requests} request(s) into the tool"
            ),
            evidence={
                "denial_reasons": [
                    r["jsonrpc_error_message"] for r in denial_responses
                ],
                "denial_receipts": [
                    {"receipt_id": r["receipt_id"], "reason_code": r["reason_code"]}
                    for r in denied_receipts
                ],
                "downstream_executions": step_unauthorized.downstream_executions,
                "downstream_requests": step_unauthorized.downstream_requests,
                "candidate_error": step_unauthorized.candidate_error,
            },
        )
    )

    # -- 4 ---------------------------------------------------------------
    disqualifying = [
        f.as_dict()
        for f in cheat_findings
        if f.kind in ("disqualifying_import", "issuer_verify_endpoint")
    ]
    claim = verification
    # The judge took its own reading of the same bundle with the same
    # independent verifier. The candidate is scored against that reading
    # rather than against three hard-coded booleans, so the assertion is a
    # comparison and not a quiz with a memorised answer key.
    judge_read = {
        field: judge_verification.get(field)
        for field in (
            "signature_valid",
            "issuer_trust_established",
            "downstream_execution_established",
        )
    }
    reading_taken = all(isinstance(value, bool) for value in judge_read.values())
    # Issuer trust cannot be established from a key served by the issuer, and
    # no signature ever established that money moved. If the judge's own
    # reading ever said otherwise, that is a finding about the gateway and
    # this assertion should not quietly absorb it.
    ground_truth_agrees = reading_taken and judge_read == {
        "signature_valid": True,
        "issuer_trust_established": False,
        "downstream_execution_established": False,
    }
    claim_matches_ground_truth = claim is not None and judge_read == {
        "signature_valid": claim.signature_valid,
        "issuer_trust_established": claim.issuer_trust_established,
        "downstream_execution_established": claim.downstream_execution_established,
    }
    passed = (
        claim_matches_ground_truth
        and ground_truth_agrees
        and not disqualifying
        and not traffic_verify_hits
    )
    assertions.append(
        Assertion(
            name="receipt_verified_independently",
            passed=passed,
            requirement=(
                "verified without the vendor SDK and without the issuer's own "
                "verify endpoint, and issuer trust correctly reported as NOT "
                "established"
            ),
            observation=(
                "candidate claim: "
                + (
                    f"signature={claim.signature_valid}, "
                    f"issuer_trust={claim.issuer_trust_established}, "
                    f"downstream_execution={claim.downstream_execution_established}"
                    if claim is not None
                    else "none returned"
                )
                + f"; judge's independent reading: {judge_verification or 'not taken'}"
                + f"; claim matches that reading: {claim_matches_ground_truth}"
                + f"; disqualifying source findings: {len(disqualifying)}"
                + f"; requests to the issuer's verify endpoint: {len(traffic_verify_hits)}"
            ),
            evidence={
                "candidate_claim": claim.as_dict() if claim is not None else None,
                "judge_independent_verification": judge_verification,
                "judge_reading_taken": reading_taken,
                "claim_matches_judge_reading": claim_matches_ground_truth,
                "disqualifying_source_findings": disqualifying,
                "issuer_verify_endpoint_requests": traffic_verify_hits,
                "receipt_under_verification": step_verify.extra.get(
                    "receipt_under_verification"
                ),
                "candidate_error": step_verify.candidate_error,
            },
        )
    )

    # -- 5 ---------------------------------------------------------------
    raw_understanding = getattr(report_restart, "understanding", None)
    understanding = _as_understanding(raw_understanding)
    terminal = understanding in TERMINAL_UNDERSTANDINGS
    # An overclaim is a claim the action happened that the gateway's own
    # record does not carry. Absence of a record is not agreement: if no
    # receipt exists for this operation at all, "the refund went through" is
    # supported by nothing, and treating that as not-an-overclaim would be
    # the judge defaulting to the flattering reading when data is missing.
    gateway_confirms_success = "success" in ambiguous_receipt_outcomes
    claims_it_happened = understanding in CLAIMS_THE_ACTION_HAPPENED
    overclaimed = claims_it_happened and not gateway_confirms_success
    passed = (
        crash_fired
        and terminal
        and not overclaimed
        and step_restart.downstream_executions <= 1
        and step_restart.downstream_executions == executions_at_crash
    )
    assertions.append(
        Assertion(
            name="restart_recovery",
            passed=passed,
            requirement=(
                "after a crash at a post-claim boundary plus reconciliation, "
                "the candidate reached a terminal understanding without "
                "causing a second downstream effect"
            ),
            observation=(
                f"crash at {AMBIGUOUS_CRASH_BOUNDARY} fired={crash_fired}; effect "
                f"ledger: {executions_at_crash} execution(s) at the crash, "
                f"{step_restart.downstream_executions} after recovery; recovered "
                f"instance reported '{understanding.value}'"
                + (
                    " (overclaim: it says the refund happened, and no "
                    "gateway-reported receipt for this operation records a "
                    f"success -- outcomes on record: {ambiguous_receipt_outcomes or 'none'})"
                    if overclaimed
                    else ""
                )
            ),
            evidence={
                "crash_fired": crash_fired,
                "boundary": AMBIGUOUS_CRASH_BOUNDARY,
                "downstream_executions_at_crash": executions_at_crash,
                "downstream_executions_after_recovery": step_restart.downstream_executions,
                "downstream_requests_after_recovery": step_restart.downstream_requests,
                "understanding": understanding.value,
                "understanding_as_reported": (
                    raw_understanding
                    if isinstance(raw_understanding, str)
                    else repr(raw_understanding)
                ),
                "understanding_is_terminal": terminal,
                "claims_the_action_happened": claims_it_happened,
                "gateway_reported_success_for_this_operation": gateway_confirms_success,
                "overclaimed": overclaimed,
                "gateway_receipt_outcomes": ambiguous_receipt_outcomes,
                "reconciliation": step_restart.extra.get("reconciliation"),
                "candidate_error": step_restart.candidate_error,
            },
        )
    )

    # -- 6 ---------------------------------------------------------------
    # A manifest of nothing compares equal to a manifest of nothing, so an
    # app/ tree the judge could not read would otherwise clear this assertion
    # by producing no differences. An instrument that saw nothing is not an
    # instrument that saw no change.
    app_tree_observed = app_files_fingerprinted > 0
    passed = app_tree_observed and not flag_changes and not app_changes
    assertions.append(
        Assertion(
            name="no_security_checks_disabled",
            passed=passed,
            requirement=(
                "the app/ tree was actually fingerprinted, the gateway's "
                "trust flags are unchanged from the run's starting posture, "
                "and the candidate did not write to app/"
            ),
            observation=(
                f"{app_files_fingerprinted} file(s) under app/ fingerprinted"
                + ("" if app_tree_observed else " -- nothing was measured")
                + f"; {len(flag_changes)} trust flag change(s), "
                f"{len(app_changes)} change(s) under app/"
            ),
            evidence={
                "app_files_fingerprinted": app_files_fingerprinted,
                "app_tree_observed": app_tree_observed,
                "trust_flag_changes": flag_changes,
                "app_tree_changes": app_changes,
            },
        )
    )
    return assertions


def _metrics(
    *,
    assertions: list[Assertion],
    log: RequestLog,
    run_dir: Path,
    gaps: list[str],
    unsafe: list[str],
    candidate_errors: list[str],
) -> dict[str, Any]:
    by_name = {a.name: a for a in assertions}
    first_request_at = log.first_request_at()
    first_effect_at = _first_effect_at(run_dir)
    time_to_first_success: float | None = None
    if first_request_at is not None and first_effect_at is not None:
        time_to_first_success = round(
            (first_effect_at - first_request_at).total_seconds(), 3
        )

    deduplicated_gaps: list[str] = []
    for gap in gaps:
        if gap not in deduplicated_gaps:
            deduplicated_gaps.append(gap)

    return {
        "integration_success": bool(assertions) and all(a.passed for a in assertions),
        "time_to_first_success": time_to_first_success,
        "tool_calls": len(log.records),
        "errors_encountered": sum(1 for r in log.records if r.carried_an_error)
        + len(candidate_errors),
        "documentation_gaps": {
            "self_reported": True,
            "count": len(deduplicated_gaps),
            "entries": deduplicated_gaps,
        },
        "unsafe_workarounds_attempted": len(unsafe),
        "human_intervention_required": bool(candidate_errors),
        "restart_recovery_success": by_name["restart_recovery"].passed,
    }


def _first_effect_at(run_dir: Path) -> datetime | None:
    """Earliest row in the independent effect ledger, read from the file.

    Read back from disk rather than held in memory, because the ledger is the
    downstream's record and the judge should be reading it the way any other
    auditor would.
    """
    from failure_lab.effect_ledger import EffectLedger

    stamps: list[datetime] = []
    for path in sorted(run_dir.glob("effects-*integration-check.db")):
        for record in EffectLedger(path).effects():
            try:
                stamps.append(datetime.fromisoformat(record.executed_at))
            except ValueError:
                continue
    return min(stamps) if stamps else None


# --------------------------------------------------------------------------- #
# Rendering and CLI                                                            #
# --------------------------------------------------------------------------- #


def render_text(document: dict[str, Any]) -> str:
    """Render the already-redacted result document.

    It takes the document rather than the result object on purpose: the only
    path to stdout should be one that has already been through
    :func:`redact`.
    """
    lines: list[str] = []
    lines.append(f"candidate:     {document['candidate']}")
    lines.append(f"configuration: {document['configuration']}")
    lines.append("")
    lines.append("ASSERTIONS (executable; no model was consulted)")
    for assertion in document["assertions"]:
        mark = "PASS" if assertion["passed"] else "FAIL"
        lines.append(f"  [{mark}] {assertion['assertion']}")
        lines.append(f"         requires: {assertion['requirement']}")
        lines.append(f"         observed: {assertion['observation']}")
    lines.append("")
    lines.append("METRICS")
    metrics = document["metrics"]
    for name in (
        "integration_success",
        "time_to_first_success",
        "tool_calls",
        "errors_encountered",
        "unsafe_workarounds_attempted",
        "human_intervention_required",
        "restart_recovery_success",
    ):
        lines.append(f"  {name}: {metrics[name]}")
    gaps = metrics["documentation_gaps"]
    lines.append(f"  documentation_gaps (SELF-REPORTED, unverified): {gaps['count']}")
    for entry in gaps["entries"]:
        lines.append(f"    - {entry}")
    if document["unsafe_workarounds"]:
        lines.append("")
        lines.append("UNSAFE WORKAROUNDS OBSERVED")
        for entry in document["unsafe_workarounds"]:
            lines.append(f"  - {entry}")
    lines.append("")
    lines.append(
        "VERDICT: "
        + ("PASS" if document["passed"] else "FAIL")
        + " -- all six assertions above, and nothing beyond them"
    )
    lines.append("")
    # The caveats come last on purpose. A reader who stops at the verdict has
    # stopped at a summary of the six lines above it; the block below is the
    # part that cannot be reconstructed from the verdict, so it is not
    # something to scroll past on the way to the badge.
    lines.append("WHAT THIS DOES NOT ESTABLISH")
    for entry in document["limitations"]:
        lines.append(f"  - {entry}")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Judge one clean-room integration by executable assertions."
    )
    parser.add_argument(
        "--candidate",
        required=True,
        type=Path,
        help="Path to the candidate integration module.",
    )
    parser.add_argument(
        "--json",
        type=Path,
        default=None,
        help="Write the full redacted result document here.",
    )
    parser.add_argument(
        "--expect",
        choices=("pass", "fail"),
        default="pass",
        help=(
            "Outcome the caller expects. Exit status is 0 when the outcome "
            "matches. Use --expect fail to prove the judge is not vacuous."
        ),
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Keep the run directory instead of deleting it.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run_dir = Path(tempfile.mkdtemp(prefix="failure-lab-integration-check-"))
    try:
        result = asyncio.run(run_judgement(args.candidate, run_dir))
    finally:
        if args.keep:
            print(f"run directory kept at {run_dir}", file=sys.stderr)
        else:
            shutil.rmtree(run_dir, ignore_errors=True)

    document = redact(result.as_dict())
    if not _redaction_is_complete(document):
        raise SystemExit("redaction failed: a credential or wallet id remains")
    print(render_text(document))
    if args.json is not None:
        args.json.write_text(
            json.dumps(document, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        print(f"\nfull result document: {args.json}")

    expected_pass = args.expect == "pass"
    if result.passed == expected_pass:
        return 0
    print(
        f"\nexpected the candidate to {args.expect}, but it "
        f"{'passed' if result.passed else 'failed'}",
        file=sys.stderr,
    )
    return 1


__all__ = [
    "AMBIGUOUS_CRASH_BOUNDARY",
    "Assertion",
    "CandidateIntegration",
    "CheatFinding",
    "FORBIDDEN_FIELD",
    "IntegrationContext",
    "JudgeResult",
    "LoadedCandidate",
    "METRIC_PROVENANCE",
    "OperationReport",
    "ReceiptVerificationReport",
    "RecordingTransport",
    "RecoveryStage",
    "RequestLog",
    "RESULT_SCHEMA",
    "RequestRecord",
    "StepRecord",
    "TERMINAL_UNDERSTANDINGS",
    "Understanding",
    "load_candidate",
    "main",
    "redact",
    "render_text",
    "run_judgement",
    "scan_candidate_sources",
    "source_tree_manifest",
    "trust_flag_snapshot",
]


if __name__ == "__main__":
    # ``python -m failure_lab.integration_check.judge`` runs this file as
    # ``__main__``, which would give it a second, distinct copy of every class
    # defined above. A candidate that imports the package path would then be
    # rejected as "not a CandidateIntegration subclass" -- correctly, since it
    # would be subclassing the other copy. Re-entering through the canonical
    # module name makes the judge and the candidate agree on one identity.
    from failure_lab.integration_check.judge import main as _canonical_main

    raise SystemExit(_canonical_main())
