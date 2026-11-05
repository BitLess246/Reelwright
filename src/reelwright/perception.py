"""Tools that let an agent *see* and *hear* media without watching it in real time.

Everything here works on a single media file in source time. Results that are
expensive to compute (scene cuts, silence, transcripts) are cached on disk.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .ffmpeg import FFmpegError, ffmpeg, parse_rate, probe
from .models import Asset, new_id

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}


def fmt_time(t: float) -> str:
    m, s = divmod(max(0.0, t), 60)
    h, m = divmod(int(m), 60)
    return f"{h}:{m:02d}:{s:05.2f}" if h else f"{m:02d}:{s:05.2f}"


# ---- caching ------------------------------------------------------------------

def _cache_key(path: str | Path, **params) -> str:
    p = Path(path)
    st = p.stat()
    raw = json.dumps([str(p.resolve()), st.st_size, int(st.st_mtime), params], sort_keys=True)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _cached(cache_dir: Path | None, name: str, path: str | Path, params: dict, compute):
    if cache_dir is None:
        return compute()
    file = Path(cache_dir) / f"{name}-{_cache_key(path, **params)}.json"
    if file.exists():
        return json.loads(file.read_text(encoding="utf-8"))
    result = compute()
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(result), encoding="utf-8")
    return result


# ---- probing ------------------------------------------------------------------

def probe_asset(path: str | Path, label: str | None = None) -> Asset:
    path = Path(path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"No such file: {path}")
    info = probe(path)
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = info.get("format", {}).get("duration")
    duration = float(duration) if duration not in (None, "N/A") else None

    is_image = path.suffix.lower() in IMAGE_EXTS
    kind = "image" if is_image else ("video" if video else "audio")
    if kind == "audio" and not audio:
        raise ValueError(f"{path.name} has no video or audio streams")
    return Asset(
        id=new_id("asset"),
        path=str(path),
        kind=kind,
        duration=None if is_image else duration,
        width=int(video["width"]) if video else None,
        height=int(video["height"]) if video else None,
        fps=None if is_image or not video else parse_rate(video.get("avg_frame_rate") or video.get("r_frame_rate")),
        has_video=bool(video),
        has_audio=bool(audio) and not is_image,
        label=label or path.stem,
    )


# ---- frames & contact sheets ------------------------------------------------

def extract_frame(path: str | Path, t: float, out: Path, width: int = 640) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    seek = [] if Path(path).suffix.lower() in IMAGE_EXTS else ["-ss", f"{max(0.0, t):.3f}"]
    ffmpeg(["-y", *seek, "-i", str(path), "-frames:v", "1",
            "-vf", f"scale={width}:-2", "-q:v", "3", str(out)])
    if not out.exists():
        raise FFmpegError(f"Could not extract a frame at {t:.2f}s (past the end of the file?)")
    return out


def _font(size: int):
    for name in ("DejaVuSans-Bold.ttf", "arialbd.ttf", "Arial Bold.ttf", "Helvetica.ttc"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def compose_grid(frames: list[tuple[Path, str]], out: Path, columns: int = 4,
                 title: str | None = None) -> Path:
    """Tile labelled frames into one image so many moments can be inspected at once."""
    if not frames:
        raise ValueError("No frames to compose")
    images = [Image.open(p).convert("RGB") for p, _ in frames]
    w = max(im.width for im in images)
    h = max(im.height for im in images)
    columns = max(1, min(columns, len(images)))
    rows = (len(images) + columns - 1) // columns
    pad, header = 6, (34 if title else 0)
    sheet = Image.new("RGB", (columns * (w + pad) + pad, rows * (h + pad) + pad + header), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    label_font = _font(max(12, w // 30))
    if title:
        draw.text((pad + 4, 8), title, fill=(235, 235, 235), font=_font(18))
    for i, (im, (_, label)) in enumerate(zip(images, frames)):
        x = pad + (i % columns) * (w + pad)
        y = header + pad + (i // columns) * (h + pad)
        sheet.paste(im, (x, y))
        box = draw.textbbox((0, 0), label, font=label_font)
        bw, bh = box[2] - box[0] + 10, box[3] - box[1] + 8
        draw.rectangle([x, y + h - bh, x + bw, y + h], fill=(0, 0, 0))
        draw.text((x + 5, y + h - bh + 2), label, fill=(255, 230, 0), font=label_font)
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, quality=88)
    return out


def contact_sheet(path: str | Path, start: float, end: float, count: int, out: Path,
                  columns: int = 4, width: int = 360, title: str | None = None) -> Path:
    count = max(1, min(count, 48))
    span = max(0.0, end - start)
    times = [start + span * (i + 0.5) / count for i in range(count)]
    tmp = out.parent / f".{out.stem}-frames"
    frames = []
    for i, t in enumerate(times):
        frames.append((extract_frame(path, t, tmp / f"{i:03d}.jpg", width), fmt_time(t)))
    return compose_grid(frames, out, columns, title)


# ---- scenes, silence, loudness ------------------------------------------------

def detect_scenes(path: str | Path, threshold: float = 0.3, cache_dir: Path | None = None) -> list[float]:
    def compute():
        proc = ffmpeg(["-i", str(path), "-an", "-sn",
                       "-vf", f"scale=320:-2,select='gt(scene,{threshold})',showinfo",
                       "-f", "null", "-"])
        return [round(float(m), 3) for m in re.findall(r"pts_time:([0-9.]+)", proc.stderr)]
    return _cached(cache_dir, "scenes", path, {"threshold": threshold}, compute)


def detect_silence(path: str | Path, noise_db: float = -35.0, min_duration: float = 0.5,
                   duration: float | None = None, cache_dir: Path | None = None) -> list[list[float]]:
    def compute():
        proc = ffmpeg(["-i", str(path), "-vn", "-sn",
                       "-af", f"silencedetect=noise={noise_db}dB:d={min_duration}",
                       "-f", "null", "-"])
        ranges, start = [], None
        for kind, value in re.findall(r"silence_(start|end): (-?[0-9.]+)", proc.stderr):
            v = max(0.0, float(value))
            if kind == "start":
                start = v
            elif start is not None:
                ranges.append([round(start, 3), round(v, 3)])
                start = None
        if start is not None and duration:
            ranges.append([round(start, 3), round(duration, 3)])
        return ranges
    return _cached(cache_dir, "silence", path, {"db": noise_db, "d": min_duration}, compute)


def loudness_profile(path: str | Path, window: float = 1.0, cache_dir: Path | None = None) -> dict:
    """Integrated loudness plus a per-window momentary loudness curve (LUFS)."""
    def compute():
        proc = ffmpeg(["-i", str(path), "-vn", "-sn", "-af", "ebur128=peak=true",
                       "-f", "null", "-"])
        buckets: dict[int, float] = {}
        for t, m in re.findall(r"t:\s*([0-9.]+)\s+TARGET:.*?M:\s*(-?[0-9.]+|-inf)", proc.stderr):
            if m == "-inf":
                continue
            b = int(float(t) // window)
            buckets[b] = max(buckets.get(b, -120.0), float(m))
        integrated = re.findall(r"I:\s+(-?[0-9.]+) LUFS", proc.stderr)
        peak = re.findall(r"Peak:\s+(-?[0-9.]+|-inf) dBFS", proc.stderr)
        return {
            "integrated_lufs": float(integrated[-1]) if integrated else None,
            "true_peak_dbfs": float(peak[-1]) if peak and peak[-1] != "-inf" else None,
            "window_seconds": window,
            "curve": [[round(b * window, 2), round(v, 1)] for b, v in sorted(buckets.items())],
        }
    return _cached(cache_dir, "loudness", path, {"window": window}, compute)


def waveform(path: str | Path, out: Path, start: float = 0.0, end: float | None = None,
             size: str = "1600x240") -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    trim = ["-ss", f"{start:.3f}"] + (["-to", f"{end:.3f}"] if end else [])
    ffmpeg(["-y", *trim, "-i", str(path), "-filter_complex",
            f"[0:a]aformat=channel_layouts=mono,showwavespic=s={size}:colors=0x4cc9f0[w]",
            "-map", "[w]", "-frames:v", "1", str(out)])
    return out


# ---- transcripts --------------------------------------------------------------

def transcribe(path: str | Path, model: str = "base", language: str | None = None,
               cache_dir: Path | None = None) -> dict:
    """Word-timestamped transcript via faster-whisper (optional dependency)."""
    def compute():
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "Transcription needs the optional dependency: pip install 'reelwright[transcribe]'. "
                "Alternatively, import an existing .srt/.vtt file with import_transcript."
            ) from e
        wm = WhisperModel(model, device="auto", compute_type="auto")
        segments, info = wm.transcribe(str(path), language=language, word_timestamps=True,
                                       vad_filter=True)
        segs = []
        for s in segments:
            segs.append({
                "start": round(s.start, 3), "end": round(s.end, 3), "text": s.text.strip(),
                "words": [{"start": round(w.start, 3), "end": round(w.end, 3), "word": w.word.strip()}
                          for w in (s.words or [])],
            })
        return {"language": info.language, "source": f"faster-whisper:{model}", "segments": segs}
    return _cached(cache_dir, "transcript", path, {"model": model, "lang": language}, compute)


_TS = r"(\d+):(\d{2}):(\d{2})[.,](\d{1,3})|(\d{2}):(\d{2})[.,](\d{1,3})"


def _ts(value: str) -> float:
    m = re.fullmatch(_TS, value.strip())
    if not m:
        raise ValueError(f"Bad timestamp {value!r}")
    if m.group(1):
        h, mi, s, ms = m.group(1, 2, 3, 4)
    else:
        h, (mi, s, ms) = "0", m.group(5, 6, 7)
    return int(h) * 3600 + int(mi) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def parse_subtitles(text: str) -> dict:
    """Parse SRT or WebVTT into the transcript format (segment-level only)."""
    segs = []
    for block in re.split(r"\n\s*\n", text.replace("\r", "").strip()):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        for i, line in enumerate(lines):
            if "-->" in line:
                a, b = line.split("-->", 1)
                body = " ".join(lines[i + 1:]).strip()
                body = re.sub(r"<[^>]+>", "", body)
                if body:
                    segs.append({"start": round(_ts(a), 3), "end": round(_ts(b.split()[0]), 3),
                                 "text": body, "words": []})
                break
    return {"language": None, "source": "imported", "segments": segs}
