"""Normalized, OEM-neutral domain models for diagnostic observations.

The public models intentionally have no field capable of holding a full VIN.
Drivers may briefly observe one, but must convert it to :class:`VinIdentity`
before returning data across the application boundary.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)
from pydantic_core import to_json

from .vin import VIN_RE as _VIN_RE
from .vin import contains_raw_vin as contains_raw_vin


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""

    return datetime.now(UTC)


_SENSITIVE_METADATA_KEYS = {
    "vin",
    "full_vin",
    "raw_vin",
    "vehicle_identification_number",
}
_OBD_DTC_RE = re.compile(r"^[PBCU][0-3][0-9A-F]{3}$")
_UDS_DTC_RE = re.compile(r"^[0-9A-F]{6}$")
MAX_PUBLIC_RESULT_BYTES = 1_048_576
MAX_STATUS_NOTES = 64
_MAX_METADATA_DEPTH = 8
_MAX_METADATA_ITEMS = 256
_MAX_METADATA_STRING_CHARS = 4_096
_MAX_METADATA_BYTES = 65_536


def reject_raw_vin_text(value: str) -> str:
    """Reject VIN-shaped tokens before text can cross a public boundary."""

    if contains_raw_vin(value):
        raise ValueError("text must not contain raw VIN data")
    return value


SafeId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
    AfterValidator(reject_raw_vin_text),
]
ShortText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=256),
    AfterValidator(reject_raw_vin_text),
]
LongText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=8_192),
    AfterValidator(reject_raw_vin_text),
]
PidCode = Annotated[str, StringConstraints(pattern=r"^01[0-9A-F]{2}$")]
SignalScalar = bool | int | float | str


def ensure_no_raw_vin(
    value: Any,
    *,
    path: str = "result",
    _seen: set[int] | None = None,
) -> None:
    """Recursively reject VIN tokens, including post-validation mutations."""

    if isinstance(value, str):
        if contains_raw_vin(value):
            raise ValueError(f"{path} must not contain raw VIN data")
        return
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise ValueError(f"{path} must not contain binary data")
    if value is None:
        return

    seen = _seen if _seen is not None else set()
    if isinstance(value, BaseModel):
        identity = id(value)
        if identity in seen:
            return
        seen.add(identity)
        for name, child in value.__dict__.items():
            ensure_no_raw_vin(child, path=f"{path}.{name}", _seen=seen)
        return
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in seen:
            return
        seen.add(identity)
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} keys must be strings")
            ensure_no_raw_vin(key, path=f"{path}.key", _seen=seen)
            ensure_no_raw_vin(child, path=f"{path}.{key}", _seen=seen)
        return
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in seen:
            return
        seen.add(identity)
        for index, child in enumerate(value):
            ensure_no_raw_vin(child, path=f"{path}[{index}]", _seen=seen)
        return
    if isinstance(value, datetime):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} must contain only finite numbers")
        return
    if isinstance(value, (bool, int)):
        return
    raise ValueError(f"{path} must contain only JSON-compatible values")


def ensure_safe_public_value(
    value: Any,
    *,
    max_bytes: int = MAX_PUBLIC_RESULT_BYTES,
) -> None:
    """Reject unsafe, unserializable, or oversized public result values."""

    ensure_no_raw_vin(value)
    try:
        payload = to_json(value)
    except Exception as exc:
        raise ValueError("result must be safely JSON serializable") from exc
    if len(payload) > max_bytes:
        raise ValueError(f"result exceeds the {max_bytes}-byte public response limit")


def _consume_metadata_budget(
    budget: dict[str, int],
    *,
    encoded_bytes: int,
) -> None:
    budget["items"] += 1
    budget["bytes"] += encoded_bytes
    if budget["items"] > _MAX_METADATA_ITEMS:
        raise ValueError(f"metadata must not exceed {_MAX_METADATA_ITEMS} values")
    if budget["bytes"] > _MAX_METADATA_BYTES:
        raise ValueError(f"metadata must not exceed {_MAX_METADATA_BYTES} encoded bytes")


def _reject_sensitive_metadata(
    value: Any,
    *,
    path: str = "metadata",
    _seen: set[int] | None = None,
    _depth: int = 0,
    _budget: dict[str, int] | None = None,
) -> Any:
    """Accept only finite JSON-like metadata and reject VIN-shaped values."""

    seen = _seen if _seen is not None else set()
    budget = _budget if _budget is not None else {"items": 0, "bytes": 0}
    if _depth > _MAX_METADATA_DEPTH:
        raise ValueError(f"{path} exceeds the maximum metadata depth")
    if isinstance(value, Mapping):
        _consume_metadata_budget(budget, encoded_bytes=2)
        identity = id(value)
        if identity in seen:
            raise ValueError(f"{path} must not contain reference cycles")
        seen.add(identity)
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} keys must be strings")
            if len(key) > _MAX_METADATA_STRING_CHARS:
                raise ValueError(f"{path} keys are too long")
            if contains_raw_vin(key):
                raise ValueError(f"{path} must not contain raw VIN data")
            _consume_metadata_budget(budget, encoded_bytes=len(key.encode("utf-8")))
            normalized = key.strip().lower().replace("-", "_").replace(" ", "_")
            if normalized in _SENSITIVE_METADATA_KEYS:
                raise ValueError(f"{path} must not contain raw VIN data")
            _reject_sensitive_metadata(
                child,
                path=f"{path}.{key}",
                _seen=seen,
                _depth=_depth + 1,
                _budget=budget,
            )
    elif isinstance(value, (list, tuple)):
        _consume_metadata_budget(budget, encoded_bytes=2)
        identity = id(value)
        if identity in seen:
            raise ValueError(f"{path} must not contain reference cycles")
        seen.add(identity)
        for index, child in enumerate(value):
            _reject_sensitive_metadata(
                child,
                path=f"{path}[{index}]",
                _seen=seen,
                _depth=_depth + 1,
                _budget=budget,
            )
    elif isinstance(value, str):
        if len(value) > _MAX_METADATA_STRING_CHARS:
            raise ValueError(f"{path} string value is too long")
        _consume_metadata_budget(budget, encoded_bytes=len(value.encode("utf-8")))
        if contains_raw_vin(value):
            raise ValueError(f"{path} must not contain raw VIN data")
    elif isinstance(value, float):
        _consume_metadata_budget(budget, encoded_bytes=32)
        if not math.isfinite(value):
            raise ValueError(f"{path} must contain only finite numbers")
    elif isinstance(value, bool) or value is None:
        _consume_metadata_budget(budget, encoded_bytes=5)
    elif isinstance(value, int):
        if value.bit_length() > 4_096:
            raise ValueError(f"{path} integer value is too large")
        _consume_metadata_budget(budget, encoded_bytes=len(str(value)))
    else:
        raise ValueError(f"{path} must contain only JSON-compatible values")
    return value


class DomainModel(BaseModel):
    """Strict base model shared by all domain values."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        str_strip_whitespace=True,
    )


