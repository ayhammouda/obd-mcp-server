from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from obd_mcp.domain import DataSource
from obd_mcp.errors import ProfileNotFoundError, ProfileValidationError
from obd_mcp.profiles import (
    DiagnosticProfile,
    ProfileLoader,
    ProfileRegistry,
    ProfileSource,
)


def profile_payload(
    *,
    source: str = "synthetic",
    redistribution_allowed: bool = True,
    license_id: str = "CC0-1.0",
    service: str = "0x22",
) -> dict[str, object]:
    read: dict[str, object] = {
        "name": "Synthetic counter",
        "ecu_id": "engine",
        "service": service,
        "identifier": "0xF40D" if service == "0x22" else "0x02",
        "description": "Data-only test fixture.",
    }
    if service == "0x22":
        read.update(
            {
                "signal_id": "synthetic_counter",
                "decoder": {
                    "data_type": "uint16",
                    "byte_offset": 0,
                    "byte_length": 2,
                    "scale": 1.0,
                    "value_offset": 0.0,
                    "unit": "count",
                },
            }
        )
    return {
        "schema_version": 1,
        "profile_id": f"test.{source}",
        "name": "Test profile",
        "version": "1",
        "provenance": {
            "source": source,
            "origin": "Created for unit tests",
            "license": license_id,
            "redistribution_allowed": redistribution_allowed,
            "confidence": 0.75,
        },
        "selector": {"protocol": "uds", "ecu_ids": ["engine"]},
        "reads": [read],
    }


def grouped_vin(separator: str) -> str:
    return separator.join(("1AA", "AAAAA", "AAA", "AA4352"))


def test_profile_supports_only_read_only_uds_shapes() -> None:
    profile = DiagnosticProfile.model_validate(profile_payload())

    assert profile.schema_version == 1
    assert profile.provenance.source is ProfileSource.SYNTHETIC
    assert profile.reads[0].service_id == 0x22
    assert profile.reads[0].identifier == 0xF40D

    dtc_profile = DiagnosticProfile.model_validate(profile_payload(service="0x19"))
    assert dtc_profile.reads[0].identifier == 0x02
    assert dtc_profile.reads[0].decoder is None


@pytest.mark.parametrize("service", ["0x10", "0x14", "0x2E", "0x31", "0x23"])
def test_profile_rejects_mutable_and_unlisted_services(service: str) -> None:
    with pytest.raises(ValidationError, match="read-only UDS"):
        DiagnosticProfile.model_validate(profile_payload(service=service))


def test_profile_schema_version_is_a_migration_discriminator() -> None:
    payload = profile_payload()
    payload["schema_version"] = 2

    with pytest.raises(ValidationError, match="schema_version"):
        DiagnosticProfile.model_validate(payload)


@pytest.mark.parametrize("value", [1, "true"])
def test_profile_redistribution_permission_requires_a_literal_boolean(
    value: object,
) -> None:
    payload = profile_payload()
    provenance = payload["provenance"]
    assert isinstance(provenance, dict)
    provenance["redistribution_allowed"] = value

    with pytest.raises(ValidationError, match="redistribution_allowed"):
        DiagnosticProfile.model_validate(payload)


@pytest.mark.parametrize("protocol", ["obd2", "kwp2000", "simulated", "unknown"])
def test_profile_schema_v1_requires_uds_selector_protocol(protocol: str) -> None:
    payload = profile_payload()
    payload["selector"] = {"protocol": protocol, "ecu_ids": ["engine"]}

    with pytest.raises(ValidationError, match="protocol"):
        DiagnosticProfile.model_validate(payload)


def test_profile_rejects_executable_or_raw_fields() -> None:
    payload = profile_payload()
    read = payload["reads"][0]  # type: ignore[index]
    read["raw_request"] = "22F40D"  # type: ignore[index]

    with pytest.raises(ValidationError, match="raw_request"):
        DiagnosticProfile.model_validate(payload)


