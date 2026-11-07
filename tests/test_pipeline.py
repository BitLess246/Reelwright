"""End-to-end: drive the MCP tool functions against real (synthetic) media."""

import asyncio
import json

import pytest

from reelwright import server as S
from reelwright.ffmpeg import probe
from reelwright.perception import loudness_profile


def _duration(path) -> float:
    return float(probe(path)["format"]["duration"])


@pytest.fixture()
def project(media, reelwright_home):
    S.create_project("demo", width=1280, height=720, fps=30)
    added = S.import_media("demo", [str(media[k]) for k in ("talk", "cut", "music", "logo")])["added"]
    ids = {a["label"]: a["id"] for a in added}
    return ids


def test_import_and_perception(project, media):
    tl = S.get_timeline("demo")
    kinds = {a["label"]: a["kind"] for a in tl["assets"]}
    assert kinds == {"talk": "video", "cut": "video", "music": "audio", "logo": "image"}
    assert S.import_media("demo", [str(media["talk"])])["already_imported"]

    scenes = S.detect_scenes("demo", project["cut"])["scene_changes"]
    assert any(abs(t - 2.0) < 0.1 for t in scenes)
    silences = S.detect_silence("demo", project["talk"])["silences"]
    assert len(silences) == 1 and abs(silences[0][0] - 2.0) < 0.1

    sheet = S.inspect_media("demo", project["talk"], count=4)
    assert sheet[1].__class__.__name__ == "Image"


def test_edit_verify_export(project, media):
    t = project
    S.add_clip("demo", t["logo"], src_out=1.5)
    S.add_clip("demo", t["talk"])
    S.add_clip("demo", t["cut"])
    assert S.get_timeline("demo")["duration"] == pytest.approx(11.5)

    dry = S.remove_silences("demo", dry_run=True)
    assert dry["would_remove"] == pytest.approx(1.2, abs=0.1)
    S.remove_silences("demo")
    assert S.get_timeline("demo")["duration"] == pytest.approx(10.3, abs=0.1)

    S.add_text("demo", "Beam design: part 1", 0.2, 1.4, position="center")
    S.add_music("demo", t["music"], start=0)
    S.import_transcript("demo", t["talk"], str(media["srt"]))
    assert "beams" in S.get_transcript("demo")  # timeline transcript follows the edit
    assert S.set_captions("demo", enabled=True)["ok"]

    grid = S.check_frames("demo", [0.5, 3.0, 9.0])
    assert grid[1].__class__.__name__ == "Image"

    prev = S.render_preview("demo", start=1.0, end=4.0)
    assert _duration(prev["path"]) == pytest.approx(3.0, abs=0.1)
    assert (prev["width"], prev["height"]) == (640, 360)

    out = S.export("demo", preset="shorts", quality="draft")
    info = probe(out["path"])
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (1080, 1920)
    assert _duration(out["path"]) == pytest.approx(10.3, abs=0.15)
    assert out["srt"].endswith(".srt")
    assert loudness_profile(out["path"])["integrated_lufs"] == pytest.approx(-14, abs=2)

    # the last edit was the caption toggle: undo turns captions off, redo restores them
    assert S.undo("demo")["message"] == "Undid: caption settings"
    assert S.get_timeline("demo")["captions"]["enabled"] is False
    S.redo("demo")
    assert S.get_timeline("demo")["captions"]["enabled"] is True


def test_notes_and_mcp_roundtrip(project):
    S.add_clip("demo", project["talk"])
    S.add_note("demo", 2.0, "cut this pause", author="human")
    notes = S.list_notes("demo")["notes"]
    assert notes[0]["text"] == "cut this pause"
    S.resolve_note("demo", notes[0]["id"])
    assert S.list_notes("demo")["notes"] == []

    result = asyncio.run(S.mcp.call_tool("get_timeline", {"project": "demo"}))
    if isinstance(result, tuple):  # mcp 1.x returns (content, structured)
        result = result[0]
    content = getattr(result, "content", result)
    payload = json.loads(content[0].text)
    assert payload["clips"][0]["asset_id"] == project["talk"]


def test_speed_crop_and_blur_fit_render(media, reelwright_home):
    from PIL import Image as PILImage

    S.create_project("reel", width=360, height=640, fps=30, fit="blur")
    aid = S.import_media("reel", [str(media["talk"])])["added"][0]["id"]
    clip = S.add_clip("reel", aid, 0, 6, speed=3.0)["clip"]
    assert S.get_timeline("reel")["duration"] == pytest.approx(2.0)
    out = S.render_preview("reel")
    assert _duration(out["path"]) == pytest.approx(2.0, abs=0.1)

    # crop to the left third of testsrc2 and check the frame really changed
    a = PILImage.open(S.check_frames("reel", [1.0])[0]["image"]).convert("L").resize((64, 64))
    S.set_clip_crop("reel", clip["id"], x=0.0, y=0.0, w=0.34, h=1.0)
    b = PILImage.open(S.check_frames("reel", [1.0])[0]["image"]).convert("L").resize((64, 64))
    diff = sum(abs(x - y) for x, y in zip(a.tobytes(), b.tobytes())) / (64 * 64)
    assert diff > 10


def test_multiline_text_file_has_no_carriage_returns(media, reelwright_home, tmp_path):
    from reelwright import render as R
    from reelwright.store import ProjectStore

    S.create_project("txt", width=320, height=180)
    aid = S.import_media("txt", [str(media["talk"])])["added"][0]["id"]
    S.add_clip("txt", aid, 0, 1)
    S.add_text("txt", "line one\nline two", 0, 1)
    p = ProjectStore.open("txt").load()
    canvas, _ = R.resolve_canvas(p, "preview")
    R.build_graph(p, canvas, tmp_path)
    assert (tmp_path / "text_0.txt").read_bytes() == b"line one\nline two"


def test_export_preset_keeps_project_fit(media, reelwright_home):
    from reelwright import render as R
    from reelwright.store import ProjectStore

    S.create_project("vert", width=1080, height=1920, fit="blur")
    S.create_project("wide", width=1920, height=1080)
    vert, wide = ProjectStore.open("vert").load(), ProjectStore.open("wide").load()
    assert R.resolve_canvas(vert, "shorts")[0].fit == "blur"     # deliberate choice kept
    assert R.resolve_canvas(wide, "shorts")[0].fit == "cover"    # reframing a 16:9 edit
    assert R.resolve_canvas(wide, "youtube")[0].fit == "contain"
    vert.settings.fit = "contain"
    assert R.resolve_canvas(vert, "shorts")[0].fit == "contain"  # same shape: nothing to reframe
