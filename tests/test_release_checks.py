from __future__ import annotations

import base64
import csv
import hashlib
import importlib.util
import io
import shutil
import stat
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from types import ModuleType

import pytest


def _load_script(name: str) -> ModuleType:
    script_path = Path(__file__).parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


check_repository_data = _load_script("check_repository_data")
check_distribution = _load_script("check_distribution")
PROJECT_ROOT = Path(__file__).parents[1]


def _project_legal_file(filename: str) -> bytes:
    return (PROJECT_ROOT / filename).read_bytes()


def _raw_vin() -> str:
    return "".join(("1HG", "CM826", "33A004352"))


def _grouped_vin(separator: str) -> str:
    raw = _raw_vin()
    return separator.join((raw[:3], raw[3:8], raw[8:11], raw[11:]))


def _private_key_header() -> str:
    return "-----BEGIN " + "PRIVATE KEY-----"


def _safe_wheel_entries() -> dict[str, bytes]:
    return {
        "obd_mcp/__init__.py": b'"""Safe package."""\n',
        "obd_mcp/server.py": b"def main() -> None:\n    return None\n",
        "obd_mcp/py.typed": b"",
        "obd_mcp_server-0.1.0.dist-info/METADATA": (
            b"Metadata-Version: 2.4\n"
            b"Name: obd-mcp-server\n"
            b"Version: 0.1.0\n"
            b"License-Expression: Apache-2.0 OR MIT\n"
            b"License-File: LICENSE\n"
            b"License-File: LICENSE-MIT\n"
            b"License-File: NOTICE\n"
            b"License-File: THIRD_PARTY_NOTICES.md\n"
        ),
        "obd_mcp_server-0.1.0.dist-info/WHEEL": b"Wheel-Version: 1.0\n",
        "obd_mcp_server-0.1.0.dist-info/entry_points.txt": (
            b"[console_scripts]\nobd-mcp = obd_mcp.cli:main\n"
        ),
        "obd_mcp_server-0.1.0.dist-info/licenses/LICENSE": _project_legal_file("LICENSE"),
        "obd_mcp_server-0.1.0.dist-info/licenses/LICENSE-MIT": _project_legal_file("LICENSE-MIT"),
        "obd_mcp_server-0.1.0.dist-info/licenses/NOTICE": _project_legal_file("NOTICE"),
        "obd_mcp_server-0.1.0.dist-info/licenses/THIRD_PARTY_NOTICES.md": (
            _project_legal_file("THIRD_PARTY_NOTICES.md")
        ),
        "obd_mcp_server-0.1.0.dist-info/RECORD": b"",
    }


def _write_wheel(
    path: Path,
    entries: dict[str, bytes],
    *,
    populate_record: bool = True,
) -> None:
    content = dict(entries)
    if populate_record:
        record_names = [name for name in content if name.endswith(".dist-info/RECORD")]
        if len(record_names) != 1:
            raise ValueError("test wheel needs exactly one RECORD")
        record_name = record_names[0]
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\n")
        for name, data in content.items():
            if name == record_name:
                continue
            digest = "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(
                b"="
            ).decode("ascii")
            writer.writerow((name, digest, len(data)))
        writer.writerow((record_name, "", ""))
        content[record_name] = output.getvalue().encode()

    with zipfile.ZipFile(path, "w") as archive:
        for name, data in content.items():
            archive.writestr(name, data)


