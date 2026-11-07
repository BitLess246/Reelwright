"""Turn a Project into a single ffmpeg filter graph and render it."""

from __future__ import annotations

import os
import platform
import time
from dataclasses import dataclass
from pathlib import Path

from . import captions as cap
from .editing import slice_project
from .ffmpeg import escape_filter_value, ffmpeg, filter_script_args
from .models import Project

# ---- presets ------------------------------------------------------------------

PRESETS: dict[str, dict] = {
    "preview":    {"width": 640,  "height": None, "crf": 32, "audio_bitrate": "96k",  "loudnorm": None, "max_fps": 30},
    "project":    {"width": None, "height": None, "crf": 20, "audio_bitrate": "192k", "loudnorm": None},
    "youtube":    {"width": 1920, "height": 1080, "crf": 20, "audio_bitrate": "192k", "loudnorm": -14.0},
    "youtube_4k": {"width": 3840, "height": 2160, "crf": 20, "audio_bitrate": "256k", "loudnorm": -14.0},
    "shorts":     {"width": 1080, "height": 1920, "crf": 21, "audio_bitrate": "192k", "loudnorm": -14.0, "fit": "cover"},
    "square":     {"width": 1080, "height": 1080, "crf": 21, "audio_bitrate": "192k", "loudnorm": -14.0, "fit": "cover"},
}

X264_SPEED = {"draft": "veryfast", "standard": "veryfast", "best": "slow"}
ENCODERS = {"libx264", "h264_nvenc", "h264_qsv", "h264_amf", "h264_videotoolbox"}

POSITIONS = {
    "top":         ("(w-text_w)/2", "h*0.07"),
    "center":      ("(w-text_w)/2", "(h-text_h)/2"),
    "bottom":      ("(w-text_w)/2", "h-text_h-h*0.08"),
    "lower-third": ("w*0.06",       "h*0.72"),
}


def find_font(preferred: str | None = None) -> str | None:
    candidates = [preferred] if preferred else []
    system = platform.system()
    if system == "Windows":
        windir = os.environ.get("WINDIR", "C:/Windows")
        candidates += [f"{windir}/Fonts/segoeuib.ttf", f"{windir}/Fonts/arialbd.ttf", f"{windir}/Fonts/arial.ttf"]
    elif system == "Darwin":
        candidates += ["/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                       "/System/Library/Fonts/Helvetica.ttc", "/Library/Fonts/Arial.ttf"]
    candidates += ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                   "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
                   "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"]
    return next((c for c in candidates if c and Path(c).exists()), None)


@dataclass
class Canvas:
    width: int
    height: int
    fps: float
    fit: str
    background: str
    sample_rate: int


def resolve_canvas(project: Project, preset: str) -> tuple[Canvas, dict]:
    if preset not in PRESETS:
        raise ValueError(f"Unknown preset {preset!r}. Choose from: {', '.join(PRESETS)}")
    p = PRESETS[preset]
    s = project.settings
    w, h = p["width"] or s.width, p["height"]
    if h is None:
        h = s.height if p["width"] is None else int(round(w * s.height / s.width / 2) * 2)
    fps = min(s.fps, p.get("max_fps", s.fps))
    # The project's own fit wins when it was chosen deliberately (cover/blur), or when the
    # project is already this shape. The preset's fit only applies when reframing a
    # differently shaped "contain" project (e.g. a 16:9 edit exported as a Short).
    same_shape = abs(w / h - s.width / s.height) < 0.01
    fit = s.fit if (s.fit != "contain" or same_shape) else p.get("fit", s.fit)
    return Canvas(int(w), int(h), fps, fit, s.background, s.sample_rate), p


# ---- graph building -------------------------------------------------------------

