"""Repeated command lines from neutral JSONL logs and optional transcript scan — no product coupling."""
from __future__ import annotations

import datetime
import glob
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Union

from clireview import Project
from clireview import review as cli_review
from clireview.history import PYPROJECT

SCAN_BYTE_CAP = 50 * 1024 * 1024
TOP_N = 20

_COMMAND_LINE = re.compile(r"^(sudo )?[a-zA-Z0-9_./-]+(\s|$)")
_HEX_OR_SHA = re.compile(r"\b[0-9a-f]{16,}\b", re.I)
_LONG_DIGITS = re.compile(r"\d{4,}")


@dataclass
class HabitsConfig:
    logs: list[str] = field(default_factory=list)
    scan: list[str] = field(default_factory=list)
    since: str = "14d"
    min_count: int = 3
    long_chars: int = 160
    long_argv: int = 14
    issue_labels: tuple[str, ...] = ("command-idea",)
    ignore: tuple[str, ...] = ("^agents ", "^clireview ")


def _habits_table(pyproject: str) -> dict[str, str]:
    match = re.search(r"^\[tool\.clireview\.habits\]\s*$(.*?)(?=^\[|\Z)", pyproject, re.M | re.S)
    if not match:
        return {}
    block = match.group(1)
    out = dict(re.findall(r'^\s*([\w.-]+)\s*=\s*["\']([^"\']*)["\']', block, re.M))
    for key, body in re.findall(r"^\s*([\w.-]+)\s*=\s*\[(.*?)\]", block, re.M | re.S):
        out[key] = json.dumps(re.findall(r'["\']([^"\']*)["\']', body))
    for key, val in re.findall(r"^\s*([\w.-]+)\s*=\s*(\d+)\s*$", block, re.M):
        out[key] = val
    return out


def load_habits_config(repo: Path) -> HabitsConfig:
    try:
        text = (repo / PYPROJECT).read_text()
    except OSError:
        text = ""
    raw = _habits_table(text)
    logs = json.loads(raw["logs"]) if raw.get("logs") else []
    scan = json.loads(raw["scan"]) if raw.get("scan") else []
    labels = json.loads(raw["issue_labels"]) if raw.get("issue_labels") else list(HabitsConfig.issue_labels)
    ignore = json.loads(raw["ignore"]) if raw.get("ignore") else list(HabitsConfig.ignore)
    return HabitsConfig(
        logs=logs,
        scan=scan,
        since=raw.get("since", "14d"),
        min_count=int(raw.get("min_count", 3)),
        long_chars=int(raw.get("long_chars", 160)),
        long_argv=int(raw.get("long_argv", 14)),
        issue_labels=tuple(labels),
        ignore=tuple(ignore),
    )


