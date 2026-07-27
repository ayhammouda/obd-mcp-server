"""Dependency-free detection of canonical and reconstructable VIN text."""

from __future__ import annotations

import re

VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$", re.IGNORECASE)
_VIN_TOKEN_RE = re.compile(
    r"(?<![A-HJ-NPR-Z0-9])[A-HJ-NPR-Z0-9]{17}(?![A-HJ-NPR-Z0-9])",
    re.IGNORECASE,
)
_VIN_MARKER_RE = re.compile(
    r"(?<![A-Z0-9])VIN(?=$|[ \t:#=_-]|[A-HJ-NPR-Z0-9]{17}(?![A-HJ-NPR-Z0-9]))",
    re.IGNORECASE,
)
_VIN_MARKER_PREFIX_RE = re.compile(r"[ \t:#=_-]*")
_VIN_SYMBOL_RUN_RE = re.compile(r"[A-HJ-NPR-Z0-9_-]+", re.IGNORECASE)
_VIN_DELIMITED_CANDIDATE_RE = re.compile(
    r"(?<![A-HJ-NPR-Z0-9_-])[A-HJ-NPR-Z0-9_-]{18,}"
    r"(?![A-HJ-NPR-Z0-9_-])",
    re.IGNORECASE,
)
_VIN_WHITESPACE_WORD_RE = re.compile(r"[A-HJ-NPR-Z0-9]+", re.IGNORECASE)
_MIN_WHITESPACE_GROUPS = 3
_MAX_WHITESPACE_GROUPS = 6
_MIN_WHITESPACE_GROUP_LENGTH = 2
_MAX_WHITESPACE_GROUP_LENGTH = 8


def contains_raw_vin(value: str) -> bool:
    """Return whether text contains a canonical or reconstructable VIN token."""

    if _VIN_TOKEN_RE.search(value) is not None or _contains_marked_vin(value):
        return True

    for match in _VIN_DELIMITED_CANDIDATE_RE.finditer(value):
        candidate = match.group(0).strip("-_")
        normalized = candidate.replace("-", "").replace("_", "")
        if ("-" in candidate or "_" in candidate) and _is_structurally_plausible_grouped_vin(
            normalized
        ):
            return True

    return _contains_whitespace_grouped_vin(value)


def _contains_marked_vin(value: str) -> bool:
    """Detect candidates immediately following an explicit VIN label."""

    for marker in _VIN_MARKER_RE.finditer(value):
        tail = value[marker.end() : marker.end() + 96]
        prefix = _VIN_MARKER_PREFIX_RE.match(tail)
        if prefix is None:  # pragma: no cover - the expression always matches
            continue
        body = tail[prefix.end() :]

        canonical = re.match(r"[A-HJ-NPR-Z0-9]{17}(?![A-HJ-NPR-Z0-9])", body, re.I)
        if canonical is not None:
            return True

        symbol_run = _VIN_SYMBOL_RUN_RE.match(body)
        if symbol_run is not None:
            candidate = symbol_run.group(0).strip("-_")
            normalized = candidate.replace("-", "").replace("_", "")
            if ("-" in candidate or "_" in candidate) and VIN_RE.fullmatch(normalized):
                return True

        explicit_separator = any(character in ":#=" for character in prefix.group(0))
        if _contains_whitespace_grouped_vin(
            body,
            start_only=True,
            require_numeric_suffix=not explicit_separator,
        ):
            return True
    return False


def _is_structurally_plausible_grouped_vin(value: str) -> bool:
    """Use the numeric VIS suffix to avoid treating normal identifiers as VINs."""

    return VIN_RE.fullmatch(value) is not None and value[-4:].isdigit()


def _contains_whitespace_grouped_vin(
    value: str,
    *,
    start_only: bool = False,
    require_numeric_suffix: bool = True,
) -> bool:
    """Detect bounded VIN groups while avoiding ordinary prose-like matches."""

    words = tuple(_VIN_WHITESPACE_WORD_RE.finditer(value))
    for start, first in enumerate(words):
        if start_only and first.start() != 0:
            return False
        candidate_parts: list[str] = []
        previous = first
        for current in words[start : start + _MAX_WHITESPACE_GROUPS]:
            if current is not first:
                separator = value[previous.end() : current.start()]
                if not separator or any(character not in " \t" for character in separator):
                    break
            part = current.group(0)
            if not (_MIN_WHITESPACE_GROUP_LENGTH <= len(part) <= _MAX_WHITESPACE_GROUP_LENGTH):
                break
            candidate_parts.append(part)
            previous = current
            candidate = "".join(candidate_parts)
            if len(candidate) > 17:
                break
            if (
                len(candidate_parts) >= _MIN_WHITESPACE_GROUPS
                and VIN_RE.fullmatch(candidate)
                and (not require_numeric_suffix or candidate[-4:].isdigit())
            ):
                return True
        if start_only:
            return False
    return False
