"""The clean-room integration check: can an AI integrate this, and survive?

The question this package answers is narrow and unflattering by design:
**given only public material, can an AI agent put the gateway in front of an
unprotected application and still be right after the failures land?**

Nothing here asks a model whether it succeeded. A model's self-report is the
one piece of evidence a trust product may never accept, because the whole
pitch is that an agent's account of its own actions is not enough. So the
judge (:mod:`failure_lab.integration_check.judge`) reads the independent
effect ledger, the fault layer outside the gateway, the gateway's own tables
and the candidate's source text, and decides mechanically. The candidate
self-reports exactly one number — where the public documentation ran out —
and that number is labelled as self-reported everywhere it appears.

Three files carry the whole idea:

``PROMPT.md``
    What the integrating AI is given. Five required operations, three
    constraints, no hints about how the judge scores.
``sample_app/``
    A refund agent that calls the tool directly with no protection. The
    "before" picture, kept small enough to read in one sitting.
``judge.py``
    Six executable assertions and eight metrics, every one of them derived
    from an observation rather than from a claim.

A judge that has never failed anything is not a judge, so the package ships
two candidates: ``reference_candidate.py``, which passes, and
``broken_candidate.py``, which retries a lost response under a fresh
idempotency key and is failed for it. Both are run in the module's own
verification.
"""

from __future__ import annotations

__all__: list[str] = []
