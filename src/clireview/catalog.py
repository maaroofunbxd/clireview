"""A CLI's commands as a searchable catalog (`clireview find`): each command's template, help and options, ranked
against a few words, with the last time it was really run (from command logs) as a filled-in example.

Ranking (BM25F over three fields):
- a word in the command's name counts 5x, in its help 1x, in its options 0.4x; filler words (STOP) are dropped;
- rare words count more than common ones (`rollout` says more than `config`, which half the commands have), and a word
  repeated in a long help counts less than in a short one;
- a query word matches a CLI word exactly (1.0), as its start (`restar` -> restart, 0.7), as an everyday synonym
  (SYNONYMS: `rollback` -> put back, 0.5), or with one typo in a word of 5+ letters (two from 8, 0.45);
- covering more of the query words multiplies the score up; commands run often lately (14-day half-life) get a small
  boost, which mostly breaks ties.
`why` says which word matched what, for --format json and tests.
"""
from __future__ import annotations

import datetime
import math
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

# Shown in a template when a command takes them: where it runs. Every other option is summarised as [options].
PLACE_OPTIONS = {"--region": "REGION", "--site": "SITE", "--cluster": "CLUSTER"}
WORD = re.compile(r"[a-z0-9][a-z0-9_.:-]*")
FIELDS = (("name", 5.0), ("help", 1.0), ("options", 0.4))
K1, B = 1.2, 0.75  # BM25: term-frequency saturation, length normalisation
EXACT, PREFIX, SYNONYM, TYPO = 1.0, 0.7, 0.5, 0.45
HALF_LIFE_DAYS, USAGE_WEIGHT = 14.0, 0.1
STOP = {
    "a", "an", "the", "to", "of", "in", "on", "for", "and", "or", "is", "it", "i", "my", "me", "do", "does", "how",
    "can", "with", "from", "by", "at", "this", "that",
}
# Everyday word -> words the CLI's names / help use for it. Keep it short: it's for the gaps the help can't close.
SYNONYMS = {
    "rollback": ["back", "restore", "revert", "down", "undo"],
    "undo": ["revert", "back"],
    "revert": ["back", "undo"],
    "deploy": ["rollout", "image", "set"],
    "bump": ["set", "image", "tag"],
    "restart": ["rollout"],
    "pod": ["pods", "deployment"],
    "pods": ["pod", "deployment"],
    "delete": ["remove", "drop", "down"],
    "remove": ["delete", "drop"],
    "check": ["status", "health", "doctor", "diff"],
    "compare": ["diff"],
    "diff": ["compare"],
    "error": ["errors", "fail", "failed"],
    "login": ["auth"],
    "open": ["up", "start"],
    "start": ["up"],
    "close": ["down", "stop"],
    "stop": ["down"],
}


def _first_paragraph(text: str) -> str:
    return " ".join((text or "").strip().split("\n\n")[0].split())


def catalog(app: Any, name: str = "cli", place_options: dict[str, str] = PLACE_OPTIONS) -> list[dict]:
    """Every leaf command of a typer app or click group/command: path, template, help, options (with their help), args."""
    if not hasattr(app, "params"):
        import typer.main

        root_cmd = typer.main.get_command(app)
    else:
        root_cmd = app
    out: list[dict] = []

    def walk(cmd: Any, path: list[str]) -> None:
        subs = getattr(cmd, "commands", None)
        if subs:
            for sub_name, sub in subs.items():
                walk(sub, path + [sub_name])
            return
        params = getattr(cmd, "params", [])
        args = [p for p in params if p.param_type_name == "argument"]
        opts = [p for p in params if p.param_type_name == "option" and "--help" not in p.opts]
        parts = list(path)
        for a in args:
            arg_name = (a.metavar or a.name or "arg").upper()
            parts.append(arg_name if a.required else f"[{arg_name}]")
        for o in opts:
            long = next((x for x in o.opts if x.startswith("--")), o.opts[0])
            flag = min(o.opts, key=len)
            if long in place_options:
                parts.append(f"{flag} {place_options[long]}" if o.required else f"[{flag} {place_options[long]}]")
            elif o.required:
                parts.append(f"{flag} {(o.metavar or o.name or 'value').upper()}")
        if any(not o.required and next((x for x in o.opts if x.startswith("--")), "") not in place_options for o in opts):
            parts.append("[options]")
        out.append({
            "command": " ".join(path),
            "template": " ".join(parts),
            "help": _first_paragraph(cmd.help or cmd.short_help or ""),
            "doc": (cmd.help or cmd.short_help or "").strip(),
            "options": [{"flags": list(o.opts), "help": _first_paragraph(getattr(o, "help", "") or "")} for o in opts],
            "args": [a.name for a in args],
            "hidden": bool(getattr(cmd, "hidden", False)),
        })

    walk(root_cmd, [name])
    return out


