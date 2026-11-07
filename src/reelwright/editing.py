"""Pure timeline operations. Every function mutates a Project in place.

Times are in seconds. "Timeline time" means position in the finished video;
"source time" means position inside an asset file.
"""

from __future__ import annotations

from .models import Clip, Crop, MusicCue, Note, Project, TextOverlay, new_id

EPS = 1e-3
MIN_CLIP = 1 / 60  # anything shorter than ~a frame is dropped


def _round(t: float) -> float:
    return round(t, 4)


def locate(project: Project, t: float) -> tuple[int, Clip, float]:
    """Return (index, clip, offset-into-clip) for timeline time t."""
    if not project.clips:
        raise ValueError("The timeline is empty")
    if t < 0 or t > project.duration + EPS:
        raise ValueError(f"Time {t:.3f}s is outside the timeline (0 – {project.duration:.3f}s)")
    start = 0.0
    for i, clip in enumerate(project.clips):
        if t < start + clip.duration - EPS or i == len(project.clips) - 1:
            return i, clip, min(max(0.0, t - start), clip.duration)
        start += clip.duration
    raise AssertionError("unreachable")


def timeline_to_source(project: Project, t: float) -> dict:
    i, clip, offset = locate(project, t)
    return {"clip_id": clip.id, "clip_index": i, "asset_id": clip.asset_id,
            "source_time": _round(clip.to_source(offset))}


def source_to_timeline(project: Project, asset_id: str, source_t: float) -> list[float]:
    """Timeline times at which a given source moment plays (an asset can be used twice)."""
    out = []
    for start, clip in zip(project.clip_starts(), project.clips):
        if clip.asset_id == asset_id and clip.src_in - EPS <= source_t <= clip.src_out + EPS:
            out.append(_round(start + clip.to_offset(source_t)))
    return out


# ---- clips ------------------------------------------------------------------

def _check_range(project: Project, asset_id: str, src_in: float, src_out: float) -> None:
    asset = project.asset(asset_id)
    if src_in < 0:
        raise ValueError("src_in must be >= 0")
    if src_out - src_in < MIN_CLIP:
        raise ValueError("src_out must be greater than src_in")
    if asset.kind != "image" and asset.duration is not None and src_out > asset.duration + 0.05:
        raise ValueError(f"src_out {src_out:.3f}s is past the end of {asset_id} ({asset.duration:.3f}s)")


def add_clip(project: Project, asset_id: str, src_in: float = 0.0,
             src_out: float | None = None, index: int | None = None,
             volume: float = 1.0, speed: float = 1.0) -> Clip:
    asset = project.asset(asset_id)
    if not asset.has_video and asset.kind != "image":
        raise ValueError(f"{asset_id} has no video stream; use add_music for audio-only assets")
    if src_out is None:
        if asset.kind == "image":
            src_out = src_in + 5.0
        elif asset.duration is None:
            raise ValueError("Asset duration unknown; pass src_out explicitly")
        else:
            src_out = asset.duration
    _check_range(project, asset_id, src_in, src_out)
    if not 0.1 <= speed <= 16:
        raise ValueError("speed must be between 0.1 and 16")
    clip = Clip(asset_id=asset_id, src_in=_round(src_in), src_out=_round(src_out), volume=volume,
                speed=speed)
    if index is None or index >= len(project.clips):
        project.clips.append(clip)
    else:
        project.clips.insert(max(0, index), clip)
    return clip


def trim_clip(project: Project, clip_id: str, src_in: float | None = None,
              src_out: float | None = None) -> Clip:
    _, clip = project.find_clip(clip_id)
    new_in = clip.src_in if src_in is None else src_in
    new_out = clip.src_out if src_out is None else src_out
    _check_range(project, clip.asset_id, new_in, new_out)
    clip.src_in, clip.src_out = _round(new_in), _round(new_out)
    return clip


def set_speed(project: Project, clip_id: str, speed: float) -> Clip:
    if not 0.1 <= speed <= 16:
        raise ValueError("speed must be between 0.1 and 16")
    _, clip = project.find_clip(clip_id)
    clip.speed = speed
    return clip


def set_crop(project: Project, clip_id: str, x: float = 0.0, y: float = 0.0,
             w: float = 1.0, h: float = 1.0) -> Clip:
    """Keep only a region of the source frame (fractions 0–1). Full frame resets it."""
    if x + w > 1.0001 or y + h > 1.0001:
        raise ValueError("crop region extends past the frame (x+w and y+h must be <= 1)")
    _, clip = project.find_clip(clip_id)
    clip.crop = None if (x, y, w, h) == (0, 0, 1, 1) else Crop(x=x, y=y, w=w, h=h)
    return clip


def split_at(project: Project, t: float) -> int:
    """Split the clip under timeline time t. Returns the index of the clip that now starts at t."""
    if abs(t) < EPS:
        return 0
    if abs(t - project.duration) < EPS:
        return len(project.clips)
    i, clip, offset = locate(project, t)
    if offset < EPS:
        return i
    if clip.duration - offset < EPS:
        return i + 1
    cut = _round(clip.to_source(offset))
    tail = clip.model_copy(update={"id": new_id("clip"), "src_in": cut})
    clip.src_out = cut
    project.clips.insert(i + 1, tail)
    return i + 1


def _remap(t: float, start: float, end: float) -> float:
    if t <= start:
        return t
    if t >= end:
        return t - (end - start)
    return start


