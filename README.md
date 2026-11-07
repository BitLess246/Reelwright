# Reelwright

**An MCP server that lets AI agents edit video through a structured timeline, with transcripts, scene detection, and preview renders for self-checking.**

AI agents can't drag clips on a timeline, and they can't watch or listen to footage in real time. Reelwright gives them what they need instead:

- **Eyes and ears.** Contact sheets, scene-cut detection, silence and loudness maps, waveforms, and word-timestamped transcripts.
- **A timeline as data.** A JSON edit list with ripple editing and undo/redo, driven by tool calls like `delete_range` and `add_text`.
- **A way to check its own work.** `check_frames` renders frames of the *edited* timeline, overlays and captions included, so the agent can verify each change before exporting.

Everything renders locally with ffmpeg, so footage never leaves your machine.

## Features

| Area | What it does |
|---|---|
| Perception | Contact sheets of any source range, scene-change detection, silence detection, EBU R128 loudness curves, waveform images, transcripts (faster-whisper or imported `.srt`/`.vtt`) |
| Editing | Add, trim, split, move and delete clips; ripple-delete any timeline range across clip boundaries; one-call silence removal with padding; per-clip volume, speed (0.1–16×) and crop |
| Reframing | `blur` fit puts 16:9 footage on a blurred fill for 9:16 reels; per-clip crop zooms into the action |
| Layers | Text overlays (top, center, bottom, lower-third), background music with automatic ducking under speech, fades |
| Captions | Generated from transcripts and kept in sync with the edit; burned in or exported as `.srt` |
| Review UI | A local web page to watch the edit, scrub the timeline and pin notes like "cut this pause" for the agent; updates live as the agent edits |
| Output | Low-res range previews; exports for YouTube 1080p/4K, Shorts 9:16 and square, with -14 LUFS loudness normalisation; GPU encoders (NVENC, QSV, AMF, VideoToolbox) |

## Requirements