def _words(text: str) -> list[str]:
    return WORD.findall(text.lower())


def _typos(a: str, b: str, limit: int) -> bool:
    """True when a and b are within `limit` edits (insert / delete / substitute / swap two neighbours)."""
    if abs(len(a) - len(b)) > limit:
        return False
    prev2, prev = None, list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            if prev2 is not None and i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        if min(cur) > limit:
            return False
        prev2, prev = prev, cur
    return prev[-1] <= limit


def _quality(q: str, w: str) -> float:
    """How well query word q matches CLI word w: exact, start of, synonym, a typo away, or 0."""
    if q == w:
        return EXACT
    if len(q) >= 2 and w.startswith(q):
        return PREFIX
    if w in SYNONYMS.get(q, ()):
        return SYNONYM
    if len(q) >= 5 and len(w) >= 5 and _typos(q, w, 2 if len(q) >= 8 else 1):
        return TYPO
    return 0.0


def _fields(entry: dict) -> dict:
    return {
        "name": _words(entry["command"].split(" ", 1)[1] if " " in entry["command"] else entry["command"]),
        "help": _words(entry["help"]),
        "options": _words(" ".join(" ".join(o["flags"]) + " " + o["help"] for o in entry["options"])),
    }


def _parse_ts(raw: Any) -> Optional[datetime.datetime]:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None


def _run_argv(r: dict) -> list[str]:
    """Tokens of a run dict from either {'argv': [...]} or {'command': '...'}."""
    if isinstance(r.get("argv"), list):
        return [str(a) for a in r["argv"]]
    cmd = r.get("command")
    if isinstance(cmd, str) and cmd.strip():
        return cmd.strip().split()
    return []