def test_profile_rejects_inconsistent_selectors_decoders_and_reads() -> None:
    payload = profile_payload()
    payload["selector"] = {"model_year_min": 2025, "model_year_max": 2020}
    with pytest.raises(ValidationError, match="model_year_min"):
        DiagnosticProfile.model_validate(payload)

    payload = profile_payload()
    decoder = payload["reads"][0]["decoder"]  # type: ignore[index]
    decoder["byte_length"] = 4  # type: ignore[index]
    with pytest.raises(ValidationError, match="requires byte_length 2"):
        DiagnosticProfile.model_validate(payload)

    payload = profile_payload()
    read = payload["reads"][0]  # type: ignore[index]
    read["decoder"] = {"data_type": "ascii"}  # type: ignore[index]
    with pytest.raises(ValidationError, match="explicit byte_length"):
        DiagnosticProfile.model_validate(payload)

    payload = profile_payload(service="0x19")
    payload["reads"][0]["identifier"] = "0x100"  # type: ignore[index]
    with pytest.raises(ValidationError, match="one byte"):
        DiagnosticProfile.model_validate(payload)

    payload = profile_payload()
    payload["reads"][0].pop("decoder")  # type: ignore[index]
    with pytest.raises(ValidationError, match="requires signal_id and decoder"):
        DiagnosticProfile.model_validate(payload)

    payload = profile_payload()
    payload["reads"].append(deepcopy(payload["reads"][0]))  # type: ignore[union-attr,index]
    with pytest.raises(ValidationError, match="unique"):
        DiagnosticProfile.model_validate(payload)

    payload = profile_payload()
    duplicate = deepcopy(payload["reads"][0])  # type: ignore[index]
    duplicate["name"] = "Second name"  # type: ignore[index]
    payload["reads"].append(duplicate)  # type: ignore[union-attr]
    with pytest.raises(ValidationError, match="ECU/service/identifier"):
        DiagnosticProfile.model_validate(payload)

    payload = profile_payload()
    duplicate = deepcopy(payload["reads"][0])  # type: ignore[index]
    duplicate["name"] = "Second name"  # type: ignore[index]
    duplicate["identifier"] = "0xF40E"  # type: ignore[index]
    payload["reads"].append(duplicate)  # type: ignore[union-attr]
    with pytest.raises(ValidationError, match="signal ids"):
        DiagnosticProfile.model_validate(payload)

    payload = profile_payload()
    payload["reads"][0]["decoder"]["scale"] = float("inf")  # type: ignore[index]
    with pytest.raises(ValidationError, match="finite"):
        DiagnosticProfile.model_validate(payload)

    payload = profile_payload()
    payload["reads"][0]["decoder"].update(  # type: ignore[index]
        {"data_type": "uint32", "byte_length": 4, "scale": 1e300}
    )
    with pytest.raises(ValidationError, match="range must remain finite"):
        DiagnosticProfile.model_validate(payload)


@pytest.mark.parametrize("data_type", ["ascii", "bytes"])
def test_profile_rejects_vehicle_identity_did_for_all_decoders(
    data_type: str,
) -> None:
    payload = profile_payload()
    read = payload["reads"][0]  # type: ignore[index]
    read["identifier"] = "0xF190"  # type: ignore[index]
    read["decoder"] = {  # type: ignore[index]
        "data_type": data_type,
        "byte_length": 17,
    }

    with pytest.raises(ValidationError, match="vehicle identity"):
        DiagnosticProfile.model_validate(payload)


@pytest.mark.parametrize("source", [member.value for member in DataSource])
def test_all_declared_provenance_sources_are_supported(source: str) -> None:
    profile = DiagnosticProfile.model_validate(profile_payload(source=source))
    assert profile.provenance.source.value == source


