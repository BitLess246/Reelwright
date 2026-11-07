"""Reelwright MCP server: the tool surface an AI agent uses to edit video."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

try:  # mcp >= 2
    from mcp.server.mcpserver import Image, MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server, Image  # type: ignore

from . import __version__
from . import captions as cap
from . import editing as E
from . import perception as P
from . import render as R
from .models import Position, Project
from .store import ProjectStore

INSTRUCTIONS = """\
Reelwright edits video through a JSON timeline. You cannot watch or hear media in
real time, so use the perception tools to look and listen, then verify every edit.

Typical loop:
1. create_project → import_media (absolute paths on the user's machine).
2. Understand the footage: inspect_media (contact sheet), get_transcript /
   transcribe, detect_silence, detect_scenes, audio_loudness, audio_waveform.
3. Edit: add_clip, trim_clip, split_clip, delete_range (ripple), move_clip,
   remove_silences, add_text, add_music, set_captions. Every edit is one undo step.
4. Verify: check_frames at the exact times you changed (renders the *edited*
   timeline, overlays and captions included), timeline_contact_sheet for the
   whole edit, render_preview for a low-res MP4 of a range.
5. export with a preset (youtube, shorts, square, youtube_4k, project).

open_review starts a local web page where the human watches the edit and pins notes;
check list_notes for their feedback.

Times are seconds. Clip src_in/src_out are positions inside the source file;
timeline times are positions in the finished video. get_timeline shows both.
Check list_notes for human feedback pinned to timeline times.
"""

try:
    mcp = _Server("reelwright", instructions=INSTRUCTIONS, version=__version__)
except TypeError:  # mcp 1.x FastMCP has no `version` argument
    mcp = _Server("reelwright", instructions=INSTRUCTIONS)


# ---- helpers -----------------------------------------------------------------------

def _store(project: str) -> ProjectStore:
    return ProjectStore.open(project)


def _transcript_path(store: ProjectStore, asset_id: str) -> Path:
    return store.cache_dir("transcripts") / f"{asset_id}.json"


def _transcripts(store: ProjectStore, project: Project) -> dict[str, dict]:
    out = {}
    for asset_id in project.assets:
        f = _transcript_path(store, asset_id)
        if f.exists():
            out[asset_id] = json.loads(f.read_text(encoding="utf-8"))
    return out


def _summary(store: ProjectStore, project: Project) -> dict:
    starts = project.clip_starts()
    has_tr = set(_transcripts(store, project))
    return {
        "project": project.name,
        "duration": round(project.duration, 3),
        "duration_hms": P.fmt_time(project.duration),
        "settings": project.settings.model_dump(),
        "captions": project.captions.model_dump(),
        "clips": [
            {"index": i, "id": c.id, "asset_id": c.asset_id,
             "label": project.assets[c.asset_id].label,
             "timeline_start": round(s, 3), "timeline_end": round(s + c.duration, 3),
             "src_in": c.src_in, "src_out": c.src_out, "duration": round(c.duration, 3),
             "volume": c.volume, "speed": c.speed,
             "crop": c.crop.model_dump() if c.crop else None}
            for i, (s, c) in enumerate(zip(starts, project.clips))
        ],
        "overlays": [o.model_dump() for o in project.overlays],
        "music": [m.model_dump() for m in project.music],
        "open_notes": [n.model_dump() for n in project.notes if not n.resolved],
        "assets": [
            {"id": a.id, "label": a.label, "kind": a.kind, "duration": a.duration,
             "resolution": f"{a.width}x{a.height}" if a.width else None, "fps": a.fps,
             "has_audio": a.has_audio, "has_transcript": a.id in has_tr, "path": a.path}
            for a in project.assets.values()
        ],
        "project_dir": str(store.dir),
    }


def _edit_result(store: ProjectStore, message: str, **extra) -> dict:
    project = store.load()
    return {"ok": True, "message": message, **extra,
            "timeline_duration": round(project.duration, 3),
            "clip_count": len(project.clips)}


def _asset_path(project: Project, asset_id: str) -> str:
    return project.asset(asset_id).path


# ---- projects ------------------------------------------------------------------------

@mcp.tool()
def list_projects() -> dict:
    """List existing Reelwright projects."""
    from .store import home
    return {"home": str(home()), "projects": ProjectStore.list()}


@mcp.tool()
def create_project(name: str, width: int = 1920, height: int = 1080, fps: float = 30.0,
                   fit: Literal["contain", "cover", "blur"] = "contain") -> dict:
    """Create a new project. width/height/fps define the canvas; `fit` controls how
    sources with a different aspect ratio are placed (contain = letterbox, cover = crop,
    blur = contained over a blurred fill — the usual way to put 16:9 footage in a 9:16 reel)."""
    store = ProjectStore.create(name, width=width, height=height, fps=fps, fit=fit)
    return _summary(store, store.load())


@mcp.tool()
def get_timeline(project: str) -> dict:
    """The full edit: clips with timeline and source times, overlays, music, open notes, assets."""
    store = _store(project)
    return _summary(store, store.load())


@mcp.tool()
def update_settings(project: str, width: int | None = None, height: int | None = None,
                    fps: float | None = None, fit: Literal["contain", "cover", "blur"] | None = None,
                    background: str | None = None, font_file: str | None = None) -> dict:
    """Change canvas settings (resolution, fps, fit, background colour, font file for text)."""
    store = _store(project)
    with store.edit("update settings") as p:
        for k, v in dict(width=width, height=height, fps=fps, fit=fit,
                         background=background, font_file=font_file).items():
            if v is not None:
                setattr(p.settings, k, v)
    return _edit_result(store, "Settings updated", settings=store.load().settings.model_dump())


@mcp.tool()
def undo(project: str) -> dict:
    """Undo the last edit."""
    store = _store(project)
    return _edit_result(store, f"Undid: {store.undo()}")


@mcp.tool()
def redo(project: str) -> dict:
    """Redo the last undone edit."""
    store = _store(project)
    return _edit_result(store, f"Redid: {store.redo()}")


@mcp.tool()
def history(project: str) -> dict:
    """Undo/redo stack descriptions (oldest first)."""
    return _store(project).history()


# ---- media -----------------------------------------------------------------------------

@mcp.tool()
def import_media(project: str, paths: list[str], labels: list[str] | None = None) -> dict:
    """Add media files (video, audio, images) to the project's asset library.
    Paths must be absolute paths on the machine running this server."""
    store = _store(project)
    added, skipped = [], []
    with store.edit(f"import {len(paths)} file(s)") as p:
        known = {a.path: a.id for a in p.assets.values()}
        for i, raw in enumerate(paths):
            resolved = str(Path(raw).expanduser().resolve())
            if resolved in known:
                skipped.append({"path": resolved, "asset_id": known[resolved]})
                continue
            asset = P.probe_asset(resolved, labels[i] if labels and i < len(labels) else None)
            p.assets[asset.id] = asset
            added.append(asset.model_dump())
    return {"added": added, "already_imported": skipped}


@mcp.tool()
def inspect_media(project: str, asset_id: str, start: float = 0.0, end: float | None = None,
                  count: int = 12, columns: int = 4) -> list:
    """Contact sheet: `count` evenly spaced frames from [start, end] of a source asset,
    each labelled with its source timestamp. Use it to see what is in a clip."""
    store = _store(project)
    p = store.load()
    asset = p.asset(asset_id)
    if not asset.has_video:
        raise ValueError(f"{asset_id} is audio-only; use audio_waveform or get_transcript instead")
    end = end if end is not None else (asset.duration or 0.0)
    out = store.cache_dir("sheets") / f"{asset_id}_{start:.2f}_{end:.2f}_{count}.jpg"
    P.contact_sheet(asset.path, start, end, count, out, columns,
                    title=f"{asset.label}  [{P.fmt_time(start)} – {P.fmt_time(end)}]")
    return [{"asset_id": asset_id, "start": start, "end": end, "frames": count, "image": str(out)},
            Image(path=str(out))]


@mcp.tool()
def detect_scenes(project: str, asset_id: str, threshold: float = 0.3) -> dict:
    """Shot-change timestamps (source time) in a video asset. Lower threshold = more sensitive."""
    store = _store(project)
    p = store.load()
    cuts = P.detect_scenes(_asset_path(p, asset_id), threshold, store.cache_dir("analysis"))
    return {"asset_id": asset_id, "threshold": threshold, "scene_changes": cuts}


@mcp.tool()
def detect_silence(project: str, asset_id: str, noise_db: float = -35.0,
                   min_duration: float = 0.5) -> dict:
    """Silent ranges [start, end] (source time) in an asset's audio."""
    store = _store(project)
    p = store.load()
    a = p.asset(asset_id)
    if not a.has_audio:
        raise ValueError(f"{asset_id} has no audio")
    ranges = P.detect_silence(a.path, noise_db, min_duration, a.duration, store.cache_dir("analysis"))
    return {"asset_id": asset_id, "noise_db": noise_db, "silences": ranges,
            "total_silence": round(sum(e - s for s, e in ranges), 2)}


@mcp.tool()
def audio_loudness(project: str, asset_id: str | None = None, render_path: str | None = None,
                   window: float = 1.0) -> dict:
    """Integrated loudness (LUFS), true peak, and a per-window loudness curve for an asset
    or a rendered file. YouTube normalises to about -14 LUFS."""
    store = _store(project)
    if render_path:
        path = render_path
    elif asset_id:
        path = _asset_path(store.load(), asset_id)
    else:
        raise ValueError("Pass asset_id or render_path")
    return P.loudness_profile(path, window, store.cache_dir("analysis"))


@mcp.tool()
def audio_waveform(project: str, asset_id: str | None = None, render_path: str | None = None,
                   start: float = 0.0, end: float | None = None) -> list:
    """Waveform image of an asset's or a rendered file's audio, to spot speech, pauses and peaks."""
    store = _store(project)
    path = render_path or (_asset_path(store.load(), asset_id) if asset_id else None)
    if not path:
        raise ValueError("Pass asset_id or render_path")
    out = store.cache_dir("sheets") / f"wave_{Path(path).stem}_{start:.2f}_{end or 0:.2f}.png"
    P.waveform(path, out, start, end)
    return [{"path": path, "start": start, "end": end, "image": str(out)}, Image(path=str(out))]


# ---- transcripts -------------------------------------------------------------------------

@mcp.tool()
def transcribe(project: str, asset_id: str, model: str = "base", language: str | None = None) -> dict:
    """Transcribe an asset with word timestamps (needs `pip install reelwright[transcribe]`).
    Results are cached; read them with get_transcript."""
    store = _store(project)
    p = store.load()
    tr = P.transcribe(_asset_path(p, asset_id), model, language, store.cache_dir("analysis"))
    _transcript_path(store, asset_id).write_text(json.dumps(tr), encoding="utf-8")
    return {"asset_id": asset_id, "language": tr["language"], "segments": len(tr["segments"])}


@mcp.tool()
def import_transcript(project: str, asset_id: str, subtitle_path: str) -> dict:
    """Attach an existing .srt or .vtt file (in source time) as an asset's transcript."""
    store = _store(project)
    store.load().asset(asset_id)
    tr = P.parse_subtitles(Path(subtitle_path).expanduser().read_text(encoding="utf-8-sig"))
    _transcript_path(store, asset_id).write_text(json.dumps(tr), encoding="utf-8")
    return {"asset_id": asset_id, "segments": len(tr["segments"])}


@mcp.tool()
def get_transcript(project: str, asset_id: str | None = None, start: float = 0.0,
                   end: float | None = None, query: str | None = None) -> str:
    """Read a transcript as timestamped lines. With asset_id: source time. Without it: the
    edited timeline (only words that survive the edit). `query` filters lines containing text."""
    store = _store(project)
    p = store.load()
    trs = _transcripts(store, p)
    if asset_id:
        if asset_id not in trs:
            raise ValueError(f"No transcript for {asset_id}; run transcribe or import_transcript")
        items = [{"start": s["start"], "end": s["end"], "text": s["text"]} for s in trs[asset_id]["segments"]]
        header = f"Transcript of {p.assets[asset_id].label} (source time)"
    else:
        items = cap.build_cues(p, trs, max_chars=90, max_duration=8.0)
        header = "Transcript of the edited timeline (timeline time)"
    lines = [header]
    for it in items:
        if it["end"] < start or (end is not None and it["start"] > end):
            continue
        if query and query.lower() not in it["text"].lower():
            continue
        lines.append(f"[{P.fmt_time(it['start'])} – {P.fmt_time(it['end'])}] {it['text']}")
    return "\n".join(lines) if len(lines) > 1 else header + "\n(no matching lines)"


# ---- editing -------------------------------------------------------------------------------

@mcp.tool()
def add_clip(project: str, asset_id: str, src_in: float = 0.0, src_out: float | None = None,
             index: int | None = None, volume: float = 1.0, speed: float = 1.0) -> dict:
    """Place [src_in, src_out] of an asset on the main track (appends unless `index` is given).
    Images default to 5 seconds. speed 2.0 plays it twice as fast (half the timeline length)."""
    store = _store(project)
    with store.edit(f"add clip from {asset_id}") as p:
        clip = E.add_clip(p, asset_id, src_in, src_out, index, volume, speed)
    return _edit_result(store, "Clip added", clip=clip.model_dump())


@mcp.tool()
def trim_clip(project: str, clip_id: str, src_in: float | None = None,
              src_out: float | None = None) -> dict:
    """Change a clip's in/out points (source time). Later clips ripple."""
    store = _store(project)
    with store.edit(f"trim {clip_id}") as p:
        clip = E.trim_clip(p, clip_id, src_in, src_out)
    return _edit_result(store, "Clip trimmed", clip=clip.model_dump())


@mcp.tool()
def split_clip(project: str, time: float) -> dict:
    """Split the clip under a timeline time into two clips."""
    store = _store(project)
    with store.edit(f"split at {time:.2f}s") as p:
        idx = E.split_at(p, time)
        right = p.clips[idx].id if idx < len(p.clips) else None
    return _edit_result(store, f"Split at {time:.3f}s", right_clip_id=right)


@mcp.tool()
def delete_clip(project: str, clip_id: str) -> dict:
    """Remove a clip; everything after it ripples left."""
    store = _store(project)
    with store.edit(f"delete {clip_id}") as p:
        removed = E.delete_clip(p, clip_id)
    return _edit_result(store, f"Removed {removed:.2f}s")


@mcp.tool()
def delete_range(project: str, start: float, end: float) -> dict:
    """Ripple-delete [start, end] of the timeline (e.g. a pause, a fumbled sentence).
    Works across clip boundaries; overlays and notes after it shift left."""
    store = _store(project)
    with store.edit(f"delete {start:.2f}–{end:.2f}s") as p:
        removed = E.delete_range(p, start, end)
    return _edit_result(store, f"Removed {removed:.2f}s")


@mcp.tool()
def move_clip(project: str, clip_id: str, index: int) -> dict:
    """Move a clip to a new position on the main track (0 = first)."""
    store = _store(project)
    with store.edit(f"move {clip_id} to {index}") as p:
        E.move_clip(p, clip_id, index)
    return _edit_result(store, "Clip moved")


@mcp.tool()
def set_clip_volume(project: str, clip_id: str, volume: float) -> dict:
    """Set a clip's audio gain (1.0 = unchanged, 0 = mute)."""
    store = _store(project)
    with store.edit(f"volume {clip_id}") as p:
        p.find_clip(clip_id)[1].volume = max(0.0, volume)
    return _edit_result(store, "Volume set")


@mcp.tool()
def set_clip_speed(project: str, clip_id: str, speed: float) -> dict:
    """Change a clip's playback speed (0.1–16; 2.0 = twice as fast). Audio pitch is kept.
    The timeline ripples to the new length."""
    store = _store(project)
    with store.edit(f"speed {clip_id} x{speed:g}") as p:
        clip = E.set_speed(p, clip_id, speed)
    return _edit_result(store, f"Speed set to {speed:g}x", clip=clip.model_dump())


@mcp.tool()
def set_clip_crop(project: str, clip_id: str, x: float = 0.0, y: float = 0.0,
                  w: float = 1.0, h: float = 1.0) -> dict:
    """Zoom into a region of the source frame for this clip. x, y, w, h are fractions of the
    source width/height (top-left origin), e.g. x=0.1 y=0.15 w=0.5 h=0.7. The region is then
    fitted to the canvas using the project's fit mode. Defaults reset to the full frame.
    Use inspect_media first to choose the region."""
    store = _store(project)
    with store.edit(f"crop {clip_id}") as p:
        clip = E.set_crop(p, clip_id, x, y, w, h)
    return _edit_result(store, "Crop reset" if clip.crop is None else "Crop set", clip=clip.model_dump())


@mcp.tool()
def remove_silences(project: str, noise_db: float = -35.0, min_silence: float = 0.6,
                    padding: float = 0.15, dry_run: bool = False) -> dict:
    """Cut dead air across the whole timeline: detects silence in every clip's audio and
    ripple-deletes it, keeping `padding` seconds either side so speech isn't clipped.
    Use dry_run=True to preview the cuts first."""
    store = _store(project)
    p = store.load()
    silences = {}
    for aid in {c.asset_id for c in p.clips}:
        a = p.assets[aid]
        if a.has_audio:
            silences[aid] = P.detect_silence(a.path, noise_db, min_silence, a.duration,
                                             store.cache_dir("analysis"))
    ranges = E.timeline_silences(p, silences)
    if dry_run:
        preview = p.model_copy(deep=True)
        cuts = E.remove_ranges(preview, ranges, padding, min_silence - 2 * padding)
        return {"dry_run": True, "cuts": cuts, "would_remove": round(p.duration - preview.duration, 2)}
    with store.edit("remove silences") as q:
        before = q.duration
        cuts = E.remove_ranges(q, ranges, padding, min_silence - 2 * padding)
        removed = before - q.duration
    return _edit_result(store, f"Removed {removed:.2f}s of silence in {len(cuts)} cut(s)", cuts=cuts)


@mcp.tool()
def add_text(project: str, text: str, start: float, end: float, position: Position = "lower-third",
             font_size: int = 48, color: str = "white", box: bool = True) -> dict:
    """Add a text overlay at timeline times. font_size is relative to a 1080p canvas.
    Use \\n for line breaks."""
    store = _store(project)
    with store.edit(f"add text {text[:20]!r}") as p:
        ov = E.add_text(p, text.replace("\\n", "\n"), start, end, position=position,
                        font_size=font_size, color=color, box=box)
    return _edit_result(store, "Text added", overlay=ov.model_dump())


@mcp.tool()
def update_text(project: str, overlay_id: str, text: str | None = None, start: float | None = None,
                end: float | None = None, position: Position | None = None,
                font_size: int | None = None, color: str | None = None, box: bool | None = None) -> dict:
    """Change any property of a text overlay."""
    store = _store(project)
    with store.edit(f"update {overlay_id}") as p:
        ov = E.update_text(p, overlay_id, text=text, start=start, end=end, position=position,
                           font_size=font_size, color=color, box=box)
    return _edit_result(store, "Text updated", overlay=ov.model_dump())


@mcp.tool()
def remove_text(project: str, overlay_id: str) -> dict:
    """Delete a text overlay."""
    store = _store(project)
    with store.edit(f"remove {overlay_id}") as p:
        E.remove_item(p.overlays, overlay_id, "text overlay")
    return _edit_result(store, "Text removed")


@mcp.tool()
def add_music(project: str, asset_id: str, start: float = 0.0, src_in: float = 0.0,
              duration: float | None = None, volume: float = 0.25, duck: bool = True,
              fade_in: float = 1.0, fade_out: float = 2.0) -> dict:
    """Lay background audio under the main track from timeline time `start`.
    duck=True lowers it automatically whenever the main track has sound (speech)."""
    store = _store(project)
    with store.edit(f"add music {asset_id}") as p:
        cue = E.add_music(p, asset_id, start, src_in=src_in, duration=duration, volume=volume,
                          duck=duck, fade_in=fade_in, fade_out=fade_out)
    return _edit_result(store, "Music added", music=cue.model_dump())


@mcp.tool()
def remove_music(project: str, music_id: str) -> dict:
    """Delete a music cue."""
    store = _store(project)
    with store.edit(f"remove {music_id}") as p:
        E.remove_item(p.music, music_id, "music cue")
    return _edit_result(store, "Music removed")


@mcp.tool()
def set_captions(project: str, enabled: bool = True, burn_in: bool = True, max_chars: int = 42,
                 max_duration: float = 3.5, font_size: int = 20) -> dict:
    """Turn captions on/off. Captions are generated from asset transcripts and follow the
    edit automatically. burn_in=False exports a sidecar .srt instead of drawing them."""
    store = _store(project)
    with store.edit("caption settings") as p:
        p.captions.enabled, p.captions.burn_in = enabled, burn_in
        p.captions.max_chars, p.captions.max_duration = max_chars, max_duration
        p.captions.font_size = font_size
        trs = _transcripts(store, p)
        cues = cap.build_cues(p, trs, max_chars, max_duration) if trs else []
    note = "" if cues or not enabled else " (no transcripts yet — run transcribe or import_transcript)"
    return _edit_result(store, f"Captions {'on' if enabled else 'off'}: {len(cues)} cue(s){note}")


# ---- review notes ----------------------------------------------------------------------------

@mcp.tool()
def add_note(project: str, time: float, text: str, author: str = "agent") -> dict:
    """Pin a review note to a timeline time (e.g. a question for the human)."""
    store = _store(project)
    with store.edit("add note") as p:
        note = E.add_note(p, time, text, author)
    return {"ok": True, "note": note.model_dump()}


@mcp.tool()
def list_notes(project: str, include_resolved: bool = False) -> dict:
    """Review notes (human feedback such as "cut this pause"), in timeline order."""
    p = _store(project).load()
    return {"notes": [n.model_dump() for n in p.notes if include_resolved or not n.resolved]}


@mcp.tool()
def resolve_note(project: str, note_id: str) -> dict:
    """Mark a review note as handled."""
    store = _store(project)
    with store.edit(f"resolve {note_id}") as p:
        note = next((n for n in p.notes if n.id == note_id), None)
        if note is None:
            raise KeyError(f"No note with id {note_id!r}")
        note.resolved = True
    return {"ok": True}


# ---- verification & output ------------------------------------------------------------------

@mcp.tool()
def check_frames(project: str, times: list[float], width: int = 480, columns: int = 4) -> list:
    """Render frames of the EDITED timeline at the given timeline times (overlays and captions
    included) and return them as one labelled grid. Use after every edit to verify it."""
    store = _store(project)
    p = store.load()
    if len(times) > 24:
        raise ValueError("Ask for at most 24 frames at once")
    frames = R.render_frames(p, times, store.cache_dir("frames"), store.cache_dir("work"),
                             _transcripts(store, p), width)
    out = store.cache_dir("sheets") / "check_frames.jpg"
    P.compose_grid([(f, P.fmt_time(t)) for f, t in zip(frames, times)], out, columns,
                   title=f"{p.name} — edited timeline")
    return [{"times": times, "image": str(out)}, Image(path=str(out))]


@mcp.tool()
def timeline_contact_sheet(project: str, count: int = 12, start: float = 0.0,
                           end: float | None = None) -> list:
    """Evenly spaced frames across the edited timeline (or a range of it) in one image."""
    p = _store(project).load()
    end = p.duration if end is None else end
    count = max(1, min(count, 24))
    times = [round(start + (end - start) * (i + 0.5) / count, 3) for i in range(count)]
    return check_frames(project, times, width=360, columns=4)


@mcp.tool()
def render_preview(project: str, start: float | None = None, end: float | None = None) -> dict:
    """Fast low-res MP4 of the edit (or of [start, end]) for review. Returns the file path."""
    store = _store(project)
    p = store.load()
    tag = "full" if start is None and end is None else f"{start or 0:.1f}-{end if end is not None else p.duration:.1f}"
    out = store.renders_dir() / f"preview_{tag}.mp4"
    return R.render(p, out, store.cache_dir("work"), "preview", _transcripts(store, p),
                    start=start, end=end)


@mcp.tool()
def export(project: str, preset: Literal["youtube", "youtube_4k", "shorts", "square", "project"] = "youtube",
           filename: str | None = None, encoder: Literal["libx264", "h264_nvenc", "h264_qsv", "h264_amf",
                                                         "h264_videotoolbox"] = "libx264",
           quality: Literal["draft", "standard", "best"] = "standard") -> dict:
    """Final render. Presets set resolution and loudness (-14 LUFS for youtube/shorts/square).
    Use a GPU encoder (h264_nvenc etc.) if the machine has one. Writes a sidecar .srt
    when captions are enabled."""
    store = _store(project)
    p = store.load()
    out = store.renders_dir() / (filename or f"{store.slug}_{preset}.mp4")
    trs = _transcripts(store, p)
    result = R.render(p, out, store.cache_dir("work"), preset, trs, encoder=encoder, quality=quality)
    if p.captions.enabled and trs:
        cues = cap.build_cues(p, trs, p.captions.max_chars, p.captions.max_duration)
        if cues:
            result["srt"] = str(cap.write_srt(cues, out.with_suffix(".srt")))
    return result


# ---- review UI -----------------------------------------------------------------------------

@mcp.tool()
def open_review(project: str | None = None, open_browser: bool = True) -> dict:
    """Start the local review UI (a web page on this machine) and optionally open it in the
    browser. The human can watch a preview, scrub the timeline and pin notes for you; read
    them with list_notes. The page updates live as you edit."""
    from . import review
    if project:
        _store(project)  # validate the name
    url = review.url_for(review.start(), project)
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    return {"url": url, "hint": "Notes left in the page appear in list_notes."}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
