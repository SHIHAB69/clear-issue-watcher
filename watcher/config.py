"""Config + per-source state. Global registry in ~/.watcher/config.json;
each source gets its own namespaced folder so nothing collides."""
import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl  # POSIX file locking for concurrent state RMW
except ImportError:  # Windows
    fcntl = None

HOME = Path(os.environ.get("WATCHER_HOME", Path.home() / ".watcher"))
CONFIG = HOME / "config.json"
LOG = HOME / "watcher.log"
SESSIONS = HOME / "sessions.tsv"


def _atomic_write(path: Path, data: str) -> None:
    """Write via temp file + os.replace so a crash mid-write can't corrupt/truncate."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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
    _atomic_write(CONFIG, json.dumps(cfg, indent=2))


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
            try:
                return json.loads(self._state.read_text())
            except (json.JSONDecodeError, OSError):   # corrupt → self-heal to default
                pass
        return {"last_checked": now_iso(), "processed": []}

    def save_state(self, s: dict) -> None:
        s["processed"] = s["processed"][-800:]
        _atomic_write(self._state, json.dumps(s, indent=1))

    def update_state(self, mutate) -> dict:
        """Locked read-modify-write: read INSIDE an exclusive lock, let `mutate`
        touch only its own fields, atomic-write. Prevents lost updates when the
        TUI worker and the background runner overlap."""
        self.dir.mkdir(parents=True, exist_ok=True)
        lockf = self.dir / "state.lock"
        with open(lockf, "w") as lf:
            if fcntl:
                fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                s = self.state()
                mutate(s)
                self.save_state(s)
                return s
            finally:
                if fcntl:
                    fcntl.flock(lf, fcntl.LOCK_UN)

    # --- queue (FIFO) ---
    def queue(self) -> list[dict]:
        if not self._queue.exists():
            return []
        out = []
        for l in self._queue.read_text().splitlines():
            if not l.strip():
                continue
            try:
                out.append(json.loads(l))
            except json.JSONDecodeError:   # skip a torn line rather than crash
                continue
        return out

    def write_queue(self, events: list[dict]) -> None:
        _atomic_write(self._queue, "".join(json.dumps(e) + "\n" for e in events))

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

    # --- mode --- (full by default: the watcher runs unlocked, no config needed)
    def mode(self) -> str:
        return self._mode.read_text().strip() if self._mode.exists() else "full"

    def set_mode(self, m: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._mode.write_text(m)

    # --- pause/resume (human interrupt of autonomous processing) ---
    @property
    def _paused(self) -> Path:
        return self.dir / "paused"

    def paused(self) -> bool:
        return self._paused.exists()

    def set_paused(self, on: bool) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        if on:
            self._paused.write_text("1")
        else:
            self._paused.unlink(missing_ok=True)

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
        try:
            return time.time() - self._lock.stat().st_mtime < stale_s
        except FileNotFoundError:      # another runner released it mid-check
            return False

    def lock(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock.write_text(str(os.getpid()))

    def unlock(self) -> None:
        self._lock.unlink(missing_ok=True)
