#!/usr/bin/env python3
"""After a block: ask RedHanded for the version to go back to, and open the fix as a PR.

Standard library plus git and npm from the runner. npm only rewrites the lockfile
(--package-lock-only --ignore-scripts), so nothing from the package runs here. A PR opened
with the workflow's own token starts no workflows, so the verdict for the fix branch is
fetched here and posted on its commit. A fork's read-only token gets a comment-free no-op.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

TIMEOUT_S = 15 * 60
BLOCKED_FILE = "redhanded-blocked.json"


def call(server, path, body=None):
    req = urllib.request.Request(server + path, data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json", "User-Agent": "redhanded-action"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def wait(server, job):
    deadline = time.monotonic() + TIMEOUT_S
    while time.monotonic() < deadline:
        j = call(server, f"/api/jobs/{job}")
        if j["state"] in ("done", "error"):
            return j
        time.sleep(3)
    return {"state": "error", "error": "timed out"}


def github(method, path, token, body=None):
    url = f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}{path}"
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body else None, method=method,
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "redhanded-action"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def run(*cmd):
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def describe_step(step):
    args = step.get("args") or {}
    result = step.get("result") or {}
    target = args.get("version", "")
    if step["tool"] == "list_versions":
        return f"Listed older releases: {', '.join(result.get('older', [])[:5])}"
    if step["tool"] == "check_version":
        return f"Ran {target} in the sandbox: **{result.get('verdict', result.get('error', '?'))}**"
    if step["tool"] == "compare_versions":
        return f"Compared {target} with the blocked version: {len(result.get('new_in_blocked_version', []))} new behaviours"
    if step["tool"] == "pin_version":
        return f"Pinned {target}" if result.get("pinned") else f"Pin of {target} refused: rules had not cleared it"
    return step["tool"]


def body_for(blocked, repair, server):
    pin = repair["pinned"]
    lines = [f"RedHanded blocked `{blocked['name']}@{blocked['version']}`. This goes back to `{pin['version']}`.", "", "### What the blocked version added"]
    changes = repair.get("new_in_blocked_version") or []
    lines += [f"- {c['kind'].replace('_', ' ')}: `{str(c['what']).replace('`', '')}`" for c in changes[:15]] or ["- Nothing recorded beyond the block itself."]
    lines += ["", f"### What the agent did ({repair['decided_by']})"]
    lines += [f"{i}. {describe_step(s)}" for i, s in enumerate(repair["steps"], 1)]
    if pin.get("scan_id"):
        lines += ["", f"Evidence for `{pin['version']}`: {server}/#{pin['scan_id']}"]
    return "\n".join(lines)


def fix_one(server, token, blocked, head):
    j = wait(server, call(server, "/api/repair", {"name": blocked["name"], "version": blocked["version"], "integrity": blocked.get("integrity")})["job"])
    if j["state"] != "done" or not j["result"].get("pinned"):
        print(f"no fix for {blocked['name']}: {j.get('error') or j['result'].get('note')}")
        return None
    repair = j["result"]
    safe = repair["pinned"]["version"]
    branch = f"redhanded/pin-{blocked['name'].replace('@', '').replace('/', '-')}-{safe}"
    old_lock = Path("package-lock.json").read_text()
    run("git", "config", "user.name", "github-actions[bot]")
    run("git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    run("git", "switch", "-c", branch)
    run("npm", "install", f"{blocked['name']}@{safe}", "--save-exact", "--package-lock-only", "--ignore-scripts", "--no-audit", "--no-fund")
    run("git", "add", "package.json", "package-lock.json")
    run("git", "commit", "-m", f"Pin {blocked['name']} to {safe}, the closest version RedHanded cleared")
    run("git", "push", "--force", "origin", f"HEAD:refs/heads/{branch}")
    sha = run("git", "rev-parse", "HEAD").strip()
    run("git", "switch", "--detach", "-")
    pr = github("POST", "/pulls", token, {"title": f"Pin {blocked['name']} to {safe}", "head": branch, "base": head, "body": body_for(blocked, repair, server)})
    verify = wait(server, call(server, "/api/lockfile", {"new_lockfile": run("git", "show", f"{sha}:package-lock.json"), "old_lockfile": old_lock})["job"])
    worst = (verify.get("result") or {}).get("worst_verdict", "UNKNOWN")
    github("POST", f"/statuses/{sha}", token, {"state": "success" if worst in ("CLEAN", "REVIEW") else "failure", "context": "redhanded", "description": f"{worst} for the fix branch"[:140]})
    return pr["html_url"]


def main():
    server = sys.argv[1].rstrip("/")
    token, head = os.environ.get("REDHANDED_TOKEN", ""), os.environ.get("GITHUB_HEAD_REF", "")
    path = Path(os.environ.get("RUNNER_TEMP", ".")) / BLOCKED_FILE
    if not (token and head and path.is_file()):
        return 0
    lines = []
    for blocked in json.loads(path.read_text()):
        try:
            url = fix_one(server, token, blocked, head)
            if url:
                lines.append(f"Fix for `{blocked['name']}@{blocked['version']}`: {url}")
        except (subprocess.CalledProcessError, urllib.error.URLError, OSError, ValueError, KeyError) as e:
            print(f"could not open a fix for {blocked['name']}: {type(e).__name__}: {getattr(e, 'stderr', '') or e}"[:400])
    if lines:
        text = "\n".join(lines)
        print(text)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
                f.write("\n" + text + "\n")
        number = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text()).get("number") if os.environ.get("GITHUB_EVENT_PATH") else None
        if number:
            github("POST", f"/issues/{number}/comments", token, {"body": text})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
