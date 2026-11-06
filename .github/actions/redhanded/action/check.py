#!/usr/bin/env python3
"""PR check: send what the pull request changes to RedHanded and fail on block.

Standard library only. Registry packages go by lockfile, and only by lockfile. A
package vendored into the repo and linked from the lockfile has no bytes on the
registry, so it is packed and uploaded. An answer that never arrives fails the check.
"""
import base64
import io
import json
import os
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path

BLOCK = {"HIGH", "CRITICAL"}
TIMEOUT_S = 15 * 60


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True).stdout


def call(server: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(server + path, data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json", "User-Agent": "redhanded-action"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def pack(folder: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in sorted(folder.rglob("*")):
            if path.is_file() and not path.is_symlink() and "node_modules" not in path.parts:
                tar.add(path, arcname="package/" + path.relative_to(folder).as_posix(), recursive=False)
    return buf.getvalue()


def changed_vendored(lock: dict, base: str) -> list[Path]:
    """Linked packages in the lockfile whose folder this PR touches."""
    out = []
    for entry in lock.get("packages", {}).values():
        target = entry.get("resolved") if entry.get("link") else None
        if target and git("diff", "--name-only", f"origin/{base}...HEAD", "--", target).strip():
            out.append(Path(target))
    return out


def wait(server: str, job: str) -> dict:
    deadline = time.monotonic() + TIMEOUT_S
    while time.monotonic() < deadline:
        j = call(server, f"/api/jobs/{job}")
        if j["state"] in ("done", "error"):
            return j
        time.sleep(3)
    return {"state": "error", "error": "timed out waiting for a verdict"}


def main() -> int:
    server = sys.argv[1].rstrip("/")
    base = os.environ.get("GITHUB_BASE_REF", "main")
    if not Path("package-lock.json").is_file():
        print("no package-lock.json")
        return 0
    new = Path("package-lock.json").read_text()
    old = git("show", f"origin/{base}:package-lock.json")

    rows, failed = [], False
    for folder in changed_vendored(json.loads(new), base):
        try:
            job = call(server, "/api/scan", {"tarball": base64.b64encode(pack(folder)).decode()})["job"]
            j = wait(server, job)
        except (urllib.error.URLError, OSError, ValueError) as e:
            j = {"state": "error", "error": str(e)}
        if j["state"] != "done":
            rows.append((str(folder), "ERROR", j.get("error", ""), ""))
            failed = True
            continue
        r = j["result"]
        rows.append((f"{r['package']}@{r['version']}", r["verdict"], "; ".join(r["reasons"]), server + r["url"]))
        failed |= r["verdict"] in BLOCK

    try:
        j = wait(server, call(server, "/api/lockfile", {"new_lockfile": new, "old_lockfile": old})["job"])
    except (urllib.error.URLError, OSError, ValueError) as e:
        j = {"state": "error", "error": str(e)}
    if j["state"] != "done":
        rows.append(("package-lock.json", "ERROR", j.get("error", ""), ""))
        failed = True
    else:
        for r in j["result"]["packages"]:
            verdict = r.get("verdict") or "UNSCANNED"
            link = f"{server}/#{r['scan_id']}" if r.get("scan_id") else ""
            rows.append((f"{r['package']}@{r['version']}", verdict, "; ".join(r.get("reasons", [])) or r.get("note", ""), link))
            failed |= verdict in BLOCK or verdict == "UNSCANNED"

    lines = ["| Package | Verdict | Why | Evidence |", "|---|---|---|---|"]
    lines += [f"| {p} | {v} | {w.replace('|', '/')} | {f'[open]({u})' if u else ''} |" for p, v, w, u in rows]
    table = "\n".join(lines) if rows else "No dependency changes."
    print(table)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(table + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
