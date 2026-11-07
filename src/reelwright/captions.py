"""Map per-asset transcripts onto the edited timeline and build caption cues."""

from __future__ import annotations

from pathlib import Path

from .models import Project


def _srt_time(t: float) -> str:
    ms = int(round(max(0.0, t) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def timeline_words(project: Project, transcripts: dict[str, dict]) -> list[dict]:
    """Every transcript word (or segment, if no word timing) that survives the edit,
    in timeline time. Each item: {start, end, text, clip_id}."""
    items: list[dict] = []
    for start, clip in zip(project.clip_starts(), project.clips):
        tr = transcripts.get(clip.asset_id)
        if not tr:
            continue
        for seg in tr.get("segments", []):
            words = seg.get("words") or []
            if words:
                for w in words:
                    mid = (w["start"] + w["end"]) / 2
                    if clip.src_in <= mid < clip.src_out:
                        items.append({
                            "start": start + clip.to_offset(max(w["start"], clip.src_in)),
                            "end": start + clip.to_offset(min(w["end"], clip.src_out)),
                            "text": w["word"], "clip_id": clip.id,
                        })
            else:
                lo, hi = max(seg["start"], clip.src_in), min(seg["end"], clip.src_out)
                # keep a segment only if most of it survived the edit
                if hi - lo > 0.5 * (seg["end"] - seg["start"]):
                    items.append({"start": start + clip.to_offset(lo), "end": start + clip.to_offset(hi),
                                  "text": seg["text"], "clip_id": clip.id, "segment": True})
    items.sort(key=lambda x: x["start"])
    return items


def build_cues(project: Project, transcripts: dict[str, dict], max_chars: int = 42,
               max_duration: float = 3.5, max_gap: float = 0.6) -> list[dict]:
    cues: list[dict] = []
    cur: dict | None = None

    def flush():
        nonlocal cur
        if cur and cur["text"].strip():
            cues.append({"start": round(cur["start"], 3), "end": round(cur["end"], 3),
                         "text": cur["text"].strip()})
        cur = None

    for item in timeline_words(project, transcripts):
        if item.get("segment"):
            flush()
            cues.append({"start": round(item["start"], 3), "end": round(item["end"], 3),
                         "text": item["text"].strip()})
            continue
        if cur is not None:
            too_long = len(cur["text"]) + 1 + len(item["text"]) > max_chars
            too_slow = item["end"] - cur["start"] > max_duration
            gap = item["start"] - cur["end"] > max_gap
            new_clip = item["clip_id"] != cur["clip_id"]
            if too_long or too_slow or gap or new_clip:
                flush()
        if cur is None:
            cur = {"start": item["start"], "end": item["end"], "text": item["text"],
                   "clip_id": item["clip_id"]}
        else:
            cur["end"] = item["end"]
            cur["text"] += " " + item["text"]
        if cur["text"].rstrip().endswith((".", "?", "!")):
            flush()
    flush()
    # never let cues overlap
    for a, b in zip(cues, cues[1:]):
        if a["end"] > b["start"]:
            a["end"] = b["start"]
    return cues


def write_srt(cues: list[dict], out: Path) -> Path:
    lines = []
    for i, c in enumerate(cues, 1):
        lines += [str(i), f"{_srt_time(c['start'])} --> {_srt_time(c['end'])}", c["text"], ""]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes("\n".join(lines).encode("utf-8"))
    return out
