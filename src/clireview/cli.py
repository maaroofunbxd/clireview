"""The clireview command: the library's history, check and review status for the CLI of the repo it runs in.

The CLI to read comes from [tool.clireview] in that repo's pyproject.toml (app, name, src; optional gh_repo and an
[tool.clireview.env] table), or from --app / --name / --src;
CLIREVIEW_CACHE moves the tree cache. `clireview check` is meant for CI: it prints what a
branch changes in the CLI and exits 1 when something in it can break a caller.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

from clireview import Project, __version__, catalog, habits, history, review
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


def _tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _load_live_catalog(project: Project) -> list[dict]:
    repo = (project.repo or Path(".")).resolve()
    root = str((repo / project.path_root).resolve())
    added = False
    if root not in sys.path:
        sys.path.insert(0, root)
        added = True
    top_pkg = project.module.split(".", 1)[0]
    existing = sys.modules.get(top_pkg)
    if existing is not None:
        mod_file = getattr(existing, "__file__", None) or ""
        if mod_file and not str(Path(mod_file).resolve()).startswith(root):
            for k in [m for m in sys.modules if m == top_pkg or m.startswith(top_pkg + ".")]:
                sys.modules.pop(k, None)
    old_env = {k: os.environ.get(k) for k in project.env}
    os.environ.update(project.env)
    try:
        mod = importlib.import_module(project.module)
        app = getattr(mod, project.attr)
        return catalog.catalog(app, name=project.name)
    finally:
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if added and root in sys.path:
            sys.path.remove(root)


def _load_runs(repo: Optional[Path]) -> list[dict]:
    if not repo:
        return []
    config = habits.load_habits_config(repo)
    runs: list[dict] = []
    for path in habits._expand_patterns(config.logs):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and (obj.get("argv") or obj.get("command")):
                runs.append(obj)
    return runs


def _render_find_entry(e: dict, runs: list[dict], *, name: str, detail: bool, missing: int = 0) -> str:
    tag = f" (misses {missing} word{'s' if missing > 1 else ''})" if missing else ""
    lines = [f"{e['template']}{tag}", f"    {e['help']}"]
    if detail and e["options"]:
        for o in e["options"]:
            lines.append(f"      {', '.join(o['flags']):<28} {o['help'][:110]}")
    used = e.get("runs") or catalog.last_runs(runs, e["command"])
    if used["last"]:
        last = used["last"]
        ts = str(last.get("ts", ""))[:16].replace("T", " ")
        cmd_str = catalog._format_run_cmd(last, name)
        meta = f" ({used['count']} run(s) recorded)"
        if last.get("invocation"):
            meta = f" (run {str(last['invocation'])[:6]}, {used['count']} run(s) recorded)"
        if "actor" in last or "exit" in last:
            lines.append(f"    last run {ts} by {last.get('actor', '')}, exit {last.get('exit', '')}: {cmd_str} {meta}")
        else:
            lines.append(f"    last run {ts}: {cmd_str}{meta}")
    return "\n".join(lines) + "\n\n"


def _render_find_markdown(found: dict, text: str, *, name: str) -> str:
    if not found["results"]:
        return f"No command matches `{text}`."
    out = []
    if not found["complete"]:
        out.append(f"_No command has every word ({', '.join(found['query'])}); closest:_")
        out.append("")
    for i, e in enumerate(found["results"]):
        missing = len(found["query"]) - e["matched"]
        tag = f" _(misses {missing} word{'s' if missing > 1 else ''})_" if missing else ""
        out.append(f"- `{e['template']}`{tag} — {e['help']}")
        if i == 0 and e["options"]:
            for o in e["options"]:
                out.append(f"  - `{', '.join(o['flags'])}`: {o['help'][:110]}")
        used = e.get("runs") or {"count": 0, "last": None}
        if used["last"]:
            last = used["last"]
            cmd_str = catalog._format_run_cmd(last, name)
            out.append(f"  - last run `{ str(last.get('ts', ''))[:16].replace('T', ' ') }`: `{cmd_str}` ({used['count']} recorded)")
    return "\n".join(out)


def cmd_find(args, project: Project) -> int:
    fmt = getattr(args, "sub_format", None) or ("json" if getattr(args, "json", False) else args.format)
    try:
        entries = _load_live_catalog(project)
    except Exception as e:
        print(f"clireview find: could not load {project.app}: {e}", file=sys.stderr)
        return 2
    runs = _load_runs(project.repo)
    words = args.words or []
    text = " ".join(words)
    if args.interactive or not words:
        if fmt != "text" or not _tty():
            print("clireview find: give a few words, or run in a terminal to browse (-i)", file=sys.stderr)
            return 2
        index = catalog.Index(entries, runs)
        query = catalog.query_words(text, name=project.name)
        ranked = index.rank(query) if query else []
        seen = {e["command"] for e in ranked}
        ordered = ranked + [e for e in index.by_usage() if e["command"] not in seen]
        header = f"ranked for: {' '.join(query)}" if query else "most used lately first"
        picked = catalog.browse_fzf(ordered, runs, header=header, name=project.name)
        if not picked:
            return 1
        sys.stdout.write(_render_find_entry(picked, runs, name=project.name, detail=True))
        return 0
    found = catalog.find(entries, text, limit=args.limit, runs=runs)
    for e in found["results"]:
        e.update(runs=catalog.last_runs(runs, e["command"]))
    if fmt == "json":
        print(json.dumps(found))
    elif fmt == "markdown":
        print(_render_find_markdown(found, text, name=project.name))
    elif not found["results"]:
        print(f"No command matches {text!r}. Try other words, or `clireview find` to browse.")
    else:
        if not found["complete"]:
            print(f"No command has every word ({', '.join(found['query'])}); closest:")
        for i, e in enumerate(found["results"]):
            missing = len(found["query"]) - e["matched"]
            sys.stdout.write(_render_find_entry(e, runs, name=project.name, detail=i == 0, missing=missing))
        print(f"clireview find -i WORDS to browse these in fzf; {project.name} <command> --help for every option; --format json for all fields")
    return 0 if found["results"] else 1


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
    p = sub.add_parser("find", help="which command does X: ranked templates, options, and last run")
    p.add_argument("words", nargs="*", help="what you want to do, in your words")
    p.add_argument("-n", "--limit", type=int, default=8, help="best N commands (default: 8)")
    p.add_argument("-i", "--interactive", action="store_true", help="browse in fzf, ranked for WORDS")
    p.add_argument("--format", dest="sub_format", choices=("text", "markdown", "json"), help="output format")
    p.add_argument("--json", action="store_true", help="shorthand for --format json")
    args = parser.parse_args(argv)
    project = load_project(args.repo, args.app, args.name, args.src)
    try:
        return {
            "check": cmd_check,
            "changes": cmd_changes,
            "status": cmd_status,
            "habits": cmd_habits,
            "find": cmd_find,
        }[args.cmd](args, project)
    except GitError as e:
        print(f"clireview: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
