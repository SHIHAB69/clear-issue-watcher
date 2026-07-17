"""The adapter contract. The engine does the polling (discover_events) in
Python, then hands each event to Claude Code; Claude reads/writes on the
platform using the tools the adapter allows + the instructions it injects.

An adapter therefore contributes four things:
  - discover_events(since)  : poll for new work (Python)
  - prompt_section()        : platform-specific how-to injected into the brief
  - allowed_tools()         : the Bash/tool grant Claude may use for this source
  - identity()              : who "I" am here (for assigned/mentions-me + anti-loop)
  - env()                   : extra subprocess env (e.g. API keys — kept out of the prompt)
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Event:
    source_slug: str
    kind: str                 # new_issue | new_comment | activity | new_task ...
    external_id: str          # stable dedupe key (issue/comment/event/activity id)
    ts: str                   # ISO8601 creation time (FIFO ordering + since)
    title: str = ""
    url: str = ""
    directive: bool = False    # comment addressed @watcher/@claude
    attempts: int = 0
    data: dict[str, Any] = field(default_factory=dict)  # raw payload for the prompt

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Event":
        return Event(**d)


class Adapter:
    """Base adapter. Subclasses set `platform`."""

    platform: str = "base"

    def __init__(self, meta: dict):
        self.meta = meta
        self.slug = meta["slug"]

    # --- polling (Python) ---
    def discover_events(self, since: str) -> list[Event]:
        raise NotImplementedError

    # --- how Claude acts on this platform ---
    def prompt_section(self) -> str:
        """Platform-specific instructions appended to the shared triage brief."""
        raise NotImplementedError

    def allowed_tools(self) -> list[str]:
        raise NotImplementedError

    def env(self) -> dict[str, str]:
        """Extra env for the Claude subprocess (secrets go here, not the prompt)."""
        return {}

    def cwd(self) -> str | None:
        """Working dir for Claude (a code repo for GitHub; None for Jetrix)."""
        return None

    # --- identity ---
    def identity(self) -> dict:
        """{'login'/'email': ..., 'name': ...} — who the watcher acts as."""
        raise NotImplementedError

    # --- anti-loop: is this event the bot's own action? ---
    def is_self_event(self, event: Event) -> bool:
        return False
