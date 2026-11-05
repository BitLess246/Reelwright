import pytest

from reelwright import captions as cap
from reelwright import editing as E
from reelwright.models import Asset, Project
from reelwright.perception import parse_subtitles


def words(*items):
    return [{"start": s, "end": e, "word": w} for s, e, w in items]


def test_words_follow_the_edit():
    p = Project(name="t")
    p.assets["a"] = Asset(id="a", path="/a.mp4", kind="video", duration=10, has_video=True, has_audio=True)
    E.add_clip(p, "a")
    tr = {"a": {"segments": [{"start": 0, "end": 6, "text": "one two three four",
                              "words": words((0.5, 1, "one"), (1, 1.5, "two"), (4, 4.5, "three"), (5, 5.5, "four"))}]}}
    E.delete_range(p, 1.6, 3.9)  # cut the pause
    cues = cap.build_cues(p, tr)
    assert [c["text"] for c in cues] == ["one two", "three four"]  # gap > max_gap splits cues
    assert cues[1]["start"] == pytest.approx(4 - 2.3)


def test_parse_srt_and_vtt():
    srt = "1\n00:00:01,000 --> 00:00:02,500\nHello <i>there</i>\n\n2\n00:00:03,000 --> 00:00:04,000\nBye\n"
    tr = parse_subtitles(srt)
    assert [(s["start"], s["end"], s["text"]) for s in tr["segments"]] == [(1, 2.5, "Hello there"), (3, 4, "Bye")]
    vtt = "WEBVTT\n\n00:01.000 --> 00:02.000 align:start\nHi\n"
    assert parse_subtitles(vtt)["segments"][0]["text"] == "Hi"


def test_srt_writer(tmp_path):
    out = cap.write_srt([{"start": 61.25, "end": 62, "text": "x"}], tmp_path / "a.srt")
    assert "00:01:01,250 --> 00:01:02,000" in out.read_text()


def test_caption_cues_are_windowed_for_partial_renders():
    from reelwright.render import caption_cues

    p = Project(name="t")
    p.assets["a"] = Asset(id="a", path="/a.mp4", kind="video", duration=10, has_video=True, has_audio=True)
    E.add_clip(p, "a")
    p.captions.enabled = True
    tr = {"a": {"segments": [{"start": 1, "end": 4, "text": "A long sentence.", "words": []}]}}
    cues = caption_cues(p, tr, start=2.0, end=2.1)  # a two-frame slice inside the cue
    assert cues == [{"start": 0.0, "end": pytest.approx(0.1), "text": "A long sentence."}]
