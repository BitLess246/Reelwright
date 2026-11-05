"""Project persistence with undo/redo history.

Layout of a project directory::

    <REELWRIGHT_HOME>/<project-name>/
        project.json        the timeline (source of truth)
        history.json        undo / redo snapshots
        cache/              frames, contact sheets, transcripts, scratch files
        renders/            previews and exports
"""

from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .models import Project

MAX_HISTORY = 100


def home() -> Path:
    root = os.environ.get("REELWRIGHT_HOME") or Path.home() / "Reelwright" / "projects"
    path = Path(root).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def slugify(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-.")
    if not slug:
        raise ValueError("Project name must contain letters or digits")
    return slug


class ProjectStore:
    def __init__(self, name: str, root: Path | None = None):
        self.slug = slugify(name)
        self.dir = (root or home()) / self.slug
        self.project_file = self.dir / "project.json"
        self.history_file = self.dir / "history.json"
        self.cache = self.dir / "cache"
        self.renders = self.dir / "renders"

    # ---- lifecycle --------------------------------------------------------
    @classmethod
    def create(cls, name: str, root: Path | None = None, **settings) -> "ProjectStore":
        store = cls(name, root)
        if store.project_file.exists():
            raise FileExistsError(f"Project {store.slug!r} already exists")
        store.dir.mkdir(parents=True, exist_ok=True)
        project = Project(name=name)
        for key, value in settings.items():
            if value is not None:
                setattr(project.settings, key, value)
        store._write(project)
        store._write_history({"undo": [], "redo": []})
        return store

    @classmethod
    def open(cls, name: str, root: Path | None = None) -> "ProjectStore":
        store = cls(name, root)
        if not store.project_file.exists():
            raise FileNotFoundError(
                f"Project {store.slug!r} not found in {store.dir.parent}. "
                "Use create_project first, or list_projects to see what exists."
            )
        return store

    @staticmethod
    def list(root: Path | None = None) -> list[str]:
        base = root or home()
        return sorted(p.name for p in base.iterdir() if (p / "project.json").exists())

    # ---- read / write -----------------------------------------------------
    def load(self) -> Project:
        return Project.model_validate_json(self.project_file.read_text(encoding="utf-8"))

    def _write(self, project: Project) -> None:
        tmp = self.project_file.with_suffix(".tmp")
        tmp.write_text(project.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(self.project_file)

    def _read_history(self) -> dict:
        if not self.history_file.exists():
            return {"undo": [], "redo": []}
        return json.loads(self.history_file.read_text(encoding="utf-8"))

    def _write_history(self, history: dict) -> None:
        self.history_file.write_text(json.dumps(history), encoding="utf-8")

    @contextmanager
    def edit(self, description: str) -> Iterator[Project]:
        """Mutate the project inside a with-block; the change becomes one undo step.

        If the block raises, nothing is saved.
        """
        before = self.load()
        project = before.model_copy(deep=True)
        yield project
        if project.model_dump() == before.model_dump():
            return
        history = self._read_history()
        history["undo"].append({"description": description, "project": before.model_dump(mode="json")})
        history["undo"] = history["undo"][-MAX_HISTORY:]
        history["redo"] = []
        self._write(project)
        self._write_history(history)

    def undo(self) -> str:
        history = self._read_history()
        if not history["undo"]:
            raise ValueError("Nothing to undo")
        step = history["undo"].pop()
        current = self.load()
        history["redo"].append({"description": step["description"], "project": current.model_dump(mode="json")})
        self._write(Project.model_validate(step["project"]))
        self._write_history(history)
        return step["description"]

    def redo(self) -> str:
        history = self._read_history()
        if not history["redo"]:
            raise ValueError("Nothing to redo")
        step = history["redo"].pop()
        current = self.load()
        history["undo"].append({"description": step["description"], "project": current.model_dump(mode="json")})
        self._write(Project.model_validate(step["project"]))
        self._write_history(history)
        return step["description"]

    def history(self) -> dict:
        h = self._read_history()
        return {
            "undo": [s["description"] for s in h["undo"]],
            "redo": [s["description"] for s in h["redo"]],
        }

    def cache_dir(self, *parts: str) -> Path:
        path = self.cache.joinpath(*parts)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def renders_dir(self) -> Path:
        self.renders.mkdir(parents=True, exist_ok=True)
        return self.renders
