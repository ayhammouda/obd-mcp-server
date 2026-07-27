"""Built-in and Python entry-point driver discovery."""

from __future__ import annotations

from collections.abc import Callable
from importlib import metadata
from typing import Any

from ..errors import DriverError, DriverUnavailableError
from .base import DiagnosticDriver
from .elm327 import Elm327Driver
from .simulator import SimulatorDriver

DRIVER_ENTRY_POINT_GROUP = "obd_mcp.drivers"
DriverFactory = Callable[..., DiagnosticDriver]

_BUILTIN_FACTORIES: dict[str, DriverFactory] = {
    "simulator": SimulatorDriver,
    "elm327": Elm327Driver,
}


def _driver_entry_points() -> dict[str, metadata.EntryPoint]:
    selected = metadata.entry_points().select(group=DRIVER_ENTRY_POINT_GROUP)
    result: dict[str, metadata.EntryPoint] = {}
    for entry_point in selected:
        if entry_point.name in _BUILTIN_FACTORIES:
            raise DriverError(f"plugin cannot override built-in driver: {entry_point.name}")
        if entry_point.name in result:
            raise DriverError(f"duplicate driver entry point: {entry_point.name}")
        result[entry_point.name] = entry_point
    return result


class DriverRegistry:
    def __init__(self, *, allow_third_party: bool = False) -> None:
        self._allow_third_party = allow_third_party

    def names(self) -> tuple[str, ...]:
        if not self._allow_third_party:
            return tuple(sorted(_BUILTIN_FACTORIES))
        return tuple(sorted((*_BUILTIN_FACTORIES, *_driver_entry_points())))

    def create(self, name: str, **kwargs: Any) -> DiagnosticDriver:
        normalized = name.strip().lower()
        factory: Any
        if normalized in _BUILTIN_FACTORIES:
            factory = _BUILTIN_FACTORIES[normalized]
        else:
            if not self._allow_third_party:
                raise DriverUnavailableError(
                    "third-party diagnostic drivers are disabled; explicit opt-in is required"
                )
            entry_points = _driver_entry_points()
            try:
                entry_point = entry_points[normalized]
            except KeyError as exc:
                raise DriverUnavailableError(f"diagnostic driver not found: {name}") from exc
            try:
                factory = entry_point.load()
            except Exception as exc:
                raise DriverUnavailableError(f"failed to load diagnostic driver: {name}") from exc
        try:
            driver = factory(**kwargs)
        except DriverError:
            raise
        except Exception as exc:
            raise DriverUnavailableError(f"failed to create diagnostic driver: {name}") from exc
        if not isinstance(driver, DiagnosticDriver):
            raise DriverUnavailableError(
                f"driver factory {name!r} did not return a DiagnosticDriver"
            )
        return driver


def create_driver(
    name: str,
    *,
    allow_third_party: bool = False,
    **kwargs: Any,
) -> DiagnosticDriver:
    return DriverRegistry(allow_third_party=allow_third_party).create(name, **kwargs)
