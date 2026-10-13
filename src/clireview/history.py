"""What changed in a typer CLI itself, merge by merge, read from git.

For each first-parent commit on a branch (normally main: the merges) the command tree before and after is loaded
from that commit's own source (`git archive` of Project.src into a temp dir, imported by a fresh Python, every
command and option walked), and the two are compared: commands added / removed / renamed, options added / removed,
and the console scripts in pyproject.toml. It reads the code, not commit messages, so it is the same whether a person
or an agent wrote the change; who did is the commits' authors plus their Co-Authored-By trailers (an agent's
trailer -> `agent:<name>`, none -> `user`).

A tree is cached by the hash of Project.src + pyproject.toml under Project.cache_dir, so only new commits are loaded
(~1 s each). Only committed code on that branch is seen: not a worktree, not an unmerged branch.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

from clireview import Project

PYPROJECT = "pyproject.toml"
AGENT_RE = re.compile(r"claude|codex|gemini|cursor|copilot|aider|anthropic|openai|devin", re.I)

# Run in a fresh interpreter with the commit's src/ first on sys.path, so its own code is what gets imported.
# argv: the sys.path folder, the app's module, its attribute, the command's name.
_DUMP = r"""
import importlib, json, sys
sys.path.insert(0, sys.argv[1])
import typer.main
app = getattr(importlib.import_module(sys.argv[2]), sys.argv[3])
out = {}
def walk(cmd, path):
    subs = getattr(cmd, "commands", None)
    if subs:
        for name, sub in subs.items():
            walk(sub, path + [name])
        return
    params = getattr(cmd, "params", [])
    out[" ".join(path)] = {
        "options": sorted(o for p in params if p.param_type_name == "option" for o in p.opts),
        "args": [p.name for p in params if p.param_type_name == "argument"],
        "help": ((cmd.help or cmd.short_help or "").strip().splitlines() or [""])[0],
    }