def _write_sdist(path: Path, entries: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


def _safe_sdist_entries(prefix: str = "obd_mcp_server-0.1.0") -> dict[str, bytes]:
    return {
        f"{prefix}/.gitignore": b".venv/\ndist/\n",
        f"{prefix}/PKG-INFO": (
            b"Metadata-Version: 2.4\n"
            b"Name: obd-mcp-server\n"
            b"Version: 0.1.0\n"
            b"License-Expression: Apache-2.0 OR MIT\n"
            b"License-File: LICENSE\n"
            b"License-File: LICENSE-MIT\n"
            b"License-File: NOTICE\n"
            b"License-File: THIRD_PARTY_NOTICES.md\n"
        ),
        f"{prefix}/pyproject.toml": b'[build-system]\nbuild-backend = "hatchling.build"\n',
        f"{prefix}/README.md": b"# OBD MCP Server\n",
        f"{prefix}/LICENSE": _project_legal_file("LICENSE"),
        f"{prefix}/LICENSE-MIT": _project_legal_file("LICENSE-MIT"),
        f"{prefix}/NOTICE": _project_legal_file("NOTICE"),
        f"{prefix}/THIRD_PARTY_NOTICES.md": _project_legal_file("THIRD_PARTY_NOTICES.md"),
        f"{prefix}/src/obd_mcp/__init__.py": b'"""Safe package."""\n',
        f"{prefix}/src/obd_mcp/py.typed": b"",
        f"{prefix}/docs/safety.md": b"# Safety\n",
        f"{prefix}/examples/obd-mcp.toml": b'[server]\ntransport = "stdio"\n',
        f"{prefix}/scripts/check_distribution.py": b'"""Release checker."""\n',
    }


def test_repository_paths_include_tracked_and_untracked_but_not_ignored(tmp_path: Path) -> None:
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is unavailable")

    subprocess.run([git, "init", "-q"], cwd=tmp_path, check=True)  # noqa: S603
    (tmp_path / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (tmp_path / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    (tmp_path / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("ignored\n", encoding="utf-8")
    subprocess.run(  # noqa: S603
        [git, "add", ".gitignore", "tracked.txt"],
        cwd=tmp_path,
        check=True,
    )

    paths = check_repository_data.repository_paths(tmp_path)

    assert PurePosixPath(".gitignore") in paths
    assert PurePosixPath("tracked.txt") in paths
    assert PurePosixPath("untracked.txt") in paths
    assert PurePosixPath("ignored.txt") not in paths


def test_repository_scanner_accepts_safe_utf8_and_binary_files(tmp_path: Path) -> None:
    (tmp_path / "safe.md").write_text(
        "Synthetic examples contain no vehicle identifiers or credentials.\n",
        encoding="utf-8",
    )
    (tmp_path / "image.bin").write_bytes(b"\xff\xd8\xff\x00")

    failures = check_repository_data.check_repository_files(
        tmp_path,
        [PurePosixPath("safe.md"), PurePosixPath("image.bin")],
    )

    assert failures == []


@pytest.mark.parametrize(
    ("filename", "content", "category"),
    [
        ("vin.txt", lambda: f"vehicle={_raw_vin()}", "raw VIN-like identifier"),
        ("lowercase-vin.txt", lambda: _raw_vin().lower(), "raw VIN-like identifier"),
        (
            "numeric-vin.txt",
            lambda: "".join(("12345678", "901234567")),
            "raw VIN-like identifier",
        ),
        ("hyphen-vin.txt", lambda: _grouped_vin("-"), "raw VIN-like identifier"),
        ("underscore-vin.txt", lambda: _grouped_vin("_"), "raw VIN-like identifier"),
        ("whitespace-vin.txt", lambda: _grouped_vin(" "), "raw VIN-like identifier"),
        ("private.txt", _private_key_header, "private key header"),
        ("aws.txt", lambda: "AKIA" + ("A" * 16), "AWS access key"),
        ("github.txt", lambda: "ghp_" + ("a" * 36), "GitHub token"),
        ("openai.txt", lambda: "sk-" + ("a" * 24), "OpenAI-style token"),
    ],
)
def test_repository_scanner_rejects_high_confidence_sensitive_text(
    tmp_path: Path,
    filename: str,
    content: object,
    category: str,
) -> None:
    value = content()
    assert isinstance(value, str)
    (tmp_path / filename).write_text(value, encoding="utf-8")

    failures = check_repository_data.check_repository_files(
        tmp_path,
        [PurePosixPath(filename)],
    )

    assert any(category in failure for failure in failures)
    assert all(_raw_vin() not in failure for failure in failures)


def test_repository_scanner_rejects_sensitive_filenames_and_key_suffixes(
    tmp_path: Path,
) -> None:
    token_name = "AKIA" + ("B" * 16) + ".txt"
    (tmp_path / token_name).write_text("not secret content\n", encoding="utf-8")
    (tmp_path / "client.key").write_text("not a credential\n", encoding="utf-8")

    failures = check_repository_data.check_repository_files(
        tmp_path,
        [PurePosixPath(token_name), PurePosixPath("client.key")],
    )

    assert any("filename contains AWS access key" in failure for failure in failures)
    assert any("risky key/certificate file type" in failure for failure in failures)


def test_bundled_profile_check_uses_authoritative_profile_loader(tmp_path: Path) -> None:
    source = Path(__file__).parents[1] / "examples/profiles/synthetic-powertrain.toml"
    valid_root = tmp_path / "valid"
    valid_root.mkdir()
    shutil.copy2(source, valid_root / source.name)
    assert check_repository_data.check_bundled_profiles(valid_root) == []

    invalid_root = tmp_path / "invalid"
    invalid_root.mkdir()
    (invalid_root / "unsafe.toml").write_text(
        """
schema_version = 1
profile_id = "unsafe.example"
name = "Unsafe example"
version = "1.0.0"

[provenance]
source = "synthetic"
origin = "Generated test data"
license = "CC0-1.0"
redistribution_allowed = true
confidence = 1.0

[[reads]]
name = "Mutable operation"
ecu_id = "engine"
service = "0x2E"
identifier = "0xF40D"
signal_id = "unsafe"

[reads.decoder]
data_type = "uint8"
""".strip(),
        encoding="utf-8",
    )

    failures = check_repository_data.check_bundled_profiles(invalid_root)

    assert len(failures) == 1
    assert "read-only UDS" in failures[0]


def test_distribution_scanner_accepts_allowlisted_wheel(tmp_path: Path) -> None:
    wheel = tmp_path / "obd_mcp_server-0.1.0-py3-none-any.whl"
    _write_wheel(wheel, _safe_wheel_entries())

    assert check_distribution.check(wheel) == []


def test_distribution_scanner_requires_all_project_licenses_in_wheel(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "obd_mcp_server-0.1.0-py3-none-any.whl"
    entries = _safe_wheel_entries()
    del entries["obd_mcp_server-0.1.0.dist-info/licenses/LICENSE-MIT"]
    _write_wheel(wheel, entries)

    failures = check_distribution.check(wheel)

    assert any("missing required project license file(s): LICENSE-MIT" in item for item in failures)


def test_distribution_scanner_rejects_decoy_dist_info_license(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "obd_mcp_server-0.1.0-py3-none-any.whl"
    entries = _safe_wheel_entries()
    del entries["obd_mcp_server-0.1.0.dist-info/licenses/LICENSE-MIT"]
    entries["decoy-9.9.9.dist-info/licenses/LICENSE-MIT"] = b"MIT License\n"
    _write_wheel(wheel, entries)

    failures = check_distribution.check(wheel)

    assert any("exactly one .dist-info directory" in item for item in failures)
    assert any("missing required project license file(s): LICENSE-MIT" in item for item in failures)


def test_distribution_scanner_requires_dual_license_metadata_in_wheel(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "obd_mcp_server-0.1.0-py3-none-any.whl"
    entries = _safe_wheel_entries()
    metadata_path = "obd_mcp_server-0.1.0.dist-info/METADATA"
    entries[metadata_path] = entries[metadata_path].replace(
        b"License-Expression: Apache-2.0 OR MIT\n",
        b"License-Expression: Apache-2.0\n",
    )
    _write_wheel(wheel, entries)

    failures = check_distribution.check(wheel)

    assert any("License-Expression must be exactly" in item for item in failures)


def test_distribution_scanner_requires_core_metadata_24_in_wheel(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "obd_mcp_server-0.1.0-py3-none-any.whl"
    entries = _safe_wheel_entries()
    metadata_path = "obd_mcp_server-0.1.0.dist-info/METADATA"
    entries[metadata_path] = entries[metadata_path].replace(
        b"Metadata-Version: 2.4\n",
        b"Metadata-Version: 2.3\n",
    )
    _write_wheel(wheel, entries)

    failures = check_distribution.check(wheel)

    assert any("Metadata-Version must be 2.4 or newer" in item for item in failures)


def test_distribution_scanner_rejects_legacy_license_classifier_in_wheel(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "obd_mcp_server-0.1.0-py3-none-any.whl"
    entries = _safe_wheel_entries()
    metadata_path = "obd_mcp_server-0.1.0.dist-info/METADATA"
    entries[metadata_path] += b"Classifier: License :: OSI Approved :: MIT License\n"
    _write_wheel(wheel, entries)

    failures = check_distribution.check(wheel)

    assert any("legacy License classifiers are not allowed" in item for item in failures)


def test_distribution_scanner_matches_dist_info_to_wheel_identity(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "obd_mcp_server-0.1.0-py3-none-any.whl"
    entries = {
        name.replace(
            "obd_mcp_server-0.1.0.dist-info",
            "unrelated-9.9.dist-info",
        ): data
        for name, data in _safe_wheel_entries().items()
    }
    _write_wheel(wheel, entries)

    failures = check_distribution.check(wheel)

    assert any(
        "metadata directory must be obd_mcp_server-0.1.0.dist-info" in item for item in failures
    )


def test_distribution_scanner_rejects_truncated_project_license_in_wheel(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "obd_mcp_server-0.1.0-py3-none-any.whl"
    entries = _safe_wheel_entries()
    entries["obd_mcp_server-0.1.0.dist-info/licenses/LICENSE-MIT"] = b"MIT License\n"
    _write_wheel(wheel, entries)

    failures = check_distribution.check(wheel)

    assert any("LICENSE-MIT: packaged content does not match" in item for item in failures)


def test_distribution_scanner_rejects_unexpected_wheel_path_and_sensitive_content(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "obd_mcp_server-0.1.0-py3-none-any.whl"
    entries = _safe_wheel_entries()
    entries["obd_mcp/private-data.json"] = b"{}\n"
    entries["obd_mcp/leak.py"] = f'VIN = "{_raw_vin()}"\n'.encode()
    _write_wheel(wheel, entries)

    failures = check_distribution.check(wheel)

    assert any(
        "private-data.json: path is not in the artifact allowlist" in item for item in failures
    )
    assert any("leak.py: contains raw VIN-like identifier" in item for item in failures)
    assert all(_raw_vin() not in failure for failure in failures)


@pytest.mark.parametrize("separator", ["-", "_", " "])
def test_distribution_scanner_rejects_grouped_vins_in_wheels(
    tmp_path: Path,
    separator: str,
) -> None:
    wheel = tmp_path / "obd_mcp_server-0.1.0-py3-none-any.whl"
    entries = _safe_wheel_entries()
    entries["obd_mcp/leak.py"] = f'IDENTIFIER = "{_grouped_vin(separator)}"\n'.encode()
    _write_wheel(wheel, entries)

    failures = check_distribution.check(wheel)

    assert any("leak.py: contains raw VIN-like identifier" in item for item in failures)
    assert all(_raw_vin() not in failure for failure in failures)


@pytest.mark.parametrize("separator", ["-", "_", " "])
def test_distribution_scanner_rejects_grouped_vins_in_sdists(
    tmp_path: Path,
    separator: str,
) -> None:
    sdist = tmp_path / "obd_mcp_server-0.1.0.tar.gz"
    entries = _safe_sdist_entries()
    prefix = "obd_mcp_server-0.1.0"
    entries[f"{prefix}/docs/leak.md"] = _grouped_vin(separator).encode()
    _write_sdist(sdist, entries)

    failures = check_distribution.check(sdist)

    assert any("leak.md: contains raw VIN-like identifier" in item for item in failures)
    assert all(_raw_vin() not in failure for failure in failures)


def test_distribution_scanner_checks_member_names_and_key_suffixes(tmp_path: Path) -> None:
    wheel = tmp_path / "obd_mcp_server-0.1.0-py3-none-any.whl"
    entries = _safe_wheel_entries()
    entries[f"obd_mcp/{_raw_vin()}.py"] = b"SAFE = True\n"
    entries["obd_mcp/client.key"] = b"not a real key\n"
    _write_wheel(wheel, entries)

    failures = check_distribution.check(wheel)

    assert any("filename contains raw VIN-like identifier" in item for item in failures)
    assert any("risky key/certificate file type" in item for item in failures)


def test_distribution_scanner_rejects_invalid_wheel_record(tmp_path: Path) -> None:
    wheel = tmp_path / "obd_mcp_server-0.1.0-py3-none-any.whl"
    entries = _safe_wheel_entries()
    entries["obd_mcp_server-0.1.0.dist-info/RECORD"] = b"obd_mcp/__init__.py,sha256=invalid,999\n"
    _write_wheel(wheel, entries, populate_record=False)

    failures = check_distribution.check(wheel)

    assert any("digest mismatch" in item for item in failures)
    assert any("size mismatch" in item for item in failures)
    assert any("missing row" in item for item in failures)


def test_distribution_scanner_accepts_allowlisted_normalized_sdist(tmp_path: Path) -> None:
    sdist = tmp_path / "obd_mcp_server-0.1.0.tar.gz"
    _write_sdist(sdist, _safe_sdist_entries())

    assert check_distribution.check(sdist) == []


def test_distribution_scanner_requires_all_project_licenses_in_sdist(
    tmp_path: Path,
) -> None:
    sdist = tmp_path / "obd_mcp_server-0.1.0.tar.gz"
    entries = _safe_sdist_entries()
    del entries["obd_mcp_server-0.1.0/LICENSE-MIT"]
    _write_sdist(sdist, entries)

    failures = check_distribution.check(sdist)

    assert any("missing required project license file(s): LICENSE-MIT" in item for item in failures)


def test_distribution_scanner_requires_license_file_metadata_in_sdist(
    tmp_path: Path,
) -> None:
    sdist = tmp_path / "obd_mcp_server-0.1.0.tar.gz"
    entries = _safe_sdist_entries()
    metadata_path = "obd_mcp_server-0.1.0/PKG-INFO"
    entries[metadata_path] = entries[metadata_path].replace(
        b"License-File: LICENSE-MIT\n",
        b"",
    )
    _write_sdist(sdist, entries)

    failures = check_distribution.check(sdist)

    assert any("License-File fields must list exactly" in item for item in failures)


def test_distribution_scanner_rejects_legacy_license_field_in_sdist(
    tmp_path: Path,
) -> None:
    sdist = tmp_path / "obd_mcp_server-0.1.0.tar.gz"
    entries = _safe_sdist_entries()
    metadata_path = "obd_mcp_server-0.1.0/PKG-INFO"
    entries[metadata_path] += b"License: Apache-2.0\n"
    _write_sdist(sdist, entries)

    failures = check_distribution.check(sdist)

    assert any("legacy License field is not allowed" in item for item in failures)


def test_distribution_scanner_rejects_truncated_project_license_in_sdist(
    tmp_path: Path,
) -> None:
    sdist = tmp_path / "obd_mcp_server-0.1.0.tar.gz"
    entries = _safe_sdist_entries()
    entries["obd_mcp_server-0.1.0/LICENSE"] = b"Apache License\n"
    _write_sdist(sdist, entries)

    failures = check_distribution.check(sdist)

    assert any("LICENSE: packaged content does not match" in item for item in failures)


def test_distribution_scanner_rejects_unallowlisted_and_mixed_prefix_sdist(
    tmp_path: Path,
) -> None:
    unexpected = tmp_path / "unexpected.tar.gz"
    entries = _safe_sdist_entries()
    entries["obd_mcp_server-0.1.0/AGENTS.md"] = b"private build instructions\n"
    _write_sdist(unexpected, entries)
    failures = check_distribution.check(unexpected)
    assert any("AGENTS.md: path is not in the artifact allowlist" in item for item in failures)

    mixed = tmp_path / "mixed.tar.gz"
    mixed_entries = _safe_sdist_entries()
    mixed_entries["other-prefix/README.md"] = b"unexpected\n"
    _write_sdist(mixed, mixed_entries)
    mixed_failures = check_distribution.check(mixed)
    assert any("exactly one top-level directory" in item for item in mixed_failures)


def test_distribution_scanner_allows_only_root_gitignore(tmp_path: Path) -> None:
    sdist = tmp_path / "nested-ignore.tar.gz"
    entries = _safe_sdist_entries()
    entries["obd_mcp_server-0.1.0/docs/.gitignore"] = b"private/\n"
    _write_sdist(sdist, entries)

    failures = check_distribution.check(sdist)

    assert any(
        "docs/.gitignore: path is not in the artifact allowlist" in item for item in failures
    )


def test_distribution_scanner_rejects_tar_symlink(tmp_path: Path) -> None:
    sdist = tmp_path / "links.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        info = tarfile.TarInfo("obd_mcp_server-0.1.0/src/obd_mcp/link.py")
        info.type = tarfile.SYMTYPE
        info.linkname = "../../private.py"
        archive.addfile(info)

    failures = check_distribution.check(sdist)

    assert any("non-regular archive member" in item for item in failures)


def test_distribution_scanner_rejects_zip_nonregular_member(tmp_path: Path) -> None:
    wheel = tmp_path / "obd_mcp_server-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        info = zipfile.ZipInfo("obd_mcp/pipe.py")
        info.create_system = 3
        info.external_attr = (stat.S_IFIFO | 0o600) << 16
        archive.writestr(info, b"")

    failures = check_distribution.check(wheel)

    assert any("non-regular archive member" in item for item in failures)
