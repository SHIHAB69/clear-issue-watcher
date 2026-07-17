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
    url = r.stdout.strip()
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
        sol = sols[int(pick) - 1]
    except (ValueError, IndexError):
        print("✗ Invalid choice.")
        return None
    slug = config.slugify("jetrix", sol["id"])
    return {"slug": slug, "platform": "jetrix", "base_url": base,
            "solution_id": sol["id"], "solution_name": sol["name"],
            "email": me.get("email"), "_key": key}


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
    config.Source(meta["slug"]).set_mode("triage")
    print(f"\n✓ Added source '{meta['slug']}' in triage mode.")
    print("  Test it now:   watcher run-once " + meta["slug"])
    print("  Go autonomous: watcher start   (installs the background runner)")


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
    ok = True
    for tool in ("python3", "claude", "gh"):
        path = shutil.which(tool)
        print(f"  {'✓' if path else '✗'} {tool}: {path or 'MISSING'}")
        ok = ok and bool(path)
    print(f"  watcher home: {config.HOME}")
    print("OK" if ok else "Some prerequisites missing (gh only needed for GitHub sources).")


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

    args = p.parse_args(argv)
    if args.cmd is None:            # bare `watcher` → add flow
        return cmd_add(args)
    {"add": cmd_add, "list": cmd_list, "remove": cmd_remove, "run-once": cmd_run_once,
     "mode": cmd_mode, "logs": cmd_logs, "doctor": cmd_doctor}[args.cmd](args)


if __name__ == "__main__":
    main()