def usage(runs: list[dict], now: Optional[datetime.datetime] = None) -> dict:
    """Command path -> recency-weighted run count (a run 14 days ago counts half), from argv or command log rows."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    out: dict = {}
    for r in runs:
        raw_tokens = _run_argv(r)
        argv = [a for a in raw_tokens if not a.startswith("-")][:4]
        if not argv:
            continue
        when = _parse_ts(r.get("ts", ""))
        if when is None:
            continue
        weight = 0.5 ** (max((now - when).total_seconds(), 0) / 86400 / HALF_LIFE_DAYS)
        for n in range(1, len(argv) + 1):  # credited to the command whichever depth it is; only leaves are looked up
            key = " ".join(argv[:n])
            out[key] = out.get(key, 0.0) + weight
        if "command" in r and "argv" not in r and len(argv) > 1:
            for n in range(1, len(argv)):
                key = " ".join(argv[1 : n + 1])
                out[key] = out.get(key, 0.0) + weight
    return out


class Index:
    """BM25F over the catalog: built once per search (about 140 commands, a few ms)."""

    def __init__(self, entries: list[dict], runs: Optional[list[dict]] = None, now: Optional[datetime.datetime] = None):
        self.entries = [e for e in entries if not e.get("hidden")]
        self.docs = [_fields(e) for e in self.entries]
        n = len(self.docs) or 1
        self.avg = {f: (sum(len(d[f]) for d in self.docs) / n) or 1.0 for f, _ in FIELDS}
        df: dict = {}
        for d in self.docs:
            for w in set(d["name"]) | set(d["help"]) | set(d["options"]):
                df[w] = df.get(w, 0) + 1
        self.idf = {w: math.log(1 + (n - c + 0.5) / (c + 0.5)) for w, c in df.items()}
        self.vocab = list(df)
        self.usage = usage(runs or [], now)
        self._variants: dict = {}

    def variants(self, q: str) -> dict:
        """CLI words q can stand for, with the match quality: {word: quality}."""
        if q not in self._variants:
            self._variants[q] = {w: x for w in self.vocab if (x := _quality(q, w))}
        return self._variants[q]

    def score(self, i: int, query: list[str]) -> tuple[float, list[dict]]:
        doc, total, why = self.docs[i], 0.0, []
        for q in query:
            best, best_why = 0.0, None
            for w, quality in self.variants(q).items():
                tf = sum(weight * doc[f].count(w) / (1 - B + B * len(doc[f]) / self.avg[f]) for f, weight in FIELDS)
                if not tf:
                    continue
                s = quality * self.idf[w] * tf * (K1 + 1) / (tf + K1)
                if s > best:
                    field = next(f for f, _ in FIELDS if w in doc[f])
                    best, best_why = s, {"word": q, "matched": w, "field": field, "how": _how(quality)}
            if best_why:
                total += best
                why.append(best_why)
        return total, why

    def rank(self, query: list[str]) -> list[dict]:
        """Every command that matched a word, best first: entry + matched, score, why."""
        out = []
        for i, e in enumerate(self.entries):
            raw, why = self.score(i, query)
            if not why:
                continue
            coverage = len(why) / len(query)
            boost = 1 + USAGE_WEIGHT * math.log1p(self.used(e))
            out.append({**e, "matched": len(why), "score": round(raw * coverage ** 2 * boost, 3), "why": why})
        return sorted(out, key=lambda e: (-e["score"], e["command"]))

    def by_usage(self) -> list[dict]:
        """Every command, the most used lately first (the order to browse in with no words)."""
        return sorted(self.entries, key=lambda e: (-self.used(e), e["command"]))

    def used(self, e: dict) -> float:
        cmd = e["command"]
        sub = cmd.split(" ", 1)[1] if " " in cmd else cmd
        return max(self.usage.get(cmd, 0.0), self.usage.get(sub, 0.0))


def _how(quality: float) -> str:
    return {EXACT: "exact", PREFIX: "prefix", SYNONYM: "synonym", TYPO: "typo"}[quality]


def query_words(text: str, stop: set[str] = STOP, name: Optional[str] = None) -> list[str]:
    extra = {name.lower()} if name else set()
    return [w for w in _words(text) if w not in stop and w not in extra]


def _root_name(entries: list[dict]) -> Optional[str]:
    if not entries:
        return None
    first = entries[0].get("command", "")
    return first.split(" ", 1)[0] if " " in first else None


def find(entries: list[dict], text: str, limit: int = 10, runs: Optional[list[dict]] = None) -> dict:
    """Best matches for `text`: {"query", "complete" (the best one has every word), "results" [entry + matched, score, why]}.
    A command must match at least half the words."""
    root = _root_name(entries)
    query = query_words(text, name=root)
    if not query:
        return {"query": [], "complete": True, "results": []}
    half = max(1, (len(query) + 1) // 2)
    ranked = [e for e in Index(entries, runs).rank(query) if e["matched"] >= half][:limit]
    return {"query": query, "complete": bool(ranked) and ranked[0]["matched"] == len(query), "results": ranked}


def _matches_command(r: dict, command: str) -> bool:
    words = command.split()
    path = words[1:] if len(words) > 1 else words
    if isinstance(r.get("argv"), list):
        argv = [str(a) for a in r["argv"]]
        return bool(path) and (argv[: len(path)] == path or argv[: len(words)] == words)
    cmd = r.get("command")
    if isinstance(cmd, str) and cmd.strip():
        tokens = cmd.strip().split()
        if tokens[: len(words)] == words:
            return True
        if path and tokens[: len(path)] == path:
            return True
    return False


def last_runs(runs: list[dict], command: str) -> dict:
    """How often a command was run and the newest run: {"count", "last"}."""
    hits = [r for r in runs if _matches_command(r, command)]
    last: Optional[dict] = max(hits, key=lambda r: r.get("ts", "")) if hits else None
    return {"count": len(hits), "last": last}


def _format_run_cmd(r: dict, root: str) -> str:
    if isinstance(r.get("argv"), list):
        return f"{root} {' '.join(str(a) for a in r['argv'])}".strip()
    raw = str(r.get("command", "")).strip()
    if not raw:
        return root
    return raw if raw == root or raw.startswith(f"{root} ") else f"{root} {raw}"


def preview_text(e: dict, runs: list[dict], name: str = "cli") -> str:
    """What fzf's preview pane shows for a command: template, full help, options, why it matched, recent runs."""
    root = e["command"].split(" ", 1)[0] if name == "cli" and " " in e.get("command", "") else name
    lines = [e["template"], "", e.get("doc") or e["help"], ""]
    if e.get("why"):
        lines.append("matched: " + ", ".join(f"{w['word']} -> {w['matched']} ({w['how']}, {w['field']})" for w in e["why"]))
        lines.append("")
    if e["options"]:
        lines.append("options:")
        lines += [f"  {', '.join(o['flags']):<26} {o['help']}" for o in e["options"]]
        lines.append("")
    hits = sorted((r for r in runs if _matches_command(r, e["command"])), key=lambda r: r.get("ts", ""), reverse=True)
    if hits:
        lines.append(f"recent runs ({len(hits)} recorded):")
        for r in hits[:5]:
            ts = str(r.get("ts", ""))[:16].replace("T", " ")
            cmd_str = _format_run_cmd(r, root)
            if "actor" in r or "exit" in r:
                lines.append(f"  {ts}  {r.get('actor', '')}  exit {r.get('exit', '')}  {cmd_str}")
            else:
                lines.append(f"  {ts}  {cmd_str}".rstrip())
    else:
        lines.append("never run here (no journal record)")
    return "\n".join(lines) + "\n"


