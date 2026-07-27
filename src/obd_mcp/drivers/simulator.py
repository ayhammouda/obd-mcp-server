"""Deterministic built-in simulator for development and tests."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import Field, field_validator, model_validator

from ..domain import (
    ConnectionState,
    DataSource,
    DiagnosticTroubleCode,
    DomainModel,
    DTCState,
    ECURef,
    EcuSnapshot,
    SignalQuality,
    SignalReading,
    TransportProtocol,
    Vehicle,
    VehicleStatus,
    VinIdentity,
)
from ..errors import DriverError, VehicleNotFoundError
from ..policy import STANDARD_PIDS, ReadOnlyPolicy
from ..profiles import DecoderDataType, ProfileReadDefinition
from .base import DiagnosticDriver

_BASE_VALUES: dict[str, int | float] = {
    "0104": 24.0,
    "0105": 91,
    "010B": 42,
    "010C": 812,
    "010D": 0,
    "010F": 24,
    "0111": 15.0,
    "012F": 68.0,
    "0142": 14.2,
}
_JITTER_AMPLITUDES: dict[str, float] = {
    "0104": 1.5,
    "0105": 2.0,
    "010B": 2.0,
    "010C": 30.0,
    "010D": 0.0,
    "010F": 1.0,
    "0111": 1.0,
    "012F": 0.5,
    "0142": 0.1,
}
_OBD_DTC_RE = re.compile(r"^[PBCU][0-3][0-9A-F]{3}$")
_MAX_SIMULATOR_ECUS = 128
_MAX_SIMULATOR_DTCS = 256
_MIN_SIMULATOR_SEED = -(2**63)
_MAX_SIMULATOR_SEED = 2**63 - 1


def _default_ecus() -> tuple[ECURef, ...]:
    return (
        ECURef(ecu_id="engine", name="Powertrain ECU", protocol=TransportProtocol.SIMULATED),
        ECURef(
            ecu_id="transmission",
            name="Transmission ECU",
            protocol=TransportProtocol.SIMULATED,
        ),
    )


def _default_dtcs() -> dict[str, tuple[str, ...]]:
    return {"engine": ("P0300",)}


class SimulatorVehicle(DomainModel):
    vehicle_id: str
    display_name: str
    vin: str | None = None
    ecus: tuple[ECURef, ...] = Field(
        default_factory=_default_ecus,
        min_length=1,
        max_length=_MAX_SIMULATOR_ECUS,
    )
    dtcs: dict[str, tuple[str, ...]] = Field(default_factory=_default_dtcs)
    pid_values: dict[str, int | float] = Field(default_factory=dict)

    @field_validator("vin")
    @classmethod
    def validate_vin(cls, value: str | None) -> str | None:
        if value is not None:
            VinIdentity.from_vin(value)
        return value

    @field_validator("pid_values", mode="before")
    @classmethod
    def reject_non_numeric_pid_values(cls, values: Any) -> Any:
        if not isinstance(values, Mapping):
            return values
        for value in values.values():
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or (isinstance(value, float) and not math.isfinite(value))
            ):
                raise ValueError("simulator PID values must be finite numbers, not booleans")
        return values

    @field_validator("pid_values")
    @classmethod
    def validate_pid_values(cls, values: dict[str, int | float]) -> dict[str, int | float]:
        policy = ReadOnlyPolicy()
        normalized: dict[str, int | float] = {}
        for key, value in values.items():
            pid = policy.authorize_standard_pids((key,))[0]
            definition = STANDARD_PIDS[pid]
            if not definition.minimum <= value <= definition.maximum:
                raise ValueError(
                    f"simulator PID {pid} must be between "
                    f"{definition.minimum} and {definition.maximum}"
                )
            normalized[pid] = value
        return normalized

    @field_validator("dtcs")
    @classmethod
    def validate_dtcs(
        cls,
        values: dict[str, tuple[str, ...]],
    ) -> dict[str, tuple[str, ...]]:
        if sum(len(codes) for codes in values.values()) > _MAX_SIMULATOR_DTCS:
            raise ValueError(f"simulator must not define more than {_MAX_SIMULATOR_DTCS} DTCs")
        for codes in values.values():
            if len(codes) != len(set(codes)):
                raise ValueError("simulator DTC codes must be unique within each ECU")
            if any(_OBD_DTC_RE.fullmatch(code) is None for code in codes):
                raise ValueError("simulator DTCs must use canonical uppercase OBD codes")
        return values

    @model_validator(mode="after")
    def validate_unique_ecus(self) -> SimulatorVehicle:
        ecu_ids = [ecu.ecu_id for ecu in self.ecus]
        if len(ecu_ids) != len(set(ecu_ids)):
            raise ValueError("simulator ECU ids must be unique")
        if "engine" not in ecu_ids:
            raise ValueError("simulator requires an engine ECU for standard PID readings")
        if any(ecu.protocol is not TransportProtocol.SIMULATED for ecu in self.ecus):
            raise ValueError("simulator ECUs must use the simulated protocol")
        if not set(self.dtcs).issubset(ecu_ids):
            raise ValueError("simulator DTC map references an unknown ECU")
        return self


def _default_vehicle() -> SimulatorVehicle:
    return SimulatorVehicle(
        vehicle_id="sim-vehicle-1",
        display_name="Synthetic demo vehicle",
    )


class SimulatorDriver(DiagnosticDriver):
    """A deterministic driver that never touches vehicle hardware."""

    def __init__(
        self,
        *,
        seed: int = 0,
        vehicles: Sequence[SimulatorVehicle | Mapping[str, Any]] | None = None,
    ) -> None:
        configured = vehicles if vehicles is not None else (_default_vehicle(),)
        validated: list[SimulatorVehicle] = []
        for item in configured:
            if isinstance(item, SimulatorVehicle):
                validated.append(item)
            else:
                validated.append(SimulatorVehicle.model_validate(dict(item)))
        if not validated:
            raise ValueError("simulator requires at least one vehicle")
        if len({item.vehicle_id for item in validated}) != len(validated):
            raise ValueError("simulator vehicle ids must be unique")
        if (
            isinstance(seed, bool)
            or not isinstance(seed, int)
            or not _MIN_SIMULATOR_SEED <= seed <= _MAX_SIMULATOR_SEED
        ):
            raise ValueError("simulator seed must be a signed 64-bit integer")
        self._seed = seed
        self._configs = {item.vehicle_id: item for item in validated}
        self._closed = False
        self._policy = ReadOnlyPolicy()

    def _ensure_open(self) -> None:
        if self._closed:
            raise DriverError("simulator driver is closed")

    def _config(self, vehicle_id: str) -> SimulatorVehicle:
        self._ensure_open()
        try:
            return self._configs[vehicle_id]
        except KeyError as exc:
            raise VehicleNotFoundError(f"vehicle not found: {vehicle_id}") from exc

    @staticmethod
    def _vehicle(config: SimulatorVehicle) -> Vehicle:
        identity = VinIdentity.from_vin(config.vin) if config.vin else None
        return Vehicle(
            vehicle_id=config.vehicle_id,
            display_name=config.display_name,
            protocol=TransportProtocol.SIMULATED,
            connection_state=ConnectionState.CONNECTED,
            vin=identity,
            metadata={"source": "synthetic"},
        )

    def _stable_fraction(self, *parts: object) -> float:
        material = ":".join((str(self._seed), *(str(part) for part in parts)))
        digest = hashlib.blake2b(material.encode("utf-8"), digest_size=8).digest()
        integer = int.from_bytes(digest, "big")
        return (integer % 20_001) / 10_000 - 1.0

    def _pid_value(self, config: SimulatorVehicle, pid: str) -> int | float:
        if pid in config.pid_values:
            return config.pid_values[pid]
        base = _BASE_VALUES[pid]
        amplitude = _JITTER_AMPLITUDES[pid]
        value = float(base) + self._stable_fraction(config.vehicle_id, pid) * amplitude
        if isinstance(base, int):
            return round(value)
        return round(value, 2)

    async def list_vehicles(self) -> tuple[Vehicle, ...]:
        self._ensure_open()
        return tuple(self._vehicle(self._configs[key]) for key in sorted(self._configs))

    async def get_vehicle_status(self, vehicle_id: str) -> VehicleStatus:
        config = self._config(vehicle_id)
        return VehicleStatus(vehicle=self._vehicle(config), ecus=config.ecus)

    async def read_standard_pids(
        self,
        vehicle_id: str,
        pids: Sequence[str],
    ) -> tuple[SignalReading, ...]:
        config = self._config(vehicle_id)
        authorized = self._policy.authorize_standard_pids(pids)
        return tuple(
            SignalReading(
                vehicle_id=vehicle_id,
                ecu_id="engine",
                pid=pid,
                signal_id=STANDARD_PIDS[pid].signal_id,
                name=STANDARD_PIDS[pid].name,
                value=self._pid_value(config, pid),
                unit=STANDARD_PIDS[pid].unit,
                quality=SignalQuality.SYNTHETIC,
                source=DataSource.SYNTHETIC,
            )
            for pid in authorized
        )

    async def read_dtcs(
        self,
        vehicle_id: str,
        ecu_id: str | None = None,
    ) -> tuple[DiagnosticTroubleCode, ...]:
        config = self._config(vehicle_id)
        selected_ecus = (ecu_id,) if ecu_id is not None else tuple(sorted(config.dtcs))
        known_ecus = {ecu.ecu_id for ecu in config.ecus}
        result: list[DiagnosticTroubleCode] = []
        for selected_ecu in selected_ecus:
            if selected_ecu not in known_ecus:
                raise DriverError(f"ECU not found: {selected_ecu}")
            result.extend(
                (
                    DiagnosticTroubleCode(
                        vehicle_id=vehicle_id,
                        ecu_id=selected_ecu,
                        code=code,
                        description="",
                        state=DTCState.STORED,
                        source=DataSource.SYNTHETIC,
                    )
                )
                for code in config.dtcs.get(selected_ecu, ())
            )
        return tuple(result)

    async def read_ecu_snapshot(
        self,
        vehicle_id: str,
        ecu_id: str,
        reads: Sequence[ProfileReadDefinition] = (),
        *,
        profile_id: str | None = None,
    ) -> EcuSnapshot:
        config = self._config(vehicle_id)
        if ecu_id not in {ecu.ecu_id for ecu in config.ecus}:
            raise DriverError(f"ECU not found: {ecu_id}")

        signals: list[SignalReading] = []
        include_dtcs = not reads
        for read in reads:
            self._policy.authorize_uds_service(read.service_id)
            if read.ecu_id != ecu_id:
                continue
            if read.service_id == 0x19:
                include_dtcs = True
                continue
            assert read.decoder is not None
            assert read.signal_id is not None
            decoder = read.decoder
            fraction = self._stable_fraction(vehicle_id, ecu_id, read.identifier)
            if decoder.data_type == DecoderDataType.ASCII:
                value: str | int | float = f"SIM-{read.identifier:04X}"
            elif decoder.data_type == DecoderDataType.BYTES:
                byte_length = decoder.byte_length or 1
                value = hashlib.blake2b(
                    f"{self._seed}:{vehicle_id}:{read.identifier}".encode(),
                    digest_size=min(byte_length, 64),
                ).hexdigest()
            else:
                raw_bounds = {
                    DecoderDataType.UINT8: (0, 2**8 - 1),
                    DecoderDataType.INT8: (-(2**7), 2**7 - 1),
                    DecoderDataType.UINT16: (0, 2**16 - 1),
                    DecoderDataType.INT16: (-(2**15), 2**15 - 1),
                    DecoderDataType.UINT32: (0, 2**32 - 1),
                    DecoderDataType.INT32: (-(2**31), 2**31 - 1),
                }
                raw_min, raw_max = raw_bounds[decoder.data_type]
                normalized_fraction = (fraction + 1.0) / 2.0
                raw_value = int(raw_min + normalized_fraction * (raw_max - raw_min))
                value = round(raw_value * decoder.scale + decoder.value_offset, 4)
            signals.append(
                SignalReading(
                    vehicle_id=vehicle_id,
                    ecu_id=ecu_id,
                    signal_id=read.signal_id,
                    name=read.name,
                    value=value,
                    unit=decoder.unit,
                    quality=SignalQuality.SYNTHETIC,
                    source=DataSource.SYNTHETIC,
                )
            )

        dtcs = await self.read_dtcs(vehicle_id, ecu_id) if include_dtcs else ()
        if reads and dtcs:
            dtcs = tuple(
                dtc.model_copy(
                    update={
                        "code": hashlib.blake2b(
                            dtc.code.encode("ascii"),
                            digest_size=3,
                        )
                        .hexdigest()
                        .upper()
                    }
                )
                for dtc in dtcs
            )
        if not reads and ecu_id == "engine":
            signals.extend(await self.read_standard_pids(vehicle_id, tuple(STANDARD_PIDS)))
        return EcuSnapshot(
            vehicle_id=vehicle_id,
            ecu_id=ecu_id,
            protocol=TransportProtocol.SIMULATED,
            signals=tuple(signals),
            dtcs=dtcs,
            profile_id=profile_id,
        )

    async def close(self) -> None:
        self._closed = True
