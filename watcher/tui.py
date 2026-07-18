"""The watcher TUI — built on Textual (the Rich authors' framework; Python's
answer to Ink, which Claude Code / Gemini CLI use). Real widgets instead of
hand-drawn curses: a scrolling log, a docked multi-line input that wraps and
grows, and a modal arrow-key approval dialog.

Layout: status bar (top) · scrolling log (fills) · multi-line input (docked bottom).
- Type a message + Enter → sent into the live session as an OPERATOR MESSAGE
  (Shift+Enter inserts a newline). Handled by a background worker.
- Commands: /stop /start (pause·resume), /mode full|triage, /chat (real Claude
  TUI), /poll, /quit.
- Background worker polls every ~60s and streams its work into the log.
- When it needs you, a modal appears: "Are you there? Yes/No" (10s → No → safe),
  then the question + the agent's suggestions + "type my own" + "you decide".
"""
import threading
import time

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from . import config, engine, runtime
from .adapters import build_adapter


class Prompt(Input):
    """Docked input. Enter submits; the widget scrolls long text (no stuck line)."""
    class Sent(Message):
        def __init__(self, text: str):
            self.text = text
            super().__init__()

    def action_submit(self) -> None:      # Enter
        txt = self.value.strip()
        self.value = ""
        if txt:
            self.post_message(self.Sent(txt))


class ApprovalScreen(ModalScreen):
    """Two-step arrow-key approval: 'Are you there?' → question + options."""
    CSS = """
    ApprovalScreen { align: center middle; }
    #box { width: 80%; max-width: 100; height: auto; border: round yellow; padding: 1 2; background: $panel; }
    #title { text-style: bold; margin-bottom: 1; }
    """

    def __init__(self, question, options, on_done):
        super().__init__()
        self.question = question
        self.options = options
        self.on_done = on_done
        self._stage = 1
        self._deadline = time.time() + 10   # 10s auto-No on stage 1

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Static("Need some clarification. Are you there?", id="title")
            yield OptionList(Option("Yes", id="yes"), Option("No", id="no"))

    def on_mount(self):
        ol = self.query_one(OptionList)
        ol.focus()
        ol.highlighted = 0                 # ensure Enter selects the first option
        self.set_interval(0.25, self._tick)

    def _tick(self):
        if self._stage == 1 and time.time() >= self._deadline:
            self._finish(None)             # timeout → safe path

    def on_option_list_option_selected(self, ev: OptionList.OptionSelected):
        if self._stage == 1:
            if ev.option.id == "yes":
                self._stage = 2
                self.query_one("#title", Static).update(self.question)
                ol = self.query_one(OptionList)
                ol.clear_options()
                opts = list(self.options) + ["Type my own answer…", "You decide (safe path)"]
                for i, o in enumerate(opts):
                    ol.add_option(Option(str(o), id=f"opt{i}"))
                self._opts = opts
                ol.highlighted = 0         # re-highlight after repopulating
                ol.focus()
            else:
                self._finish(None)
        else:
            idx = int(ev.option.id[3:])
            choice = self._opts[idx]
            if choice.startswith("You decide"):
                self._finish(None)
            elif choice.startswith("Type my own"):
                self.app.push_screen(_TypeAnswer(self.question), self._typed)
            else:
                self._finish(choice)

    def _typed(self, text):
        self._finish(text or None)

    def _finish(self, answer):
        cb = self.on_done
        self.on_done = None
        if cb:
            self.dismiss()
            cb(answer)


class _TypeAnswer(ModalScreen):
    CSS = "_TypeAnswer { align: center middle; } #b { width:80%; border: round cyan; padding:1 2; background:$panel; }"

    def __init__(self, q):
        super().__init__(); self.q = q

    def compose(self):
        with Vertical(id="b"):
            yield Static(self.q)
            yield Input(placeholder="type your answer, Enter to send")

    def on_input_submitted(self, ev: Input.Submitted):
        self.dismiss(ev.value.strip())


