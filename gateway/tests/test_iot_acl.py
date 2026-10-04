"""Directional permissions and deny precedence for the frozen IoT bridge."""

import pytest

from app.schemas.iot import ACLPermission
from app.services.iot_bridge import TopicACLEngine


@pytest.mark.proof
@pytest.mark.parametrize("granted", list(ACLPermission))
@pytest.mark.parametrize("required", [ACLPermission.READ, ACLPermission.WRITE])
def test_acl_requires_directional_permission(granted, required):
    assert TopicACLEngine.check(
        {"device/control": granted}, "device/control", required
    ) is (granted in (required, ACLPermission.READ_WRITE))


@pytest.mark.proof
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("allow_pattern", ["device/room/camera", "device/+/camera"])
def test_matching_deny_overrides_allow_order_and_specificity(reverse, allow_pattern):
    rules = [
        (allow_pattern, ACLPermission.READ_WRITE),
        ("device/room/#", ACLPermission.DENY),
    ]
    if reverse:
        rules.reverse()
    for required in (ACLPermission.READ, ACLPermission.WRITE):
        assert not TopicACLEngine.check(dict(rules), "device/room/camera", required)


@pytest.mark.proof
def test_nonmatching_deny_does_not_block_allowed_topic():
    assert TopicACLEngine.check(
        {
            "device/+/telemetry": ACLPermission.READ,
            "device/+/camera": ACLPermission.DENY,
        },
        "device/room/telemetry",
        ACLPermission.READ,
    )
