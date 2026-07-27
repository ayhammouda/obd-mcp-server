from __future__ import annotations

import pytest
from pydantic import ValidationError

from obd_mcp.domain import Vehicle, VinIdentity
from obd_mcp.errors import (
    MutableOperationDeniedError,
    PolicyDeniedError,
    RawCommandDeniedError,
)
from obd_mcp.policy import STANDARD_PIDS, ReadOnlyPolicy, normalize_pid


def test_standard_pid_surface_is_fixed_and_normalized() -> None:
    policy = ReadOnlyPolicy()

    assert set(STANDARD_PIDS) == {
        "0104",
        "0105",
        "010B",
        "010C",
        "010D",
        "010F",
        "0111",
        "012F",
        "0142",
    }
    assert policy.authorize_standard_pids(("0c", "010D", 0x0C)) == ("010C", "010D")
    assert normalize_pid("0x0f") == "010F"


@pytest.mark.parametrize("pid", ["0123", "0902", "04FF", "not-hex", 0x100])
def test_unlisted_or_malformed_pids_are_denied(pid: str | int) -> None:
    with pytest.raises(PolicyDeniedError):
        ReadOnlyPolicy().authorize_standard_pids((pid,))


@pytest.mark.parametrize("service_id", [0x04, 0x08])
def test_mutable_obd_services_are_explicitly_denied(service_id: int) -> None:
    with pytest.raises(MutableOperationDeniedError):
        ReadOnlyPolicy().authorize_obd_service(service_id)


@pytest.mark.parametrize(
    "service_id",
    [0x10, 0x11, 0x14, 0x27, 0x2E, 0x2F, 0x31, 0x34, 0x36, 0x3D, 0x85],
)
def test_mutable_uds_services_are_explicitly_denied(service_id: int) -> None:
    with pytest.raises(MutableOperationDeniedError):
        ReadOnlyPolicy().authorize_uds_service(service_id)


@pytest.mark.parametrize("service_id", [0x10, 0x14, 0x27, 0x2E, 0x31, 0x3B])
def test_mutable_kwp_services_are_explicitly_denied(service_id: int) -> None:
    with pytest.raises(MutableOperationDeniedError):
        ReadOnlyPolicy().authorize_kwp_service(service_id)


def test_default_deny_and_raw_command_rejection() -> None:
    policy = ReadOnlyPolicy()

    policy.authorize_obd_service(0x01, pid="010C")
    policy.authorize_obd_service(0x03)
    policy.authorize_uds_service(0x19)
    policy.authorize_uds_service(0x22)

    with pytest.raises(PolicyDeniedError):
        policy.authorize_obd_service(0x09)
    with pytest.raises(PolicyDeniedError):
        policy.authorize_uds_service(0x23)
    with pytest.raises(PolicyDeniedError):
        policy.authorize_kwp_service(0x21)
    with pytest.raises(RawCommandDeniedError):
        policy.reject_raw_command("01 0C")


def test_vin_identity_is_redacted_and_metadata_cannot_bypass_it() -> None:
    raw_vin = "A" * 17
    identity = VinIdentity.from_vin(raw_vin)
    payload = identity.model_dump_json()

    assert raw_vin not in payload
    assert identity.redacted == "*************AAAA"
    assert identity.fingerprint.startswith("sha256:")

    with pytest.raises(ValidationError):
        Vehicle(
            vehicle_id="vehicle",
            display_name="Vehicle",
            metadata={"nested": {"full-vin": raw_vin}},
        )
