"""Command-line entry point.

    reelwright                      run the MCP server over stdio (what MCP clients call)
    reelwright serve --http         run over streamable HTTP instead
    reelwright review [project]     open the review UI in your browser
    reelwright doctor               check ffmpeg, filters, fonts and optional extras
    reelwright info <project>       print the timeline summary
    reelwright note <project> <time> <text>   leave review feedback for the agent
    reelwright export <project> [--preset youtube]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys


def _doctor() -> int:
    from .ffmpeg import ffmpeg, ffmpeg_bin, ffprobe_bin
    from .render import find_font
    from .store import home

    ok = True
    print(f"projects folder : {home()}")
    for name, path in (("ffmpeg", ffmpeg_bin()), ("ffprobe", ffprobe_bin())):
        found = shutil.which(path) or path
        print(f"{name:<16}: {found if shutil.which(path) else 'NOT FOUND'}")
        ok &= bool(shutil.which(path))
    if ok:
        filters = ffmpeg(["-filters"]).stdout
        for f in ("drawtext", "subtitles", "sidechaincompress", "silencedetect", "ebur128", "loudnorm"):
            present = f" {f} " in filters
            ok &= present or f == "subtitles"
            print(f"  filter {f:<17}: {'ok' if present else 'missing'}")
        encoders = ffmpeg(["-encoders"]).stdout
        gpu = [e for e in ("h264_nvenc", "h264_qsv", "h264_amf", "h264_videotoolbox") if e in encoders]
        print(f"  GPU encoders compiled in: {', '.join(gpu) or 'none'} (availability depends on hardware)")
    print(f"font for text   : {find_font() or 'none found (set font_file in project settings)'}")
    try:
        import faster_whisper  # noqa: F401
        print("transcription   : faster-whisper installed")
    except ImportError:
        print("transcription   : not installed (pip install 'reelwright[transcribe]')")
    print("status          :", "ready" if ok else "problems found")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(prog="reelwright", description="Reelwright MCP video-editing server")
    sub = parser.add_subparsers(dest="cmd")
    serve = sub.add_parser("serve", help="run the MCP server (default)")
    serve.add_argument("--http", action="store_true", help="use streamable HTTP instead of stdio")
    rev = sub.add_parser("review", help="open the local review UI in your browser")
    rev.add_argument("project", nargs="?")
    rev.add_argument("--port", type=int, default=8765)
    rev.add_argument("--no-browser", action="store_true")
    sub.add_parser("doctor", help="check the environment")
    info = sub.add_parser("info", help="print a project's timeline")
    info.add_argument("project")
    note = sub.add_parser("note", help="pin a review note to a timeline time")
    note.add_argument("project")
    note.add_argument("time", type=float)
    note.add_argument("text")
    exp = sub.add_parser("export", help="render a project")
    exp.add_argument("project")
    exp.add_argument("--preset", default="youtube")
    exp.add_argument("--encoder", default="libx264")
    args = parser.parse_args(argv)

    if args.cmd in (None, "serve"):
        from .server import mcp
        mcp.run("streamable-http" if getattr(args, "http", False) else "stdio")
    elif args.cmd == "review":
        from .review import serve_forever
        serve_forever(args.project, port=args.port, open_browser=not args.no_browser)
    elif args.cmd == "doctor":
        sys.exit(_doctor())
    elif args.cmd == "info":
        from .server import get_timeline
        print(json.dumps(get_timeline(args.project), indent=2))
    elif args.cmd == "note":
        from .server import add_note
        print(json.dumps(add_note(args.project, args.time, args.text, author="human"), indent=2))
    elif args.cmd == "export":
        from .server import export
        print(json.dumps(export(args.project, preset=args.preset, encoder=args.encoder), indent=2))


if __name__ == "__main__":
    main()
