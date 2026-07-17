"""The watcher TUI — one screen, Claude-Code-like.

Layout: header · scrolling log · fixed bottom input.
- Type anything + Enter → sent into the live session as an OPERATOR MESSAGE
  (not a ticket comment); handled even while issues are being worked (queued).
- Slash commands: /stop /start (pause·resume autonomous), /mode full|triage,
  /chat (open the full Claude TUI on this session), /poll, /help, /quit.
- Every ~60s it checks for new issues and works them, streaming into the log.
- When it needs you, an arrow-key prompt appears: "Are you there? Yes/No"
  (10s → No → safe path); on Yes, the question + suggestions + "type my own".

curses runs on the main thread; a worker thread does the polling/turns and
pushes log lines + approval requests back to the UI.
"""
import collections
import curses
import os
import shutil
import subprocess
import threading
import time

from . import config, engine, runtime
from .adapters import build_adapter


class TUI:
    def __init__(self, slug: str):
        self.slug = slug
        self.src = config.Source(slug)
        self.adapter = build_adapter(self.src.meta)
        self.ident = self.src.meta.get("repo") or self.src.meta.get("solution_name") or slug
        self.log = collections.deque(maxlen=3000)
        self.loglock = threading.Lock()
        self.msgq = collections.deque()          # operator messages (main→worker)
        self.inp = ""
        self.scroll = 0                           # 0 = pinned to bottom
        self.stopflag = threading.Event()
        self.autonomous = True
        self.status = "starting"
        self.pending = None                       # (question, options, Event, holder[])
        self.pendlock = threading.Lock()
        self._suspend = threading.Event()         # pause worker during /chat

    # ---------- worker (background thread) ----------
    def emit(self, line):
        with self.loglock:
            for l in str(line).splitlines() or [""]:
                self.log.append(l)

    def ask(self, question, options):
        ev = threading.Event(); holder = []
        with self.pendlock:
            self.pending = (question, options, ev, holder)
        ev.wait()                                 # main thread services it
        return holder[0] if holder else None

    def worker(self):
        self.src.set_paused(True)                 # background launchd defers to the TUI
        tick = 0
        try:
            while not self.stopflag.is_set():
                if self._suspend.is_set():
                    time.sleep(0.2); continue
                # operator messages always handled (even when autonomous is off)
                while self.msgq and not self.stopflag.is_set():
                    text = self.msgq.popleft()
                    self.status = "answering you"
                    try:
                        runtime.chat(self.src, self.adapter, text, emit=self.emit)
                    except Exception as e:  # noqa: BLE001
                        self.emit(f"[error] {e}")
                # autonomous issue processing
                if self.autonomous and not self.stopflag.is_set():
                    try:
                        if tick % 30 == 0:
                            self.status = f"checking {self.ident} for new issues…"
                            engine.discover_into_queue(self.src, self.adapter)
                        if self.src.queue():
                            self.status = "working an issue…"
                            engine.drain_queue(self.src, self.adapter, interactive=False,
                                               emit=self.emit, ask=self.ask)
                    except Exception as e:  # noqa: BLE001
                        self.emit(f"[error] {e}")
                self.status = "idle" if self.autonomous else "paused — /start to resume"
                for _ in range(20):               # ~2s, responsive
                    if self.stopflag.is_set() or self.msgq:
                        break
                    time.sleep(0.1)
                tick += 1
        finally:
            self.src.set_paused(False)

    # ---------- rendering (main thread) ----------
    def _draw(self, scr):
        scr.erase()
        h, w = scr.getmaxyx()
        mode = self.src.mode()
        auto = "AUTONOMOUS" if self.autonomous else "PAUSED"
        head = f" watcher · {self.ident} · mode={mode} · {auto} · {self.status} "
        scr.addnstr(0, 0, head.ljust(w), w, curses.A_REVERSE)
        # log pane: rows 1..h-3
        top, bottom = 1, h - 3
        rows = bottom - top + 1
        with self.loglock:
            lines = list(self.log)
        view = lines[-rows:] if self.scroll == 0 else lines[max(0, len(lines) - rows - self.scroll):
                                                             len(lines) - self.scroll]
        for i, line in enumerate(view):
            scr.addnstr(top + i, 0, line, w - 1)
        scr.addnstr(h - 2, 0, ("─" * w), w)
        prompt = "› " + self.inp
        scr.addnstr(h - 1, 0, prompt.ljust(w), w)
        scr.move(h - 1, min(len(prompt), w - 1))
        scr.refresh()

    def _select(self, scr, title, options, timeout=None):
        """Arrow-key select. Returns index, or None on timeout/Esc."""
        idx = 0
        deadline = (time.time() + timeout) if timeout else None
        scr.nodelay(True)
        while True:
            h, w = scr.getmaxyx()
            scr.erase()
            scr.addnstr(0, 0, " watcher — needs your input ".ljust(w), w, curses.A_REVERSE)
            scr.addnstr(2, 2, title[:w - 4], w - 4, curses.A_BOLD)
            for i, opt in enumerate(options):
                marker = "❯ " if i == idx else "  "
                attr = curses.A_REVERSE if i == idx else curses.A_NORMAL
                scr.addnstr(4 + i, 4, (marker + str(opt))[:w - 6], w - 6, attr)
            if deadline:
                left = max(0, int(deadline - time.time()))
                scr.addnstr(4 + len(options) + 1, 4, f"(auto in {left}s → safe path)", w - 6, curses.A_DIM)
            scr.addnstr(h - 1, 0, " ↑/↓ move · Enter select · Esc = safe ".ljust(w), w, curses.A_DIM)
            scr.refresh()
            ch = scr.getch()
            if ch in (curses.KEY_UP, ord("k")):
                idx = (idx - 1) % len(options)
            elif ch in (curses.KEY_DOWN, ord("j")):
                idx = (idx + 1) % len(options)
            elif ch in (curses.KEY_ENTER, 10, 13):
                scr.nodelay(True); return idx
            elif ch == 27:                         # Esc
                scr.nodelay(True); return None
            if deadline and time.time() >= deadline:
                scr.nodelay(True); return None
            time.sleep(0.03)

    def _read_line(self, scr, label):
        curses.echo(); curses.curs_set(1)
        h, w = scr.getmaxyx()
        scr.erase()
        scr.addnstr(2, 2, label, w - 4, curses.A_BOLD)
        scr.refresh()
        scr.nodelay(False)
        try:
            s = scr.getstr(4, 4, w - 8).decode(errors="ignore")
        except Exception:
            s = ""
        curses.noecho()
        scr.nodelay(True)
        return s.strip()

    def _handle_approval(self, scr):
        with self.pendlock:
            pend = self.pending
        if not pend:
            return
        question, options, ev, holder = pend
        # step 1 — are you there?
        i = self._select(scr, "Need some clarification. Are you there?", ["Yes", "No"], timeout=10)
        if i != 0:                                 # No or timeout → safe path
            holder.append(None)
        else:
            opts = list(options) + ["✎ Type my own answer…", "You decide (take the safe path)"]
            j = self._select(scr, question, opts, timeout=None)
            if j is None or opts[j].startswith("You decide"):
                holder.append(None)
            elif opts[j].startswith("✎"):
                holder.append(self._read_line(scr, question + "  → your answer:") or None)
            else:
                holder.append(opts[j])
        with self.pendlock:
            self.pending = None
        ev.set()

    def _chat_handoff(self, scr):
        self._suspend.set()
        curses.endwin()
        sid = self.src.session_id()
        cmd = [shutil.which("claude") or "claude"] + (["--resume", sid] if sid else [])
        print("↪ full Claude TUI (watcher paused). Exit it (Ctrl-D) to return.\n")
        try:
            subprocess.run(cmd, cwd=self.adapter.cwd(), env={**os.environ, **self.adapter.env()})
        finally:
            self._suspend.clear()
            scr.clear(); curses.doupdate()

    def _submit(self, scr):
        line = self.inp.strip()
        self.inp = ""
        if not line:
            return
        if line in ("/quit", "/exit", "/q"):
            self.stopflag.set(); return
        if line == "/help":
            self.emit("commands: /stop /start · /mode full|triage · /chat · /poll · /quit · "
                      "anything else = message to the session"); return
        if line in ("/stop", "/pause"):
            self.autonomous = False; self.emit("⏸ autonomous paused (/start to resume)"); return
        if line in ("/start", "/resume"):
            self.autonomous = True; self.emit("▶ autonomous resumed"); return
        if line == "/poll":
            self.emit("… checking now"); self._force_poll = True; return
        if line.startswith("/mode"):
            p = line.split()
            if len(p) == 2 and p[1] in ("triage", "full"):
                self.src.set_mode(p[1]); self.emit(f"mode → {p[1]}")
            else:
                self.emit("usage: /mode full|triage")
            return
        if line == "/chat":
            self._chat_handoff(scr); return
        # operator message → into the session (queued, handled by the worker)
        self.emit(f"🧑 you: {line}")
        self.msgq.append(line)

    def _main(self, scr):
        curses.curs_set(1)
        scr.nodelay(True)
        try:
            curses.use_default_colors()
        except Exception:
            pass
        t = threading.Thread(target=self.worker, daemon=True)
        t.start()
        self.emit(f"live on {self.ident}. type a message, or /help. checking every ~60s.")
        while not self.stopflag.is_set():
            with self.pendlock:
                has_pending = self.pending is not None
            if has_pending:
                self._handle_approval(scr)
                continue
            self._draw(scr)
            ch = scr.getch()
            if ch == -1:
                time.sleep(0.05); continue
            if ch in (curses.KEY_ENTER, 10, 13):
                self._submit(scr)
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                self.inp = self.inp[:-1]
            elif ch == curses.KEY_PPAGE:
                self.scroll += 5
            elif ch == curses.KEY_NPAGE:
                self.scroll = max(0, self.scroll - 5)
            elif 32 <= ch < 127:
                self.inp += chr(ch)
        self.stopflag.set()


def run(slug: str):
    src = config.Source(slug)
    if not src.meta:
        print(f"✗ No source '{slug}'.")
        return
    tui = TUI(slug)
    curses.wrapper(tui._main)
    print("watcher closed.")