class WatcherApp(App):
    CSS = """
    #status { dock: top; height: 1; background: $accent; color: $text; }
    RichLog { background: $surface; }
    Prompt { dock: bottom; border: round $primary; }
    """
    BINDINGS = [Binding("ctrl+c", "quit", "quit", show=False)]

    def __init__(self, slug: str):
        super().__init__()
        self.slug = slug
        self.src = config.Source(slug)
        self.adapter = build_adapter(self.src.meta)
        self.ident = self.src.meta.get("repo") or self.src.meta.get("solution_name") or slug
        self.msgq = []
        self.msglock = threading.Lock()
        self.stopflag = threading.Event()
        self.autonomous = True
        self.status = "starting"
        self.force_poll = threading.Event()

    def compose(self) -> ComposeResult:
        yield Static("", id="status")
        yield RichLog(highlight=False, markup=False, wrap=True, id="log")
        p = Prompt(placeholder="message the watcher…  (Enter sends · /help)")
        yield p

    def on_mount(self):
        who = self.src.meta.get("operator_login") or self.src.meta.get("email") or "?"
        self._log(f"● connected to {self.ident} as {who}")
        self._log(f"● mode: {self.src.mode()} · checking for new activity every ~60s")
        self._log("● type a message to talk to it · /help for commands · Ctrl-C to quit")
        self.query_one(Prompt).focus()
        self.set_interval(0.5, self._refresh_status)
        threading.Thread(target=self.worker, daemon=True).start()

    # ---- UI helpers (main thread) ----
    def _log(self, line):
        self.query_one("#log", RichLog).write(line)

    def _refresh_status(self):
        auto = "AUTONOMOUS" if self.autonomous else "PAUSED"
        last = (self.src.state().get("last_checked", "") or "")[11:19]
        self.query_one("#status", Static).update(
            f" watcher · {self.ident} · {auto} · {self.status} · checked {last} ")

    # ---- called from the worker thread ----
    def emit(self, line):
        try:
            self.call_from_thread(self._log, str(line))
        except Exception:
            pass

    def ask(self, question, options):
        ev = threading.Event(); holder = []

        def show():
            def done(ans):
                holder.append(ans); ev.set()
            self.push_screen(ApprovalScreen(question, options, done))
        self.call_from_thread(show)
        while not ev.wait(0.2):
            if self.stopflag.is_set():
                return None
        return holder[0] if holder else None

    def worker(self):
        self.src.set_paused(True)
        tick = 0
        try:
            while not self.stopflag.is_set():
                with self.msglock:
                    pending = list(self.msgq); self.msgq.clear()
                for text in pending:
                    self.status = "answering you"
                    try:
                        runtime.chat(self.src, self.adapter, text, emit=self.emit)
                    except Exception as e:  # noqa: BLE001
                        self.emit(f"[error] {e}")
                if self.autonomous and not self.stopflag.is_set():
                    try:
                        if tick % 30 == 0 or self.force_poll.is_set():
                            forced = self.force_poll.is_set(); self.force_poll.clear()
                            self.status = f"checking {self.ident}…"
                            n = engine.discover_into_queue(self.src, self.adapter)
                            if n:
                                self.emit(f"· found {n} new item(s)")
                            elif forced:
                                self.emit(f"· checked {self.ident} — nothing new")
                        if self.src.queue():
                            self.status = "working an issue…"
                            engine.drain_queue(self.src, self.adapter, interactive=False,
                                               emit=self.emit, ask=self.ask)
                    except Exception as e:  # noqa: BLE001
                        self.emit(f"[error] {e}")
                self.status = "idle" if self.autonomous else "paused — /start to resume"
                for _ in range(20):
                    if self.stopflag.is_set() or self.msgq or self.force_poll.is_set():
                        break
                    time.sleep(0.1)
                tick += 1
        finally:
            self.src.set_paused(False)

    # ---- input ----
    def on_prompt_sent(self, ev: "Prompt.Sent"):
        line = ev.text
        if line in ("/quit", "/exit"):
            self.exit(); return
        if line == "/help":
            self._log("commands: /stop /start · /mode full|triage · /chat · /poll · /quit · "
                      "anything else = message to the session"); return
        if line in ("/stop", "/pause"):
            self.autonomous = False; self._log("⏸ autonomous paused (/start to resume)"); return
        if line in ("/start", "/resume"):
            self.autonomous = True; self._log("▶ autonomous resumed"); return
        if line == "/poll":
            self._log("… checking now"); self.force_poll.set(); return
        if line.startswith("/mode"):
            p = line.split()
            if len(p) == 2 and p[1] in ("triage", "full"):
                self.src.set_mode(p[1]); self._log(f"mode → {p[1]}")
            else:
                self._log("usage: /mode full|triage")
            return
        if line == "/chat":
            self._chat_handoff(); return
        self._log(f"🧑 you: {line}")
        with self.msglock:
            self.msgq.append(line)

    def _chat_handoff(self):
        import subprocess, shutil, os
        sid = self.src.session_id()
        cmd = [shutil.which("claude") or "claude"] + (["--resume", sid] if sid else [])
        with self.suspend():                 # drop out of the TUI, run real Claude, come back
            subprocess.run(cmd, cwd=self.adapter.cwd(), env={**os.environ, **self.adapter.env()})

    def action_quit(self):
        self.stopflag.set()
        self.exit()


def run(slug: str):
    src = config.Source(slug)
    if not src.meta:
        print(f"✗ No source '{slug}'.")
        return
    app = WatcherApp(slug)
    try:
        app.run()
    finally:
        app.stopflag.set()
        src.set_paused(False)
    print("watcher closed.")
