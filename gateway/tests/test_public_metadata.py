"""Public identity metadata must never invent an escalation path."""

from types import SimpleNamespace

import pytest

from app.main import _public_contact_metadata, app
from app.routers.well_known import _provider_manifest


def _contact(**overrides):
    values = {
        "PUBLIC_CONTACT_NAME": "",
        "PUBLIC_CONTACT_EMAIL": "",
        "PUBLIC_CONTACT_URL": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_openapi_omits_contact_until_real_identity_is_configured() -> None:
    assert _public_contact_metadata(_contact()) is None
    assert "contact" not in app.openapi()["info"]


def test_openapi_contact_accepts_complete_non_placeholder_identity() -> None:
    assert _public_contact_metadata(
        _contact(
            PUBLIC_CONTACT_NAME="Accountable Operator",
            PUBLIC_CONTACT_EMAIL="operator@designpartnerlabs.co",
            PUBLIC_CONTACT_URL="https://cal.com/design-partner-labs/pilot",
        )
    ) == {
        "name": "Accountable Operator",
        "email": "operator@designpartnerlabs.co",
        "url": "https://cal.com/design-partner-labs/pilot",
    }


def test_agent_manifest_provider_uses_the_same_complete_contact_gate() -> None:
    assert _provider_manifest(_contact()) == {"status": "contact_not_configured"}
    complete = _contact(
        PUBLIC_CONTACT_NAME="Accountable Operator",
        PUBLIC_CONTACT_EMAIL="operator@designpartnerlabs.co",
        PUBLIC_CONTACT_URL="https://cal.com/design-partner-labs/pilot",
    )
    assert _provider_manifest(complete) == {
        "name": "Accountable Operator",
        "email": "operator@designpartnerlabs.co",
        "website": "https://cal.com/design-partner-labs/pilot",
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"PUBLIC_CONTACT_NAME": "Operator"},
        {
            "PUBLIC_CONTACT_NAME": "Placeholder Operator",
            "PUBLIC_CONTACT_EMAIL": "operator@designpartnerlabs.co",
            "PUBLIC_CONTACT_URL": "https://cal.com/design-partner-labs/pilot",
        },
        {
            "PUBLIC_CONTACT_NAME": "Operator",
            "PUBLIC_CONTACT_EMAIL": "support@agent-middleware.dev",
            "PUBLIC_CONTACT_URL": "https://cal.com/design-partner-labs/pilot",
        },
        {
            "PUBLIC_CONTACT_NAME": "Operator",
            "PUBLIC_CONTACT_EMAIL": "not-an-email",
            "PUBLIC_CONTACT_URL": "https://cal.com/design-partner-labs/pilot",
        },
        {
            "PUBLIC_CONTACT_NAME": "Operator",
            "PUBLIC_CONTACT_EMAIL": "operator@designpartnerlabs.co",
            "PUBLIC_CONTACT_URL": "http://company.test",
        },
        {
            "PUBLIC_CONTACT_NAME": "Test Operator",
            "PUBLIC_CONTACT_EMAIL": "operator@designpartnerlabs.co",
            "PUBLIC_CONTACT_URL": "https://cal.com/design-partner-labs/pilot",
        },
        {
            "PUBLIC_CONTACT_NAME": "Agent Middleware API",
            "PUBLIC_CONTACT_EMAIL": "operator@designpartnerlabs.co",
            "PUBLIC_CONTACT_URL": "https://cal.com/design-partner-labs/pilot",
        },
        {
            "PUBLIC_CONTACT_NAME": "Operator",
            "PUBLIC_CONTACT_EMAIL": "operator@company.test",
            "PUBLIC_CONTACT_URL": "https://cal.com/design-partner-labs/pilot",
        },
        {
            "PUBLIC_CONTACT_NAME": "Operator",
            "PUBLIC_CONTACT_EMAIL": "operator@company.invalid",
            "PUBLIC_CONTACT_URL": "https://cal.com/design-partner-labs/pilot",
        },
        {
            "PUBLIC_CONTACT_NAME": "Operator",
            "PUBLIC_CONTACT_EMAIL": "operator@designpartnerlabs.co",
            "PUBLIC_CONTACT_URL": "https://",
        },
        {
            "PUBLIC_CONTACT_NAME": "Operator",
            "PUBLIC_CONTACT_EMAIL": "operator@designpartnerlabs.co",
            "PUBLIC_CONTACT_URL": "https://calendar.company.test/pilot",
        },
        {
            "PUBLIC_CONTACT_NAME": "Operator",
            "PUBLIC_CONTACT_EMAIL": "operator@designpartnerlabs.co",
            "PUBLIC_CONTACT_URL": "https://www.thisisatest.tech/",
        },
    ],
)
def test_openapi_contact_rejects_partial_or_placeholder_identity(overrides) -> None:
    with pytest.raises(ValueError):
        _public_contact_metadata(_contact(**overrides))
