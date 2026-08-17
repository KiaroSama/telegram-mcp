"""The merge policy in the feature doc, against the fork's real module list.

Not a message-view test - it reads the repository, not a message - so it moved
out of ``test_message_view.py`` when that file was split.
"""

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


# Spelled out because the document states the count in words, not digits.
_FORK_IMPORT_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six"}


def _fork_owned_paths():
    """Fork-authored modules under telegram_mcp/, straight from git.

    A fork file is one upstream does not have, so upstream is the authority
    rather than a list in this test. Skips where that cannot be established —
    a shallow clone, no upstream remote — instead of asserting against a guess.
    """
    import subprocess

    def _tracked(ref):
        result = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", ref, "telegram_mcp/"],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            pytest.skip(f"cannot resolve {ref}: {result.stderr.strip()[:120]}")
        return {line for line in result.stdout.splitlines() if line.endswith(".py")}

    fork_only = _tracked("HEAD") - _tracked("upstream/main")
    # tools/*.py are covered by the derived import block above.
    return sorted(p for p in fork_only if not p.startswith("telegram_mcp/tools/"))


def test_the_merge_contract_matches_the_fork_imports():
    """The merge policy block is what someone reads before `git merge upstream/main`.
    A fork module missing from it reads as upstream code, so it is derived from the
    imports rather than trusted: both the module list and the count come from
    tools/__init__.py, never from a number written into this test."""
    init = (REPO / "telegram_mcp" / "tools" / "__init__.py").read_text(encoding="utf-8")
    _, marker, fork_block = init.partition("# Fork additions")
    assert marker, "the fork import block lost its '# Fork additions' marker"

    modules = re.findall(r"^from telegram_mcp\.tools\.(\w+) import \*", fork_block, re.M)
    assert modules, "no fork tool imports found below the marker"

    doc = (REPO / "docs" / "visual-structured-access.md").read_text(encoding="utf-8")
    for name in modules:
        assert f"telegram_mcp/tools/{name}.py" in doc, f"merge policy omits tools/{name}.py"

    # Fork modules that are not tool imports cannot be derived from the block
    # above. Listing them by hand is what let text_fidelity.py and
    # media_transfer.py go missing when message_view.py and tools/inspection.py
    # were split, so ask git which files upstream does not have.
    for path in _fork_owned_paths():
        assert path in doc, f"merge policy omits {path}"

    word = _FORK_IMPORT_WORDS.get(len(modules))
    assert word, f"extend _FORK_IMPORT_WORDS: the fork now has {len(modules)} imports"
    assert (
        f"({word} import lines)" in doc
    ), f"the doc does not say '{word} import lines' for {len(modules)} fork imports"

    # Prove the check bites: this is exactly how tools/effects.py went missing.
    assert "telegram_mcp/tools/nonexistent.py" not in doc
