#!/usr/bin/env python3
"""Fail when release artifacts contain private or unintended files."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import re
import stat
import sys
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

try:
    from .check_repository_data import (
        sensitive_bytes_findings,
        sensitive_text_findings,
        unsafe_path_reason,
    )
except ImportError:  # pragma: no cover - direct script execution
    from check_repository_data import (
        sensitive_bytes_findings,
        sensitive_text_findings,
        unsafe_path_reason,
    )

MAX_ARTIFACT_BYTES = 25 * 1024 * 1024
MAX_MEMBER_BYTES = 5 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
MAX_MEMBERS = 2_048

_DIST_INFO_RE = re.compile(r"^[A-Za-z0-9_.-]+\.dist-info$")
_SDIST_PREFIX_RE = re.compile(r"^obd_mcp_server-[A-Za-z0-9][A-Za-z0-9._+-]*$")
_PACKAGE_PART_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DOC_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+\.md$")
_EXAMPLE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+\.(?:json|toml)$")
_SCRIPT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\.py$")

_SDIST_ROOT_FILES = {
    # Hatchling 1.31 force-includes the active VCS exclusion file in sdists.
    # Permit only this exact root path; nested/additional ignore files still fail.
    ".gitignore",
    "CHANGELOG.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "NOTICE",
    "PKG-INFO",
    "README.md",
    "SECURITY.md",
    "THIRD_PARTY_NOTICES.md",
    "build-constraints.in",
    "build-constraints.txt",
    "pyproject.toml",
}
_DIST_INFO_FILES = {
    "METADATA",
    "RECORD",
    "WHEEL",
    "entry_points.txt",
}
_DIST_INFO_LICENSES = {
    "LICENSE",
    "NOTICE",
    "THIRD_PARTY_NOTICES.md",
}
_SDIST_DIRECTORIES = {
    "docs",
    "examples",
    "scripts",
    "src",
}


@dataclass(frozen=True)
class ArtifactMember:
    """A normalized archive member and its optional regular-file contents."""

    name: str
    data: bytes | None
    is_directory: bool = False
    unsafe_type: str | None = None


def _validate_archive_size(path: Path) -> None:
    if path.stat().st_size > MAX_ARTIFACT_BYTES:
        raise ValueError(f"artifact exceeds {MAX_ARTIFACT_BYTES} bytes")


def artifact_members(path: Path) -> list[ArtifactMember]:
    """Read bounded regular-file members and describe unsafe archive entries."""

    _validate_archive_size(path)
    members: list[ArtifactMember] = []
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_MEMBERS:
                raise ValueError(f"artifact contains more than {MAX_MEMBERS} members")
            total_size = sum(info.file_size for info in infos)
            if total_size > MAX_TOTAL_UNCOMPRESSED_BYTES:
                raise ValueError(f"artifact expands beyond {MAX_TOTAL_UNCOMPRESSED_BYTES} bytes")
            for info in infos:
                unix_mode = info.external_attr >> 16
                file_type = stat.S_IFMT(unix_mode)
                if info.is_dir():
                    members.append(ArtifactMember(info.filename, None, is_directory=True))
                elif file_type == stat.S_IFLNK:
                    members.append(ArtifactMember(info.filename, None, unsafe_type="symbolic link"))
                elif file_type not in {0, stat.S_IFREG}:
                    members.append(
                        ArtifactMember(
                            info.filename,
                            None,
                            unsafe_type="non-regular archive member",
                        )
                    )
                elif info.file_size > MAX_MEMBER_BYTES:
                    members.append(
                        ArtifactMember(
                            info.filename,
                            None,
                            unsafe_type=f"file exceeds {MAX_MEMBER_BYTES} bytes",
                        )
                    )
                else:
                    members.append(ArtifactMember(info.filename, archive.read(info)))
        return members

    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            infos = archive.getmembers()
            if len(infos) > MAX_MEMBERS:
                raise ValueError(f"artifact contains more than {MAX_MEMBERS} members")
            total_size = sum(info.size for info in infos if info.isfile())
            if total_size > MAX_TOTAL_UNCOMPRESSED_BYTES:
                raise ValueError(f"artifact expands beyond {MAX_TOTAL_UNCOMPRESSED_BYTES} bytes")
            for info in infos:
                if info.isdir():
                    members.append(ArtifactMember(info.name, None, is_directory=True))
                elif not info.isfile():
                    members.append(
                        ArtifactMember(info.name, None, unsafe_type="non-regular archive member")
                    )
                elif info.size > MAX_MEMBER_BYTES:
                    members.append(
                        ArtifactMember(
                            info.name,
                            None,
                            unsafe_type=f"file exceeds {MAX_MEMBER_BYTES} bytes",
                        )
                    )
                else:
                    extracted = archive.extractfile(info)
                    if extracted is None:
                        members.append(
                            ArtifactMember(info.name, None, unsafe_type="unreadable archive member")
                        )
                    else:
                        members.append(ArtifactMember(info.name, extracted.read()))
        return members

    raise ValueError(f"unsupported artifact: {path}")


def _safe_package_path(path: PurePosixPath) -> bool:
    if not path.parts or path.parts[0] != "obd_mcp":
        return False
    relative = path.parts[1:]
    if not relative:
        return True
    if not all(_PACKAGE_PART_RE.fullmatch(part) for part in relative[:-1]):
        return False
    filename = relative[-1]
    if filename == "py.typed":
        return len(relative) == 1
    if filename.endswith(".py"):
        return _PACKAGE_PART_RE.fullmatch(filename[:-3]) is not None
    return _PACKAGE_PART_RE.fullmatch(filename) is not None


def _safe_package_entry(path: PurePosixPath, *, is_directory: bool) -> bool:
    if not _safe_package_path(path):
        return False
    if is_directory:
        return path.name != "py.typed" and "." not in path.name
    return path.name.endswith(".py") or path.name == "py.typed"


def _safe_wheel_path(path: PurePosixPath, *, is_directory: bool) -> bool:
    if _safe_package_entry(path, is_directory=is_directory):
        return True
    if not path.parts or not _DIST_INFO_RE.fullmatch(path.parts[0]):
        return False
    relative = path.parts[1:]
    if not relative:
        return is_directory
    if len(relative) == 1:
        return is_directory or relative[0] in _DIST_INFO_FILES
    if len(relative) == 2 and relative[0] == "licenses":
        return is_directory or relative[1] in _DIST_INFO_LICENSES
    return False


def _is_wheel_record(path: PurePosixPath) -> bool:
    return (
        len(path.parts) == 2
        and _DIST_INFO_RE.fullmatch(path.parts[0]) is not None
        and path.parts[1] == "RECORD"
    )


def _wheel_record_failures(members: list[ArtifactMember]) -> list[str]:
    """Verify RECORD structurally instead of secret-scanning encoded digests."""

    failures: list[str] = []
    regular_files = {member.name: member.data for member in members if member.data is not None}
    record_names = [name for name in regular_files if _is_wheel_record(PurePosixPath(name))]
    if len(record_names) != 1:
        return ["wheel must contain exactly one .dist-info/RECORD file"]

    record_name = record_names[0]
    record_data = regular_files[record_name]
    assert record_data is not None
    try:
        record_text = record_data.decode("utf-8")
    except UnicodeDecodeError:
        return [f"{record_name}: RECORD is not valid UTF-8"]

    recorded: set[str] = set()
    try:
        rows = list(csv.reader(io.StringIO(record_text, newline="")))
    except csv.Error as error:
        return [f"{record_name}: invalid RECORD CSV: {error}"]

    for row_number, row in enumerate(rows, start=1):
        if len(row) != 3:
            failures.append(f"{record_name}: row {row_number} must contain three fields")
            continue
        member_name, digest, size = row
        if member_name in recorded:
            failures.append(f"{record_name}: duplicate row for {member_name}")
            continue
        recorded.add(member_name)
        member_data = regular_files.get(member_name)
        if member_data is None:
            failures.append(f"{record_name}: row references a missing regular file")
            continue

        if member_name == record_name:
            if digest or size:
                failures.append(f"{record_name}: its own digest and size must be empty")
            continue

        expected_digest = "sha256=" + base64.urlsafe_b64encode(
            hashlib.sha256(member_data).digest()
        ).rstrip(b"=").decode("ascii")
        if digest != expected_digest:
            failures.append(f"{record_name}: digest mismatch for {member_name}")
        if size != str(len(member_data)):
            failures.append(f"{record_name}: size mismatch for {member_name}")

    missing = sorted(set(regular_files) - recorded)
    if missing:
        failures.append(f"{record_name}: missing row(s) for {len(missing)} file(s)")
    return failures


def _safe_sdist_path(path: PurePosixPath, *, is_directory: bool) -> bool:
    if not path.parts:
        return True
    if len(path.parts) == 1:
        return path.name in (_SDIST_DIRECTORIES if is_directory else _SDIST_ROOT_FILES)

    directory = path.parts[0]
    relative = path.parts[1:]
    if directory == "src":
        if not relative or relative[0] != "obd_mcp":
            return False
        return _safe_package_entry(PurePosixPath(*relative), is_directory=is_directory)
    if directory == "docs":
        if is_directory:
            return len(relative) == 0 or relative == ("adr",)
        return (len(relative) == 1 and _DOC_NAME_RE.fullmatch(relative[0]) is not None) or (
            len(relative) == 2
            and relative[0] == "adr"
            and _DOC_NAME_RE.fullmatch(relative[1]) is not None
        )
    if directory == "scripts":
        return (is_directory and len(relative) == 0) or (
            len(relative) == 1 and _SCRIPT_NAME_RE.fullmatch(relative[0]) is not None
        )
    if directory == "examples":
        if is_directory and len(relative) in {0, 1}:
            return len(relative) == 0 or relative[0] == "profiles"
        if len(relative) == 1:
            return _EXAMPLE_NAME_RE.fullmatch(relative[0]) is not None
        if len(relative) == 2 and relative[0] == "profiles":
            return _EXAMPLE_NAME_RE.fullmatch(relative[1]) is not None
    return False


def _normalized_sdist_paths(
    members: list[ArtifactMember],
) -> tuple[list[tuple[ArtifactMember, PurePosixPath]], list[str]]:
    normalized: list[tuple[ArtifactMember, PurePosixPath]] = []
    failures: list[str] = []
    prefixes: set[str] = set()
    for member in members:
        path = PurePosixPath(member.name)
        if not path.parts:
            failures.append(f"{member.name}: empty archive path")
            continue
        prefixes.add(path.parts[0])
    if len(prefixes) != 1:
        failures.append("sdist must contain exactly one top-level directory")
        return normalized, failures
    prefix = next(iter(prefixes))
    if _SDIST_PREFIX_RE.fullmatch(prefix) is None:
        failures.append(f"{prefix}: unexpected sdist top-level directory")
        return normalized, failures
    for member in members:
        path = PurePosixPath(member.name)
        normalized.append((member, PurePosixPath(*path.parts[1:])))
    return normalized, failures


def check(path: Path) -> list[str]:
    """Return release-safety failures for one wheel or source distribution."""

    try:
        members = artifact_members(path)
    except (OSError, tarfile.TarError, ValueError, zipfile.BadZipFile) as error:
        return [f"{path.name}: cannot inspect artifact: {error}"]

    failures: list[str] = []
    seen: set[str] = set()
    for member in members:
        if member.name in seen:
            failures.append(f"{path.name}: {member.name}: duplicate archive member")
        seen.add(member.name)

    if path.suffix == ".whl":
        normalized = [(member, PurePosixPath(member.name)) for member in members]
        allowlisted = _safe_wheel_path
        failures.extend(f"{path.name}: {failure}" for failure in _wheel_record_failures(members))
    else:
        normalized, prefix_failures = _normalized_sdist_paths(members)
        failures.extend(f"{path.name}: {failure}" for failure in prefix_failures)
        allowlisted = _safe_sdist_path

    for member, member_path in normalized:
        display = member.name
        reason = unsafe_path_reason(PurePosixPath(member.name))
        if reason is not None:
            failures.append(f"{path.name}: {display}: {reason}")
        failures.extend(
            f"{path.name}: {display}: filename contains {finding}"
            for finding in sensitive_text_findings(member.name)
        )
        if member.unsafe_type is not None:
            failures.append(f"{path.name}: {display}: {member.unsafe_type}")
        if not allowlisted(member_path, is_directory=member.is_directory):
            failures.append(f"{path.name}: {display}: path is not in the artifact allowlist")
        if member.data is not None and not (
            path.suffix == ".whl" and _is_wheel_record(member_path)
        ):
            failures.extend(
                f"{path.name}: {display}: contains {finding}"
                for finding in sensitive_bytes_findings(member.data)
            )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("artifacts", nargs="+", type=Path)
    args = parser.parse_args()

    failures: list[str] = []
    for artifact in args.artifacts:
        if not artifact.is_file():
            failures.append(f"missing artifact: {artifact}")
            continue
        failures.extend(check(artifact))

    if failures:
        print("Distribution check failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    print(f"Checked {len(args.artifacts)} artifact(s); paths and text content passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
