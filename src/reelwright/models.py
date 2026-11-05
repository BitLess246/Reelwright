"""Timeline data model.

The timeline is deliberately simple and fully serialisable to JSON so an agent
(or a human) can read the whole edit at a glance:

* ``clips`` form the main track. They play back-to-back, so a clip's position
  on the timeline is implied by the clips before it. Removing a clip ripples
  everything after it automatically.
* ``overlays`` are text layers placed at absolute timeline times.
* ``music`` cues are background audio placed at absolute timeline times and
  mixed under the main track, optionally ducked under speech.
* ``notes`` are review comments pinned to timeline times (from a human or an
  agent) so feedback survives between sessions.
"""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, Field


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


class Asset(BaseModel):
    id: str
    path: str
    kind: Literal["video", "audio", "image"]
    duration: float | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    has_video: bool = False
    has_audio: bool = False
    label: str | None = None


class Clip(BaseModel):
    id: str = Field(default_factory=lambda: new_id("clip"))
    asset_id: str
    src_in: float = 0.0
    src_out: float
    volume: float = 1.0

    @property
    def duration(self) -> float:
        return max(0.0, self.src_out - self.src_in)


Position = Literal["top", "center", "bottom", "lower-third"]


class TextOverlay(BaseModel):
    id: str = Field(default_factory=lambda: new_id("text"))
    text: str
    start: float
    end: float
    position: Position = "lower-third"
    font_size: int = 48
    color: str = "white"
    box: bool = True


class MusicCue(BaseModel):
    id: str = Field(default_factory=lambda: new_id("music"))
    asset_id: str
    start: float = 0.0
    src_in: float = 0.0
    duration: float | None = None  # None = until the end of the asset or the timeline
    volume: float = 0.25
    duck: bool = True
    fade_in: float = 1.0
    fade_out: float = 2.0


class Note(BaseModel):
    id: str = Field(default_factory=lambda: new_id("note"))
    time: float
    text: str
    author: str = "human"
    resolved: bool = False


class CaptionSettings(BaseModel):
    enabled: bool = False
    burn_in: bool = True
    max_chars: int = 42
    max_duration: float = 3.5
    font_size: int = 20  # libass units, relative to a 288px-high canvas


class Settings(BaseModel):
    width: int = 1920
    height: int = 1080
    fps: float = 30.0
    sample_rate: int = 48000
    fit: Literal["contain", "cover"] = "contain"
    background: str = "black"
    font_file: str | None = None


class Project(BaseModel):
    schema_version: int = 1
    name: str
    settings: Settings = Field(default_factory=Settings)
    assets: dict[str, Asset] = Field(default_factory=dict)
    clips: list[Clip] = Field(default_factory=list)
    overlays: list[TextOverlay] = Field(default_factory=list)
    music: list[MusicCue] = Field(default_factory=list)
    notes: list[Note] = Field(default_factory=list)
    captions: CaptionSettings = Field(default_factory=CaptionSettings)

    # ---- derived timeline info -------------------------------------------
    @property
    def duration(self) -> float:
        return sum(c.duration for c in self.clips)

    def clip_starts(self) -> list[float]:
        starts, t = [], 0.0
        for c in self.clips:
            starts.append(t)
            t += c.duration
        return starts

    def find_clip(self, clip_id: str) -> tuple[int, Clip]:
        for i, c in enumerate(self.clips):
            if c.id == clip_id:
                return i, c
        raise KeyError(f"No clip with id {clip_id!r}")

    def asset(self, asset_id: str) -> Asset:
        try:
            return self.assets[asset_id]
        except KeyError:
            raise KeyError(f"No asset with id {asset_id!r}") from None
