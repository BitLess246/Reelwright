import pytest

from reelwright import editing as E
from reelwright.models import Asset, Project
from reelwright.store import ProjectStore


def make_project() -> Project:
    p = Project(name="t")
    p.assets["a"] = Asset(id="a", path="/a.mp4", kind="video", duration=10, has_video=True, has_audio=True)
    p.assets["b"] = Asset(id="b", path="/b.mp4", kind="video", duration=5, has_video=True, has_audio=True)
    p.assets["m"] = Asset(id="m", path="/m.mp3", kind="audio", duration=60, has_audio=True)
    E.add_clip(p, "a")          # 0-10
    E.add_clip(p, "b", 1, 4)    # 10-13
    return p


def test_durations_and_locate():
    p = make_project()
    assert p.duration == 13
    i, clip, off = E.locate(p, 11.5)
    assert (i, clip.asset_id, off) == (1, "b", 1.5)
    assert E.timeline_to_source(p, 11.5)["source_time"] == 2.5
    assert E.source_to_timeline(p, "b", 2.5) == [11.5]


def test_split():
    p = make_project()
    idx = E.split_at(p, 4)
    assert idx == 1 and len(p.clips) == 3
    assert (p.clips[0].src_out, p.clips[1].src_in) == (4, 4)
    assert p.duration == 13
    assert E.split_at(p, 4) == 1  # splitting on a boundary is a no-op
    assert len(p.clips) == 3


def test_delete_range_across_clips_remaps_overlays_and_notes():
    p = make_project()
    E.add_text(p, "inside", 8.5, 9.5)
    E.add_text(p, "after", 12, 12.5)
    E.add_text(p, "spanning", 7, 11)
    E.add_note(p, 12.2, "check")
    removed = E.delete_range(p, 8, 11)
    assert removed == 3 and p.duration == 10
    assert [(c.asset_id, c.src_in, c.src_out) for c in p.clips] == [("a", 0, 8), ("b", 2, 4)]
    texts = {o.text: (o.start, o.end) for o in p.overlays}
    assert "inside" not in texts
    assert texts["after"] == (9, 9.5)
    assert texts["spanning"] == (7, 8)
    assert p.notes[0].time == pytest.approx(9.2)


def test_music_cue_inside_deleted_range_is_advanced():
    p = make_project()
    E.add_music(p, "m", start=2)
    E.delete_range(p, 0, 5)
    cue = p.music[0]
    assert (cue.start, cue.src_in) == (0, 3)


def test_slice_project():
    p = make_project()
    s = E.slice_project(p, 9, 11)
    assert s.duration == pytest.approx(2)
    assert [(c.asset_id, c.src_in, c.src_out) for c in s.clips] == [("a", 9, 10), ("b", 1, 2)]
    assert p.duration == 13  # original untouched


def test_remove_ranges_keeps_padding():
    p = make_project()
    cuts = E.remove_ranges(p, [(2.0, 4.0)], padding=0.2, min_length=0.4)
    assert cuts == [(2.2, 3.8)]
    assert p.duration == pytest.approx(13 - 1.6)


def test_validation():
    p = make_project()
    with pytest.raises(ValueError):
        E.add_clip(p, "a", 2, 11)  # past the end
    with pytest.raises(ValueError):
        E.add_clip(p, "m")  # audio-only on the main track


def test_store_undo_redo(tmp_path):
    store = ProjectStore.create("Demo Project", root=tmp_path)
    assert store.slug == "Demo-Project"
    with store.edit("add asset") as p:
        p.assets["a"] = Asset(id="a", path="/a.mp4", kind="video", duration=10, has_video=True)
    with store.edit("add clip") as p:
        E.add_clip(p, "a")
    assert store.load().duration == 10
    assert store.undo() == "add clip"
    assert store.load().duration == 0
    assert store.redo() == "add clip"
    assert store.load().duration == 10
    assert store.history() == {"undo": ["add asset", "add clip"], "redo": []}
    with pytest.raises(ValueError):
        with store.edit("bad") as p:
            E.add_clip(p, "a", 5, 2)
    assert store.history()["undo"][-1] == "add clip"  # failed edit saved nothing


def test_speed_changes_timeline_length_and_mapping():
    p = make_project()                       # a: 0-10, b: 1-4 (3 s)
    E.set_speed(p, p.clips[0].id, 2.0)       # a now lasts 5 s
    assert p.duration == pytest.approx(8)
    assert E.timeline_to_source(p, 2.5)["source_time"] == pytest.approx(5)
    assert E.source_to_timeline(p, "a", 5) == [2.5]
    idx = E.split_at(p, 2.5)                 # split maps through speed
    assert (p.clips[0].src_out, p.clips[idx].src_in) == (5, 5)
    assert p.clips[idx].speed == 2.0
    E.delete_range(p, 0, 1)                  # 1 s of timeline = 2 s of source
    assert p.clips[0].src_in == pytest.approx(2)
    with pytest.raises(ValueError):
        E.set_speed(p, p.clips[0].id, 50)


def test_crop_validation_and_reset():
    p = make_project()
    cid = p.clips[0].id
    E.set_crop(p, cid, 0.2, 0.1, 0.5, 0.8)
    assert p.clips[0].crop.w == 0.5
    with pytest.raises(ValueError):
        E.set_crop(p, cid, 0.6, 0, 0.5, 1)
    E.set_crop(p, cid)
    assert p.clips[0].crop is None


def test_silences_map_through_speed():
    p = make_project()
    E.set_speed(p, p.clips[0].id, 2.0)
    assert E.timeline_silences(p, {"a": [(4.0, 6.0)]}) == [(2.0, 3.0)]
