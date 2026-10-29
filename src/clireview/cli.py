"""The clireview command: the library's history, check and review status for the CLI of the repo it runs in.

The CLI to read comes from [tool.clireview] in that repo's pyproject.toml (app, name, src; optional gh_repo and an
[tool.clireview.env] table), or from --app / --name / --src;
CLIREVIEW_CACHE moves the tree cache. `clireview check` is meant for CI: it prints what a
branch changes in the CLI and exits 1 when something in it can break a caller.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

from clireview import Project, __version__, habits, history, review
from clireview.history import PYPROJECT, GitError, _git


def _table(pyproject: str, name: str) -> dict:
    """The string values of one TOML table (no tomllib: Python 3.9)."""
    match = re.search(rf"^\[{re.escape(name)}\]\s*$(.*?)(?=^\[|\Z)", pyproject, re.M | re.S)
    if not match:
        return {}
    return dict(re.findall(r'^\s*([\w.-]+)\s*=\s*["\']([^"\']*)["\']', match.group(1), re.M))


def load_project(repo: Path, app: Optional[str] = None, name: Optional[str] = None, src: Optional[str] = None) -> Project:
    """The Project from [tool.clireview], with any of app / name / src given here taking its place."""
    try:
        text = (repo / PYPROJECT).read_text()
    except OSError:
        text = ""
    conf = _table(text, "tool.clireview")
    app, src = app or conf.get("app"), src or conf.get("src")
    if not app or not src:
        raise SystemExit("clireview: no CLI to read: set app and src in [tool.clireview] of pyproject.toml, "
                         "or pass --app and --src")
    # The command's name: given, configured, else the console script that runs the app's module, else the repo.
    module = app.split(":", 1)[0]
    script = next((s for s, target in history.parse_scripts(text).items() if target.split(":")[0] == module), None)
    cache = os.environ.get("CLIREVIEW_CACHE")
    return Project(**({"cache_dir": Path(cache)} if cache else {}), app=app, name=name or conf.get("name") or script or repo.resolve().name, src=src, repo=repo,
                   env=_table(text, "tool.clireview.env"), gh_repo=conf.get("gh_repo", ""))


def _markdown(head: str, base: str, change: dict) -> str:
    bad, new = review.breaking(change), review.added(change)
    if not bad and not new:
        return f"**CLI changes** `{base[:8]}..{head[:8]}`: none."
    out = [f"**CLI changes** `{base[:8]}..{head[:8]}`", ""]
    out += [f"- ⚠️ `{line}` (breaking)" for line in bad]
    out += [f"- `{line}`" for line in new]
    return "\n".join(out)


def cmd_check(args, project: Project) -> int:
    head = _git(project.repo, "rev-parse", args.head).strip()
    base = _git(project.repo, "merge-base", args.base, head).strip()
    change = review.net_change(project.repo, base, head, project=project)
    if change.get("errors"):
        for k, e in change["errors"].items():
            print(f"! could not load the CLI {k}: {e}", file=sys.stderr)
        return 2
    bad = review.breaking(change)
    if args.format == "json":
        print(json.dumps({"base": base, "head": head, "change": change, "breaking": bad, "added": review.added(change)}))
    elif args.format == "markdown":
        print(_markdown(head, base, change))
    else:
        for line in bad:
            print(f"{line}   (breaking)")
        for line in review.added(change):
            print(line)
        if not bad and not review.added(change):
            print("no CLI changes")
    return 1 if bad and not args.allow_breaking else 0


def cmd_changes(args, project: Project) -> int:
    entries = history.history(project.repo, args.ref, since=args.since, base=args.base, project=project)
    if args.format == "json":
        print(json.dumps(entries))
        return 0
    for entry in entries:
        print(f"{entry['sha'][:8]} {entry['when'][:10]} {entry['actor']}  {entry['subject']}")
        for line in history.lines(entry):
            print(f"    {line}")
    return 0


def cmd_habits(args, project: Project) -> int:
    config = habits.load_habits_config(project.repo)
    code, payload = habits.run(project, config, since=args.since, redact=not args.no_redact)
    if code != 0:
        print(payload, file=sys.stderr)
        return code
    report = payload
    if args.format == "json":
        print(json.dumps(report))
    elif args.format == "markdown":
        print(habits.render_markdown(report))
    else:
        sys.stdout.write(habits.render_text(report))
    return 0


def cmd_status(args, project: Project) -> int:
    st = review.status(project.repo, every_days=args.every, ref=args.ref)
    if args.format == "json":
        print(json.dumps(st))
        return 0
    last = f"{st['last']['tag']} ({st['days']} days ago)" if st["last"] else "none"
    print(f"last review: {last}; merges since: {st['merges']}; due: {'yes' if st['due'] else 'no'}")
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="clireview", description="What changed in a typer or click CLI, read from git.")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("-C", "--repo", type=Path, default=Path("."), help="the git checkout (default: here)")
    parser.add_argument("--app", help="the typer app or click command, module:attr")
    parser.add_argument("--name", help="the command's name, first word of every command path")
    parser.add_argument("--src", help="the package folder in the repo, e.g. src/mytool")
    parser.add_argument("--format", choices=("text", "markdown", "json"), default="text")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check", help="what a branch changes in the CLI; exit 1 if it can break a caller")
    p.add_argument("base", nargs="?", default="origin/main", help="compared from its merge base with head (origin/main)")
    p.add_argument("head", nargs="?", default="HEAD")
    p.add_argument("--allow-breaking", action="store_true", help="report breaking changes but exit 0")
    p = sub.add_parser("changes", help="every merge that changed the CLI, oldest first")
    p.add_argument("--ref", default="main")
    p.add_argument("--since", help="an ISO date or git time, e.g. 2026-10-01 or '2 weeks ago'")
    p.add_argument("--base", help="after this sha or tag instead")
    p = sub.add_parser("status", help="whether a review is due (reviewed-* tags)")
    p.add_argument("--ref", default="main")
    p.add_argument("--every", type=int, default=review.EVERY_DAYS, help="days between reviews")
    p = sub.add_parser("habits", help="repeated command lines from configured JSONL logs and optional scan")
    p.add_argument("--since", help="ignore older log lines when ts is present (default from config, e.g. 14d)")
    p.add_argument("--no-redact", action="store_true", help="do not replace long numbers or hex in normalization")
    args = parser.parse_args(argv)
    project = load_project(args.repo, args.app, args.name, args.src)
    try:
        return {"check": cmd_check, "changes": cmd_changes, "status": cmd_status, "habits": cmd_habits}[args.cmd](
            args, project
        )
    except GitError as e:
        print(f"clireview: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
