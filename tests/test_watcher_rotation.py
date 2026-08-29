"""The watcher command, run exactly as it is handed to a person.

Two things this pins that the older test could not:

* The command is executed **as returned**. The previous version stripped the
  wrapper and ran the script body directly, which is precisely how the quoting
  defect survived: the string given to users wrapped the script in double
  quotes, so ``$p`` and ``$o`` were expanded by the shell they pasted it into
  before the child PowerShell ever parsed them.
* Rotation is proved with a replacement that is **longer** than the offset
  already read. Inferring rotation from the file getting shorter missed exactly
  this: a fresh generation that grew past the mark within one poll interval
  looked like ordinary growth, so the watcher seeked into it and everything it
  had written first was skipped in silence.
"""

import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

import pytest

from telegram_mcp.tools import events

windows_only = pytest.mark.skipif(
    sys.platform != "win32", reason="the PowerShell watcher is the Windows one"
)

# Generous: a cold PowerShell start plus a 500ms poll, on a loaded CI runner.
WATCH_TIMEOUT_SECONDS = 40.0


def _collect(stream, sink):
    for line in iter(stream.readline, ""):
        sink.put(line)


def _replace_when_windows_lets_go(source: Path, destination: Path) -> None:
    """Rename over a file a reader may be holding, bounded.

    The watcher opens the feed with FILE_SHARE_DELETE, so a rename over it is
    permitted - but not instantaneously in every ordering, and under a loaded
    machine this lost often enough to make the test red while the watcher was
    behaving correctly.

    Production does the same thing: `_rotate_feed_if_needed` logs the OSError
    and leaves the rotation to the next open. Retrying here models that rather
    than pretending the rename is infallible.
    """
    deadline = time.monotonic() + 10.0
    while True:
        try:
            os.replace(source, destination)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


class _Watcher:
    """The exact command, running, with its output on a queue."""

    def __init__(self, command: str):
        # `.split()` is exact because -EncodedCommand is base64: four bare
        # tokens, no quotes, nothing for a shell or for this test to reinterpret.
        self.process = subprocess.Popen(
            command.split(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
        )
        self.lines: "queue.Queue[str]" = queue.Queue()
        threading.Thread(
            target=_collect, args=(self.process.stdout, self.lines), daemon=True
        ).start()

    def await_line(self, marker: str, seconds: float = WATCH_TIMEOUT_SECONDS) -> None:
        """Wait for one specific line, bounded, rather than sleeping and hoping."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                line = self.lines.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            if marker in line:
                return
        pytest.fail(f"the watcher never emitted a line containing {marker!r}")

    def close(self):
        self.process.kill()
        self.process.wait(timeout=30)


@windows_only
def test_the_command_handed_to_a_person_has_nothing_a_shell_can_expand():
    """The defect in one assertion. `$p`, `$o`, `$len`, `$f`, `$r` and `$line` all
    lived inside outer double quotes, so PowerShell substituted them from the
    caller's own session before the child ever saw the script."""
    command = events.incoming_feed_state()["watch_command"]

    assert "$" not in command
    assert "-EncodedCommand" in command


@windows_only
def test_a_rotation_to_a_longer_file_still_emits_its_first_event(monkeypatch, tmp_path):
    """The case a length comparison cannot see.

    Creation time cannot decide it either, on Windows: NTFS tunneling hands a
    name recreated within about fifteen seconds the OLD creation stamp, and a
    rotation takes milliseconds. The watcher compares the file's own first
    bytes, which nothing can tunnel.
    """
    path = tmp_path / "feed.jsonl"
    monkeypatch.setenv("TELEGRAM_EVENT_FEED_FILE", str(path))
    # Long enough that the replacement below can be longer still.
    path.write_text('{"old": "' + ("x" * 400) + '"}\n', encoding="utf-8")

    watcher = _Watcher(events.incoming_feed_state()["watch_command"])
    try:
        # Prove it is reading the first generation before replacing it, rather
        # than assuming the watcher has started.
        watcher.await_line('"old"')

        replacement = tmp_path / "next.jsonl"
        replacement.write_text(
            '{"first_of_the_new_generation": 1}\n' + '{"pad": "' + ("y" * 600) + '"}\n',
            encoding="utf-8",
        )
        assert replacement.stat().st_size > path.stat().st_size, (
            "the replacement has to be LONGER than the offset already read, or this "
            "tests the shrink case the old logic already handled"
        )

        _replace_when_windows_lets_go(replacement, path)

        watcher.await_line("first_of_the_new_generation")
    finally:
        watcher.close()


@windows_only
def test_an_ordinary_append_is_not_mistaken_for_a_rotation(monkeypatch, tmp_path):
    """Guard the guard. A continuity check that reset on every poll would pass
    the test above and re-emit the whole file for ever."""
    path = tmp_path / "feed.jsonl"
    monkeypatch.setenv("TELEGRAM_EVENT_FEED_FILE", str(path))
    path.write_text('{"first": 1}\n', encoding="utf-8")

    watcher = _Watcher(events.incoming_feed_state()["watch_command"])
    try:
        watcher.await_line('"first"')

        with open(path, "a", encoding="utf-8") as handle:
            handle.write('{"second": 2}\n')
        watcher.await_line('"second"')

        # Drain what arrives over a couple of poll intervals and count how often
        # the first line comes back. Re-reading from zero would repeat it.
        seen = 0
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                line = watcher.lines.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            if '"first"' in line:
                seen += 1
        assert seen == 0, "the watcher re-emitted a line it had already sent"
    finally:
        watcher.close()
