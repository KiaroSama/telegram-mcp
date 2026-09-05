"""The secret gate must refuse when it cannot look, not pass because it did not.

`scripts/secret_scan.py` is the last thing standing between a credential and a
public repository, and it used to fail open. `tracked_files` ran git with
`check=False` and read stdout regardless; every way git can fail - not a
repository, a held `index.lock`, git absent from PATH - produces empty stdout.
That became an empty file list, which scanned clean, which exited 0. Measured
before the fix, in a directory holding a file with an API-hash shape:

    secret scan: 0 file(s) clean
    EXIT=0

Nothing had been examined. The tests here pin the distinction the old code could
not make: an empty list because nothing is staged is a pass, and an empty list
because git never answered is a refusal.
"""

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

# The scanner is a script rather than a package module, so the suite reaches it
# the way the shell and the pre-commit hook do.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import secret_scan  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
SCANNER = REPO / "scripts" / "secret_scan.py"
SCRIPTS = REPO / "scripts"


def _git_returning(monkeypatch, **outcome):
    """Replace the git call with a fixed outcome, so the test does not depend on
    the state of any real checkout."""
    recorded = {}

    def _run(command, **kwargs):
        recorded["command"] = command
        recorded["kwargs"] = kwargs
        if "raises" in outcome:
            raise outcome["raises"]
        return subprocess.CompletedProcess(
            command,
            outcome.get("returncode", 0),
            outcome.get("stdout", ""),
            outcome.get("stderr", ""),
        )

    monkeypatch.setattr(secret_scan.subprocess, "run", _run)
    return recorded


def test_a_git_failure_refuses_rather_than_reporting_a_clean_tree(monkeypatch):
    """The exact fail-open. Exit 128 with empty stdout used to become "0 file(s)
    clean"; the only honest answer is that the question could not be asked."""
    _git_returning(monkeypatch, returncode=128, stderr="fatal: not a git repository")

    with pytest.raises(secret_scan.ScanUnavailable, match="128"):
        secret_scan.tracked_files(staged=False)


def test_git_missing_from_path_refuses_too(monkeypatch):
    """A gate that cannot find git has not cleared anything."""
    _git_returning(monkeypatch, raises=FileNotFoundError("git"))

    with pytest.raises(secret_scan.ScanUnavailable, match="could not be run"):
        secret_scan.tracked_files(staged=False)


def test_a_hung_git_is_bounded_rather_than_waited_on(monkeypatch):
    """A credential helper that prompts, or a held index lock, blocks git for as
    long as it likes. Unbounded that is a gate which never reports at all, which
    is worse than one that fails."""
    recorded = _git_returning(monkeypatch, raises=subprocess.TimeoutExpired(cmd="git", timeout=60))

    with pytest.raises(secret_scan.ScanUnavailable, match="did not answer"):
        secret_scan.tracked_files(staged=False)

    assert recorded["kwargs"].get("timeout"), "the git call was made with no timeout"


def test_nothing_staged_is_still_a_legitimate_pass(monkeypatch):
    """The distinction the fix turns on. An empty answer from a git that WORKED
    means there is nothing to scan - turning that into a refusal would fail every
    clean commit."""
    _git_returning(monkeypatch, returncode=0, stdout="")

    assert secret_scan.tracked_files(staged=True) == []


def test_the_gate_exits_nonzero_when_it_cannot_build_the_file_list(tmp_path):
    """End to end, through the entry point the pre-commit hook actually calls."""
    # A KNOWN placeholder, not a novel credential shape. The scanner refused
    # this very file on CI when it first became tracked - which is the gate
    # working, and also the reason a new file is not covered locally: it is
    # untracked, `git ls-files` cannot see it, so the first real scan of it
    # happens after the commit. The content is incidental here anyway; the
    # point of the test is that NOTHING gets scanned.
    leak = tmp_path / "leak.py"
    leak.write_text(
        "TELEGRAM_API_HASH = '0123456789abcdef0123456789abcdef'",
        encoding="utf-8",
    )
    environment = dict(os.environ, GIT_DIR=str(tmp_path / "no-such-git-dir"))

    finished = subprocess.run(
        [sys.executable, str(SCANNER)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        env=environment,
    )

    assert finished.returncode == 1, finished.stdout + finished.stderr
    assert "COULD NOT RUN" in finished.stderr, finished.stderr
    assert "clean" not in finished.stdout, "it still claimed a clean tree"


def test_every_subprocess_run_under_scripts_carries_a_timeout():
    """Static, because the failure it guards against is a hang: a test that waits
    for one cannot report it. `scripts/` holds the gate and the test watchdog, so
    an unbounded call here is a tool that can stop answering with nothing to
    point at."""
    unbounded = []
    for script in sorted(SCRIPTS.glob("*.py")):
        tree = ast.parse(script.read_text(encoding="utf-8"), filename=str(script))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            # `subprocess.run` specifically, matched through the module rather
            # than the bare attribute: `asyncio.run` is also a `.run` and is not
            # a process call at all.
            if not isinstance(target, ast.Attribute) or target.attr != "run":
                continue
            module = target.value
            if not isinstance(module, ast.Name) or module.id != "subprocess":
                continue
            if not any(keyword.arg == "timeout" for keyword in node.keywords):
                unbounded.append(f"{script.name}:{node.lineno}")

    assert not unbounded, "subprocess.run with no timeout: " + ", ".join(unbounded)
