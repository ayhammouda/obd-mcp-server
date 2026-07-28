#!/usr/bin/env python3
"""Check repository paths, text content, and bundled profiles before release."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

from obd_mcp.vin import contains_raw_vin

MAX_TEXT_FILE_BYTES = 5 * 1024 * 1024

FORBIDDEN_REPOSITORY_PARTS = {
    ".env",
    ".git",
    ".idea",
    ".pytest_cache",
    ".venv",
    ".vscode",
    "__pycache__",
    "captures",
    "licensed",
    "private",
}
FORBIDDEN_REPOSITORY_SUFFIXES = {
    ".asc",
    ".blf",
    ".db",
    ".log",
    ".pcap",
    ".sqlite",
    ".sqlite3",
}
RISKY_KEY_OR_CERT_SUFFIXES = {
    ".cer",
    ".crt",
    ".der",
    ".jks",
    ".key",
    ".keystore",
    ".p12",
    ".pem",
    ".pfx",
}
RISKY_KEY_FILENAMES = {
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
}

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private key header",
        re.compile(
            r"-----BEGIN (?:DSA |EC |ENCRYPTED |OPENSSH |PGP |RSA )?"
            r"PRIVATE KEY(?: BLOCK)?-----"
        ),
    ),
    ("AWS access key", re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])")),
    (
        "GitHub token",
        re.compile(r"(?<![A-Za-z0-9])(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})"),
    ),
    ("GitLab token", re.compile(r"(?<![A-Za-z0-9])glpat-[A-Za-z0-9_-]{20,}")),
    ("Google API key", re.compile(r"(?<![A-Za-z0-9])AIza[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])")),
    ("npm token", re.compile(r"(?<![A-Za-z0-9])npm_[A-Za-z0-9]{36}(?![A-Za-z0-9])")),
    (
        "OpenAI-style token",
        re.compile(r"(?<![A-Za-z0-9])sk-(?:proj-)?[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])"),
    ),
    ("PyPI token", re.compile(r"(?<![A-Za-z0-9])pypi-[A-Za-z0-9_-]{50,}")),
    ("Slack token", re.compile(r"(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{20,}")),
    ("Stripe live secret", re.compile(r"(?<![A-Za-z0-9])sk_live_[A-Za-z0-9]{16,}")),
)


def repository_paths(root: Path = Path()) -> list[PurePosixPath]:
    """Return files that are tracked or would be added by a normal commit."""

    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git executable not found")
    result = subprocess.run(  # noqa: S603 - executable resolved above; arguments are fixed.
        [git, "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        check=True,
        capture_output=True,
        cwd=root,
    )
    try:
        return [PurePosixPath(raw.decode("utf-8")) for raw in result.stdout.split(b"\0") if raw]
    except UnicodeDecodeError as error:
        raise RuntimeError("repository contains a non-UTF-8 path") from error


def unsafe_path_reason(path: PurePosixPath) -> str | None:
    """Return a release-safety reason for a risky repository or archive path."""

    if path.is_absolute() or ".." in path.parts or "\\" in path.as_posix():
        return "unsafe or non-portable path"
    folded_parts = {part.casefold() for part in path.parts}
    if folded_parts & FORBIDDEN_REPOSITORY_PARTS:
        return "private/capture path"
    folded_name = path.name.casefold()
    if folded_name.startswith(".env"):
        return "environment file"
    if folded_name in RISKY_KEY_FILENAMES:
        return "private-key filename"
    suffix = path.suffix.casefold()
    if suffix in RISKY_KEY_OR_CERT_SUFFIXES:
        return "risky key/certificate file type"
    if suffix in FORBIDDEN_REPOSITORY_SUFFIXES:
        return "capture/database file type"
    return None


def sensitive_text_findings(text: str) -> list[str]:
    """Identify high-confidence secrets and raw VIN-like identifiers.

    Findings intentionally contain only category names, never matched material.
    """

    findings: list[str] = []
    if contains_raw_vin(text):
        findings.append("raw VIN-like identifier")
    for label, pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            findings.append(label)
    return findings


def sensitive_bytes_findings(data: bytes) -> list[str]:
    """Scan UTF-8 text bytes; binary/non-UTF-8 data is intentionally ignored."""

    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return []
    return sensitive_text_findings(text)


def check_tracked(paths: list[PurePosixPath]) -> list[str]:
    """Check repository path names without reading their contents."""

    failures: list[str] = []
    for path in paths:
        reason = unsafe_path_reason(path)
        if reason is not None:
            failures.append(f"{path}: {reason} must not be tracked")
        failures.extend(
            f"{path}: filename contains {finding}"
            for finding in sensitive_text_findings(path.as_posix())
        )
    return failures


def check_repository_files(root: Path, paths: list[PurePosixPath]) -> list[str]:
    """Check names and UTF-8 contents for all candidate repository files."""

    failures = check_tracked(paths)
    for relative_path in sorted(set(paths), key=str):
        path = root.joinpath(*relative_path.parts)
        try:
            if path.is_symlink():
                failures.append(f"{relative_path}: symbolic links are not release candidates")
                continue
            if not path.is_file():
                failures.append(f"{relative_path}: repository candidate is not a regular file")
                continue
            size = path.stat().st_size
            if size > MAX_TEXT_FILE_BYTES:
                failures.append(
                    f"{relative_path}: exceeds the {MAX_TEXT_FILE_BYTES}-byte content scan limit"
                )
                continue
            data = path.read_bytes()
        except OSError as error:
            failures.append(f"{relative_path}: cannot scan repository candidate: {error}")
            continue
        for finding in sensitive_bytes_findings(data):
            failures.append(f"{relative_path}: contains {finding}")
    return failures


def check_bundled_profiles(root: Path) -> list[str]:
    """Validate published profiles with the runtime's authoritative loader."""

    if not root.exists():
        return []

    # Delay the project import so check_distribution.py can reuse the content
    # scanner even when only the sdist scripts are on sys.path.
    from obd_mcp.errors import ProfileValidationError
    from obd_mcp.profiles import ProfileLoader

    failures: list[str] = []
    loader = ProfileLoader()
    candidates = sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.casefold() in {".json", ".toml"}
    )
    for path in candidates:
        try:
            loader.load_path(path, bundled=True)
        except ProfileValidationError:
            failures.append(
                f"{path}: profile validation failed; run obd-mcp check-config locally for details"
            )
    return failures


def main() -> int:
    root = Path.cwd()
    failures = check_repository_files(root, repository_paths(root))
    for profile_root in (root / "profiles/bundled", root / "examples/profiles"):
        failures.extend(check_bundled_profiles(profile_root))

    if failures:
        print("Repository data check failed:", file=sys.stderr)
        print(
            "Finding details are withheld because candidate paths and parser errors may "
            "contain sensitive data.",
            file=sys.stderr,
        )
        return 1

    print("Repository paths, text content, and bundled profiles passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
