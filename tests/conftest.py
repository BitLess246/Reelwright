import shutil
import subprocess
from pathlib import Path

import pytest

requires_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def _ff(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


@pytest.fixture(scope="session")
def media(tmp_path_factory) -> dict[str, Path]:
    """Small synthetic clips: speech-like tone with a 1.5 s gap, a hard scene cut, music, an image."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    d = tmp_path_factory.mktemp("media")
    _ff("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=6",
        "-f", "lavfi", "-i", "sine=f=440:d=6,volume='if(between(t,2,3.5),0,1)':eval=frame",
        "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(d / "talk.mp4"))
    _ff("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=2",
        "-f", "lavfi", "-i", "smptebars=size=640x360:rate=30:duration=2",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]",
        "-c:v", "libx264", "-preset", "ultrafast", str(d / "cut.mp4"))
    _ff("-f", "lavfi", "-i", "sine=f=220:d=20", "-c:a", "libmp3lame", str(d / "music.mp3"))
    _ff("-f", "lavfi", "-i", "color=c=teal:size=400x400", "-frames:v", "1", str(d / "logo.png"))
    (d / "talk.srt").write_text(
        "1\n00:00:00,200 --> 00:00:01,800\nHello and welcome.\n\n"
        "2\n00:00:03,600 --> 00:00:05,800\nToday we talk about beams.\n", encoding="utf-8")
    return {p.stem if p.suffix != ".srt" else "srt": p for p in d.iterdir()}


@pytest.fixture()
def reelwright_home(tmp_path, monkeypatch):
    monkeypatch.setenv("REELWRIGHT_HOME", str(tmp_path / "projects"))
    return tmp_path / "projects"
