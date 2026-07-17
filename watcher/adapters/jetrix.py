"""Jetrix adapter — against the verified dev-jetrix Developer API (PR #177).

meta: {
  slug, platform: "jetrix",
  base_url: "http://localhost:8080/api/v1",
  solution_id: "<solutionId>",
  solution_name: "…",
  email: "owner@techjays.com",     # from /dev/me (assigned/mentions-me + anti-loop)
}
The API key is NOT stored in meta/config in plaintext by the engine prompt; it
lives in the source's gitignored `key` file and is injected as JETRIX_API_KEY
into the Claude subprocess env. Python polling reads it the same way.

Trigger source is the merged /activity feed (comments + activity incl. created).
"""
import json
import urllib.request
import urllib.error
from pathlib import Path

from .base import Adapter, Event
from ..runtime import SIGNATURE
from .. import config


class JetrixAdapter(Adapter):
    platform = "jetrix"

    def __init__(self, meta: dict):
        super().__init__(meta)
        self.base = meta["base_url"].rstrip("/")
        self.solution_id = meta["solution_id"]
        self.email = meta.get("email", "")

    # --- key handling ---
    def _key_path(self) -> Path:
        return config.Source(self.slug).dir / "key"

    def key(self) -> str:
        p = self._key_path()
        return p.read_text().strip() if p.exists() else ""

    def env(self) -> dict:
        return {"JETRIX_API_KEY": self.key()}

    def cwd(self):
        return None  # no local code repo

    # --- HTTP (Python polling) ---
    def _get(self, path: str):
        """Returns the unwrapped payload. Jetrix wraps every /dev response as
        {status, message, results:[<payload>]} — results[0] is the payload
        (identity object, the solutions list, or the activity {items} object)."""
        req = urllib.request.Request(self.base + path,
                                     headers={"x-api-key": self.key()})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise RuntimeError("JETRIX_AUTH: key invalid/expired — mint a new one")
            raise RuntimeError(f"jetrix GET {path}: HTTP {e.code}")
        if isinstance(data, dict) and "results" in data:
            res = data["results"]
            return res[0] if isinstance(res, list) and res else res
        return data

    def identity(self) -> dict:
        me = self._get("/dev/me")
        return {"email": me.get("email"), "name": me.get("name"),
                "is_org_admin": me.get("isOrgAdmin")}

    def discover_events(self, since: str) -> list[Event]:
        # newest-first feed; page back via nextBefore until we pass `since`.
        events: list[Event] = []
        before = ""
        while True:
            path = f"/dev/solutions/{self.solution_id}/activity?since={since}&limit=200"
            if before:
                path += f"&before={before}"
            resp = self._get(path)
            items = resp.get("items", [])
            for it in items:
                created = it.get("createdAt", "")
                if created <= since:
                    continue
                kind = "new_task" if it.get("type") == "created" else \
                       ("new_comment" if it.get("kind") == "comment" else "activity")
                body = it.get("body", "") or ""
                mentions = it.get("mentions", []) or []
                events.append(Event(
                    self.slug, kind, str(it.get("id")), created,
                    title=f"task {it.get('taskNumber')}",
                    url="",
                    directive=any(t in body.lower() for t in ("@watcher", "@claude")),
                    data={"taskNumber": it.get("taskNumber"),
                          "taskId": it.get("taskId"),
                          "authorEmail": it.get("authorEmail"),
                          "body": body[:2000],
                          "mentions": mentions,
                          "activity_type": it.get("type"),
                          "action": it.get("action"),
                          "mentions_me": self.email in mentions,
                          "_self": it.get("authorEmail") == self.email and SIGNATURE in body}))
            nxt = resp.get("nextBefore")
            if not nxt or not items:
                break
            before = nxt
        return events

    def is_self_event(self, event: Event) -> bool:
        return bool(event.data.get("_self"))

    def allowed_tools(self) -> list[str]:
        # Claude reads/writes Jetrix via curl to the configured base URL only.
        return [
            "Read", "Grep", "Glob", "TodoWrite",
            f"Bash(curl:*)",
        ]

    def prompt_section(self) -> str:
        b = self.base
        return (
            f"Platform: Jetrix (task tracker — NO local code repo, so triage/act on "
            f"tasks; never code-fix/deploy). You act as `{self.email}`.\n"
            f"Base URL: {b}. Your API key is in the env var $JETRIX_API_KEY — send it "
            f"as the header `x-api-key: $JETRIX_API_KEY` on every curl. Never print the key.\n"
            f"Solution (project) id: {self.solution_id}.\n"
            f"READ context for a task: `curl -s -H \"x-api-key: $JETRIX_API_KEY\" "
            f"'{b}/dev/solutions/{self.solution_id}/export'` (filter with ?status=/"
            f"?assigneeEmail=/?updatedSince=). Each task has rich `sections`, comments, activity.\n"
            f"REPLY (comment): `curl -s -X POST -H \"x-api-key: $JETRIX_API_KEY\" "
            f"-H 'Content-Type: application/json' '{b}/comment/create' "
            f"-d '{{\"solutionId\":\"{self.solution_id}\",\"taskId\":\"<TASK_OBJECTID>\","
            f"\"text\":\"…\",\"mentions\":[],\"parentCommentId\":null}}'`  "
            f"(taskId = the ObjectId `id`, NOT the number). End comments with \"{SIGNATURE}\".\n"
            f"CHANGE STATUS: `curl -s -X PUT … '{b}/solutions/{self.solution_id}/tasks/<taskNumber>' "
            f"-d '{{\"status\":\"inProgress\"}}'` (uses the numeric taskNumber; valid: todo, "
            f"reopen, inProgress, agentExecuting, devReview, inQaReview, blocked, done). "
            f"This endpoint returns the task object FLAT (not under results).\n"
            f"ASSIGN (SET semantics): `curl -s -X POST … "
            f"'{b}/solutions/{self.solution_id}/tasks/<taskNumber>/assign' "
            f"-d '{{\"assigneeIds\":[\"<userId>\"]}}'`.\n"
            f"Mentions register only via the mentions[userId] array; put @Name in text for display.\n"
            f"To mention/act on the right people, get userIds/emails from the export "
            f"(assignees[].id/email)."
        )
