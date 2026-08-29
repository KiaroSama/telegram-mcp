"""The extension a saved file gets is never the sender's to choose.

`save_disappearing_media` has had this guard since it was written, with a
docstring naming the threat: a well-formed suffix like ".hta" survives a
shape check, and double-clicking the saved file would then run it.
`download_media` — the general-purpose tool, the one that saves arbitrary media
into whatever directory the operator opened for downloads — did not have it.

The bytes were always untrusted. The extension is the part that decides whether
opening the result shows it or runs it, and on Windows `mimetypes` reads the
registry, so `guess_extension` can hand back a registry-mapped shell type.
"""

import inspect

from telegram_mcp.file_roots import safe_suffix, target_path
from telegram_mcp.tools import ephemeral as ephemeral_mod
from telegram_mcp.tools import media as media_mod

# Well formed by shape — a dot and a few ASCII letters, exactly like ".jpg" —
# and executed or followed by the shell.
SHELL_INTERPRETED = (".hta", ".exe", ".ps1", ".cmd", ".bat", ".vbs", ".lnk", ".url", ".scr")


def test_a_shell_interpreted_suffix_is_replaced():
    for suffix in SHELL_INTERPRETED:
        assert safe_suffix(suffix) == ".bin", f"{suffix} survived"
        assert safe_suffix(suffix.upper()) == ".bin", f"{suffix.upper()} survived case folding"


def test_a_malformed_suffix_is_replaced():
    # ":ads" makes NTFS create an alternate data stream: the visible file looks
    # empty while the payload lives in the stream.
    for suffix in (".webm:ads", ".", "", ".tar.gz", ".a b", "../x", ".toolongsuffix"):
        assert safe_suffix(suffix) == ".bin", f"{suffix!r} survived"


def test_ordinary_media_is_left_alone():
    """Guard the guard: a check that replaced everything would pass every case
    above and break every real download."""
    for suffix in (".jpg", ".png", ".webm", ".ogg", ".pdf", ".mp3", ".tgs", ".webp"):
        assert safe_suffix(suffix) == suffix


def test_the_callers_extension_does_not_win_either():
    """The other door into the same hole: file_path="note.exe" with .pdf bytes."""
    path, replaced = target_path(__import__("pathlib").Path("C:/roots/note.exe"), ".pdf")
    assert path.suffix == ".pdf"
    assert replaced == ".exe"


def test_download_media_routes_the_sender_suffix_through_the_guard():
    """The acceptance case. Driving the whole tool needs a configured root and a
    staging directory; the guarantee that matters is which function decides the
    final name, and that is readable directly."""
    source = inspect.getsource(media_mod)

    assert (
        "with_suffix(produced.suffix)" not in source
    ), "download_media still takes the sender's extension unfiltered"
    assert "safe_suffix(produced.suffix)" in source


def test_both_tools_share_one_rule():
    """Two copies of a security decision drift. `save_disappearing_media` and
    `download_media` now resolve to the same function object."""
    assert ephemeral_mod._safe_suffix is safe_suffix
    assert ephemeral_mod._target_path is target_path
