#!/usr/bin/env python3
"""Require a Developer Certificate of Origin sign-off on each PR commit."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from typing import NamedTuple

_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_SIGN_OFF_PATTERN = re.compile(
    r"(?im)^Signed-off-by:\s*(?P<name>[^<\r\n]+?)\s*"
    r"<(?P<email>[^<>\s]+@[^<>\s]+)>\s*$"
)
_RECORD_SEPARATOR = "\x1e"
_FIELD_SEPARATOR = "\x1f"


class Commit(NamedTuple):
    sha: str
    author_name: str
    author_email: str
    message: str


def _load_commits(base_sha: str, head_sha: str) -> list[Commit]:
    revision_range = f"{base_sha}..{head_sha}"
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git executable is required")
    result = subprocess.run(  # noqa: S603
        [
            git,
            "log",
            (
                f"--format=%H{_FIELD_SEPARATOR}%an{_FIELD_SEPARATOR}"
                f"%ae{_FIELD_SEPARATOR}%B{_RECORD_SEPARATOR}"
            ),
            revision_range,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    commits: list[Commit] = []
    for record in result.stdout.split(_RECORD_SEPARATOR):
        record = record.strip()
        if not record:
            continue
        fields = record.split(_FIELD_SEPARATOR, 3)
        if len(fields) != 4:
            raise RuntimeError("git returned an unexpected commit record")
        commits.append(
            Commit(
                sha=fields[0],
                author_name=fields[1],
                author_email=fields[2],
                message=fields[3],
            )
        )
    return commits


def _has_matching_author_signoff(commit: Commit) -> bool:
    expected_name = " ".join(commit.author_name.split()).casefold()
    expected_email = commit.author_email.strip().casefold()
    for match in _SIGN_OFF_PATTERN.finditer(commit.message):
        signer_name = " ".join(match.group("name").split()).casefold()
        signer_email = match.group("email").strip().casefold()
        if signer_name == expected_name and signer_email == expected_email:
            return True
    return False


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or any(_SHA_PATTERN.fullmatch(value) is None for value in args):
        print("usage: check_dco.py BASE_SHA HEAD_SHA", file=sys.stderr)
        return 2

    unsigned = [
        commit.sha
        for commit in _load_commits(args[0], args[1])
        if not _has_matching_author_signoff(commit)
    ]
    if unsigned:
        print(
            "DCO sign-off missing or not matched to the author for commit(s): "
            + ", ".join(commit_sha[:12] for commit_sha in unsigned),
            file=sys.stderr,
        )
        print(
            "Amend each commit with a Signed-off-by trailer (git commit -s).",
            file=sys.stderr,
        )
        return 1

    print("DCO check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
