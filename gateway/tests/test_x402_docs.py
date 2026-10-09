"""Docstring coverage for the x402 router module.

The router module must stay self describing: it keeps its schemas local
rather than growing app/schemas, so a reader meeting it for the first time
needs the module docstring and per schema docstrings. This test locks that
in. It asserts presence only, never wording.
"""

from __future__ import annotations

from app.routers import x402


def test_module_docstring_present() -> None:
    assert x402.__doc__, "gateway/app/routers/x402.py needs a module docstring"


def test_public_schema_docstrings_present() -> None:
    for name in (
        "X402ParseRequest",
        "X402RequirementResponse",
        "X402SettleRequest",
        "X402AttestationResponse",
        "X402SettleResponse",
    ):
        cls = getattr(x402, name)
        assert cls.__doc__, f"{name} needs a class docstring"


def test_public_endpoint_docstrings_present() -> None:
    assert x402.parse_payment_required.__doc__
    assert x402.settle_payment_required.__doc__
