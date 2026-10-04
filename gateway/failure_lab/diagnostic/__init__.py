"""The self-serve diagnostic: **Agent Action Safety Check**.

A developer opens one page on their own machine, presses one button, and the
harness runs a real failure against a disposable sandbox: an agent calls a
tool, the tool executes and commits, the response is discarded on the way back,
the agent retries. The page then shows what the downstream system's own ledger
counted, in that configuration and in three others, and says plainly which of
them needed a gateway.

The interesting design constraint is not the page. It is that the page has to
be able to tell the visitor they do not need the product, in the same type
size, without a pitch underneath.
:mod:`failure_lab.diagnostic.answer` computes that outcome from the same
:class:`~failure_lab.report.Comparison` objects the evidence bundle is built
from, and :mod:`failure_lab.diagnostic.pages` gives it the same accent as the
outcome that favours the product. The offer block is rendered from
:attr:`~failure_lab.diagnostic.answer.DiagnosticAnswer.recommends_the_product`,
which has exactly one true case, so there is no branch in the renderer that can
put a sales pitch under a result that did not point at one.

Layout:

``answer``  the one visitor-facing outcome, by precedence, and what it does and
            does not establish.
``offer``   the commercial offer, defined once, with no price invented for it.
``pages``   server-rendered HTML: one template, one stylesheet, one script, no
            CDN and no framework.
``runs``    booting the sandbox once per process, executing one run, streaming
            its event log live, and deleting everything it touched.
``server``  the Starlette application: loopback only, one run at a time, no
            credential accepted anywhere, every body redacted and then scanned
            again before it is sent.
``steps``   translating the harness's own event log into the PRD's six lines,
            with the instrument reading that produced each one.

Entry point: ``python -m failure_lab serve``, which reaches :func:`main` here.
"""

from __future__ import annotations

from failure_lab.diagnostic.answer import (
    Answer,
    DiagnosticAnswer,
    build_answer,
    headline_for,
)
from failure_lab.diagnostic.offer import OFFER, Offer, OfferItem
from failure_lab.diagnostic.runs import (
    DEFAULT_SCENARIOS,
    PUBLIC_SCENARIOS,
    ProductionRefused,
    RunRecord,
    RunRefused,
)
from failure_lab.diagnostic.server import (
    DiagnosticService,
    DiagnosticSettings,
    create_app,
    main,
    serve,
)
from failure_lab.diagnostic.steps import Step, StepStream

__all__ = [
    "DEFAULT_SCENARIOS",
    "OFFER",
    "PUBLIC_SCENARIOS",
    "Answer",
    "DiagnosticAnswer",
    "DiagnosticService",
    "DiagnosticSettings",
    "Offer",
    "OfferItem",
    "ProductionRefused",
    "RunRecord",
    "RunRefused",
    "Step",
    "StepStream",
    "build_answer",
    "create_app",
    "headline_for",
    "main",
    "serve",
]