def test_bundled_distribution_rules_do_not_block_private_mounts(tmp_path: Path) -> None:
    path = tmp_path / "private.json"
    path.write_text(
        json.dumps(
            profile_payload(
                source="licensed-oem",
                redistribution_allowed=False,
                license_id="LicenseRef-Private-Data",
            )
        ),
        encoding="utf-8",
    )
    loader = ProfileLoader()

    private = loader.load_path(path, bundled=False)
    assert private.provenance.redistribution_allowed is False

    with pytest.raises(ProfileValidationError, match="redistribution"):
        loader.load_path(path, bundled=True)


def test_bundled_profile_requires_meaningful_data_license(tmp_path: Path) -> None:
    path = tmp_path / "bad-license.json"
    path.write_text(
        json.dumps(profile_payload(license_id="Proprietary")),
        encoding="utf-8",
    )

    with pytest.raises(ProfileValidationError, match="license"):
        ProfileLoader().load_path(path, bundled=True)


def test_bundled_profile_rejects_licensed_oem_even_if_self_declared_redistributable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "licensed-oem.json"
    path.write_text(
        json.dumps(
            profile_payload(
                source="licensed-oem",
                redistribution_allowed=True,
                license_id="LicenseRef-OEM",
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(ProfileValidationError, match="licensed OEM"):
        ProfileLoader().load_path(path, bundled=True)


def test_profile_validation_errors_do_not_echo_private_values(tmp_path: Path) -> None:
    path = tmp_path / "private.json"
    payload = profile_payload()
    secret_value = "private-oem-identifier-material"
    payload["reads"][0]["raw_request"] = secret_value  # type: ignore[index]
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ProfileValidationError) as exc_info:
        ProfileLoader().load_path(path)

    assert "raw_request" in str(exc_info.value)
    assert secret_value not in str(exc_info.value)


@pytest.mark.parametrize(
    ("original", "duplicate"),
    [
        ('"license": "CC0-1.0"', '"license": "CC0-1.0", "license": "MIT"'),
        ('"service": "0x22"', '"service": "0x22", "service": "0x19"'),
    ],
)
def test_json_loader_rejects_duplicate_keys_at_any_nesting_level(
    tmp_path: Path,
    original: str,
    duplicate: str,
) -> None:
    path = tmp_path / "duplicate.json"
    document = json.dumps(profile_payload()).replace(original, duplicate, 1)
    path.write_text(document, encoding="utf-8")

    with pytest.raises(ProfileValidationError, match="duplicate object keys"):
        ProfileLoader().load_path(path)


@pytest.mark.parametrize(
    "field",
    ["version", "provenance.notes", "selector.manufacturer", "reads.description"],
)
def test_json_loader_rejects_vins_in_all_profile_text_fields(
    tmp_path: Path,
    field: str,
) -> None:
    payload = profile_payload()
    vin = grouped_vin("-")
    if field == "version":
        payload["version"] = vin
    elif field == "provenance.notes":
        provenance = payload["provenance"]
        assert isinstance(provenance, dict)
        provenance["notes"] = vin
    elif field == "selector.manufacturer":
        selector = payload["selector"]
        assert isinstance(selector, dict)
        selector["manufacturer"] = vin
    else:
        reads = payload["reads"]
        assert isinstance(reads, list)
        read = reads[0]
        assert isinstance(read, dict)
        read["description"] = vin

    path = tmp_path / "profile.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ProfileValidationError, match="VIN-shaped"):
        ProfileLoader().load_path(path)


def test_toml_loader_rejects_whitespace_grouped_vin(tmp_path: Path) -> None:
    vin = grouped_vin(" ")
    path = tmp_path / "profile.toml"
    path.write_text(
        f"""
schema_version = 1
profile_id = "test.toml"
name = "TOML profile"
version = "1"

[provenance]
source = "community"
origin = "Unit test"
license = "CC-BY-4.0"
redistribution_allowed = true
confidence = 0.5
notes = "{vin}"

[[reads]]
name = "Counter"
ecu_id = "engine"
service = "0x22"
identifier = "0xF40D"
signal_id = "counter"

[reads.decoder]
data_type = "uint16"
byte_length = 2
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ProfileValidationError, match="VIN-shaped"):
        ProfileLoader().load_path(path)


def test_toml_loader_and_registry(tmp_path: Path) -> None:
    path = tmp_path / "profile.toml"
    path.write_text(
        """
schema_version = 1
profile_id = "test.toml"
name = "TOML profile"
version = "1"

[provenance]
source = "community"
origin = "Unit test"
license = "CC-BY-4.0"
redistribution_allowed = true
confidence = 0.5

[selector]
protocol = "uds"
ecu_ids = ["engine"]

[[reads]]
name = "Counter"
ecu_id = "engine"
service = "0x22"
identifier = "0xF40D"
signal_id = "counter"

[reads.decoder]
data_type = "uint16"
byte_length = 2
unit = "count"
""".strip(),
        encoding="utf-8",
    )
    profile = ProfileLoader().load_path(path, bundled=True)
    registry = ProfileRegistry((profile,))

    assert registry.ids() == ("test.toml",)
    assert registry.get("test.toml") is profile
    with pytest.raises(ProfileNotFoundError):
        registry.get("missing")
    with pytest.raises(ProfileValidationError, match="duplicate"):
        registry.add(profile)


def test_loader_rejects_undeclared_formats(tmp_path: Path) -> None:
    path = tmp_path / "profile.yaml"
    path.write_text("schema_version: 1", encoding="utf-8")

    with pytest.raises(ProfileValidationError, match="unsupported"):
        ProfileLoader().load_path(path)


def test_loader_handles_wrapped_documents_and_filesystem_failures(tmp_path: Path) -> None:
    loader = ProfileLoader(max_profile_bytes=10_000)
    wrapped = tmp_path / "wrapped.json"
    wrapped.write_text(json.dumps({"profile": profile_payload()}), encoding="utf-8")

    assert loader.load_path(wrapped).profile_id == "test.synthetic"

    with pytest.raises(ProfileValidationError, match="cannot read"):
        loader.load_path(tmp_path / "missing.json")
    with pytest.raises(ProfileValidationError, match="regular file"):
        loader.load_path(tmp_path)

    invalid = tmp_path / "invalid.json"
    invalid.write_text("{", encoding="utf-8")
    with pytest.raises(ProfileValidationError, match="invalid profile"):
        loader.load_path(invalid)

    too_large = tmp_path / "large.json"
    too_large.write_text("x" * 20, encoding="utf-8")
    with pytest.raises(ProfileValidationError, match="exceeds"):
        ProfileLoader(max_profile_bytes=10).load_path(too_large)


def test_registry_can_load_trusted_bundled_and_private_directories(tmp_path: Path) -> None:
    bundled = tmp_path / "bundled"
    mounted = tmp_path / "mounted"
    bundled.mkdir()
    mounted.mkdir()
    bundled_payload = profile_payload()
    private_payload = profile_payload(
        source="licensed-oem",
        redistribution_allowed=False,
        license_id="LicenseRef-Private-Data",
    )
    (bundled / "synthetic.json").write_text(json.dumps(bundled_payload), encoding="utf-8")
    (mounted / "private.json").write_text(json.dumps(private_payload), encoding="utf-8")

    registry = ProfileRegistry.from_directories(bundled=(bundled,), mounted=(mounted,))

    assert registry.ids() == ("test.licensed-oem", "test.synthetic")
    assert len(registry.all()) == 2
    with pytest.raises(ProfileValidationError, match="does not exist"):
        ProfileLoader().load_directory(tmp_path / "missing")
    with pytest.raises(ProfileValidationError, match="not a directory"):
        ProfileLoader().load_directory(bundled / "synthetic.json")
