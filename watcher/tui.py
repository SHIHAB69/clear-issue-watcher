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
        self._busy = threading.Event()            # worker is mid-turn (don't handoff/quit over it)
        self.force_poll = threading.Event()       # /poll → discover now

    # ---------- worker (background thread) ----------
    def emit(self, line):
        with self.loglock:
            for l in str(line).splitlines() or [""]:
                self.log.append(l)

    def ask(self, question, options):
        ev = threading.Event(); holder = []
        with self.pendlock:
            self.pending = (question, options, ev, holder)
        # wait for the main thread to service it, but stay responsive to /quit so
        # teardown (finally: set_paused(False)) isn't blocked forever on an ask.
        while not ev.wait(0.2):
            if self.stopflag.is_set():
                with self.pendlock:
                    self.pending = None
                return None
        return holder[0] if holder else None

    def worker(self):
        self.src.set_paused(True)                 # background launchd defers to the TUI
        tick = 0
        try:
            while not self.stopflag.is_set():
                if self._suspend.is_set():
                    time.sleep(0.2); continue
                self._busy.set()
                try:
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
                            if tick % 30 == 0 or self.force_poll.is_set():
                                self.force_poll.clear()
                                self.status = f"checking {self.ident} for new issues…"
                                engine.discover_into_queue(self.src, self.adapter)
                            if self.src.queue():
                                self.status = "working an issue…"
                                engine.drain_queue(self.src, self.adapter, interactive=False,
                                                   emit=self.emit, ask=self.ask)
                        except Exception as e:  # noqa: BLE001
                            self.emit(f"[error] {e}")
                finally:
                    self._busy.clear()
                self.status = "idle" if self.autonomous else "paused — /start to resume"
                for _ in range(20):               # ~2s, responsive
                    if self.stopflag.is_set() or self.msgq or self.force_poll.is_set():
                        break
                    time.sleep(0.1)
                tick += 1
        finally:
            self.src.set_paused(False)

    # ---------- rendering (main thread) ----------
    @staticmethod
    def _safe(scr, y, x, text, attr=0):
        """Bounds-checked write that never touches the bottom-right corner cell
        (which raises curses.error) and never draws off-screen."""
        h, w = scr.getmaxyx()
        if y < 0 or y >= h or x < 0 or x >= w:
            return
        # leave the final column free on every row so the cursor can't be pushed
        # off the bottom-right corner (the classic addnwstr ERR)
        maxlen = w - x - 1
        if maxlen <= 0:
            return
        try:
            scr.addnstr(y, x, str(text), maxlen, attr)
        except curses.error:
            pass

    def _draw(self, scr):
        scr.erase()
        h, w = scr.getmaxyx()
        if h < 3 or w < 10:
            scr.refresh(); return
        mode = self.src.mode()
        auto = "AUTONOMOUS" if self.autonomous else "PAUSED"
        head = f" watcher · {self.ident} · mode={mode} · {auto} · {self.status} "
        self._safe(scr, 0, 0, head.ljust(w), curses.A_REVERSE)
        # log pane: rows 1..h-3
        top, bottom = 1, h - 3
        rows = max(1, bottom - top + 1)
        with self.loglock:
            lines = list(self.log)
        self.scroll = max(0, min(self.scroll, max(0, len(lines) - rows)))  # bound scroll (#25)
        end = len(lines) - self.scroll
        view = lines[max(0, end - rows):end]
        for i, line in enumerate(view):
            self._safe(scr, top + i, 0, line)
        self._safe(scr, h - 2, 0, "─" * (w - 1))
        prompt = "› " + self.inp
        self._safe(scr, h - 1, 0, prompt.ljust(w))
        try:
            scr.move(h - 1, min(len(prompt), w - 2))
        except curses.error:
            pass
        scr.refresh()

    def _select(self, scr, title, options, timeout=None):
        """Arrow-key select. Returns index, or None on timeout/Esc. Scrolls the
        option list and never writes off-screen or the bottom-right corner."""
        idx = 0
        deadline = (time.time() + timeout) if timeout else None
        scr.nodelay(True)
        while True:
            h, w = scr.getmaxyx()
            scr.erase()
            self._safe(scr, 0, 0, " watcher — needs your input ".ljust(w), curses.A_REVERSE)
            self._safe(scr, 2, 2, title, curses.A_BOLD)
            avail = max(1, (h - 1) - 4)            # rows 4 .. h-2 usable; h-1 = footer
            first = max(0, idx - avail + 1)
            for i in range(first, len(options)):
                row = 4 + (i - first)
                if row >= h - 1:                   # never reach the footer row
                    break
                marker = "> " if i == idx else "  "
                attr = curses.A_REVERSE if i == idx else curses.A_NORMAL
                self._safe(scr, row, 4, marker + str(options[i]), attr)
            if deadline:
                left = max(0, int(deadline - time.time()))
                hint_row = min(4 + (len(options) - first) + 1, h - 2)
                self._safe(scr, hint_row, 4, f"(auto in {left}s -> safe path)", curses.A_DIM)
            self._safe(scr, h - 1, 0, " up/down move · Enter select · Esc = safe ".ljust(w), curses.A_DIM)
            scr.refresh()
            ch = scr.getch()
            if ch in (curses.KEY_UP, ord("k")):
                idx = (idx - 1) % len(options)
            elif ch in (curses.KEY_DOWN, ord("j")):
                idx = (idx + 1) % len(options)
            elif ch in (curses.KEY_ENTER, 10, 13):
                return idx
            elif ch == 27:                         # Esc
                return None
            if deadline and time.time() >= deadline:
                return None
            time.sleep(0.03)

    def _read_line(self, scr, label):
        curses.echo()
        try:
            curses.curs_set(1)
        except curses.error:
            pass
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
        # wait for any in-progress worker turn to finish so we don't run two
        # sessions on the same session id concurrently (corruption).
        waited = 0.0
        while self._busy.is_set() and waited < 180:
            self._safe(scr, 0, 0, " finishing current turn before handing off… ".ljust(1),
                       curses.A_REVERSE)
            scr.refresh(); time.sleep(0.3); waited += 0.3
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
            self.emit("… checking now"); self.force_poll.set(); return
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
        try:
            curses.curs_set(1)
        except curses.error:
            pass
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
    try:
        curses.wrapper(tui._main)
    finally:
        tui.stopflag.set()
        src.set_paused(False)      # never leave the source paused after the TUI exits
    print("watcher closed.")
