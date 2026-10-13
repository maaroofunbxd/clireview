"""The weekly review of a CLI: what was merged since the last review, and the tag that marks a review done.

For the maintainer, to keep up with what agents merged. A review is an annotated git tag `reviewed-YYYY-MM-DD` on
the commit of main that was reviewed; its message is the short review note. A review reads from the last one
(start_of, merges, net_change); status() says when the next one is due.
"""
from __future__ import annotations

import datetime
import json
import re
import subprocess
from pathlib import Path
from typing import Optional

from clireview import Project
from clireview.history import AGENT_RE, GitError, _git, diff_trees, load_tree

TAG_PREFIX = "reviewed-"
EVERY_DAYS = 7
ISSUE_LABELS = ("command-idea", "rename")
REQUESTED_BY = re.compile(r"^Requested-by:\s*(.+)$", re.M | re.I)


def last_review(repo: Path) -> Optional[dict]:
    """The newest reviewed-* tag: {tag, sha, date, note}, or None when there has been no review."""
    out = _git(repo, "for-each-ref", "--sort=-creatordate", "--count=1",
               "--format=%(refname:short)%1f%(*objectname)%1f%(objectname)%1f%(creatordate:iso-strict)%1f%(contents:subject)",
               f"refs/tags/{TAG_PREFIX}*")
    if not out.strip():
        return None
    tag, commit, obj, date, note = out.strip().split("\x1f", 4)
    # %(*objectname) is the commit an annotated tag points to; a lightweight tag has only %(objectname).
    return {"tag": tag, "sha": commit or obj, "date": date, "note": note}


def merges(repo: Path, ref: str = "main", base: Optional[str] = None, since: Optional[str] = None) -> list[dict]:
    """First-parent commits on `ref` after `base` (a sha or tag), or since an ISO time: newest first,
    with actor (agent:<name> or user) and the repos that asked for it (Requested-by: trailers)."""
    args = ["log", "--first-parent", "--format=%H%x1f%cI%x1f%s%x1e", f"{base}..{ref}" if base else ref]
    if since and not base:
        args.append(f"--since={since}")
    out = []
    for rec in _git(repo, *args).split("\x1e"):
        if not rec.strip():
            continue
        sha, when, subject = rec.strip().split("\x1f", 2)
        body = _git(repo, "log", "--format=%B", f"{sha}^1..{sha}") if _has_parent(repo, sha) else ""
        requested = sorted({m.strip() for m in REQUESTED_BY.findall(body)})
        agents = sorted({a.split("<")[0].strip() for a in re.findall(r"^Co-Authored-By:\s*(.+)$", body, re.M | re.I)
                         if AGENT_RE.search(a)})
        out.append({"sha": sha, "when": when, "subject": subject, "requested_by": requested,
                    "actor": "agent:" + ",".join(agents) if agents else "user"})
    return out


def _has_parent(repo: Path, sha: str) -> bool:
    try:
        _git(repo, "rev-parse", "--verify", "-q", f"{sha}^1")
        return True
    except GitError:
        return False


def status(repo: Path, now: Optional[datetime.datetime] = None, every_days: int = EVERY_DAYS, ref: str = "main") -> dict:
    """Whether a review is due: {due, last, days, merges}. Due when the last review is `every_days` old (or there is
    none) and something was merged since. Cheap: no command trees are loaded."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    last = last_review(repo)
    count = len(_git(repo, "rev-list", "--first-parent", f"{last['sha']}..{ref}" if last else ref).split())
    # Python 3.9 parses no trailing "Z"
    days = (now - datetime.datetime.fromisoformat(last["date"].replace("Z", "+00:00"))).days if last else None
    due = count > 0 and (days is None or days >= every_days)
    return {"due": due, "last": last, "days": days, "merges": count}


def tag_name(repo: Path, day: datetime.date) -> str:
    """reviewed-YYYY-MM-DD, or -2, -3 ... when that day already has one."""
    base = f"{TAG_PREFIX}{day.isoformat()}"
    existing = set(_git(repo, "tag", "--list", f"{base}*").split())
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


def tag_review(repo: Path, tag: str, sha: str, note: str, push: bool = True, remote: str = "origin") -> None:
    """Create the annotated tag (the note is its message) and push it."""
    _git(repo, "tag", "-a", tag, sha, "-m", note)
    if push:
        _git(repo, "push", remote, f"refs/tags/{tag}")


def open_issues(gh_repo: str, labels: tuple[str, ...] = ISSUE_LABELS) -> list[dict]:
    """Open issues with any of `labels`, oldest first: {number, title, labels, url, created}. Raises RuntimeError."""
    seen: dict[int, dict] = {}
    for label in labels:
        proc = subprocess.run(["gh", "issue", "list", "-R", gh_repo, "--label", label, "--state", "open", "--limit", "100",
                               "--json", "number,title,labels,url,createdAt"], capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"gh issue list --label {label}: {proc.stderr.strip() or proc.returncode}")
        for i in json.loads(proc.stdout or "[]"):
            seen[i["number"]] = {"number": i["number"], "title": i["title"], "url": i["url"], "created": i["createdAt"],
                                 "labels": sorted(lb["name"] for lb in i.get("labels", []))}
    return sorted(seen.values(), key=lambda i: i["number"])


def start_of(repo: Path, ref: str = "main", base: Optional[str] = None, since: Optional[str] = None) -> Optional[str]:
    """The commit a review starts from: `base`, else the last first-parent commit on `ref` before `since`."""
    if base:
        return _git(repo, "rev-parse", base).strip()
    out = _git(repo, "rev-list", "-1", "--first-parent", f"--before={since}", ref).strip() if since else ""
    return out or None


def net_change(repo: Path, start: Optional[str], ref: str = "main", *, project: Project) -> dict:
    """The CLI at `start` against the CLI at `ref` (not merge by merge: removed then re-added is no change)."""
    if not start:
        return {}
    old, new = load_tree(repo, start, project), load_tree(repo, _git(repo, "rev-parse", ref).strip(), project)
    errors = {k: t["error"] for k, t in (("before", old), ("after", new)) if "error" in t}
    return {"errors": errors} if errors else diff_trees(old, new)


def breaking(change: dict) -> list[str]:
    """What in a net change can break a caller (a script, a scheduled task, another repo, the docs):
    removed or renamed commands, removed options, changed arguments, removed console scripts."""
    out = [f"- {c}" for c in change.get("removed", [])]
    out += [f"{a} -> {b}" for a, b in change.get("renamed", [])]
    for cmd, opt in (change.get("options") or {}).items():
        if opt.get("removed"):
            out.append(f"~ {cmd}: removed {' '.join(opt['removed'])}")
        if "args" in opt:
            out.append(f"~ {cmd}: args {' '.join(opt['args'][0]) or '()'} -> {' '.join(opt['args'][1]) or '()'}")
    out += [f"- script {s}" for s in change.get("scripts_removed", [])]
    return out


def added(change: dict) -> list[str]:
    """New commands, options and console scripts in a net change."""
    out = [f"+ {c}" for c in change.get("added", [])]
    out += [f"~ {cmd}: added {' '.join(opt['added'])}" for cmd, opt in (change.get("options") or {}).items() if opt.get("added")]
    out += [f"+ script {s}" for s in change.get("scripts_added", [])]
    return out
