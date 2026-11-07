"""Review UI HTTP API."""

import json
import time
import urllib.error
import urllib.request

import pytest

from reelwright import review
from reelwright import server as S


def call(base, path, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
        return r.status, dict(r.headers), (json.loads(raw) if "json" in r.headers.get("Content-Type", "") else raw)


@pytest.fixture()
def base(media, reelwright_home):
    S.create_project("rev", width=640, height=360)
    ids = {a["label"]: a["id"] for a in S.import_media("rev", [str(media["talk"])])["added"]}
    S.add_clip("rev", ids["talk"])
    S.add_text("rev", "Hello", 0.5, 2)
    url = review.start(port=0)
    yield url
    review.stop()


def test_page_and_project(base):
    status, headers, page = call(base, "/")
    assert status == 200 and b"Reelwright" in page
    assert call(base, "/api/projects")[2]["projects"] == ["rev"]
    data = call(base, "/api/project/rev")[2]
    assert data["duration"] == pytest.approx(6, abs=0.05)
    assert data["preview"] == {"exists": False, "stale": True, "fingerprint": None}
    assert data["overlays"][0]["text"] == "Hello"
    with pytest.raises(urllib.error.HTTPError) as e:
        call(base, "/api/project/nope")
    assert e.value.code == 404


def test_notes_roundtrip_reaches_the_agent(base):
    call(base, "/api/project/rev/notes", "POST", {"time": 2.5, "text": "cut this pause"})
    notes = S.list_notes("rev")["notes"]
    assert [(n["time"], n["text"], n["author"]) for n in notes] == [(2.5, "cut this pause", "human")]
    nid = notes[0]["id"]
    call(base, f"/api/project/rev/notes/{nid}/resolve", "POST")
    assert S.list_notes("rev")["notes"] == []
    call(base, f"/api/project/rev/notes/{nid}", "DELETE")
    assert S.list_notes("rev", include_resolved=True)["notes"] == []
    with pytest.raises(urllib.error.HTTPError) as e:
        call(base, "/api/project/rev/notes", "POST", {"time": 1, "text": "  "})
    assert e.value.code == 400


def test_render_preview_staleness_and_range_requests(base):
    call(base, "/api/project/rev/render", "POST")
    for _ in range(120):
        job = call(base, "/api/project/rev/version")[2]["render_job"]
        if job["state"] != "running":
            break
        time.sleep(0.25)
    assert job["state"] == "done", job
    preview = call(base, "/api/project/rev")[2]["preview"]
    assert preview["exists"] and not preview["stale"]

    # notes don't make the preview stale, edits do
    call(base, "/api/project/rev/notes", "POST", {"time": 1, "text": "ok"})
    assert not call(base, "/api/project/rev")[2]["preview"]["stale"]
    S.delete_range("rev", 2, 3)
    assert call(base, "/api/project/rev")[2]["preview"]["stale"]

    status, headers, chunk = call(base, "/media/rev/preview.mp4", headers={"Range": "bytes=0-99"})
    assert status == 206 and len(chunk) == 100
    assert headers["Content-Range"].startswith("bytes 0-99/")

    # undo through the UI restores the edit
    call(base, "/api/project/rev/undo", "POST")
    assert not call(base, "/api/project/rev")[2]["preview"]["stale"]