- Python 3.10+
- [ffmpeg](https://ffmpeg.org/) and ffprobe on your `PATH`, built with `libass` and `libfreetype` (standard full builds include both)
  - Windows: `winget install Gyan.FFmpeg`
  - macOS: `brew install ffmpeg`
  - Linux: `sudo apt install ffmpeg`

## Install

```bash
git clone https://github.com/BitLess246/Reelwright.git
cd Reelwright
pip install -e .                  # core
pip install -e ".[transcribe]"    # optional: local transcription with faster-whisper
reelwright doctor                 # check ffmpeg, filters, fonts and GPU encoders
```

## Connect it to an MCP client

**Claude Desktop.** Add this to `claude_desktop_config.json` (on Windows: `%APPDATA%\Claude\claude_desktop_config.json`), then restart Claude Desktop:

```json
{
  "mcpServers": {
    "reelwright": {
      "command": "reelwright",
      "env": { "REELWRIGHT_HOME": "C:/Users/YOU/Videos/Reelwright" }
    }
  }
}
```

If `reelwright` isn't on the `PATH` Claude Desktop sees, use the full path to your Python with `"args": ["-m", "reelwright"]`.

**Claude Code:**

```bash
claude mcp add reelwright -- reelwright
```

**Other clients.** Run `reelwright` for stdio or `reelwright serve --http` for streamable HTTP.

`REELWRIGHT_HOME` is where projects are stored (default: `~/Reelwright/projects`). `REELWRIGHT_FFMPEG` and `REELWRIGHT_FFPROBE` override the ffmpeg binaries.

## Example

Ask your agent something like:

> Make a project called "beam-lesson", import `D:/footage/lecture.mp4` and `D:/music/calm.mp3`. Remove the silences, add a title card that says "Simply Supported Beams" for the first 3 seconds, put the music under it at low volume, turn on captions, check a few frames, then export for YouTube and as a Short.

A typical run looks like this:

```
create_project  → import_media → inspect_media / transcribe
remove_silences (dry_run=True, then for real)
add_text, add_music, set_captions
check_frames [0.5, 2.0, 45.0]  → looks at the result, fixes anything off
render_preview (a range) → export preset=youtube → export preset=shorts
```

## Tools

| Tool | Purpose |
|---|---|
| `list_projects`, `create_project`, `get_timeline`, `update_settings` | Manage projects; `get_timeline` returns the whole edit with timeline and source times |
| `undo`, `redo`, `history` | Every edit is one undo step, persisted on disk |
| `import_media` | Probe and add video, audio and image files |
| `inspect_media` | Contact sheet of a source range with timestamps (image) |
| `detect_scenes`, `detect_silence` | Shot changes and silent ranges, in source time |
| `audio_loudness`, `audio_waveform` | Loudness curve and LUFS, or a waveform image, for an asset or a render |
| `transcribe`, `import_transcript`, `get_transcript` | Word-timestamped transcripts; read in source time or as the edited timeline |
| `add_clip`, `trim_clip`, `split_clip`, `delete_clip`, `delete_range`, `move_clip`, `set_clip_volume` | Main-track editing (ripple) |
| `set_clip_speed`, `set_clip_crop` | Speed ramps and zoom-in reframing per clip |
| `remove_silences` | Detect and cut dead air across the timeline; supports `dry_run` |
| `add_text`, `update_text`, `remove_text` | Text overlays |
| `add_music`, `remove_music` | Background audio with ducking and fades |
| `set_captions` | Captions from transcripts, burned in or as `.srt` |
| `add_note`, `list_notes`, `resolve_note` | Review notes pinned to timeline times |
| `open_review` | Start the local review UI and open it in the browser |
| `check_frames`, `timeline_contact_sheet` | Frames of the edited timeline for self-checking (image) |
| `render_preview`, `export` | Low-res range previews and final exports |

## How the timeline works

A project is one `project.json`:

- **`clips`** form the main track and play back-to-back. Each clip has `src_in`/`src_out` in source time; its position on the timeline is implied by the clips before it, so deleting anything ripples automatically.
- **`overlays`** and **`music`** sit at absolute timeline times. Ripple deletes shift them so they stay attached to the content around them.
- **Transcripts** are stored per asset in source time and mapped through the edit whenever captions or `get_transcript` need them, so captions stay correct after every cut.

```
<REELWRIGHT_HOME>/<project>/
  project.json     the edit
  history.json     undo/redo
  cache/           transcripts, analysis, frames, contact sheets
  renders/         previews and exports
```

## Review UI

```bash
reelwright review beam-lesson
```

This opens a page on `http://localhost:8765` (local only). The agent can also open it with the `open_review` tool.

- **Watch** a low-res preview of the edit. A badge tells you when the agent has changed the edit since the last render; one click re-renders.
- **Scrub** the timeline: video clips, text overlays, music and notes each get their own lane. Click any transcript line or clip to jump there.
- **Leave notes**: pause, press <kbd>N</kbd>, type what you want changed, press <kbd>Enter</kbd>. The note is pinned to that moment, and the agent reads it with `list_notes`. Agent notes (questions for you) show up in blue.
- **Undo/redo** the agent's edits from the page.

Keys: <kbd>Space</kbd> play/pause, <kbd>←</kbd>/<kbd>→</kbd> 1 s (<kbd>Shift</kbd> for 5 s), <kbd>,</kbd>/<kbd>.</kbd> one frame, <kbd>N</kbd> new note, <kbd>Ctrl</kbd>+<kbd>Z</kbd> undo.

## CLI

```bash
reelwright doctor                          # environment check
reelwright review beam-lesson              # open the review UI
reelwright info beam-lesson                # print the timeline
reelwright note beam-lesson 42.5 "cut this pause"   # leave feedback for the agent
reelwright export beam-lesson --preset shorts --encoder h264_nvenc
```

## Development

```bash
pip install -e ".[dev]"
pytest
```

The tests generate small synthetic clips with ffmpeg, so no media files are needed.

## Roadmap

- [x] Local review UI: scrub the timeline in a browser and leave notes
- [ ] Filler-word removal ("um", "uh") from word timestamps
- [ ] Transitions (crossfade, dip to black) and Ken Burns on images
- [ ] Picture-in-picture and B-roll overlay track
- [ ] Render progress reporting and background export jobs
- [ ] Speaker diarisation and face-aware auto-reframing for Shorts

## License

[Apache-2.0](LICENSE). Reelwright calls the ffmpeg you have installed and does not bundle it.