def caption_cues(project: Project, transcripts: dict[str, dict] | None,
                 start: float = 0.0, end: float | None = None) -> list[dict]:
    """Caption cues for the full edit, shifted into the [start, end] window."""
    if not (project.captions.enabled and project.captions.burn_in and transcripts):
        return []
    end = project.duration if end is None else end
    out = []
    for c in cap.build_cues(project, transcripts, project.captions.max_chars,
                            project.captions.max_duration):
        if c["end"] > start and c["start"] < end:
            out.append({"start": max(0.0, c["start"] - start),
                        "end": min(end, c["end"]) - start, "text": c["text"]})
    return out


def build_graph(project: Project, canvas: Canvas, workdir: Path,
                cues: list[dict] | None = None) -> tuple[list[str], str, float]:
    """Returns (input args, filter_complex string, total duration). Output pads: [vout] [aout]."""
    if not project.clips:
        raise ValueError("The timeline is empty — add a clip first")
    W, H, fps, sr = canvas.width, canvas.height, canvas.fps, canvas.sample_rate
    inputs: list[str] = []
    chains: list[str] = []
    concat_pads: list[str] = []
    n_in = 0

    def fit_chain(src: str, i: int, pre: str) -> str:
        """Filters that place one clip on the canvas; output pad [v{i}]."""
        if canvas.fit == "blur":
            # background: the same frame, cropped to fill and blurred; foreground: contained
            bw, bh = max(2, W // 4 // 2 * 2), max(2, H // 4 // 2 * 2)
            return (
                f"{src}{pre}split=2[bg{i}][fg{i}];"
                f"[bg{i}]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
                f"gblur=sigma=8,eq=brightness=-0.12:saturation=0.9,scale={W}:{H}[bgb{i}];"
                f"[fg{i}]scale={W}:{H}:force_original_aspect_ratio=decrease[fgs{i}];"
                f"[bgb{i}][fgs{i}]overlay=(W-w)/2:(H-h)/2,"
            )
        if canvas.fit == "cover":
            fit = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"
        else:
            fit = (f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
                   f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color={canvas.background}")
        return f"{src}{pre}{fit},"

    for i, clip in enumerate(project.clips):
        asset = project.asset(clip.asset_id)
        d = clip.duration
        if asset.kind == "image":
            inputs += ["-loop", "1", "-framerate", f"{fps:g}", "-t", f"{d:.4f}", "-i", asset.path]
        else:
            inputs += ["-ss", f"{clip.src_in:.4f}", "-t", f"{clip.source_length:.4f}", "-i", asset.path]
        idx = n_in
        n_in += 1
        pre = ""
        if clip.crop:
            c = clip.crop
            pre += (f"crop=trunc(iw*{c.w:.4f}/2)*2:trunc(ih*{c.h:.4f}/2)*2:"
                    f"iw*{c.x:.4f}:ih*{c.y:.4f},")
        if clip.speed != 1 and asset.kind != "image":
            pre += f"setpts=(PTS-STARTPTS)/{clip.speed:g},"
        chains.append(
            fit_chain(f"[{idx}:v]", i, pre) +
            f"setsar=1,fps={fps:g},format=yuv420p,"
            f"tpad=stop_mode=clone:stop_duration={d:.4f},trim=duration={d:.4f},setpts=PTS-STARTPTS[v{i}]"
        )
        if asset.has_audio:
            tempo = ""
            if clip.speed != 1:
                parts, sp = [], clip.speed
                while sp > 2.0:
                    parts.append("atempo=2.0"); sp /= 2.0
                while sp < 0.5:
                    parts.append("atempo=0.5"); sp /= 0.5
                parts.append(f"atempo={sp:.6g}")
                tempo = ",".join(parts) + ","
            chains.append(
                f"[{idx}:a]aresample={sr},aformat=sample_fmts=fltp:channel_layouts=stereo,{tempo}"
                f"volume={clip.volume:g},apad,atrim=duration={d:.4f},asetpts=PTS-STARTPTS[a{i}]"
            )
        else:
            chains.append(f"anullsrc=r={sr}:cl=stereo,atrim=duration={d:.4f}[a{i}]")
        concat_pads.append(f"[v{i}][a{i}]")

    total = project.duration
    chains.append(f"{''.join(concat_pads)}concat=n={len(project.clips)}:v=1:a=1[vcat][acat]")

    # --- text overlays
    vlabel = "vcat"
    font = find_font(project.settings.font_file)
    scale = H / 1080
    for k, ov in enumerate(project.overlays):
        if ov.start >= total:
            continue
        # bytes, not text mode: on Windows text mode turns \n into \r\n and drawtext
        # renders the \r as an extra blank line
        (workdir / f"text_{k}.txt").write_bytes(ov.text.replace("\r\n", "\n").encode("utf-8"))
        x, y = POSITIONS[ov.position]
        opts = [
            *( [f"fontfile={escape_filter_value(font)}"] if font else [] ),
            f"textfile=text_{k}.txt", f"fontsize={max(8, int(ov.font_size * scale))}",
            f"fontcolor={ov.color}", f"x={x}", f"y={y}",
            f"enable='between(t,{ov.start:.3f},{min(ov.end, total):.3f})'",
        ]
        if ov.box:
            opts += ["box=1", "boxcolor=black@0.55", f"boxborderw={max(4, int(18 * scale))}"]
        chains.append(f"[{vlabel}]drawtext={':'.join(opts)}[vt{k}]")
        vlabel = f"vt{k}"

    # --- burned-in captions
    if cues:
        cap.write_srt(cues, workdir / "captions.srt")
        style = (f"Fontsize={project.captions.font_size},Bold=1,Outline=2,Shadow=0,"
                 f"BorderStyle=1,MarginV=22")
        chains.append(f"[{vlabel}]subtitles=captions.srt:force_style='{style}'[vcap]")
        vlabel = "vcap"
    chains.append(f"[{vlabel}]null[vout]")

    # --- music
    ducked, plain = [], []
    for k, cue in enumerate(project.music):
        if cue.start >= total:
            continue
        asset = project.asset(cue.asset_id)
        avail = (asset.duration - cue.src_in) if asset.duration else total
        length = min(cue.duration if cue.duration is not None else avail, total - cue.start, avail)
        if length <= 0.05:
            continue
        inputs += ["-ss", f"{cue.src_in:.4f}", "-t", f"{length:.4f}", "-i", asset.path]
        idx = n_in
        n_in += 1
        fades = []
        if cue.fade_in > 0:
            fades.append(f"afade=t=in:d={min(cue.fade_in, length / 2):.3f}")
        if cue.fade_out > 0:
            fo = min(cue.fade_out, length / 2)
            fades.append(f"afade=t=out:st={length - fo:.3f}:d={fo:.3f}")
        delay = int(round(cue.start * 1000))
        chain = (f"[{idx}:a]aresample={sr},aformat=sample_fmts=fltp:channel_layouts=stereo,"
                 f"volume={cue.volume:g}," + ",".join(fades + [f"adelay={delay}:all=1"]) + f"[m{k}]")
        chains.append(chain.replace(",,", ","))
        (ducked if cue.duck else plain).append(f"[m{k}]")

    alabel = "acat"
    if ducked or plain:
        parts = []
        if ducked:
            if len(ducked) > 1:
                chains.append(f"{''.join(ducked)}amix=inputs={len(ducked)}:normalize=0[mduck_in]")
                din = "[mduck_in]"
            else:
                din = ducked[0]
            chains.append("[acat]asplit=2[voice][sc]")
            chains.append(f"{din}[sc]sidechaincompress=threshold=0.02:ratio=10:attack=15:release=350[mducked]")
            parts = ["[voice]", "[mducked]"]
        else:
            parts = ["[acat]"]
        parts += plain
        chains.append(f"{''.join(parts)}amix=inputs={len(parts)}:normalize=0:duration=first[amix]")
        alabel = "amix"
    chains.append(f"[{alabel}]anull[aout]")
    return inputs, ";\n".join(chains), total


def _encoder_args(encoder: str, quality: str, crf: int) -> list[str]:
    if encoder not in ENCODERS:
        raise ValueError(f"Unknown encoder {encoder!r}. Choose from: {', '.join(sorted(ENCODERS))}")
    if encoder == "libx264":
        return ["-c:v", "libx264", "-preset", X264_SPEED.get(quality, "veryfast"), "-crf", str(crf)]
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p7" if quality == "best" else "p4", "-cq", str(crf + 3)]
    if encoder == "h264_qsv":
        return ["-c:v", "h264_qsv", "-global_quality", str(crf + 2)]
    if encoder == "h264_amf":
        return ["-c:v", "h264_amf", "-rc", "cqp", "-qp_i", str(crf), "-qp_p", str(crf + 2)]
    return ["-c:v", "h264_videotoolbox", "-q:v", "60"]


# ---- public API ------------------------------------------------------------------

def render(project: Project, out: Path, workdir: Path, preset: str = "preview",
           transcripts: dict[str, dict] | None = None, encoder: str = "libx264",
           quality: str = "standard", start: float | None = None, end: float | None = None,
           timeout: float | None = None) -> dict:
    """Render the whole timeline (or the [start, end] range of it) to an MP4."""
    s = start or 0.0
    e = project.duration if end is None else min(end, project.duration)
    cues = caption_cues(project, transcripts, s, e)
    if start is not None or end is not None:
        project = slice_project(project, s, e)
    canvas, p = resolve_canvas(project, preset)
    workdir.mkdir(parents=True, exist_ok=True)
    inputs, graph, total = build_graph(project, canvas, workdir, cues)
    if p.get("loudnorm") is not None:
        graph = graph.replace("[aout]", "[apre]") + f";\n[apre]loudnorm=I={p['loudnorm']}:TP=-1.5:LRA=11[aout]"
    (workdir / "graph.txt").write_text(graph, encoding="utf-8")
    out = Path(out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    ffmpeg(["-y", *inputs, *filter_script_args("graph.txt"),
            "-map", "[vout]", "-map", "[aout]",
            *_encoder_args(encoder, quality, p["crf"]), "-pix_fmt", "yuv420p",
            "-r", f"{canvas.fps:g}", "-c:a", "aac", "-b:a", p["audio_bitrate"], "-ar", str(canvas.sample_rate),
            "-movflags", "+faststart", "-t", f"{total:.4f}", str(out)],
           timeout=timeout, cwd=workdir)
    return {"path": str(out), "duration": round(total, 3), "width": canvas.width,
            "height": canvas.height, "fps": canvas.fps, "preset": preset,
            "render_seconds": round(time.time() - t0, 1)}


def render_frames(project: Project, times: list[float], out_dir: Path, workdir: Path,
                  transcripts: dict[str, dict] | None = None, width: int = 640) -> list[Path]:
    """Render single frames of the *edited* timeline (overlays and captions included)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    fps = project.settings.fps
    for i, t in enumerate(times):
        t = min(max(0.0, t), max(0.0, project.duration - 1 / fps))
        t_end = min(project.duration, t + 2 / fps)
        sliced = slice_project(project, t, t_end)
        cues = caption_cues(project, transcripts, t, t_end)
        canvas, _ = resolve_canvas(sliced, "preview")
        canvas.width = width
        canvas.height = int(round(width * project.settings.height / project.settings.width / 2) * 2)
        wd = workdir / f"frame_{i}"
        wd.mkdir(parents=True, exist_ok=True)
        inputs, graph, _ = build_graph(sliced, canvas, wd, cues)
        (wd / "graph.txt").write_text(graph + ";\n[aout]anullsink", encoding="utf-8")
        out = (out_dir / f"t{t:09.3f}.jpg").resolve()
        ffmpeg(["-y", *inputs, *filter_script_args("graph.txt"), "-map", "[vout]",
                "-frames:v", "1", "-q:v", "3", str(out)], cwd=wd)
        frames.append(out)
    return frames
