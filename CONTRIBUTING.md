# Contributing

This is a Telegram MCP server: Telethon talks to Telegram, FastMCP exposes the tools to an
MCP client. It is GPL-3.0-or-later, and what you contribute is under that licence too.

## Setup

Follow [Quick Start](README.md#quick-start) in the README. This file deliberately does not
repeat those steps: a second copy of them is a second thing that can go stale.

## Two rules a first change is likely to trip

Both sit in the README's [Development](README.md#development) section, low in a long file.

**Patch a module at its source, not through `runtime`.** Every tool module opens with
`from telegram_mcp.runtime import *`, so a name exists in two places at once; rebinding the
re-exported one leaves the code reading its own. `tests/test_tool_registry.py` guards that.
It will go red — but it cannot tell you that patching the wrong name is what you did, so it
is worth knowing before you see it.

**Run the suite through `scripts/run_tests_guarded.py`, never bare `pytest`.** A raw
`pytest` has no wall ceiling, no no-progress ceiling and no process-tree cleanup. This
project drives ffmpeg, ffprobe and a native rlottie decoder, each of which can wedge
without exiting, and a wedged child outlives the pytest process that started it — so the
run either sits there or reports success over a live orphan. The guarded runner bounds both
clocks and is not done until everything the suite spawned is gone.

## Commands

```bash
uv run python scripts/run_tests_guarded.py --
uv run black --check .
uv run flake8 .
```

The launchers have their own PowerShell suites, which pytest does not see. On Windows:

```powershell
Get-ChildItem tests -Filter 'test_*.ps1' | ForEach-Object { pwsh -NoProfile -File $_.FullName }
```

That is discovery rather than a list, and CI does the same, so a suite you add as
`tests/test_*.ps1` is picked up without touching a workflow. Two exist today:
`tests/test_launchers.ps1` and `tests/test_account_manager.ps1`.

## Commits

A conventional prefix and a short subject line. Write a body only when `git log --oneline`
would mislead someone without it; the diff already carries the detail. From this history:

```text
test: prove a helper's descendants die with the call
```

## Tests

A test that passes against the code as it stood before your fix is not a test of your fix.
Where a change repairs a defect, write the test first and watch it fail — that failure is
the only evidence it is attached to the bug and not to what the code already did.