def fzf_lines(ordered: list[dict]) -> list[str]:
    """One line per command for fzf: index (hidden), template, help."""
    width = min(max((len(e["template"]) for e in ordered), default=0), 70)
    return [f"{i}\t{e['template']:<{width}}\t{e['help'][:110]}" for i, e in enumerate(ordered)]


def _numbered_pick(options: list[str], prompt: str = "command>") -> Optional[str]:
    if not options:
        return None
    print(f"\n{prompt} (fzf not installed — pick by number)")
    for idx, opt in enumerate(options[:50], start=1):
        print(f"  {idx:2}. {opt}")
    if len(options) > 50:
        print(f"  … {len(options) - 50} more (install fzf for full list)")
    try:
        raw = input("Enter number: ").strip()
        if not raw:
            return None
        num = int(raw)
        if 1 <= num <= min(50, len(options)):
            return options[num - 1]
    except (ValueError, EOFError):
        return None
    print("Invalid selection.", file=sys.stderr)
    return None


def browse_fzf(ordered: list[dict], runs: list[dict], header: str, name: str = "cli") -> Optional[dict]:
    """Interactive fzf browser over `ordered` commands with a numbered stdin fallback when fzf is missing."""
    if not ordered:
        return None
    lines = fzf_lines(ordered)
    if not shutil.which("fzf"):
        choice = _numbered_pick([line.replace("\t", "  ") for line in lines], prompt="command>")
        return ordered[int(choice.split()[0])] if choice else None
    with tempfile.TemporaryDirectory(prefix="clireview-find-") as tmp:
        for i, e in enumerate(ordered):  # previews written up front: the pane is a cat, no CLI start-up per line
            (Path(tmp) / str(i)).write_text(preview_text(e, runs, name=name))
        proc = subprocess.run(
            ["fzf", "--delimiter", "\t", "--with-nth", "2..", "--tiebreak", "index", "--layout", "reverse", "--height", "90%",
             "--prompt", "command> ", "--header", f"{header} · type to narrow · ctrl-s: fzf's order / this one · enter: show",
             "--bind", "ctrl-s:toggle-sort", "--preview", f"cat {tmp}/{{1}}", "--preview-window", "right,50%,wrap"],
            input="\n".join(lines), text=True, stdout=subprocess.PIPE,
        )
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return ordered[int(proc.stdout.split("\t", 1)[0])]
