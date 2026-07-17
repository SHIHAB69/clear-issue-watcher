"""`watcher` CLI. Phase-1 commands: add (interactive), list, remove, run-once,
mode, logs, doctor. Scheduling/daemon/TUI arrive in later phases.
"""
import argparse
import json
import shutil
import subprocess
import sys
import urllib.request
import urllib.error
from pathlib import Path

from . import config, engine


def _sh(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


# ---------------- add: GitHub (auto-detect) ----------------
def _add_github() -> dict | None:
    cwd = Path.cwd()
    r = _sh(["git", "-C", str(cwd), "remote", "get-url", "origin"])
    if r.returncode != 0:
        print("✗ Not a git repo here. Open watcher inside your project folder.")
        return None
    url = r.stdout.strip().rstrip("/")     # tolerate a trailing slash on the remote URL
    import re
    m = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?$", url)
    if not m:
        print(f"✗ Couldn't parse repo from remote: {url}")
        return None
    repo = m.group(1)
    who = _sh(["gh", "api", "user", "-q", ".login"])
    if who.returncode != 0:
        print("✗ GitHub CLI not authenticated. Run: gh auth login")
        return None
    login = who.stdout.strip()
    slug = config.slugify("github", repo)
    print(f"✓ GitHub repo: {repo}\n✓ Acting as: {login}\n✓ Project dir: {cwd}")
    return {"slug": slug, "platform": "github", "repo": repo,
            "project_dir": str(cwd), "operator_login": login}


# ---------------- add: Jetrix (key + solution picker) ----------------
def _jetrix_get(base, key, path):
    req = urllib.request.Request(base.rstrip("/") + path, headers={"x-api-key": key})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode())
    if isinstance(data, dict) and "results" in data:   # unwrap {status,message,results:[payload]}
        res = data["results"]
        return res[0] if isinstance(res, list) and res else res
    return data


def _add_jetrix() -> dict | None:
    base = input("Jetrix base URL [http://localhost:8080/api/v1]: ").strip() \
        or "http://localhost:8080/api/v1"
    key = input("Paste your Jetrix API key (jtx_live_…): ").strip()
    if not key.startswith("jtx_"):
        print("✗ That doesn't look like a jtx_ key.")
        return None
    try:
        me = _jetrix_get(base, key, "/dev/me")
        sols = _jetrix_get(base, key, "/dev/solutions")
    except urllib.error.HTTPError as e:
        print(f"✗ Auth failed (HTTP {e.code}). Mint a fresh key and retry.")
        return None
    except Exception as e:  # noqa: BLE001
        print(f"✗ Couldn't reach Jetrix at {base}: {e}")
        return None
    print(f"✓ Authenticated as {me.get('email')} (org admin: {me.get('isOrgAdmin')})")
    if not sols:
        print("✗ No solutions visible to you.")
        return None
    print("\nYour solutions:")
    for i, s in enumerate(sols, 1):
        print(f"  {i}. {s['name']}  ({s.get('taskCount', '?')} tasks)  [{s['id']}]")
    pick = input("Pick a number: ").strip()
    try:
        idx = int(pick)
        if not 1 <= idx <= len(sols):        # reject 0/negatives (Python neg-indexing) + out of range
            raise IndexError
        sol = sols[idx - 1]
    except (ValueError, IndexError):
        print("✗ Invalid choice.")
        return None
    slug = config.slugify("jetrix", sol["id"])
    return {"slug": slug, "platform": "jetrix", "base_url": base,
            "solution_id": sol["id"], "solution_name": sol["name"],
            "email": me.get("email"), "_key": key}


def _cwd_repo() -> str | None:
    """owner/name of the git repo in the current directory, or None."""
    r = _sh(["git", "rev-parse", "--is-inside-work-tree"])
    if r.returncode != 0:
        return None
    u = _sh(["git", "remote", "get-url", "origin"])
    if u.returncode != 0:
        return None
    import re
    m = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?$", u.stdout.strip().rstrip("/"))
    return m.group(1) if m else None


