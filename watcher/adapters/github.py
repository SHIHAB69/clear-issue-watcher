"""GitHub adapter — ports the current watcher's gh polling into the interface.

meta: {
  slug, platform: "github",
  repo: "owner/name",
  project_dir: "/abs/path",      # Claude cwd → the repo's CLAUDE.md is context
  operator_login: "gh-login",    # who we act as (anti-loop + assigned-me)
  gh_config_dir: "/abs/path"     # optional: scoped gh identity (GH_CONFIG_DIR)
}
"""
import json
import subprocess

from .base import Adapter, Event
from ..runtime import SIGNATURE

MEANINGFUL_EVENTS = {"assigned", "unassigned", "labeled", "unlabeled",
                     "closed", "reopened", "renamed", "milestoned", "demilestoned"}


class GitHubAdapter(Adapter):
    platform = "github"

    def __init__(self, meta: dict):
        super().__init__(meta)
        self.repo = meta["repo"]
        self.project_dir = meta["project_dir"]
        self.me = meta.get("operator_login", "")
        if not self.me:
            # anti-loop AND the activity self-filter depend on knowing who we are;
            # an empty identity would make the watcher react to its own actions forever.
            raise ValueError("operator_login is required for a GitHub source")

    # gh runs under the scoped config dir if provided (keeps global gh untouched)
    def env(self) -> dict:
        d = {}
        if self.meta.get("gh_config_dir"):
            d["GH_CONFIG_DIR"] = self.meta["gh_config_dir"]
        return d

    def cwd(self):
        return self.project_dir

    def _gh(self, args: list[str]) -> list | dict:
        import os
        env = {**os.environ, **self.env()}
        out = subprocess.run(["gh"] + args, capture_output=True, text=True,
                             timeout=60, env=env)
        if out.returncode != 0:
            raise RuntimeError(f"gh {' '.join(args)}: {out.stderr.strip()[:200]}")
        return json.loads(out.stdout) if out.stdout.strip() else []

    def identity(self) -> dict:
        return {"login": self.me}

    def discover_events(self, since: str) -> list[Event]:
        events: list[Event] = []
        # new issues (--paginate: don't drop >per_page new issues; `since` bounds it)
        for it in self._gh(["api", "--paginate", f"repos/{self.repo}/issues", "-X", "GET",
                            "-f", f"since={since}", "-f", "state=all", "-f", "per_page=100"]):
            if "pull_request" in it or it.get("created_at", "") < since:
                continue
            events.append(Event(self.slug, "new_issue", str(it["id"]),
                                it["created_at"], it.get("title", ""),
                                it.get("html_url", ""),
                                data={"issue_number": it["number"],
                                      "author": it["user"]["login"]}))
        # comments (anyone; self-signed skipped in is_self_event). --paginate to not drop.
        for c in self._gh(["api", "--paginate", f"repos/{self.repo}/issues/comments", "-X", "GET",
                          "-f", f"since={since}", "-f", "per_page=100"]):
            if c.get("created_at", "") < since:
                continue
            if "/pull/" in (c.get("html_url", "") or ""):   # PR conversation comment — skip
                continue
            body = c.get("body", "") or ""
            num = int(c["issue_url"].rstrip("/").rsplit("/", 1)[-1])
            events.append(Event(self.slug, "new_comment", str(c["id"]),
                                c["created_at"], "", c.get("html_url", ""),
                                directive=any(t in body.lower() for t in ("@watcher", "@claude")),
                                data={"issue_number": num,
                                      "comment_author": c["user"]["login"],
                                      "comment_body": body[:2000],
                                      "_self": SIGNATURE in body}))
        # issue activity (endpoint has no `since`; newest-first → page until we cross `since`)
        page, MAX_PAGES = 1, 20      # ~2000 events/poll safety cap
        while page <= MAX_PAGES:
            batch = self._gh(["api", f"repos/{self.repo}/issues/events", "-X", "GET",
                             "-f", "per_page=100", "-f", f"page={page}"])
            if not batch:
                break
            crossed = False
            for ev in batch:
                if (ev.get("created_at") or "") <= since:
                    crossed = True            # this + all later pages are older
                    continue
                if ev.get("event") not in MEANINGFUL_EVENTS:
                    continue
                if (ev.get("actor") or {}).get("login") == self.me:   # bot's own action
                    continue
                iss = ev.get("issue") or {}
                if "pull_request" in iss or not iss.get("number"):
                    continue
                events.append(Event(self.slug, "activity", str(ev["id"]), ev["created_at"],
                                    "", iss.get("html_url", ""),
                                    data={"issue_number": iss["number"],
                                          "action": ev.get("event"),
                                          "actor": (ev.get("actor") or {}).get("login")}))
            if crossed:
                break
            page += 1
        return events

    def is_self_event(self, event: Event) -> bool:
        return bool(event.data.get("_self"))

    def allowed_tools(self) -> list[str]:
        return [
            "Read", "Grep", "Glob", "TodoWrite",
            "Bash(gh issue view:*)", "Bash(gh issue comment:*)", "Bash(gh issue edit:*)",
            f"Bash(gh api repos/{self.repo}:*)", "Bash(gh auth token:*)",
            "Bash(git log:*)", "Bash(git show:*)", "Bash(git diff:*)", "Bash(git status:*)",
            # full-mode extras are gated by the brief's hard limits, not the grant:
            "Write", "Edit", "Bash(git add:*)", "Bash(git commit:*)",
        ]

    def prompt_section(self) -> str:
        return (
            f"Platform: GitHub. Repo: `{self.repo}`. You act as `{self.me}`.\n"
            f"Working directory is the local checkout — read its CLAUDE.md first for "
            f"project context.\n"
            f"Read issues/comments with `gh issue view <n> --repo {self.repo} --comments`.\n"
            f"Reply with `gh issue comment <n> --repo {self.repo} --body \"…\"`; assign with "
            f"`gh issue edit <n> --repo {self.repo} --add-assignee <user>`.\n"
            f"End every comment you post with the signature line: \"{SIGNATURE}\".\n"
            f"This is a code project: in full mode you may investigate, and (within the "
            f"hard limits) implement/commit fixes. In triage mode, comment only."
        )
