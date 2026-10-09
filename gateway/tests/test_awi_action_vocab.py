"""Focused unit coverage for the AWI action vocabulary registry."""

import pytest

from app.schemas.awi import (
    AWIActionCategory,
    AWIActionRiskLevel,
    AWIActionStatus,
    AWIActionTier,
    AWIStandardAction,
)
from app.services.awi_action_vocab import (
    AWIActionDefinition,
    AWIActionVocabulary,
    get_awi_vocabulary,
)


@pytest.fixture
def vocab() -> AWIActionVocabulary:
    return AWIActionVocabulary()


def test_every_standard_action_is_registered(vocab: AWIActionVocabulary) -> None:
    missing = [a for a in AWIStandardAction if vocab.get_action(a) is None]
    assert missing == []


def test_singleton_is_shared() -> None:
    assert get_awi_vocabulary() is get_awi_vocabulary()


def test_list_all_and_by_category(vocab: AWIActionVocabulary) -> None:
    assert len(vocab.list_all_actions()) == len(AWIStandardAction)
    nav = vocab.list_actions_by_category(AWIActionCategory.NAVIGATION)
    assert {a.action for a in nav} == {
        AWIStandardAction.NAVIGATE_TO,
        AWIStandardAction.SCROLL,
    }
    assert vocab.list_actions_by_category(AWIActionCategory.SEARCH) != []


def test_definition_metadata(vocab: AWIActionVocabulary) -> None:
    login = vocab.get_action(AWIStandardAction.LOGIN)
    assert login is not None
    assert login.status == AWIActionStatus.PROVISIONAL
    assert login.risk_level == AWIActionRiskLevel.HIGH
    assert login.sensitive_parameters == ["username", "password"]
    click = vocab.get_action(AWIStandardAction.CLICK_BUTTON)
    assert click is not None
    assert click.tier == AWIActionTier.COMPATIBILITY


def test_to_public_dict_is_transport_safe(vocab: AWIActionVocabulary) -> None:
    action = vocab.get_action(AWIStandardAction.CHECKOUT)
    assert action is not None
    public = action.to_public_dict()
    assert public["action"] == AWIStandardAction.CHECKOUT.value
    assert public["category"] == AWIActionCategory.TRANSACTION.value
    assert public["risk_level"] == AWIActionRiskLevel.HIGH.value
    assert public["sensitive_parameters"] == ["payment_method"]


def test_validate_parameters_happy_path(vocab: AWIActionVocabulary) -> None:
    valid, error = vocab.validate_parameters(
        AWIStandardAction.SEARCH_AND_SORT, {"query": "laptops"}
    )
    assert (valid, error) == (True, None)


def test_validate_parameters_missing_required(vocab: AWIActionVocabulary) -> None:
    valid, error = vocab.validate_parameters(AWIStandardAction.SEARCH_AND_SORT, {})
    assert valid is False
    assert error is not None and "query" in error


def test_validate_parameters_unknown_action(vocab: AWIActionVocabulary) -> None:
    vocab._actions.pop(AWIStandardAction.SCROLL)
    valid, error = vocab.validate_parameters(AWIStandardAction.SCROLL, {})
    assert valid is False
    assert error is not None and "Unknown action" in error


def test_validate_login_accepts_handle_or_legacy_pair(
    vocab: AWIActionVocabulary,
) -> None:
    assert vocab.validate_parameters(
        AWIStandardAction.LOGIN, {"credential_handle": "credh_123"}
    ) == (True, None)
    assert vocab.validate_parameters(
        AWIStandardAction.LOGIN,
        {"username": "agent@example.com", "password": "secret"},
    ) == (True, None)


def test_validate_login_rejects_partial_legacy(
    vocab: AWIActionVocabulary,
) -> None:
    valid, error = vocab.validate_parameters(
        AWIStandardAction.LOGIN, {"username": "agent@example.com"}
    )
    assert valid is False
    assert error is not None and "credential_handle" in error


def test_redact_parameters_catches_key_variants(
    vocab: AWIActionVocabulary,
) -> None:
    params = {
        "paymentMethod": "card-token",
        "nested": {"access-token": "access-secret", "safe": "visible"},
    }
    redacted = vocab.redact_parameters(AWIStandardAction.CHECKOUT, params)
    assert redacted["paymentMethod"] == "[REDACTED]"
    assert redacted["nested"] == {"access-token": "[REDACTED]", "safe": "visible"}
    assert params["paymentMethod"] == "card-token"


def test_redact_parameters_login_secrets_and_lists(
    vocab: AWIActionVocabulary,
) -> None:
    redacted = vocab.redact_parameters(
        AWIStandardAction.LOGIN,
        {"username": "agent@example.com", "Password": "s3cret", "remember_me": True},
    )
    assert redacted == {
        "username": "[REDACTED]",
        "Password": "[REDACTED]",
        "remember_me": True,
    }
    listed = vocab.redact_parameters(
        AWIStandardAction.FILL_FORM, {"fields": [{"token": "abc", "label": "x"}]}
    )
    assert listed == {"fields": [{"token": "[REDACTED]", "label": "x"}]}


def test_check_preconditions_met_and_unmet(vocab: AWIActionVocabulary) -> None:
    met, unmet = vocab.check_preconditions(
        AWIStandardAction.ADD_TO_CART,
        {"capabilities": ["item_visible", "cart_accessible"]},
    )
    assert (met, unmet) == (True, [])
    met, unmet = vocab.check_preconditions(
        AWIStandardAction.ADD_TO_CART, {"capabilities": ["item_visible"]}
    )
    assert met is False
    assert unmet == ["cart_accessible"]


def test_check_preconditions_unknown_action(vocab: AWIActionVocabulary) -> None:
    vocab._actions.pop(AWIStandardAction.LOGOUT)
    met, unmet = vocab.check_preconditions(AWIStandardAction.LOGOUT, {})
    assert met is False
    assert len(unmet) == 1 and "Unknown action" in unmet[0]


def test_estimated_cost_known_and_unknown(vocab: AWIActionVocabulary) -> None:
    assert vocab.get_estimated_cost(AWIStandardAction.CHECKOUT) > 0
    vocab._actions.pop(AWIStandardAction.CHECKOUT)
    assert vocab.get_estimated_cost(AWIStandardAction.CHECKOUT) == 0.0


def test_register_custom_action_overrides(vocab: AWIActionVocabulary) -> None:
    vocab.register_custom_action(
        AWIActionDefinition(
            action=AWIStandardAction.SCROLL,
            category=AWIActionCategory.NAVIGATION,
            description="Custom scroll",
            parameters={},
            required_preconditions=[],
            postconditions=["scrolled"],
            estimated_cost=0.123,
        )
    )
    updated = vocab.get_action(AWIStandardAction.SCROLL)
    assert updated is not None
    assert updated.description == "Custom scroll"
    assert vocab.get_estimated_cost(AWIStandardAction.SCROLL) == 0.123