walk(typer.main.get_command(app), [sys.argv[4]])
print(json.dumps(out))
"""


class GitError(Exception):
    pass


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout


def _key(repo: Path, sha: str, project: Project) -> str:
    parts = []
    for path in (project.src, PYPROJECT):
        try:
            parts.append(_git(repo, "rev-parse", f"{sha}:{path}").strip())
        except GitError:
            parts.append("-")
    return hashlib.sha256(" ".join(parts).encode()).hexdigest()[:24]


def parse_scripts(pyproject: str) -> dict:
    """[project.scripts] of a pyproject.toml as {name: target} (no tomllib: CI runs Python 3.9)."""
    match = re.search(r"^\[project\.scripts\]\s*$(.*?)(?=^\[|\Z)", pyproject, re.M | re.S)
    if not match:
        return {}
    return dict(re.findall(r'^\s*([\w.-]+)\s*=\s*"([^"]+)"', match.group(1), re.M))


def load_tree(repo: Path, sha: str, project: Project, python: str = sys.executable) -> dict:
    """{"commands": {...}, "scripts": {...}} at a commit, or {"error": ...} if its CLI does not import."""
    key = _key(repo, sha, project)
    cached = project.cache_dir / f"{key}.json"
    try:
        return json.loads(cached.read_text())
    except (OSError, ValueError):
        pass
    try:
        scripts = parse_scripts(_git(repo, "show", f"{sha}:{PYPROJECT}"))
    except GitError:
        scripts = {}
    with tempfile.TemporaryDirectory(prefix="cli-tree-") as tmp:
        archive = subprocess.run(["git", "-C", str(repo), "archive", sha, project.src], capture_output=True)
        if archive.returncode != 0:
            tree = {"error": f"no {project.src} at {sha[:8]}", "scripts": scripts}
        else:
            subprocess.run(["tar", "-x", "-C", tmp], input=archive.stdout, check=True)
            env = {**os.environ, **project.env, "PYTHONDONTWRITEBYTECODE": "1"}
            proc = subprocess.run([python, "-c", _DUMP, str(Path(tmp) / project.path_root), project.module, project.attr,
                                   project.name], capture_output=True, text=True, env=env, cwd=tmp, timeout=120)
            if proc.returncode == 0:
                tree = {"commands": json.loads(proc.stdout.strip().splitlines()[-1]), "scripts": scripts}
            else:
                last = (proc.stderr.strip().splitlines() or ["exit " + str(proc.returncode)])[-1]
                tree = {"error": last[:300], "scripts": scripts}
    try:
        project.cache_dir.mkdir(parents=True, exist_ok=True)
        cached.write_text(json.dumps(tree))
    except OSError:
        pass
    return tree


def _signature(info: dict) -> tuple:
    return (tuple(o for o in info["options"] if o != "--help"), tuple(info["args"]))


def diff_trees(old: dict, new: dict) -> dict:
    """Commands added / removed / renamed, options per command, console scripts. Empty lists when nothing changed."""
    before, after = old.get("commands") or {}, new.get("commands") or {}
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    renamed = []
    for gone in list(removed):
        # Same parameters and same help line under a new name: a rename, not a remove + add.
        match = next((a for a in added if _signature(after[a]) == _signature(before[gone])
                      and after[a]["help"] == before[gone]["help"]), None)
        if match:
            renamed.append([gone, match])
            removed.remove(gone)
            added.remove(match)
    options = {}
    for cmd in sorted(set(before) & set(after)):
        plus = sorted(set(after[cmd]["options"]) - set(before[cmd]["options"]))
        minus = sorted(set(before[cmd]["options"]) - set(after[cmd]["options"]))
        args_changed = before[cmd]["args"] != after[cmd]["args"]
        if plus or minus or args_changed:
            options[cmd] = {"added": plus, "removed": minus,
                            **({"args": [before[cmd]["args"], after[cmd]["args"]]} if args_changed else {})}
    old_scripts, new_scripts = old.get("scripts") or {}, new.get("scripts") or {}
    return {
        "added": added,
        "removed": removed,
        "renamed": renamed,
        "options": options,
        "scripts_added": sorted(set(new_scripts) - set(old_scripts)),
        "scripts_removed": sorted(set(old_scripts) - set(new_scripts)),
    }


def is_empty(change: dict) -> bool:
    return not any(change.get(k) for k in ("added", "removed", "renamed", "options", "scripts_added", "scripts_removed"))


def who(repo: Path, parent: str, sha: str) -> dict:
    """Authors and agents of the commits a first-parent commit brought in; actor = agent:<name> or user."""
    out = _git(repo, "log", "--no-merges", "--format=%an%x1f%(trailers:key=Co-Authored-By,valueonly,separator=%x1e)",
               f"{parent}..{sha}")
    authors, agents, hand = set(), set(), 0
    for line in out.splitlines():
        if not line.strip():
            continue
        author, _, trailers = line.partition("\x1f")
        authors.add(author)
        found = [t.split("<")[0].strip() for t in trailers.split("\x1e") if t.strip() and AGENT_RE.search(t)]
        agents.update(found)
        hand += not found
    actor = "agent:" + ",".join(sorted(agents)) if agents else "user"
    return {"authors": sorted(authors), "agents": sorted(agents), "actor": actor, "commits_without_agent": hand}


def history(repo: Path, ref: str = "main", since: Optional[str] = None, workers: int = 4,
            progress: Optional[Callable[[int, int], None]] = None, base: Optional[str] = None, *,
            project: Project) -> list[dict]:
    """One entry per first-parent commit on `ref` (after `base`, a sha or tag, or since an ISO time) that changed
    the CLI, oldest first."""
    args = ["log", "--first-parent", "--format=%H%x1f%P%x1f%cI%x1f%s", f"{base}..{ref}" if base else ref]
    if since and not base:
        args.append(f"--since={since}")
    commits = []
    for line in _git(repo, *args).splitlines():
        sha, parents, when, subject = line.split("\x1f", 3)
        if parents:
            commits.append({"sha": sha, "parent": parents.split()[0], "when": when, "subject": subject})
    commits.reverse()
    # Skip commits that touched neither project.src nor pyproject.toml: same tree before and after.
    commits = [c for c in commits if _key(repo, c["parent"], project) != _key(repo, c["sha"], project)]
    shas = list(dict.fromkeys(s for c in commits for s in (c["parent"], c["sha"])))
    trees: dict = {}
    done = [0]

    def load(sha: str) -> None:
        trees[sha] = load_tree(repo, sha, project)
        done[0] += 1
        if progress:
            progress(done[0], len(shas))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(load, shas))
    entries = []
    for c in commits:
        old, new = trees[c["parent"]], trees[c["sha"]]
        errors = {k: t["error"] for k, t in (("before", old), ("after", new)) if "error" in t}
        change = diff_trees(old, new) if not errors else {}
        if errors or not is_empty(change):
            entries.append({**c, **who(repo, c["parent"], c["sha"]), "change": change, **({"errors": errors} if errors else {})})
    return entries


def lines(entry: dict) -> list[str]:
    """The change as short lines: + added, - removed, ~ options, -> renamed."""
    change = entry.get("change") or {}
    out = [f"+ {c}" for c in change.get("added", [])]
    out += [f"- {c}" for c in change.get("removed", [])]
    out += [f"{a} -> {b.split(' ')[-1]}" for a, b in change.get("renamed", [])]
    for cmd, opt in (change.get("options") or {}).items():
        bits = ([f"added {' '.join(opt['added'])}"] if opt["added"] else []) + \
               ([f"removed {' '.join(opt['removed'])}"] if opt["removed"] else [])
        if "args" in opt:
            bits.append(f"args {' '.join(opt['args'][0]) or '()'} -> {' '.join(opt['args'][1]) or '()'}")
        out.append(f"~ {cmd}: {'; '.join(bits)}")
    out += [f"+ script {s}" for s in change.get("scripts_added", [])]
    out += [f"- script {s}" for s in change.get("scripts_removed", [])]
    out += [f"! could not load the CLI {k}: {e}" for k, e in (entry.get("errors") or {}).items()]
    return out
