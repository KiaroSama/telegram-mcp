"""Reading the owner's emoji off disk, and re-reading them after they change.

The export is REGENERATED whenever a pack is rebuilt, so the interesting
property is not "can it read a directory" - it is what a second run does. A
refresh that re-fingerprints everything is a five-minute wait after every pack
edit; one that re-fingerprints nothing serves the old picture for an emoji that
was replaced, which is worse, because the shortlist looks right and is wrong.

So every test here is about the SECOND run: unchanged files skipped, touched
files redone, vanished ones forgotten, and fingerprints that came from Telegram
for somebody else's emoji left alone throughout.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import emoji_packs  # noqa: E402


def _webp(path, colour=(255, 0, 0, 255), size=(8, 8)):
    from PIL import Image

    Image.new("RGBA", size, colour).save(path, "WEBP")
    return path


def _export(root, packs):
    """A miniature Emoji Mapper export. `packs` is {set_name: [(id, glyph)]}."""
    thumbs = root / emoji_packs.THUMBS
    thumbs.mkdir(parents=True, exist_ok=True)
    listed = []
    for number, (set_name, emoji) in enumerate(packs.items(), start=1):
        listed.append(
            {
                "set_name": set_name,
                "title": f"Pack {number}",
                "family": "general",
                "link": f"https://t.me/addemoji/{set_name}",
                "count": len(emoji),
            }
        )
        (root / f"{set_name}.json").write_text(
            json.dumps(
                {
                    "set_name": set_name,
                    "emoji": [
                        {"custom_emoji_id": i, "glyph": g, "role": "emoji", "name": None}
                        for i, g in emoji
                    ],
                }
            ),
            encoding="utf-8",
        )
        for emoji_id, _glyph in emoji:
            _webp(thumbs / f"{emoji_id}.webp")
    (root / emoji_packs.INDEX).write_text(
        json.dumps({"pack_count": len(packs), "packs": listed}), encoding="utf-8"
    )
    return root


def _fingerprint(_picture):
    """Stands in for emoji_vision: the refresh logic is what is under test."""
    return {"silhouette": [1.0], "colour": [1.0], "structure": [1.0]}


# --------------------------------------------------------------- finding it


def test_a_directory_without_an_index_is_refused_by_name(tmp_path):
    """Pointed at the wrong folder this would silently catalogue nothing, and a
    `nearest` over an empty index answers confidently with junk."""
    with pytest.raises(SystemExit, match="not an Emoji Mapper export"):
        emoji_packs.find_root(str(tmp_path))


def test_no_directory_at_all_names_the_environment_variable(tmp_path, monkeypatch):
    monkeypatch.delenv(emoji_packs.ENV_VAR, raising=False)
    with pytest.raises(SystemExit, match=emoji_packs.ENV_VAR):
        emoji_packs.find_root(None)


def test_the_environment_variable_is_used_when_no_argument_is_given(tmp_path, monkeypatch):
    root = _export(tmp_path / "export", {"pack_a": [("111", "✅")]})
    monkeypatch.setenv(emoji_packs.ENV_VAR, str(root))

    assert emoji_packs.find_root(None) == root


# ---------------------------------------------------------------- catalogue


def test_pack_metadata_is_joined_onto_every_emoji(tmp_path):
    """The pack title and addemoji link are the two facts a rendered picture can
    never supply, and they are why this reads the export rather than Telegram."""
    root = _export(tmp_path, {"pack_a": [("111", "✅"), ("222", "💳")]})

    entries, problems = emoji_packs.read_catalogue(root)

    assert problems == []
    assert entries["222"]["glyph"] == "💳"
    assert entries["222"]["title"] == "Pack 1"
    assert entries["222"]["link"] == "https://t.me/addemoji/pack_a"
    assert entries["222"]["pack"] == "pack_a"


def test_ids_stay_strings(tmp_path):
    """`6325520979057445692` through a JSON number comes back ...445888, which is
    a different emoji that renders perfectly."""
    root = tmp_path
    _export(root, {"pack_a": [("6325520979057445692", "✅")]})
    # Rewrite the id as a JSON NUMBER, which is what a careless exporter emits.
    source = root / "pack_a.json"
    source.write_text(
        source.read_text(encoding="utf-8").replace('"6325520979057445692"', "6325520979057445692"),
        encoding="utf-8",
    )

    entries, _ = emoji_packs.read_catalogue(root)

    assert "6325520979057445692" in entries


def test_a_pack_missing_its_json_is_reported_not_raised(tmp_path):
    """The export is written while packs rebuild; half of it is still worth
    indexing, and an exception here throws away the other 33 packs."""
    root = _export(tmp_path, {"pack_a": [("111", "✅")], "pack_b": [("222", "💳")]})
    (root / "pack_b.json").unlink()

    entries, problems = emoji_packs.read_catalogue(root)

    assert "111" in entries and "222" not in entries
    assert len(problems) == 1 and "pack_b" in problems[0]


def test_an_emoji_with_only_a_webm_thumb_is_found_but_carries_no_webp(tmp_path):
    root = _export(tmp_path, {"pack_a": [("111", "✅")]})
    (root / emoji_packs.THUMBS / "111.webp").unlink()
    (root / emoji_packs.THUMBS / "111.webm").write_bytes(b"not really a video")

    entries, _ = emoji_packs.read_catalogue(root)

    assert entries["111"]["thumb"].suffix == ".webm"


def test_the_exports_own_still_is_preferred_over_the_animation(tmp_path):
    """Choosing a frame is guesswork; the export already chose one."""
    root = _export(tmp_path, {"pack_a": [("111", "✅")]})
    _webp(root / emoji_packs.THUMBS / "111_still.webp")

    entries, _ = emoji_packs.read_catalogue(root)

    assert entries["111"]["thumb"].name == "111_still.webp"


# ------------------------------------------------------------------ refresh


def test_the_first_run_fingerprints_everything(tmp_path):
    entries, _ = emoji_packs.read_catalogue(_export(tmp_path, {"p": [("1", "a"), ("2", "b")]}))

    stored, counts = emoji_packs.refresh({}, entries, _fingerprint)

    assert counts["added"] == 2
    assert sorted(stored) == ["1", "2"]
    assert stored["1"]["src"] == "packs"


def test_the_second_run_fingerprints_nothing(tmp_path):
    """The whole reason the stamp exists. 6739 emoji is minutes of work."""
    entries, _ = emoji_packs.read_catalogue(_export(tmp_path, {"p": [("1", "a"), ("2", "b")]}))
    stored, _ = emoji_packs.refresh({}, entries, _fingerprint)

    def _explode(_picture):
        raise AssertionError("an unchanged thumbnail was fingerprinted again")

    _stored, counts = emoji_packs.refresh(stored, entries, _explode)

    assert counts == {
        "added": 0,
        "updated": 0,
        "unchanged": 2,
        "removed": 0,
        "skipped": 0,
        "problems": [],
    }


def test_a_replaced_thumbnail_is_fingerprinted_again(tmp_path):
    """The failure this guards is silent: the shortlist still ranks the emoji,
    using the picture it had before the pack was rebuilt."""
    root = _export(tmp_path, {"p": [("1", "a")]})
    entries, _ = emoji_packs.read_catalogue(root)
    stored, _ = emoji_packs.refresh({}, entries, _fingerprint)

    thumb = root / emoji_packs.THUMBS / "1.webp"
    _webp(thumb, colour=(0, 0, 255, 255), size=(16, 16))
    entries, _ = emoji_packs.read_catalogue(root)

    stored, counts = emoji_packs.refresh(stored, entries, lambda p: {"silhouette": [0.5]})

    assert counts["updated"] == 1 and counts["unchanged"] == 0
    assert stored["1"]["silhouette"] == [0.5]


def test_an_emoji_the_export_no_longer_has_is_forgotten(tmp_path):
    root = _export(tmp_path, {"p": [("1", "a"), ("2", "b")]})
    entries, _ = emoji_packs.read_catalogue(root)
    stored, _ = emoji_packs.refresh({}, entries, _fingerprint)

    _export(root, {"p": [("1", "a")]})
    entries, _ = emoji_packs.read_catalogue(root)
    stored, counts = emoji_packs.refresh(stored, entries, _fingerprint)

    assert counts["removed"] == 1
    assert "2" not in stored


def test_fingerprints_that_did_not_come_from_the_export_are_left_alone(tmp_path):
    """`nearest` is asked about emoji from OTHER people's packs; those were
    fingerprinted from Telegram and the export knows nothing about them."""
    entries, _ = emoji_packs.read_catalogue(_export(tmp_path, {"p": [("1", "a")]}))
    stored = {"999": {"pack": "someone_elses_set", "silhouette": [0.1]}}

    stored, counts = emoji_packs.refresh(stored, entries, _fingerprint)

    assert counts["removed"] == 0
    assert stored["999"]["silhouette"] == [0.1]


def test_a_webm_only_emoji_is_skipped_rather_than_failing_the_run(tmp_path):
    root = _export(tmp_path, {"p": [("1", "a"), ("2", "b")]})
    (root / emoji_packs.THUMBS / "2.webp").unlink()
    (root / emoji_packs.THUMBS / "2.webm").write_bytes(b"video")
    entries, _ = emoji_packs.read_catalogue(root)

    stored, counts = emoji_packs.refresh({}, entries, _fingerprint)

    assert counts["added"] == 1 and counts["skipped"] == 1
    assert "2" not in stored


def test_an_unreadable_thumbnail_is_reported_and_the_rest_still_index(tmp_path):
    """A file caught mid-write during an export rebuild."""
    root = _export(tmp_path, {"p": [("1", "a"), ("2", "b")]})
    (root / emoji_packs.THUMBS / "2.webp").write_bytes(b"truncated")
    entries, _ = emoji_packs.read_catalogue(root)

    stored, counts = emoji_packs.refresh({}, entries, _fingerprint)

    assert counts["added"] == 1 and counts["skipped"] == 1
    assert len(counts["problems"]) == 1 and "2" in counts["problems"][0]


# ------------------------------------------------------------------ picture


def test_an_animated_thumbnail_yields_a_frame_with_something_in_it(tmp_path):
    """An animation legitimately starts empty; frame 0 is how an emoji ends up
    looking like a blank box and being judged on it."""
    from PIL import Image

    blank = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
    lit = Image.new("RGBA", (8, 8), (255, 0, 0, 255))
    path = tmp_path / "animated.webp"
    blank.save(path, "WEBP", save_all=True, append_images=[lit, blank], duration=100)

    picture = emoji_packs.open_picture(path)

    alpha = picture.getchannel("A").histogram()
    assert sum(level * count for level, count in enumerate(alpha)) > 0
