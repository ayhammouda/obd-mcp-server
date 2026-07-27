"""Declarative, data-only diagnostic profile loading.

Profiles can describe only the two read-only UDS services used by this
project: ReadDTCInformation (0x19) and ReadDataByIdentifier (0x22). They
cannot contain scripts, raw request bytes, transport commands, or write
services.
"""

from __future__ import annotations

import json
import math
import re
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    AliasChoices,
    Field,
    StrictBool,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from .domain import (
    DataSource,
    DomainModel,
    SafeId,
    ShortText,
    TransportProtocol,
)
from .errors import ProfileNotFoundError, ProfileValidationError
from .policy import ReadOnlyPolicy
from .vin import contains_raw_vin

_MAX_PROFILE_BYTES = 1_048_576
_LICENSE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+(): /-]{0,127}$")
_DISALLOWED_BUNDLED_LICENSES = {"none", "unknown", "unlicensed", "proprietary"}
_SENSITIVE_IDENTITY_DIDS = frozenset({0xF190})


ProfileSource = DataSource


def _reject_profile_vins(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if contains_raw_vin(str(key)):
                raise ValueError("profile must not contain VIN-shaped data")
            _reject_profile_vins(child)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            _reject_profile_vins(child)
        return
    if isinstance(value, str) and contains_raw_vin(value):
        raise ValueError("profile must not contain VIN-shaped data")


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, child in pairs:
        if key in value:
            raise ProfileValidationError("profile JSON contains duplicate object keys")
        value[key] = child
    return value


class DecoderDataType(StrEnum):
    UINT8 = "uint8"
    INT8 = "int8"
    UINT16 = "uint16"
    INT16 = "int16"
    UINT32 = "uint32"
    INT32 = "int32"
    ASCII = "ascii"
    BYTES = "bytes"


class ByteOrder(StrEnum):
    BIG = "big"
    LITTLE = "little"


class ProfileProvenance(DomainModel):
    source: ProfileSource
    origin: ShortText
    license: str = Field(min_length=1, max_length=128)
    redistribution_allowed: StrictBool
    confidence: float = Field(ge=0.0, le=1.0)
    notes: str = Field(default="", max_length=2_048)

    @field_validator("license")
    @classmethod
    def license_must_be_plain_data(cls, value: str) -> str:
        normalized = value.strip()
        if not _LICENSE_RE.fullmatch(normalized):
            raise ValueError("license must be a short SPDX identifier or data-license expression")
        return normalized


class VehicleSelector(DomainModel):
    """Optional, OEM-neutral matching hints for a mounted profile."""

    protocol: Literal[TransportProtocol.UDS] = TransportProtocol.UDS
    manufacturer: str | None = Field(default=None, min_length=1, max_length=128)
    model: str | None = Field(default=None, min_length=1, max_length=128)
    model_year_min: int | None = Field(default=None, ge=1980, le=2200)
    model_year_max: int | None = Field(default=None, ge=1980, le=2200)
    ecu_ids: tuple[SafeId, ...] = ()

    @model_validator(mode="after")
    def years_must_be_ordered(self) -> VehicleSelector:
        if (
            self.model_year_min is not None
            and self.model_year_max is not None
            and self.model_year_min > self.model_year_max
        ):
            raise ValueError("model_year_min must not exceed model_year_max")
        return self


class ProfileDecoder(DomainModel):
    data_type: DecoderDataType
    byte_offset: int = Field(default=0, ge=0, le=4_095)
    byte_length: int | None = Field(default=None, ge=1, le=4_096)
    byte_order: ByteOrder = ByteOrder.BIG
    scale: float = Field(default=1.0, allow_inf_nan=False)
    value_offset: float = Field(default=0.0, allow_inf_nan=False)
    unit: str = Field(default="", max_length=64)

    @model_validator(mode="after")
    def length_matches_scalar_type(self) -> ProfileDecoder:
        expected_lengths = {
            DecoderDataType.UINT8: 1,
            DecoderDataType.INT8: 1,
            DecoderDataType.UINT16: 2,
            DecoderDataType.INT16: 2,
            DecoderDataType.UINT32: 4,
            DecoderDataType.INT32: 4,
        }
        expected = expected_lengths.get(self.data_type)
        if expected is not None and self.byte_length not in (None, expected):
            raise ValueError(f"{self.data_type.value} requires byte_length {expected}")
        numeric_bounds = {
            DecoderDataType.UINT8: (0, 2**8 - 1),
            DecoderDataType.INT8: (-(2**7), 2**7 - 1),
            DecoderDataType.UINT16: (0, 2**16 - 1),
            DecoderDataType.INT16: (-(2**15), 2**15 - 1),
            DecoderDataType.UINT32: (0, 2**32 - 1),
            DecoderDataType.INT32: (-(2**31), 2**31 - 1),
        }
        bounds = numeric_bounds.get(self.data_type)
        if bounds is not None:
            transformed = tuple(raw * self.scale + self.value_offset for raw in bounds)
            if not all(math.isfinite(value) for value in transformed):
                raise ValueError("decoder transformed numeric range must remain finite")
        if (
            self.data_type in (DecoderDataType.ASCII, DecoderDataType.BYTES)
            and self.byte_length is None
        ):
            raise ValueError(f"{self.data_type.value} requires an explicit byte_length")
        return self


def _parse_int(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        try:
            return int(stripped, 0)
        except ValueError:
            if re.fullmatch(r"[0-9A-Fa-f]+", stripped):
                return int(stripped, 16)
    return value


class ProfileReadDefinition(DomainModel):
    name: ShortText
    ecu_id: SafeId
    service_id: int = Field(
        ge=0,
        le=0xFF,
        validation_alias=AliasChoices("service_id", "service"),
        serialization_alias="service",
    )
    identifier: int = Field(
        ge=0,
        le=0xFFFF,
        validation_alias=AliasChoices("identifier", "did", "subfunction"),
    )
    signal_id: SafeId | None = None
    decoder: ProfileDecoder | None = None
    description: str = Field(default="", max_length=512)

    @field_validator("service_id", "identifier", mode="before")
    @classmethod
    def parse_hex_integer(cls, value: Any) -> Any:
        return _parse_int(value)

    @model_validator(mode="after")
    def enforce_read_only_uds_shape(self) -> ProfileReadDefinition:
        try:
            ReadOnlyPolicy().authorize_uds_service(self.service_id)
        except Exception as exc:
            raise PydanticCustomError(
                "unsafe_uds_service",
                "profile service must be read-only UDS 0x19 or 0x22",
            ) from exc
        if self.service_id == 0x19:
            if self.identifier > 0xFF:
                raise ValueError("UDS 0x19 subfunction must fit in one byte")
            if self.decoder is not None or self.signal_id is not None:
                raise ValueError("UDS 0x19 returns DTCs and cannot define a signal decoder")
        if self.service_id == 0x22 and (self.signal_id is None or self.decoder is None):
            raise ValueError("UDS 0x22 requires signal_id and decoder")
        if self.service_id == 0x22 and self.identifier in _SENSITIVE_IDENTITY_DIDS:
            raise ValueError("profile identifier exposes prohibited vehicle identity data")
        return self


class DiagnosticProfile(DomainModel):
    schema_version: Literal[1] = 1
    profile_id: SafeId
    name: ShortText
    version: str = Field(min_length=1, max_length=64)
    provenance: ProfileProvenance
    selector: VehicleSelector = Field(default_factory=VehicleSelector)
    reads: tuple[ProfileReadDefinition, ...] = Field(min_length=1, max_length=512)

    @model_validator(mode="before")
    @classmethod
    def document_must_not_contain_vins(cls, value: Any) -> Any:
        _reject_profile_vins(value)
        return value

    @model_validator(mode="after")
    def reads_must_be_unambiguous(self) -> DiagnosticProfile:
        names = [read.name.casefold() for read in self.reads]
        if len(names) != len(set(names)):
            raise ValueError("profile read names must be unique")
        operations = [(read.ecu_id, read.service_id, read.identifier) for read in self.reads]
        if len(operations) != len(set(operations)):
            raise ValueError("profile ECU/service/identifier reads must be unique")
        signals = [
            (read.ecu_id, read.signal_id) for read in self.reads if read.signal_id is not None
        ]
        if len(signals) != len(set(signals)):
            raise ValueError("profile signal ids must be unique within each ECU")
        return self


def _validate_bundled_profile(profile: DiagnosticProfile) -> None:
    provenance = profile.provenance
    if not provenance.redistribution_allowed:
        raise ProfileValidationError(
            f"bundled profile {profile.profile_id!r} disallows redistribution"
        )
    license_id = provenance.license.strip()
    if not license_id or license_id.casefold() in _DISALLOWED_BUNDLED_LICENSES:
        raise ProfileValidationError(
            f"bundled profile {profile.profile_id!r} needs an SPDX or data license"
        )
    if provenance.source is ProfileSource.LICENSED_OEM:
        raise ProfileValidationError("licensed OEM profiles may not be bundled")


class ProfileLoader:
    """Load strict profiles from JSON or TOML files."""

    def __init__(self, *, max_profile_bytes: int = _MAX_PROFILE_BYTES) -> None:
        if max_profile_bytes < 1:
            raise ValueError("max_profile_bytes must be positive")
        self._max_profile_bytes = max_profile_bytes

    def load_path(self, path: str | Path, *, bundled: bool = False) -> DiagnosticProfile:
        source_path = Path(path).expanduser()
        try:
            stat = source_path.stat()
        except OSError as exc:
            raise ProfileValidationError(f"cannot read profile {source_path}: {exc}") from exc
        if not source_path.is_file():
            raise ProfileValidationError(f"profile path is not a regular file: {source_path}")
        if stat.st_size > self._max_profile_bytes:
            raise ProfileValidationError(
                f"profile {source_path} exceeds {self._max_profile_bytes} bytes"
            )

        try:
            data = self._decode(source_path)
        except ProfileValidationError:
            raise
        except Exception as exc:
            raise ProfileValidationError(f"invalid profile document: {source_path}") from exc

        if set(data) == {"profile"} and isinstance(data["profile"], dict):
            data = data["profile"]
        try:
            profile = DiagnosticProfile.model_validate(data)
        except ValidationError as exc:
            raise ProfileValidationError(
                f"invalid profile {source_path}: {_validation_summary(exc)}"
            ) from exc

        if bundled:
            _validate_bundled_profile(profile)
        return profile

    def load_directory(
        self,
        directory: str | Path,
        *,
        bundled: bool = False,
    ) -> tuple[DiagnosticProfile, ...]:
        root = Path(directory).expanduser()
        if not root.exists():
            raise ProfileValidationError(f"profile directory does not exist: {root}")
        if not root.is_dir():
            raise ProfileValidationError(f"profile directory is not a directory: {root}")
        candidates = sorted(
            path
            for path in root.iterdir()
            if path.is_file() and path.suffix.casefold() in {".json", ".toml"}
        )
        return tuple(self.load_path(path, bundled=bundled) for path in candidates)

    @staticmethod
    def _decode(path: Path) -> dict[str, Any]:
        suffix = path.suffix.casefold()
        if suffix == ".json":
            value = json.loads(
                path.read_text(encoding="utf-8"),
                object_pairs_hook=_reject_duplicate_json_keys,
            )
        elif suffix == ".toml":
            value = tomllib.loads(path.read_text(encoding="utf-8"))
        else:
            raise ProfileValidationError(f"unsupported profile format: {path.suffix}")
        if not isinstance(value, dict):
            raise ProfileValidationError("profile document root must be an object")
        return value


class ProfileRegistry:
    """In-memory registry of already validated declarative profiles."""

    def __init__(self, profiles: Iterable[DiagnosticProfile] = ()) -> None:
        self._profiles: dict[str, DiagnosticProfile] = {}
        for profile in profiles:
            self.add(profile)

    @classmethod
    def from_directories(
        cls,
        *,
        bundled: Iterable[str | Path] = (),
        mounted: Iterable[str | Path] = (),
        loader: ProfileLoader | None = None,
    ) -> ProfileRegistry:
        profile_loader = loader or ProfileLoader()
        profiles: list[DiagnosticProfile] = []
        for directory in bundled:
            profiles.extend(profile_loader.load_directory(directory, bundled=True))
        for directory in mounted:
            # Private mounted data is runtime-usable even when redistribution is false.
            profiles.extend(profile_loader.load_directory(directory, bundled=False))
        return cls(profiles)

    def add(self, profile: DiagnosticProfile) -> None:
        if profile.profile_id in self._profiles:
            raise ProfileValidationError(f"duplicate profile id: {profile.profile_id}")
        self._profiles[profile.profile_id] = profile

    def get(self, profile_id: str) -> DiagnosticProfile:
        try:
            return self._profiles[profile_id]
        except KeyError as exc:
            raise ProfileNotFoundError(f"profile not found: {profile_id}") from exc

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._profiles))

    def all(self) -> tuple[DiagnosticProfile, ...]:
        return tuple(self._profiles[key] for key in sorted(self._profiles))


def _validation_summary(error: ValidationError) -> str:
    """Format profile failures without echoing licensed or private values."""

    failures: list[str] = []
    for item in error.errors(include_input=False, include_url=False):
        location = ".".join(str(part) for part in item["loc"]) or "profile"
        failures.append(f"{location}: {item['msg']}")
    return "; ".join(failures)
