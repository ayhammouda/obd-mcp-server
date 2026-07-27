"""Hard default-deny policy for all vehicle-facing operations."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from types import MappingProxyType

from .errors import (
    MutableOperationDeniedError,
    PolicyDeniedError,
    RawCommandDeniedError,
)


@dataclass(frozen=True, slots=True)
class StandardPidDefinition:
    pid: str
    signal_id: str
    name: str
    unit: str
    py_obdii_command: str
    minimum: float
    maximum: float


# This is the complete public PID surface. Adding a PID is a policy change and
# must happen in source control; callers cannot supply arbitrary OBD commands.
_STANDARD_PID_DEFINITIONS = {
    "0104": StandardPidDefinition(
        "0104",
        "engine_load",
        "Calculated engine load",
        "%",
        "ENGINE_LOAD",
        0.0,
        100.0,
    ),
    "0105": StandardPidDefinition(
        "0105",
        "engine_coolant_temperature",
        "Engine coolant temperature",
        "degC",
        "ENGINE_COOLANT_TEMP",
        -40.0,
        215.0,
    ),
    "010B": StandardPidDefinition(
        "010B",
        "intake_manifold_pressure",
        "Intake manifold absolute pressure",
        "kPa",
        "INTAKE_PRESSURE",
        0.0,
        255.0,
    ),
    "010C": StandardPidDefinition(
        "010C",
        "engine_speed",
        "Engine speed",
        "rpm",
        "ENGINE_SPEED",
        0.0,
        16_383.75,
    ),
    "010D": StandardPidDefinition(
        "010D",
        "vehicle_speed",
        "Vehicle speed",
        "km/h",
        "VEHICLE_SPEED",
        0.0,
        255.0,
    ),
    "010F": StandardPidDefinition(
        "010F",
        "intake_air_temperature",
        "Intake air temperature",
        "degC",
        "INTAKE_AIR_TEMP",
        -40.0,
        215.0,
    ),
    "0111": StandardPidDefinition(
        "0111",
        "throttle_position",
        "Throttle position",
        "%",
        "THROTTLE_POSITION",
        0.0,
        100.0,
    ),
    "012F": StandardPidDefinition(
        "012F",
        "fuel_level",
        "Fuel level",
        "%",
        "FUEL_LEVEL",
        0.0,
        100.0,
    ),
    "0142": StandardPidDefinition(
        "0142",
        "control_module_voltage",
        "Control module voltage",
        "V",
        "VEHICLE_VOLTAGE",
        0.0,
        65.535,
    ),
}
STANDARD_PIDS = MappingProxyType(_STANDARD_PID_DEFINITIONS)

READ_ONLY_OBD_SERVICES = frozenset({0x01, 0x03})
READ_ONLY_UDS_SERVICES = frozenset({0x19, 0x22})

MUTABLE_OBD_SERVICES = frozenset({0x04, 0x08})
MUTABLE_UDS_SERVICES = frozenset(
    {
        0x10,  # DiagnosticSessionControl changes ECU session.
        0x11,  # ECUReset.
        0x14,  # ClearDiagnosticInformation.
        0x27,  # SecurityAccess.
        0x28,  # CommunicationControl.
        0x2E,  # WriteDataByIdentifier.
        0x2F,  # InputOutputControlByIdentifier.
        0x31,  # RoutineControl.
        0x34,  # RequestDownload.
        0x35,  # RequestUpload.
        0x36,  # TransferData.
        0x37,  # RequestTransferExit.
        0x3D,  # WriteMemoryByAddress.
        0x85,  # ControlDTCSetting.
    }
)
MUTABLE_KWP_SERVICES = frozenset(
    {
        0x10,  # StartDiagnosticSession.
        0x11,  # ECUReset.
        0x14,  # ClearDiagnosticInformation.
        0x27,  # SecurityAccess.
        0x28,  # Disable/enable normal message transmission.
        0x2E,  # WriteDataByCommonIdentifier.
        0x30,  # InputOutputControlByLocalIdentifier.
        0x31,  # StartRoutineByLocalIdentifier.
        0x32,  # StopRoutineByLocalIdentifier.
        0x34,  # RequestDownload.
        0x35,  # RequestUpload.
        0x36,  # TransferData.
        0x37,  # RequestTransferExit.
        0x3B,  # WriteDataByLocalIdentifier.
    }
)

_HEX_RE = re.compile(r"^[0-9A-F]+$")


def normalize_pid(value: str | int) -> str:
    """Normalize a Mode 01 PID to the canonical ``01XX`` representation."""

    if isinstance(value, int):
        if not 0 <= value <= 0xFF:
            raise PolicyDeniedError("PID must be between 0x00 and 0xFF")
        return f"01{value:02X}"
    normalized = value.strip().upper().replace(" ", "").replace("_", "")
    if normalized.startswith("0X"):
        normalized = normalized[2:]
    if len(normalized) == 2:
        normalized = f"01{normalized}"
    if len(normalized) != 4 or not _HEX_RE.fullmatch(normalized):
        raise PolicyDeniedError("PID must be a hexadecimal Mode 01 PID")
    if not normalized.startswith("01"):
        raise PolicyDeniedError("only Mode 01 standard PIDs are accepted")
    return normalized


class ReadOnlyPolicy:
    """Central policy gate.

    Every operation is denied unless one of the explicit authorizers below
    accepts it. There is intentionally no method that returns permission for a
    raw command.
    """

    standard_pids = STANDARD_PIDS

    def authorize_standard_pids(
        self,
        pids: Iterable[str | int] | None = None,
    ) -> tuple[str, ...]:
        requested = STANDARD_PIDS if pids is None else pids
        authorized: list[str] = []
        for pid in requested:
            normalized = normalize_pid(pid)
            if normalized not in STANDARD_PIDS:
                raise PolicyDeniedError(
                    f"PID {normalized} is not in the fixed read-only allowlist",
                    details={"pid": normalized},
                )
            if normalized not in authorized:
                authorized.append(normalized)
        return tuple(authorized)

    def authorize_obd_service(self, service_id: int, *, pid: str | int | None = None) -> None:
        if service_id in MUTABLE_OBD_SERVICES:
            raise MutableOperationDeniedError(
                f"OBD service 0x{service_id:02X} can mutate vehicle state"
            )
        if service_id not in READ_ONLY_OBD_SERVICES:
            raise PolicyDeniedError(f"OBD service 0x{service_id:02X} is not allowed")
        if service_id == 0x01:
            if pid is None:
                raise PolicyDeniedError("Mode 01 requires an allowlisted PID")
            self.authorize_standard_pids((pid,))
        elif pid is not None:
            raise PolicyDeniedError("Mode 03 does not accept a PID")

    def authorize_uds_service(self, service_id: int) -> None:
        if service_id in MUTABLE_UDS_SERVICES:
            raise MutableOperationDeniedError(
                f"UDS service 0x{service_id:02X} can mutate ECU state"
            )
        if service_id not in READ_ONLY_UDS_SERVICES:
            raise PolicyDeniedError(f"UDS service 0x{service_id:02X} is not allowed")

    def authorize_kwp_service(self, service_id: int) -> None:
        if service_id in MUTABLE_KWP_SERVICES:
            raise MutableOperationDeniedError(
                f"KWP service 0x{service_id:02X} can mutate ECU state"
            )
        # The generic public API has no KWP profile/query surface.
        raise PolicyDeniedError(f"KWP service 0x{service_id:02X} is not allowed")

    def reject_raw_command(self, *_args: object, **_kwargs: object) -> None:
        raise RawCommandDeniedError("raw vehicle commands are never permitted")
