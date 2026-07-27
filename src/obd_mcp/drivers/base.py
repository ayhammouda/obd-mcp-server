"""Async driver contract for read-only diagnostic backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import TYPE_CHECKING

from ..domain import (
    DiagnosticTroubleCode,
    EcuSnapshot,
    SignalReading,
    Vehicle,
    VehicleStatus,
)

if TYPE_CHECKING:
    from ..profiles import ProfileReadDefinition


class DiagnosticDriver(ABC):
    """Minimal async interface implemented by built-in and plugin drivers.

    There is deliberately no raw-command method in this interface.
    """

    @abstractmethod
    async def list_vehicles(self) -> tuple[Vehicle, ...]:
        """List vehicles managed by this driver."""

    @abstractmethod
    async def get_vehicle_status(self, vehicle_id: str) -> VehicleStatus:
        """Return a normalized status snapshot."""

    @abstractmethod
    async def read_standard_pids(
        self,
        vehicle_id: str,
        pids: Sequence[str],
    ) -> tuple[SignalReading, ...]:
        """Read only the fixed Mode 01 PID allowlist."""

    @abstractmethod
    async def read_dtcs(
        self,
        vehicle_id: str,
        ecu_id: str | None = None,
    ) -> tuple[DiagnosticTroubleCode, ...]:
        """Read diagnostic trouble codes without clearing them."""

    @abstractmethod
    async def read_ecu_snapshot(
        self,
        vehicle_id: str,
        ecu_id: str,
        reads: Sequence[ProfileReadDefinition] = (),
        *,
        profile_id: str | None = None,
    ) -> EcuSnapshot:
        """Read an ECU snapshot using already validated read definitions."""

    @abstractmethod
    async def close(self) -> None:
        """Release driver resources; must be idempotent."""
