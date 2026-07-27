from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import ValidationError

from obd_mcp.domain import (
    DiagnosticIssue,
    DiagnosticTroubleCode,
    SignalReading,
    Vehicle,
    VehicleStatus,
    VinIdentity,
    ensure_no_raw_vin,
    ensure_safe_public_value,
)


def synthetic_vin() -> str:
    return "A" * 17


@pytest.mark.parametrize(
    "factory",
    [
        lambda vin: Vehicle(vehicle_id="demo", display_name=f"Vehicle {vin}"),
        lambda vin: SignalReading(
            vehicle_id="demo",
            signal_id="serial",
            name="Serial",
            value=f"observed {vin}",
        ),
        lambda vin: DiagnosticIssue(
            issue_id="issue-one",
            vehicle_id="demo",
            title="Observation",
            description=f"copied identifier {vin}",
        ),
    ],
)
def test_public_domain_text_rejects_vin_tokens(factory: Callable[[str], object]) -> None:
    with pytest.raises(ValidationError, match="raw VIN"):
        factory(synthetic_vin())


def test_vin_marker_without_separator_cannot_bypass_text_guard() -> None:
    with pytest.raises(ValidationError, match="raw VIN"):
        Vehicle(
            vehicle_id="demo",
            display_name=f"VIN{synthetic_vin()}",
        )


def test_vin_marker_with_internal_separators_cannot_bypass_text_guard() -> None:
    separated_vin = "-".join(("AAAA", "AAAA", "AAAA", "AAAA", "A"))

    with pytest.raises(ValidationError, match="raw VIN"):
        Vehicle(
            vehicle_id="demo",
            display_name=f"VIN: {separated_vin}",
        )


@pytest.mark.parametrize("separator", ["-", "_", " "])
def test_unlabeled_delimited_vin_cannot_bypass_public_guards(separator: str) -> None:
    vin = separator.join(("1AA", "AAAAA", "AAA", "AA4352"))

    with pytest.raises(ValidationError, match="raw VIN"):
        Vehicle(vehicle_id="demo", display_name=vin)

    with pytest.raises(ValueError, match="raw VIN"):
        ensure_safe_public_value({"observation": vin})


def test_outbound_guard_catches_post_validation_mutation() -> None:
    vehicle = Vehicle(vehicle_id="demo", display_name="Demo", metadata={})
    vehicle.metadata["note"] = f"late mutation {synthetic_vin()}"

    with pytest.raises(ValueError, match="raw VIN"):
        ensure_no_raw_vin(vehicle)


@pytest.mark.parametrize(
    "value",
    [
        lambda vin: vin.encode(),
        lambda vin: {vin},
    ],
)
def test_metadata_rejects_non_json_containers_that_can_serialize_a_vin(
    value: Callable[[str], object],
) -> None:
    with pytest.raises(ValidationError, match="metadata"):
        Vehicle(
            vehicle_id="demo",
            display_name="Demo",
            metadata={"opaque": value(synthetic_vin())},
        )


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (lambda vin: vin.encode(), "binary data"),
        (lambda vin: {vin}, "JSON-compatible"),
    ],
)
def test_outbound_guard_rejects_mutated_binary_and_set_values(
    value: Callable[[str], object],
    message: str,
) -> None:
    vehicle = Vehicle(vehicle_id="demo", display_name="Demo", metadata={})
    vehicle.metadata["opaque"] = value(synthetic_vin())

    with pytest.raises(ValueError, match=message):
        ensure_no_raw_vin(vehicle)


def test_outbound_guard_rejects_serializer_backed_arbitrary_objects() -> None:
    vehicle = Vehicle(vehicle_id="demo", display_name="Demo", metadata={})
    vehicle.metadata["opaque"] = Path(synthetic_vin())

    with pytest.raises(ValueError, match="JSON-compatible"):
        ensure_no_raw_vin(vehicle)


def test_vin_identity_is_the_only_supported_public_vin_representation() -> None:
    identity = VinIdentity.from_vin(synthetic_vin())

    assert synthetic_vin() not in identity.model_dump_json()
    assert identity.redacted.endswith("AAAA")


def test_invalid_obd_dtc_first_digit_is_rejected() -> None:
    with pytest.raises(ValidationError, match="normalized"):
        DiagnosticTroubleCode(vehicle_id="demo", code="PA000")


def test_vehicle_metadata_and_status_notes_are_bounded() -> None:
    with pytest.raises(ValidationError, match="string value is too long"):
        Vehicle(
            vehicle_id="demo",
            display_name="Demo",
            metadata={"oversized": "x" * 4_097},
        )

    vehicle = Vehicle(vehicle_id="demo", display_name="Demo")
    with pytest.raises(ValidationError, match="at most 64"):
        VehicleStatus(vehicle=vehicle, notes=tuple("note" for _ in range(65)))


def test_vehicle_metadata_depth_is_bounded() -> None:
    nested: dict[str, object] = {}
    cursor = nested
    for index in range(10):
        child: dict[str, object] = {}
        cursor[f"level-{index}"] = child
        cursor = child

    with pytest.raises(ValidationError, match="maximum metadata depth"):
        Vehicle(vehicle_id="demo", display_name="Demo", metadata=nested)