def delete_range(project: Project, start: float, end: float) -> float:
    """Ripple-delete [start, end) from the timeline. Returns seconds removed.

    Text overlays and notes are remapped so they stay attached to the content
    around them; music cues that start inside the removed range are advanced.
    """
    start = max(0.0, start)
    end = min(project.duration, end)
    if end - start < EPS:
        return 0.0
    first = split_at(project, start)
    last = split_at(project, end)
    del project.clips[first:last]
    project.clips = [c for c in project.clips if c.duration >= MIN_CLIP]

    removed = end - start
    kept: list[TextOverlay] = []
    for ov in project.overlays:
        s, e = _remap(ov.start, start, end), _remap(ov.end, start, end)
        if e - s >= 0.05:
            ov.start, ov.end = _round(s), _round(e)
            kept.append(ov)
    project.overlays = kept

    cues: list[MusicCue] = []
    for cue in project.music:
        if start <= cue.start < end:
            skipped = end - cue.start
            cue.src_in = _round(cue.src_in + skipped)
            if cue.duration is not None:
                cue.duration = _round(cue.duration - skipped)
                if cue.duration <= 0.05:
                    continue
            cue.start = _round(start)
        elif cue.start >= end:
            cue.start = _round(cue.start - removed)
        cues.append(cue)
    project.music = cues

    for note in project.notes:
        note.time = _round(_remap(note.time, start, end))
    return _round(removed)


def delete_clip(project: Project, clip_id: str) -> float:
    i, _ = project.find_clip(clip_id)
    start = project.clip_starts()[i]
    return delete_range(project, start, start + project.clips[i].duration)


def move_clip(project: Project, clip_id: str, index: int) -> None:
    i, clip = project.find_clip(clip_id)
    project.clips.pop(i)
    project.clips.insert(max(0, min(index, len(project.clips))), clip)


def keep_range(project: Project, start: float, end: float) -> None:
    """Reduce the project to [start, end] of the timeline, shifted to begin at 0."""
    total = project.duration
    if end < total:
        delete_range(project, end, total)
    if start > 0:
        delete_range(project, 0, start)


def slice_project(project: Project, start: float, end: float) -> Project:
    copy = project.model_copy(deep=True)
    keep_range(copy, start, end)
    return copy


# ---- overlays, music, notes ---------------------------------------------------

def add_text(project: Project, text: str, start: float, end: float, **style) -> TextOverlay:
    if end <= start:
        raise ValueError("end must be greater than start")
    ov = TextOverlay(text=text, start=_round(start), end=_round(end),
                     **{k: v for k, v in style.items() if v is not None})
    project.overlays.append(ov)
    project.overlays.sort(key=lambda o: o.start)
    return ov


def update_text(project: Project, overlay_id: str, **changes) -> TextOverlay:
    for ov in project.overlays:
        if ov.id == overlay_id:
            for k, v in changes.items():
                if v is not None:
                    setattr(ov, k, v)
            if ov.end <= ov.start:
                raise ValueError("end must be greater than start")
            return ov
    raise KeyError(f"No text overlay with id {overlay_id!r}")


def remove_item(items: list, item_id: str, kind: str) -> None:
    for i, item in enumerate(items):
        if item.id == item_id:
            del items[i]
            return
    raise KeyError(f"No {kind} with id {item_id!r}")


def add_music(project: Project, asset_id: str, start: float = 0.0, **opts) -> MusicCue:
    asset = project.asset(asset_id)
    if not asset.has_audio:
        raise ValueError(f"{asset_id} has no audio stream")
    cue = MusicCue(asset_id=asset_id, start=_round(start),
                   **{k: v for k, v in opts.items() if v is not None})
    project.music.append(cue)
    return cue


def add_note(project: Project, time: float, text: str, author: str = "human") -> Note:
    note = Note(time=_round(time), text=text, author=author)
    project.notes.append(note)
    project.notes.sort(key=lambda n: n.time)
    return note


# ---- silence removal ----------------------------------------------------------

def timeline_silences(project: Project, silences_by_asset: dict[str, list[tuple[float, float]]]
                      ) -> list[tuple[float, float]]:
    """Map per-asset silent ranges (source time) onto the timeline."""
    out: list[tuple[float, float]] = []
    for start, clip in zip(project.clip_starts(), project.clips):
        for s, e in silences_by_asset.get(clip.asset_id, []):
            lo, hi = max(s, clip.src_in), min(e, clip.src_out)
            if hi - lo > EPS:
                out.append((start + clip.to_offset(lo), start + clip.to_offset(hi)))
    out.sort()
    merged: list[tuple[float, float]] = []
    for s, e in out:
        if merged and s <= merged[-1][1] + EPS:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def remove_ranges(project: Project, ranges: list[tuple[float, float]], padding: float = 0.15,
                  min_length: float = 0.4) -> list[tuple[float, float]]:
    """Ripple-delete ranges (timeline time), keeping `padding` seconds of air at each side."""
    cuts = []
    for s, e in ranges:
        s2 = s + padding if s > EPS else s  # don't pad the very start of the video
        e2 = e - padding if e < project.duration - EPS else e
        if e2 - s2 >= min_length:
            cuts.append((s2, e2))
    for s, e in sorted(cuts, reverse=True):
        delete_range(project, s, e)
    return [(_round(s), _round(e)) for s, e in cuts]
