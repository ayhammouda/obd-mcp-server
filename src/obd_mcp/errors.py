"""Stable application errors safe to translate at the MCP boundary."""

from __future__ import annotations

from typing import Any


class OBDMCPError(Exception):
    """Base class for expected, user-presentable application failures."""

    code = "obd_mcp_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class PolicyDeniedError(OBDMCPError):
    code = "policy_denied"


class MutableOperationDeniedError(PolicyDeniedError):
    code = "mutable_operation_denied"


class RawCommandDeniedError(PolicyDeniedError):
    code = "raw_command_denied"


class UnsupportedOperationError(OBDMCPError):
    code = "unsupported_operation"


class DriverError(OBDMCPError):
    code = "driver_error"


class DriverUnavailableError(DriverError):
    code = "driver_unavailable"


class VehicleNotFoundError(DriverError):
    code = "vehicle_not_found"


class ProfileError(OBDMCPError):
    code = "profile_error"


class ProfileValidationError(ProfileError):
    code = "profile_validation_error"


class ProfileNotFoundError(ProfileError):
    code = "profile_not_found"


class StorageError(OBDMCPError):
    code = "storage_error"


class IssueNotFoundError(StorageError):
    code = "issue_not_found"


class ServiceClosedError(OBDMCPError):
    code = "service_closed"
