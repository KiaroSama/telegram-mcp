"""`bytes` must be cleaned like `str`, not decoded raw at the JSON boundary.

`sanitize_dict` walked dicts, lists and strings and let `bytes` fall through
untouched, and both JSON serializers then decoded it with ``errors="replace"``
and emitted the result. So a bytes-typed TL field reached the model as text that
had passed through neither the control-character stripping nor the
4096-character bound every `str` gets.

Pure functions, so no fakes: build the value, assert on the cleaned value.
"""

import json

from sanitize import _json_default, format_tool_result, sanitize_dict
from telegram_mcp.runtime import json_serializer

BIDI_OVERRIDE = "‮"  # right-to-left override: reorders what a reader sees
HOSTILE = f"a{BIDI_OVERRIDE}bcd".encode("utf-8")


def test_a_bidi_override_does_not_survive_a_bytes_value():
    assert sanitize_dict({"x": HOSTILE}) == {"x": "abcd"}


def test_a_long_bytes_value_gets_the_same_bound_as_a_string():
    raw = b"a" * 5000

    cleaned = sanitize_dict({"x": raw})["x"]

    assert cleaned == sanitize_dict({"x": "a" * 5000})["x"], "bytes took a different bound"
    assert cleaned.endswith("... [truncated]")
    assert len(cleaned) == 4096 + len("... [truncated]")


def test_the_walker_reaches_bytes_nested_in_a_list():
    data = {"events": [{"payload": HOSTILE}]}

    assert sanitize_dict(data) == {"events": [{"payload": "abcd"}]}


def test_invalid_utf8_is_replaced_rather_than_raising():
    cleaned = sanitize_dict({"x": b"hello\xff" + BIDI_OVERRIDE.encode("utf-8")})["x"]

    assert cleaned == "hello�", cleaned


def test_benign_ascii_bytes_read_exactly_as_they_did_before():
    assert sanitize_dict({"x": b"hello world"}) == {"x": "hello world"}


# Both serializers, by name: this plan exists because fixing one and forgetting
# the other leaves the gap open, it just moves.
def test_json_default_cleans_a_hostile_bytes_value():
    assert _json_default(HOSTILE) == "abcd"


def test_json_serializer_cleans_a_hostile_bytes_value():
    assert json_serializer(HOSTILE) == "abcd"


def test_neither_serializer_leaks_the_override_through_json_dumps():
    assert BIDI_OVERRIDE not in format_tool_result([{"payload": HOSTILE}])
    assert BIDI_OVERRIDE not in json.dumps({"payload": HOSTILE}, default=json_serializer)