def parse_since(spec: str, now: Optional[datetime.datetime] = None) -> Optional[datetime.datetime]:
    """ISO date/time or a git-style relative window like 14d / 2w."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    m = re.fullmatch(r"(\d+)([dhw])", spec.strip())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"d": datetime.timedelta(days=n), "h": datetime.timedelta(hours=n), "w": datetime.timedelta(weeks=n)}[unit]
        return now - delta
    try:
        dt = datetime.datetime.fromisoformat(spec.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None


def normalize(command: str, *, redact: bool = True) -> str:
    s = " ".join(command.strip().split())
    if not redact:
        return s
    s = _HEX_OR_SHA.sub("#", s)
    s = _LONG_DIGITS.sub("#", s)
    return s


def _expand_patterns(patterns: list[str]) -> list[Path]:
    out: list[Path] = []
    seen: set[str] = set()
    for pat in patterns:
        pat = os.path.expanduser(pat)
        if any(c in pat for c in "*?[]"):
            for p in sorted(glob.glob(pat, recursive=True)):
                if p not in seen and Path(p).is_file():
                    seen.add(p)
                    out.append(Path(p))
        else:
            p = Path(pat)
            if p.is_file() and str(p) not in seen:
                seen.add(str(p))
                out.append(p)
    return out


def _ignored(command: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(p, command) for p in patterns)


def _parse_ts(raw: object) -> Optional[datetime.datetime]:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None


def read_log_file(path: Path, since: Optional[datetime.datetime], ignore: tuple[str, ...]) -> tuple[int, list[str]]:
    lines_in_window = 0
    commands: list[str] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0, []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        cmd = obj.get("command")
        if not isinstance(cmd, str) or not cmd.strip():
            continue
        ts = _parse_ts(obj.get("ts"))
        if since and ts and ts < since:
            continue
        if since and ts:
            lines_in_window += 1
        elif not since or not ts:
            lines_in_window += 1
        if _ignored(cmd, ignore):
            continue
        commands.append(cmd)
    return lines_in_window, commands


def _command_from_scan_line(line: str) -> Optional[str]:
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    if len(s) > 500:
        return None
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            for key in ("command", "text"):
                val = obj.get(key)
                if isinstance(val, str) and val.strip():
                    s = val.strip()
                    break
    except json.JSONDecodeError:
        pass
    if _COMMAND_LINE.match(s):
        return s
    return None


def read_scan_files(patterns: list[str], ignore: tuple[str, ...]) -> tuple[int, list[str], list[str]]:
    files = _expand_patterns(patterns)
    commands: list[str] = []
    read_paths: list[str] = []
    total = 0
    for path in files:
        if total >= SCAN_BYTE_CAP:
            break
        try:
            data = path.read_bytes()
        except OSError:
            continue
        chunk = data[: max(0, SCAN_BYTE_CAP - total)]
        total += len(chunk)
        read_paths.append(str(path))
        for line in chunk.decode("utf-8", errors="replace").splitlines():
            cmd = _command_from_scan_line(line)
            if cmd and not _ignored(cmd, ignore):
                commands.append(cmd)
    return len(read_paths), commands, read_paths


def count_commands(commands: list[str], *, redact: bool) -> dict[str, int]:
    buckets: dict[str, int] = {}
    for cmd in commands:
        key = normalize(cmd, redact=redact)
        buckets[key] = buckets.get(key, 0) + 1
    return buckets


def _argv_len(normalized: str) -> int:
    return len(normalized.split())


def matches_issue(command: str, title: str) -> bool:
    cmd_l, title_l = command.lower(), title.lower()
    if cmd_l in title_l or title_l in cmd_l:
        return True
    cmd_t = set(re.findall(r"\w+", cmd_l))
    title_t = set(re.findall(r"\w+", title_l))
    return len(cmd_t & title_t) >= 2


def find_issue(command: str, issues: list[dict]) -> Optional[dict]:
    for issue in issues:
        if matches_issue(command, issue["title"]):
            return issue
    return None


def classify(
    normalized: str,
    count: int,
    *,
    name: str,
    min_count: int,
    long_chars: int,
    long_argv: int,
    issue: Optional[dict],
) -> Optional[dict]:
    if count < min_count:
        return None
    is_cli = normalized.startswith(f"{name} ")
    is_long = len(normalized) >= long_chars or _argv_len(normalized) >= long_argv
    tags = ["repeat"]
    if is_long:
        tags.append("long")
    tags.append("cli" if is_cli else "other")
    tags.append("has_issue" if issue else "no_issue")
    suggestion = _suggestion(normalized, name, is_cli, is_long, issue)
    entry = {"command": normalized, "count": count, "tags": tags, "suggestion": suggestion}
    if issue:
        entry["issue"] = {"number": issue["number"], "url": issue["url"], "title": issue["title"]}
    return entry


def _suggestion(normalized: str, name: str, is_cli: bool, is_long: bool, issue: Optional[dict]) -> str:
    if issue:
        return f"Open issue #{issue['number']} — comment +1 if still happening"
    if is_cli and is_long:
        return f"Often long `{name}` — narrow command, task, or script in repo?"
    if not is_cli:
        return f"Often run outside `{name}` — new subcommand, wrapper script, or doc example?"
    return "No open issue — consider filing (template below)"


def _gh_examples(gh_repo: str, issue: Optional[dict], normalized: str) -> list[str]:
    if not gh_repo:
        return []
    out: list[str] = []
    if issue:
        out.append(f'gh issue comment {issue["number"]} -R {gh_repo} --body "Still seeing this: `{normalized}`"')
    else:
        out.append(
            f'gh issue create -R {gh_repo} --label command-idea --title "command-idea: habit" '
            f'--body "Repeated command:\\n\\n`{normalized}`"'
        )
    return out


def build_report(
    project: Project,
    config: HabitsConfig,
    *,
    since_override: Optional[str] = None,
    redact: bool = True,
    open_issues_fn: Optional[Callable[..., list[dict]]] = None,
) -> dict:
    since_spec = since_override or config.since
    since = parse_since(since_spec)
    files_read = 0
    lines_in_window = 0
    all_commands: list[str] = []
    log_paths = _expand_patterns(config.logs)
    for path in log_paths:
        files_read += 1
        n, cmds = read_log_file(path, since, config.ignore)
        lines_in_window += n
        all_commands.extend(cmds)
    if config.scan:
        n_files, scan_cmds, _ = read_scan_files(config.scan, config.ignore)
        files_read += n_files
        lines_in_window += len(scan_cmds)
        all_commands.extend(scan_cmds)
    buckets = count_commands(all_commands, redact=redact)
    issues: list[dict] = []
    if project.gh_repo:
        fetch = open_issues_fn or cli_review.open_issues
        try:
            issues = fetch(project.gh_repo, config.issue_labels)
        except RuntimeError as e:
            return {"error": str(e)}
    classified: list[dict] = []
    for norm, count in buckets.items():
        issue = find_issue(norm, issues) if issues else None
        row = classify(
            norm,
            count,
            name=project.name,
            min_count=config.min_count,
            long_chars=config.long_chars,
            long_argv=config.long_argv,
            issue=issue,
        )
        if row:
            row["gh_examples"] = _gh_examples(project.gh_repo, issue if issue else None, norm)
            classified.append(row)
    classified.sort(key=lambda r: (-r["count"], r["command"]))
    repeated_other = [r for r in classified if "other" in r["tags"] and "long" not in r["tags"]][:TOP_N]
    repeated_long_cli = [r for r in classified if "cli" in r["tags"] and "long" in r["tags"]][:TOP_N]
    already_tracked = [r for r in classified if "has_issue" in r["tags"]][:TOP_N]
    not_tracked = [r for r in classified if "no_issue" in r["tags"]][:TOP_N]
    return {
        "summary": {
            "files_read": files_read,
            "lines_in_window": lines_in_window,
            "bucket_count": len(buckets),
            "since": since_spec,
            "name": project.name,
            "gh_repo": project.gh_repo,
        },
        "repeated_other": repeated_other,
        "repeated_long_cli": repeated_long_cli,
        "already_tracked": already_tracked,
        "not_tracked": not_tracked,
    }


def render_text(report: dict) -> str:
    if report.get("error"):
        return report["error"]
    s = report["summary"]
    lines = [
        "Summary",
        f"  files read: {s['files_read']}",
        f"  lines in window: {s['lines_in_window']}",
        f"  buckets: {s['bucket_count']} (since {s['since']})",
        "",
    ]
    name = s["name"]

    def section(title: str, rows: list[dict]) -> None:
        lines.append(title)
        if not rows:
            lines.append("  (none)")
            lines.append("")
            return
        for row in rows:
            lines.append(f"  [{row['count']}] {row['command']}")
            lines.append(f"      {row['suggestion']}")
            for ex in row.get("gh_examples") or []:
                lines.append(f"      e.g. {ex}")
        lines.append("")

    section("Repeated (other)", report["repeated_other"])
    section(f"Repeated long ({name})", report["repeated_long_cli"])
    section("Already tracked", report["already_tracked"])
    section("Not tracked", report["not_tracked"])
    return "\n".join(lines).rstrip() + "\n"


def render_markdown(report: dict) -> str:
    if report.get("error"):
        return f"**Error:** {report['error']}\n"
    s = report["summary"]
    out = [
        "## Habits summary",
        "",
        f"- Files read: {s['files_read']}",
        f"- Lines in window: {s['lines_in_window']}",
        f"- Buckets: {s['bucket_count']} (since `{s['since']}`)",
        "",
    ]
    name = s["name"]

    def section(title: str, rows: list[dict]) -> None:
        out.append(f"### {title}")
        out.append("")
        if not rows:
            out.append("_None._")
            out.append("")
            return
        for row in rows:
            out.append(f"- **({row['count']})** `{row['command']}` — {row['suggestion']}")
        out.append("")

    section("Repeated (other)", report["repeated_other"])
    section(f"Repeated long ({name})", report["repeated_long_cli"])
    section("Already tracked", report["already_tracked"])
    section("Not tracked", report["not_tracked"])
    return "\n".join(out)


def run(
    project: Project,
    config: HabitsConfig,
    *,
    since: Optional[str] = None,
    redact: bool = True,
    open_issues_fn: Optional[Callable[..., list[dict]]] = None,
) -> tuple[int, Union[dict, str]]:
    if not config.logs and not config.scan:
        return 2, "clireview habits: set logs or scan in [tool.clireview.habits]"
    report = build_report(project, config, since_override=since, redact=redact, open_issues_fn=open_issues_fn)
    if report.get("error"):
        return 2, f"clireview habits: {report['error']}"
    return 0, report
