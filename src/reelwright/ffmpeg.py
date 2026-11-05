"""Thin wrappers around the ffmpeg / ffprobe command-line tools."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path


class FFmpegError(RuntimeError):
    pass


def ffmpeg_bin() -> str:
    return os.environ.get("REELWRIGHT_FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"


def ffprobe_bin() -> str:
    return os.environ.get("REELWRIGHT_FFPROBE") or shutil.which("ffprobe") or "ffprobe"


def run(args: list[str], timeout: float | None = None,
        cwd: str | Path | None = None) -> subprocess.CompletedProcess:
    """Run a command, raising FFmpegError with the tail of stderr on failure."""
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace", cwd=cwd,
        )
    except FileNotFoundError as e:
        raise FFmpegError(
            f"{args[0]} not found. Install ffmpeg and make sure it is on PATH "
            "(or set REELWRIGHT_FFMPEG / REELWRIGHT_FFPROBE)."
        ) from e
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-15:])
        raise FFmpegError(f"{Path(args[0]).name} failed (exit {proc.returncode}):\n{tail}")
    return proc


def ffmpeg(args: list[str], timeout: float | None = None,
           cwd: str | Path | None = None) -> subprocess.CompletedProcess:
    return run([ffmpeg_bin(), "-hide_banner", "-nostdin", *args], timeout=timeout, cwd=cwd)


_MAJOR: int | None = None


def ffmpeg_major_version() -> int:
    global _MAJOR
    if _MAJOR is None:
        out = run([ffmpeg_bin(), "-hide_banner", "-version"]).stdout
        m = re.search(r"ffmpeg version n?(\d+)", out)
        _MAJOR = int(m.group(1)) if m else 6
    return _MAJOR


def filter_script_args(filename: str) -> list[str]:
    """Read a filter graph from a file (avoids command-line length limits on Windows)."""
    if ffmpeg_major_version() >= 7:
        return ["-/filter_complex", filename]
    return ["-filter_complex_script", filename]


def probe(path: str | Path) -> dict:
    proc = run([
        ffprobe_bin(), "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ])
    return json.loads(proc.stdout or "{}")


def escape_filter_value(value: str) -> str:
    """Escape a value (usually a path) for use inside an ffmpeg filter graph.

    Forward slashes work on every platform, and the drive colon on Windows
    (``C:/``) has to be escaped because ``:`` separates filter options.
    """
    v = str(value).replace("\\", "/")
    v = v.replace(":", r"\:").replace("'", r"'\''")
    return f"'{v}'"


def parse_rate(rate: str | None) -> float | None:
    if not rate or rate in ("0/0", "0"):
        return None
    if "/" in rate:
        num, den = rate.split("/", 1)
        try:
            return float(num) / float(den) if float(den) else None
        except ValueError:
            return None
    try:
        return float(rate)
    except ValueError:
        return None