def _bare(args):
    """`watcher` with no subcommand — context-aware:
    - inside a registered project → open its live view
    - inside a git repo not yet watched → offer to add THIS project
    - otherwise → set up (if empty) or pick from the dashboard
    """
    from . import attach, scheduler, tui
    srcs = config.load_config()["sources"]
    cwd_repo = _cwd_repo()

    # 1) in a git project? prefer the source that matches THIS repo
    if cwd_repo:
        match = next((s for s in srcs if s.get("platform") == "github"
                      and s.get("repo") == cwd_repo), None)
        if match:
            return tui.run(match["slug"])
        # a git repo we don't watch yet → offer to add it
        print(f"This project is `{cwd_repo}`, not watched yet.")
        if input("Add it now? [Y/n]: ").strip().lower() in ("", "y", "yes"):
            meta = _add_github()
            if meta:
                config.add_source(meta)
                config.Source(meta["slug"]).set_mode("full")
                print(f"✓ Added '{meta['slug']}' (full mode — unlocked).")
                if input("Open its live view now? [Y/n]: ").strip().lower() in ("", "y", "yes"):
                    return tui.run(meta["slug"])
                return
        if not srcs:
            return          # declined and nothing else to open
        # otherwise fall through to the picker so existing sources stay reachable

    # 2) not in a project (or declined above with sources present) — set up or pick
    if not srcs:
        print("👋 No sources yet — let's set one up.\n")
        cmd_add(args)
        srcs = config.load_config()["sources"]
        if srcs and input("\nRun watcher in the background from now on? [Y/n]: ")\
                .strip().lower() in ("", "y", "yes"):
            print(scheduler.start())
        return
    print("Your sources:")
    for i, s in enumerate(srcs, 1):
        src = config.Source(s["slug"])
        ident = s.get("repo") or s.get("solution_name") or ""
        print(f"  {i}. {s['slug']}  [{s['platform']}] {ident}  mode={src.mode()}")
    pick = input("Open which? (number, or 'a' to add another): ").strip()
    if pick.lower() == "a":
        return cmd_add(args)
    try:
        tui.run(srcs[int(pick) - 1]["slug"])
    except (ValueError, IndexError):
        print("nothing selected.")


def cmd_add(_args):
    print("Add a source. Platform?\n  1. GitHub (run inside a project folder)\n  2. Jetrix")
    choice = input("Pick [1]: ").strip() or "1"
    meta = _add_github() if choice == "1" else _add_jetrix() if choice == "2" else None
    if not meta:
        sys.exit(1)
    key = meta.pop("_key", None)
    config.add_source(meta)
    if key:  # store jetrix key in the source's gitignored dir, not config.json
        (config.Source(meta["slug"]).dir / "key").write_text(key)
    config.Source(meta["slug"]).set_mode("full")
    print(f"\n✓ Added source '{meta['slug']}' — full mode (unlocked, no config needed).")
    print("  Open it:       watcher   (from the project dir)")
    print("  Background:    watcher start")


def cmd_list(_args):
    srcs = config.load_config()["sources"]
    if not srcs:
        print("No sources yet. Run `watcher` inside a project, or `watcher add`.")
        return
    for s in srcs:
        src = config.Source(s["slug"])
        q = len(src.queue())
        ident = s.get("repo") or s.get("solution_name") or ""
        print(f"● {s['slug']}  [{s['platform']}] {ident}  mode={src.mode()}  queued={q}")


def cmd_remove(args):
    if config.remove_source(args.slug):
        print(f"✓ Removed {args.slug} (state folder left in place; delete manually if desired).")
    else:
        print(f"✗ No source '{args.slug}'.")


def cmd_run_once(args):
    if args.slug:
        engine.run_source(args.slug)
    else:
        engine.run_all()
    print("cycle complete — see: watcher logs")


def cmd_mode(args):
    src = config.Source(args.slug)
    if not src.meta:
        print(f"✗ No source '{args.slug}'.")
        sys.exit(1)
    if args.value:
        if args.value not in ("triage", "full"):
            print("✗ mode must be triage or full")
            sys.exit(1)
        src.set_mode(args.value)
        print(f"✓ {args.slug} → {args.value}")
    else:
        print(src.mode())


def cmd_logs(args):
    if not config.LOG.exists():
        print("(no log yet)")
        return
    if args.follow:
        subprocess.run(["tail", "-f", str(config.LOG)])
    else:
        print("\n".join(config.LOG.read_text().splitlines()[-40:]))


def cmd_doctor(_args):
    from . import scheduler
    ok = True
    for tool in ("python3", "claude", "gh"):
        path = shutil.which(tool)
        print(f"  {'✓' if path else '✗'} {tool}: {path or 'MISSING'}")
        ok = ok and bool(path)
    print(f"  watcher home: {config.HOME}")
    print(f"  scheduler:    {scheduler.status()}")
    print("OK" if ok else "Some prerequisites missing (gh only needed for GitHub sources).")


def cmd_start(args):
    from . import scheduler
    print(scheduler.start(interval=args.interval))


def cmd_stop(_args):
    from . import scheduler
    print(scheduler.stop())


def cmd_status(_args):
    from . import scheduler
    print(f"scheduler: {scheduler.status()}")
    cmd_list(_args)


def cmd_attach(args):
    from . import attach
    attach.attach(args.slug)


def cmd_chat(args):
    """Hand off to the REAL Claude Code TUI, resumed on this source's session —
    full input panel, interject, arrow-key permissions, Esc to interrupt.
    Pauses autonomous processing for the source while you're in it."""
    from .adapters import build_adapter
    src = config.Source(args.slug)
    if not src.meta:
        print(f"✗ No source '{args.slug}'. See: watcher list")
        sys.exit(1)
    adapter = build_adapter(src.meta)
    claude = shutil.which("claude") or "claude"
    sid = src.session_id()
    cmd = [claude, "--resume", sid] if sid else [claude]
    import os
    env = {**os.environ, **adapter.env()}
    print(f"↪ opening Claude Code on {args.slug}"
          + (f" (resuming session {sid[:8]})" if sid else " (new session)")
          + " — autonomous processing paused while you're in here.\n")
    src.set_paused(True)
    try:
        subprocess.run(cmd, cwd=adapter.cwd(), env=env)   # inherits your terminal = full TUI
    finally:
        src.set_paused(False)
        # the interactive session may have created/advanced a session id; keep the newest
        print(f"\n↩ back to watcher. autonomous processing resumed for {args.slug}.")


