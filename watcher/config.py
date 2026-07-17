"""Config + per-source state. Global registry in ~/.watcher/config.json;
each source gets its own namespaced folder so nothing collides."""
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(os.environ.get("WATCHER_HOME", Path.home() / ".watcher"))
CONFIG = HOME / "config.json"
LOG = HOME / "watcher.log"
SESSIONS = HOME / "sessions.tsv"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slugify(platform: str, ident: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", f"{platform}-{ident}".lower()).strip("-")


def log(msg: str) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat()} {msg}\n")


def load_config() -> dict:
    if CONFIG.exists():
        return json.loads(CONFIG.read_text())
    return {"sources": []}


def save_config(cfg: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2))


def get_source(slug: str) -> dict | None:
    return next((s for s in load_config()["sources"] if s["slug"] == slug), None)


def add_source(source: dict) -> None:
    cfg = load_config()
    cfg["sources"] = [s for s in cfg["sources"] if s["slug"] != source["slug"]]
    cfg["sources"].append(source)
    save_config(cfg)
    Source(source["slug"]).dir.mkdir(parents=True, exist_ok=True)
    (Source(source["slug"]).dir / "scratch").mkdir(exist_ok=True)


def remove_source(slug: str) -> bool:
    cfg = load_config()
    before = len(cfg["sources"])
    cfg["sources"] = [s for s in cfg["sources"] if s["slug"] != slug]
    save_config(cfg)
    return len(cfg["sources"]) < before


class Source:
    """Per-source paths + state helpers. `meta` is the registry entry."""

    def __init__(self, slug: str):
        self.slug = slug
        self.dir = HOME / slug

    # --- registry entry ---
    @property
    def meta(self) -> dict:
        return get_source(self.slug) or {}

    # --- files ---
    @property
    def _state(self) -> Path:
        return self.dir / "state.json"

    @property
    def _queue(self) -> Path:
        return self.dir / "queue.jsonl"

    @property
    def _session(self) -> Path:
        return self.dir / "session"

    @property
    def _mode(self) -> Path:
        return self.dir / "mode"

    @property
    def _lock(self) -> Path:
        return self.dir / "lock"

    # --- state ---
    def state(self) -> dict:
        if self._state.exists():
            return json.loads(self._state.read_text())
        return {"last_checked": now_iso(), "processed": []}

    def save_state(self, s: dict) -> None:
        s["processed"] = s["processed"][-800:]
        self.dir.mkdir(parents=True, exist_ok=True)
        self._state.write_text(json.dumps(s, indent=1))

    # --- queue (FIFO) ---
    def queue(self) -> list[dict]:
        if not self._queue.exists():
            return []
        return [json.loads(l) for l in self._queue.read_text().splitlines() if l.strip()]

    def write_queue(self, events: list[dict]) -> None:
        self._queue.write_text("".join(json.dumps(e) + "\n" for e in events))

    def enqueue(self, event: dict) -> None:
        with self._queue.open("a") as f:
            f.write(json.dumps(event) + "\n")

    # --- rolling session ---
    def session_id(self) -> str:
        return self._session.read_text().strip() if self._session.exists() else ""

    def set_session_id(self, sid: str) -> None:
        if sid:
            self._session.write_text(sid)

    def clear_session(self) -> None:
        self._session.unlink(missing_ok=True)

    # --- mode ---
    def mode(self) -> str:
        return self._mode.read_text().strip() if self._mode.exists() else "triage"

    def set_mode(self, m: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._mode.write_text(m)

    # --- rolling-session memory (compaction) ---
    @property
    def _memory(self) -> Path:
        return self.dir / "memory.md"

    def memory(self) -> str:
        return self._memory.read_text() if self._memory.exists() else ""

    def set_memory(self, text: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._memory.write_text(text)

    # --- lock (per source) ---
    def locked(self, stale_s: int = 3600) -> bool:
        return self._lock.exists() and time.time() - self._lock.stat().st_mtime < stale_s

    def lock(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock.write_text(str(os.getpid()))

    def unlock(self) -> None:
        self._lock.unlink(missing_ok=True)
