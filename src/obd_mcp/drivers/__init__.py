"""Read-only diagnostic driver interfaces and built-ins."""

from .base import DiagnosticDriver
from .elm327 import Elm327Driver
from .registry import DRIVER_ENTRY_POINT_GROUP, DriverRegistry, create_driver
from .simulator import SimulatorDriver, SimulatorVehicle

__all__ = [
    "DRIVER_ENTRY_POINT_GROUP",
    "DiagnosticDriver",
    "DriverRegistry",
    "Elm327Driver",
    "SimulatorDriver",
    "SimulatorVehicle",
    "create_driver",
]