def cmd_pause(args):
    config.Source(args.slug).set_paused(True)
    print(f"⏸  {args.slug} paused (autonomous processing held). Resume: watcher resume {args.slug}")


def cmd_resume(args):
    config.Source(args.slug).set_paused(False)
    print(f"▶  {args.slug} resumed.")


def cmd_migrate_legacy(_args):
    """Import the legacy single-file watcher (~/.clear-issue-watcher) as a source."""
    import plistlib
    legacy = Path.home() / ".clear-issue-watcher"
    lcfg = legacy / "config.json"
    if not lcfg.exists():
        print("✗ No legacy config at ~/.clear-issue-watcher/config.json — nothing to migrate.")
        return
    old = json.loads(lcfg.read_text())
    repo = old.get("github_repo"); pdir = old.get("project_dir")
    login = old.get("operator_login", "")
    if not repo or not pdir:
        print("✗ Legacy config missing github_repo/project_dir.")
        return
    # find GH_CONFIG_DIR from the old launchd plist, if any
    gh_dir = ""
    plist = Path.home() / "Library/LaunchAgents/com.clear.issue-watcher.plist"
    if plist.exists():
        try:
            gh_dir = plistlib.loads(plist.read_bytes()).get("EnvironmentVariables", {}).get("GH_CONFIG_DIR", "")
        except Exception:
            pass
    meta = {"slug": config.slugify("github", repo), "platform": "github",
            "repo": repo, "project_dir": pdir, "operator_login": login}
    if gh_dir:
        meta["gh_config_dir"] = gh_dir
    config.add_source(meta)
    src = config.Source(meta["slug"])
    # preserve last_checked so the backlog isn't re-triaged; fresh processed set
    old_state = legacy / "state.json"
    last = json.loads(old_state.read_text()).get("last_checked") if old_state.exists() else None
    st = src.state()
    if last:
        st["last_checked"] = last
    src.save_state(st)
    old_mode = (legacy / "mode")
    src.set_mode(old_mode.read_text().strip() if old_mode.exists() else "full")
    print(f"✓ Migrated '{repo}' → source '{meta['slug']}' "
          f"(mode={src.mode()}, gh_config_dir={gh_dir or 'default'}, last_checked preserved).")
    print("\nNow switch schedulers so only ONE runner is active:")
    print("  launchctl unload ~/Library/LaunchAgents/com.clear.issue-watcher.plist   # stop legacy")
    print("  watcher start                                                            # start new engine")
    print("  watcher list")


def main(argv=None):
    p = argparse.ArgumentParser(prog="watcher",
                                description="persistent Claude Code runtime for task sources")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("add", help="add a source (interactive)")
    sub.add_parser("list", help="list sources")
    r = sub.add_parser("remove"); r.add_argument("slug")
    ro = sub.add_parser("run-once", help="run one cycle now (headless test)")
    ro.add_argument("slug", nargs="?")
    m = sub.add_parser("mode"); m.add_argument("slug"); m.add_argument("value", nargs="?")
    lg = sub.add_parser("logs"); lg.add_argument("-f", "--follow", action="store_true")
    sub.add_parser("doctor", help="check prerequisites")
    st = sub.add_parser("start", help="install the background runner (scheduler)")
    st.add_argument("--interval", type=int, default=120, help="poll seconds (default 120)")
    sub.add_parser("stop", help="remove the background runner")
    sub.add_parser("status", help="scheduler + sources overview")
    at = sub.add_parser("attach", help="live monitor a source (chat + approvals)")
    at.add_argument("slug")
    ch = sub.add_parser("chat", help="open the REAL Claude Code TUI on a source's session")
    ch.add_argument("slug")
    pz = sub.add_parser("pause", help="hold autonomous processing for a source"); pz.add_argument("slug")
    rs = sub.add_parser("resume", help="resume autonomous processing"); rs.add_argument("slug")
    sub.add_parser("migrate-legacy", help="import the old ~/.clear-issue-watcher as a source")

    args = p.parse_args(argv)
    if args.cmd is None:            # bare `watcher` is the hero command
        return _bare(args)
    {"add": cmd_add, "list": cmd_list, "remove": cmd_remove, "run-once": cmd_run_once,
     "mode": cmd_mode, "logs": cmd_logs, "doctor": cmd_doctor, "start": cmd_start,
     "stop": cmd_stop, "status": cmd_status, "attach": cmd_attach, "chat": cmd_chat,
     "pause": cmd_pause, "resume": cmd_resume,
     "migrate-legacy": cmd_migrate_legacy}[args.cmd](args)


if __name__ == "__main__":
    main()
