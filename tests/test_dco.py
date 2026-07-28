from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType

import pytest


def _load_script() -> ModuleType:
    script_path = Path(__file__).parents[1] / "scripts" / "check_dco.py"
    spec = importlib.util.spec_from_file_location("check_dco", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load DCO check")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_dco = _load_script()


def test_dco_requires_two_full_commit_shas(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert check_dco.main(["main", "head"]) == 2
    assert "usage:" in capsys.readouterr().err


def test_dco_reports_unsigned_commits(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base = "a" * 40
    head = "b" * 40

    monkeypatch.setattr(
        check_dco,
        "_load_commits",
        lambda _base, _head: [
            check_dco.Commit(
                sha=head,
                author_name="Example Contributor",
                author_email="contributor@example.org",
                message="Unsigned subject",
            )
        ],
    )

    assert check_dco.main([base, head]) == 1
    assert head[:12] in capsys.readouterr().err


def test_dco_accepts_a_valid_sign_off(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base = "a" * 40
    head = "b" * 40
    message = "Subject\n\nSigned-off-by: Example Contributor <contributor@example.org>"

    monkeypatch.setattr(
        check_dco,
        "_load_commits",
        lambda _base, _head: [
            check_dco.Commit(
                sha=head,
                author_name="Example Contributor",
                author_email="contributor@example.org",
                message=message,
            )
        ],
    )

    assert check_dco.main([base, head]) == 0
    assert capsys.readouterr().out == "DCO check passed\n"


def test_dco_rejects_signoff_from_a_different_identity(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base = "a" * 40
    head = "b" * 40
    monkeypatch.setattr(
        check_dco,
        "_load_commits",
        lambda _base, _head: [
            check_dco.Commit(
                sha=head,
                author_name="Actual Author",
                author_email="author@example.org",
                message=("Subject\n\nSigned-off-by: Different Person <different@example.org>"),
            )
        ],
    )

    assert check_dco.main([base, head]) == 1
    assert "matched to the author" in capsys.readouterr().err


def test_dco_accepts_standard_dependabot_signoff_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base = "a" * 40
    head = "b" * 40
    monkeypatch.setattr(
        check_dco,
        "_load_commits",
        lambda _base, _head: [
            check_dco.Commit(
                sha=head,
                author_name="dependabot[bot]",
                author_email="49699333+dependabot[bot]@users.noreply.github.com",
                message="Subject\n\nSigned-off-by: dependabot[bot] <support@github.com>",
            )
        ],
    )

    assert check_dco.main(["--allow-dependabot", base, head]) == 0
    assert capsys.readouterr().out == "DCO check passed\n"


def test_dco_rejects_dependabot_signoff_without_explicit_enablement(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base = "a" * 40
    head = "b" * 40
    monkeypatch.setattr(
        check_dco,
        "_load_commits",
        lambda _base, _head: [
            check_dco.Commit(
                sha=head,
                author_name="dependabot[bot]",
                author_email="49699333+dependabot[bot]@users.noreply.github.com",
                message="Subject\n\nSigned-off-by: dependabot[bot] <support@github.com>",
            )
        ],
    )

    assert check_dco.main([base, head]) == 1
    assert "matched to the author" in capsys.readouterr().err


def test_dco_still_rejects_unsigned_maintainer_commit_on_dependabot_pr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base = "a" * 40
    head = "b" * 40
    monkeypatch.setattr(
        check_dco,
        "_load_commits",
        lambda _base, _head: [
            check_dco.Commit(
                sha=head,
                author_name="Example Maintainer",
                author_email="maintainer@example.org",
                message="Unsigned maintainer change",
            )
        ],
    )

    assert check_dco.main(["--allow-dependabot", base, head]) == 1
    assert head[:12] in capsys.readouterr().err


def test_dco_loader_includes_merge_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = "a" * 40
    head = "b" * 40
    observed_command: list[str] = []
    stdout = f"{head}\x1fMerge Author\x1fmerge@example.org\x1fMerge branch 'main'\x1e"

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        observed_command.extend(command)
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(check_dco.shutil, "which", lambda _name: "/usr/bin/git")
    monkeypatch.setattr(check_dco.subprocess, "run", fake_run)

    commits = check_dco._load_commits(base, head)

    assert "--no-merges" not in observed_command
    assert commits[0].sha == head
    assert commits[0].message == "Merge branch 'main'"
