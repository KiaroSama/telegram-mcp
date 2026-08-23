"""Sender-controlled strings in the shared message-listing helpers.

No network: the message is a fake object graph, because the only thing under
test is what the helpers do to strings that arrived from a stranger. Four values
in a listing are written by whoever sent the message rather than by the account
reading it -- a sticker's alt text, a document's filename, an inline button's
label and the URL hidden behind a link -- and all four reach the model through
``get_media_label`` and ``message_to_dict``.

The property guarded here is two-sided, and the second half matters as much as
the first: the bidi override and the invisible padding must not survive, *and* a
legitimate filename or a ZWJ-joined family emoji must come back untouched. A
cleaner that flattens real content is its own bug, which is exactly why this
project uses ``display_name`` rather than the blunter ``sanitize_name``.
"""

from types import SimpleNamespace

from helpers_unicode import FAMILY, HOSTILE

# The bound is imported from button_view, which is where this project defined the
# machine-value convention -- so this asserts the listing agrees with it rather than
# re-stating whatever number the listing happens to use.
from telegram_mcp.button_view import MAX_MACHINE_VALUE
from telegram_mcp.tools.messages import get_media_label, message_to_dict

# The two characters HOSTILE carries that these assertions name directly: the
# override that makes a label read as something else, and the invisible padding.
RLO = "‮"
ZWSP = "​"
INVISIBLE_ONLY = RLO + ZWSP  # hostile with nothing legible left after cleaning


def _msg(**fields):
    """A message carrying only the fields the helpers read."""
    return SimpleNamespace(id=7, date="2026-08-23", sender=None, **fields)


def _sticker(alt):
    return _msg(sticker=SimpleNamespace(attributes=[SimpleNamespace(alt=alt)]))


def _document(name):
    return _msg(document=object(), file=SimpleNamespace(name=name))


# --------------------------------------------------------------------------
# get_media_label: the sticker alt


def test_sticker_alt_loses_its_bidi_override_and_invisible_padding():
    label = get_media_label(_sticker(HOSTILE))

    assert RLO not in label
    assert ZWSP not in label
    assert label == "sticker evil"


def test_sticker_alt_is_bounded():
    label = get_media_label(_sticker("h" * 5000))

    # display_name's prose default, not the machine bound: an alt is read as text.
    assert len(label) < 5000


def test_sticker_without_an_alt_has_no_trailing_separator():
    assert get_media_label(_msg(sticker=SimpleNamespace(attributes=[]))) == "sticker"


def test_sticker_whose_alt_is_only_hidden_characters_falls_back_to_the_bare_label():
    # The alt is truthy on the way in, so a naive clean would emit "sticker ".
    assert get_media_label(_sticker(INVISIBLE_ONLY)) == "sticker"


def test_sticker_alt_keeps_a_family_emoji_whole():
    # FAMILY is one grapheme held together by ZWJ. Deleting every format character
    # -- sanitize_name's habit -- would split it into three separate people.
    assert get_media_label(_sticker(FAMILY)) == f"sticker {FAMILY}"


# --------------------------------------------------------------------------
# get_media_label: the document filename


def test_document_filename_loses_its_bidi_override():
    label = get_media_label(_document(f"invoice{RLO}fdp.exe"))

    assert RLO not in label
    assert label == "document: invoicefdp.exe"


def test_benign_document_filename_round_trips_unchanged():
    assert get_media_label(_document("report.pdf")) == "document: report.pdf"


def test_document_filename_of_only_hidden_characters_falls_back_to_the_bare_label():
    assert get_media_label(_document(INVISIBLE_ONLY)) == "document"


def test_document_without_a_filename_is_still_reported():
    assert get_media_label(_msg(document=object(), file=None)) == "document"


# --------------------------------------------------------------------------
# message_to_dict: buttons and hidden link URLs


def test_button_label_is_cleaned_in_the_listing_dict():
    msg = _msg(buttons=[[SimpleNamespace(text=f"Confirm{RLO}"), SimpleNamespace(text="Cancel")]])

    assert message_to_dict(msg)["buttons"] == ["Confirm", "Cancel"]


def test_button_label_of_only_hidden_characters_is_dropped_not_emitted_empty():
    msg = _msg(buttons=[[SimpleNamespace(text=INVISIBLE_ONLY), SimpleNamespace(text="Cancel")]])

    assert message_to_dict(msg)["buttons"] == ["Cancel"]


def test_hidden_link_url_is_capped_at_the_machine_bound():
    msg = _msg(entities=[SimpleNamespace(url="https://example.test/" + "h" * 5000)])

    url = message_to_dict(msg)["link_urls"][0]
    # Bounded so a hostile link cannot flood the context, but far above the prose
    # default, which would cut a real Mini App link in half.
    assert len(url) == MAX_MACHINE_VALUE


def test_hidden_link_url_loses_its_bidi_override():
    msg = _msg(entities=[SimpleNamespace(url=f"https://example.test/{RLO}gpj.exe")])

    assert RLO not in message_to_dict(msg)["link_urls"][0]


def test_benign_link_url_round_trips_unchanged():
    msg = _msg(entities=[SimpleNamespace(url="https://example.test/a?b=1&c=2")])

    assert message_to_dict(msg)["link_urls"] == ["https://example.test/a?b=1&c=2"]


def test_listing_dict_carries_the_cleaned_media_label():
    # The label reaches the model through this dict, not only through get_media_label.
    assert message_to_dict(_document(f"report{RLO}.pdf"))["media"] == "document: report.pdf"
