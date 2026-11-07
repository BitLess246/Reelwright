"""Local review UI: watch the edit in a browser, see the timeline, leave notes for the agent.

Runs a small HTTP server on 127.0.0.1 (standard library only). The page polls the
project, so edits made by the agent show up live; notes you add are read by the
agent through the ``list_notes`` tool.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import traceback
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import captions as cap
from . import editing as E
from . import render as R
from .store import ProjectStore

PREVIEW_NAME = "review_preview.mp4"


# ---- project helpers ---------------------------------------------------------------

def _transcripts(store: ProjectStore, project) -> dict[str, dict]:
    out = {}
    folder = store.cache / "transcripts"
    for asset_id in project.assets:
        f = folder / f"{asset_id}.json"
        if f.exists():
            out[asset_id] = json.loads(f.read_text(encoding="utf-8"))
    return out


def render_fingerprint(store: ProjectStore) -> str:
    """Hash of everything that affects the rendered picture and sound (notes excluded)."""
    project = store.load()
    data = project.model_dump(mode="json", exclude={"notes"})
    folder = store.cache / "transcripts"
    data["_transcripts"] = sorted(
        (f.name, f.stat().st_mtime_ns) for f in folder.glob("*.json")
    ) if folder.exists() else []
    return hashlib.sha1(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]


def preview_state(store: ProjectStore) -> dict:
    video = store.renders / PREVIEW_NAME
    meta = store.renders / (PREVIEW_NAME + ".json")
    if not video.exists() or not meta.exists():
        return {"exists": False, "stale": True, "fingerprint": None}
    rendered = json.loads(meta.read_text(encoding="utf-8")).get("fingerprint")
    return {"exists": True, "stale": rendered != render_fingerprint(store), "fingerprint": rendered}


def _music_end(project, cue) -> float:
    asset = project.assets[cue.asset_id]
    available = (asset.duration - cue.src_in) if asset.duration else project.duration
    length = cue.duration if cue.duration is not None else available
    return round(min(project.duration, cue.start + max(0.0, length)), 3)


def project_payload(name: str) -> dict:
    store = ProjectStore.open(name)
    p = store.load()
    trs = _transcripts(store, p)
    starts = p.clip_starts()
    return {
        "name": p.name,
        "slug": store.slug,
        "version": store.project_file.stat().st_mtime_ns,
        "duration": round(p.duration, 3),
        "settings": p.settings.model_dump(),
        "captions_enabled": p.captions.enabled,
        "clips": [
            {"id": c.id, "index": i, "asset_id": c.asset_id, "label": p.assets[c.asset_id].label,
             "kind": p.assets[c.asset_id].kind, "start": round(s, 3), "end": round(s + c.duration, 3),
             "src_in": c.src_in, "src_out": c.src_out, "volume": c.volume}
            for i, (s, c) in enumerate(zip(starts, p.clips))
        ],
        "overlays": [o.model_dump() for o in p.overlays],
        "music": [
            {**m.model_dump(), "label": p.assets[m.asset_id].label, "end": _music_end(p, m)}
            for m in p.music
        ],
        "notes": [n.model_dump() for n in p.notes],
        "transcript": cap.build_cues(p, trs, max_chars=90, max_duration=8.0) if trs else [],
        "history": store.history(),
        "preview": preview_state(store),
    }


# ---- render jobs -----------------------------------------------------------------------

class RenderJobs:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}

    def status(self, slug: str) -> dict:
        with self._lock:
            return dict(self._jobs.get(slug, {"state": "idle"}))

    def start(self, name: str) -> dict:
        store = ProjectStore.open(name)
        with self._lock:
            if self._jobs.get(store.slug, {}).get("state") == "running":
                return dict(self._jobs[store.slug])
            self._jobs[store.slug] = {"state": "running", "started": time.time()}
        threading.Thread(target=self._run, args=(store,), daemon=True).start()
        return self.status(store.slug)

    def _run(self, store: ProjectStore) -> None:
        try:
            fingerprint = render_fingerprint(store)
            project = store.load()
            if not project.clips:
                raise ValueError("The timeline is empty")
            renders = store.renders_dir()
            tmp = renders / ("." + PREVIEW_NAME)
            result = R.render(project, tmp, store.cache_dir("work_review"), "preview",
                              _transcripts(store, project))
            tmp.replace(renders / PREVIEW_NAME)
            (renders / (PREVIEW_NAME + ".json")).write_text(
                json.dumps({"fingerprint": fingerprint, **result}), encoding="utf-8")
            state = {"state": "done", "seconds": result["render_seconds"]}
        except Exception as e:  # surface the error in the UI
            state = {"state": "error", "error": str(e)}
        with self._lock:
            self._jobs[store.slug] = state


JOBS = RenderJobs()


# ---- HTTP ----------------------------------------------------------------------------------

def _page() -> bytes:
    return resources.files("reelwright").joinpath("static/review.html").read_bytes()


class Handler(BaseHTTPRequestHandler):
    server_version = "Reelwright"

    def log_message(self, fmt, *args):  # keep stdio clean (the MCP transport may use it)
        pass

    # -- plumbing
    def _json(self, data, status: int = 200) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _send_file(self, path: Path, ctype: str) -> None:
        size = path.stat().st_size
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        status = HTTPStatus.OK
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
                else:
                    start = max(0, size - int(m.group(2)))
                if start > end or start >= size:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                status = HTTPStatus.PARTIAL_CONTENT
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-cache")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with path.open("rb") as f:
            f.seek(start)
            remaining = length
            try:
                while remaining > 0:
                    chunk = f.read(min(1 << 16, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the browser cancelled the request while seeking

    def _route(self, method: str) -> None:
        path = urlparse(self.path).path
        parts = [unquote(p) for p in path.strip("/").split("/") if p]
        try:
            if method == "GET" and parts in ([], ["index.html"]):
                body = _page()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if method == "GET" and parts == ["api", "projects"]:
                return self._json({"projects": ProjectStore.list()})
            if len(parts) >= 3 and parts[0] == "api" and parts[1] == "project":
                return self._project_route(method, parts[2], parts[3:])
            if method == "GET" and len(parts) == 3 and parts[0] == "media" and parts[2] == "preview.mp4":
                store = ProjectStore.open(parts[1])
                video = store.renders / PREVIEW_NAME
                if not video.exists():
                    return self._json({"error": "No preview rendered yet"}, 404)
                return self._send_file(video, "video/mp4")
            self._json({"error": "Not found"}, 404)
        except FileNotFoundError as e:
            self._json({"error": str(e)}, 404)
        except (KeyError, ValueError) as e:
            self._json({"error": str(e).strip("'\"")}, 400)
        except Exception as e:
            traceback.print_exc()
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def _project_route(self, method: str, name: str, rest: list[str]) -> None:
        store = ProjectStore.open(name)
        if method == "GET" and rest == []:
            data = project_payload(name)
            data["render_job"] = JOBS.status(store.slug)
            return self._json(data)
        if method == "GET" and rest == ["version"]:
            return self._json({"version": store.project_file.stat().st_mtime_ns,
                               "render_job": JOBS.status(store.slug)})
        if method == "POST" and rest == ["render"]:
            return self._json(JOBS.start(name))
        if method == "POST" and rest == ["notes"]:
            body = self._body()
            text = str(body.get("text", "")).strip()
            if not text:
                raise ValueError("Note text is empty")
            with store.edit("add note (review)") as p:
                t = max(0.0, min(float(body.get("time", 0)), p.duration))
                note = E.add_note(p, t, text, author=str(body.get("author") or "human"))
            return self._json({"ok": True, "note": note.model_dump()})
        if method == "POST" and len(rest) == 3 and rest[0] == "notes" and rest[2] in ("resolve", "reopen"):
            with store.edit(f"{rest[2]} note") as p:
                note = next((n for n in p.notes if n.id == rest[1]), None)
                if note is None:
                    raise KeyError(f"No note with id {rest[1]}")
                note.resolved = rest[2] == "resolve"
            return self._json({"ok": True})
        if method == "DELETE" and len(rest) == 2 and rest[0] == "notes":
            with store.edit("delete note") as p:
                E.remove_item(p.notes, rest[1], "note")
            return self._json({"ok": True})
        if method == "POST" and rest in (["undo"], ["redo"]):
            desc = store.undo() if rest == ["undo"] else store.redo()
            return self._json({"ok": True, "message": f"{rest[0].capitalize()}: {desc}"})
        self._json({"error": "Not found"}, 404)

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_DELETE(self):
        self._route("DELETE")


# ---- server lifecycle ----------------------------------------------------------------------

_server: ThreadingHTTPServer | None = None
_server_lock = threading.Lock()


def start(host: str = "127.0.0.1", port: int = 8765) -> str:
    """Start the review server in a background thread (once per process). Returns its base URL."""
    global _server
    with _server_lock:
        if _server is None:
            try:
                srv = ThreadingHTTPServer((host, port), Handler)
            except OSError:
                srv = ThreadingHTTPServer((host, 0), Handler)  # port busy: pick a free one
            srv.daemon_threads = True
            threading.Thread(target=srv.serve_forever, daemon=True, name="reelwright-review").start()
            _server = srv
        h, p = _server.server_address[:2]
        return f"http://{'localhost' if h in ('127.0.0.1', '0.0.0.0') else h}:{p}"


def stop() -> None:
    global _server
    with _server_lock:
        if _server is not None:
            _server.shutdown()
            _server.server_close()
            _server = None


def url_for(base: str, project: str | None) -> str:
    return f"{base}/#{ProjectStore(project).slug}" if project else base


def serve_forever(project: str | None = None, host: str = "127.0.0.1", port: int = 8765,
                  open_browser: bool = True) -> None:
    url = url_for(start(host, port), project)
    print(f"Reelwright review UI: {url}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        stop()
