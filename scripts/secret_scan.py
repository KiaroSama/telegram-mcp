#!/usr/bin/env python3
"""Refuse to publish a credential, a private file, or a personal path.

This repository is public. That makes two different mistakes permanent rather than
embarrassing: a secret committed once stays reachable in every fork, and GitHub's
own guidance is explicit that neither the repository owner nor GitHub Support can
remove it from someone else's fork. So the check runs before the push, not after.

Three things are looked for, and they fail for different reasons:

* **Credential shapes** - a Telegram session string, an API hash, a bot token, a
  private key. Matched by shape rather than by name, because the variable a secret
  is assigned to is not what makes it a secret.
* **Protected paths** - `.env`, `secrets.md`, a session database, the agent
  directories. These are git-ignored, so this catches the day someone forces one
  in past the ignore rules.
* **Personal paths** - a home directory or a machine name is not a credential, but
  it is still something a public repository should not carry.

Placeholders are expected and must not fire: `.env.example` exists to show the
shape of a credential. They are allowed by VALUE, never by file, so a real secret
pasted into an example file is still caught.

Exit 0 when clean, 1 when not. `--staged` looks at what is about to be committed,
otherwise every tracked file is scanned.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# Values that are deliberately in the tree as examples. Allowed by value, so the
# same file carrying a REAL credential still fails.
KNOWN_PLACEHOLDERS = {
    "0123456789abcdef0123456789abcdef",
    "12345678901234567890123456789012",
    "abcdefabcdefabcdefabcdefabcdefab",
    "your_api_hash_here",
}

CREDENTIAL_PATTERNS = {
    "Telegram API hash": re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])"),
    # Deliberately looser than "exactly 35 after the colon". Tokens in the wild are
    # 34-35 and the length is not documented as fixed, so pinning it exactly made
    # this miss the commonest secret of all - which a guard that misses it is worse
    # than no guard, because it reads as coverage.
    "Telegram bot token": re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,45}\b"),
    "Telethon session string": re.compile(r"\b1[A-Za-z0-9+/_-]{200,}\b"),
    "private key block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
}

# A home directory or a machine name is not a credential, but a public repository
# should not carry one either. Documentation is full of example paths, so the NAME
# decides: a conventional stand-in is fine, a real account is not. Exempting whole
# files instead would have made this check meaningless in exactly the files most
# likely to carry a real path.
GENERIC_ACCOUNT_NAMES = {
    "user",
    "users",
    "you",
    "dev",
    "runner",
    "example",
    "username",
    "youruser",
    "john",
    "jane",
    "alice",
    "bob",
}

PERSONAL_PATTERNS = {
    "Windows home directory": re.compile(r"[A-Za-z]:[\\/]Users[\\/]([A-Za-z0-9._-]+)"),
    "POSIX home directory": re.compile(r"/home/([a-z0-9._-]+)"),
}

PROTECTED_PATHS = (
    re.compile(r"(^|/)\.env$"),
    re.compile(r"(^|/)\.env\.(?!example$)"),
    re.compile(r"(^|/)secrets\.md$"),
    re.compile(r"\.session$"),
    re.compile(r"(^|/)\.(ai|claude|kiro|codex|cursor|cline|agents)/"),
    re.compile(r"(^|/)graphify-out/"),
    re.compile(r"(^|/)\.ignoreme"),
    re.compile(r"(^|/)plans/"),
    re.compile(r"(^|/)explain-AI\.md$"),
)


def tracked_files(staged: bool) -> list:
    command = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"]
    if not staged:
        command = ["git", "ls-files"]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    return [line for line in result.stdout.splitlines() if line.strip()]


def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="ignore")
    except (OSError, ValueError):
        return ""


def _masked(value: str) -> str:
    """Enough to find it, never enough to use it."""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]} ({len(value)} chars)"


def scan(paths: list) -> list:
    findings = []
    for path in paths:
        for pattern in PROTECTED_PATHS:
            if pattern.search(path):
                findings.append(f"{path}: a protected path is being committed")
                break

        text = _read(path)
        if not text:
            continue

        for name, pattern in CREDENTIAL_PATTERNS.items():
            for match in pattern.findall(text):
                if match in KNOWN_PLACEHOLDERS:
                    continue
                line = text[: text.index(match)].count("\n") + 1
                findings.append(f"{path}:{line}: {name} -> {_masked(match)}")

        for name, pattern in PERSONAL_PATTERNS.items():
            for account in pattern.findall(text):
                if account.lower() in GENERIC_ACCOUNT_NAMES:
                    continue
                line = text[: text.index(account)].count("\n") + 1
                findings.append(f"{path}:{line}: {name} -> {account}")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--staged",
        action="store_true",
        help="scan what is staged rather than every tracked file",
    )
    parser.add_argument("paths", nargs="*", help="specific files (pre-commit passes these)")
    known = parser.parse_args()

    paths = known.paths or tracked_files(known.staged)
    findings = scan(paths)
    if not findings:
        print(f"secret scan: {len(paths)} file(s) clean")
        return 0

    print("secret scan REFUSED the push:", file=sys.stderr)
    for finding in findings:
        print(f"  {finding}", file=sys.stderr)
    print(
        "\nThis repository is public. A credential committed once stays reachable in\n"
        "every fork, and GitHub cannot remove it from someone else's fork for you -\n"
        "so rotate anything real that appears above rather than only deleting it.\n"
        "A deliberate example belongs in KNOWN_PLACEHOLDERS in this file.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