class TransportProtocol(StrEnum):
    OBD2 = "obd2"
    UDS = "uds"
    KWP2000 = "kwp2000"
    SIMULATED = "simulated"
    UNKNOWN = "unknown"


class ConnectionState(StrEnum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


class SignalQuality(StrEnum):
    GOOD = "good"
    STALE = "stale"
    UNSUPPORTED = "unsupported"
    ERROR = "error"
    SYNTHETIC = "synthetic"


class DTCState(StrEnum):
    STORED = "stored"
    PENDING = "pending"
    PERMANENT = "permanent"
    HISTORIC = "historic"
    UNKNOWN = "unknown"


class IssueSeverity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class IssueStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class TimelineEventType(StrEnum):
    OPENED = "opened"
    NOTE = "note"
    STATUS_CHANGED = "status_changed"
    CLOSED = "closed"


class DataSource(StrEnum):
    STANDARD = "standard"
    LICENSED_OEM = "licensed-oem"
    COMMUNITY = "community"
    SYNTHETIC = "synthetic"


# Backwards-friendly semantic alias: readings and DTCs share one provenance enum.
SignalSource = DataSource


class VinIdentity(DomainModel):
    """Pseudonymous public representation of a VIN.

    ``fingerprint`` is deliberately truncated for readability; it is an
    identifier, not an authentication primitive or an anonymization guarantee.
    """

    fingerprint: Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{16}$")]
    redacted: Annotated[str, StringConstraints(pattern=r"^\*{13}[A-HJ-NPR-Z0-9]{4}$")]

    @classmethod
    def from_vin(cls, vin: str, *, secret: bytes = b"") -> VinIdentity:
        normalized = vin.strip().upper()
        if not _VIN_RE.fullmatch(normalized):
            raise ValueError("VIN must contain 17 valid ISO 3779 characters")
        digest = hashlib.sha256(secret + normalized.encode("ascii")).hexdigest()[:16]
        return cls(fingerprint=f"sha256:{digest}", redacted=f"{'*' * 13}{normalized[-4:]}")


class ECURef(DomainModel):
    ecu_id: SafeId
    name: ShortText
    protocol: TransportProtocol = TransportProtocol.UNKNOWN
    available: bool = True


class Vehicle(DomainModel):
    vehicle_id: SafeId
    display_name: ShortText
    protocol: TransportProtocol = TransportProtocol.UNKNOWN
    connection_state: ConnectionState = ConnectionState.UNKNOWN
    vin: VinIdentity | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def metadata_must_not_leak_vin(cls, value: dict[str, Any]) -> dict[str, Any]:
        _reject_sensitive_metadata(value)
        return value


class VehicleStatus(DomainModel):
    vehicle: Vehicle
    observed_at: AwareDatetime = Field(default_factory=utc_now)
    ecus: tuple[ECURef, ...] = ()
    notes: tuple[ShortText, ...] = Field(default=(), max_length=MAX_STATUS_NOTES)


class SignalReading(DomainModel):
    vehicle_id: SafeId
    ecu_id: SafeId | None = None
    pid: PidCode | None = None
    signal_id: SafeId
    name: ShortText
    value: SignalScalar
    unit: Annotated[
        str,
        StringConstraints(strip_whitespace=True, max_length=64),
        AfterValidator(reject_raw_vin_text),
    ] = ""
    captured_at: AwareDatetime = Field(default_factory=utc_now)
    quality: SignalQuality = SignalQuality.GOOD
    source: DataSource = DataSource.STANDARD
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    @field_validator("value")
    @classmethod
    def value_must_be_finite(cls, value: SignalScalar) -> SignalScalar:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("signal value must be finite")
        if isinstance(value, str) and len(value) > 1_024:
            raise ValueError("signal string value is too long")
        if isinstance(value, str):
            reject_raw_vin_text(value)
        return value


class DiagnosticTroubleCode(DomainModel):
    vehicle_id: SafeId
    ecu_id: SafeId | None = None
    code: Annotated[str, StringConstraints(strip_whitespace=True, min_length=5, max_length=6)]
    description: Annotated[
        str,
        StringConstraints(strip_whitespace=True, max_length=512),
        AfterValidator(reject_raw_vin_text),
    ] = ""
    state: DTCState = DTCState.UNKNOWN
    observed_at: AwareDatetime = Field(default_factory=utc_now)
    source: DataSource = DataSource.STANDARD
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not (_OBD_DTC_RE.fullmatch(normalized) or _UDS_DTC_RE.fullmatch(normalized)):
            raise ValueError("DTC must be a normalized five-character OBD or six-hex UDS code")
        return normalized


# Concise alias for callers that prefer the standards terminology.
DTC = DiagnosticTroubleCode


class EcuSnapshot(DomainModel):
    vehicle_id: SafeId
    ecu_id: SafeId
    protocol: TransportProtocol = TransportProtocol.UNKNOWN
    captured_at: AwareDatetime = Field(default_factory=utc_now)
    signals: tuple[SignalReading, ...] = ()
    dtcs: tuple[DiagnosticTroubleCode, ...] = ()
    profile_id: SafeId | None = None
    profile_source: DataSource | None = None
    profile_confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class DiagnosticIssue(DomainModel):
    issue_id: SafeId
    vehicle_id: SafeId
    title: ShortText
    description: Annotated[
        str,
        StringConstraints(strip_whitespace=True, max_length=8_192),
        AfterValidator(reject_raw_vin_text),
    ] = ""
    severity: IssueSeverity = IssueSeverity.MEDIUM
    status: IssueStatus = IssueStatus.OPEN
    dtc_codes: tuple[Annotated[str, StringConstraints(min_length=5, max_length=6)], ...] = ()
    opened_at: AwareDatetime = Field(default_factory=utc_now)
    closed_at: AwareDatetime | None = None

    @field_validator("dtc_codes")
    @classmethod
    def normalize_dtc_codes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for value in values:
            code = value.strip().upper()
            if not (_OBD_DTC_RE.fullmatch(code) or _UDS_DTC_RE.fullmatch(code)):
                raise ValueError(f"invalid DTC code: {value!r}")
            if code not in normalized:
                normalized.append(code)
        return tuple(normalized)


class TimelineEvent(DomainModel):
    event_id: SafeId
    issue_id: SafeId
    vehicle_id: SafeId
    event_type: TimelineEventType
    occurred_at: AwareDatetime = Field(default_factory=utc_now)
    message: Annotated[
        str,
        StringConstraints(strip_whitespace=True, max_length=2_048),
        AfterValidator(reject_raw_vin_text),
    ] = ""
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("details")
    @classmethod
    def details_must_not_leak_vin(cls, value: dict[str, Any]) -> dict[str, Any]:
        _reject_sensitive_metadata(value, path="details")
        return value


class IssueTimeline(DomainModel):
    issue: DiagnosticIssue
    events: tuple[TimelineEvent, ...]
