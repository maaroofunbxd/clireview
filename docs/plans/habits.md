# `clireview habits` — repeated commands, without product coupling

Hand-off for a **generic** read-only report: what command lines keep showing up in logs and transcripts,
which are long, which already have an open GitHub issue, and which look like one-offs.

**Constraint (decided):** clireview must not import or name any product CLI (no Unbxd, no journal layout baked in).
Consumers point at **their own files** in `pyproject.toml`. Optional export adapters live **outside** this repo.

## Goal

Help a maintainer trim `command-idea` noise and spot:

- repeated **raw** shell (not the packaged CLI),
- repeated **long** invocations of the packaged CLI (escape hatches used like a habit),
- gaps with **no** matching open issue,
- duplicates (**issue already exists** → comment, don’t re-file).

Not a scheduler, not auto-filing issues, not priority/focus (that stays elsewhere).

## Command

```bash
clireview habits [--since 14d] [--format text|json|markdown] [-C REPO]
```

Runs in the **consumer repo** (same as `check` / `changes`): reads `[tool.clireview]` + optional
`[tool.clireview.habits]`. Exit **0** always if the report printed; **2** if config or inputs are unusable.

## Inputs (config only — no hardcoded paths)

```toml
[tool.clireview]
name = "mytool"          # first word of “this CLI” for classification
gh_repo = "owner/mytool" # optional: match open issues

[tool.clireview.habits]
# One or more JSONL files; each line is one executed command (see schema below).
logs = ["~/.local/mytool/commands.jsonl", "/var/log/agent-runs/*.jsonl"]

# Optional: text/jsonl files to scan for command-like lines (see extraction).
scan = ["~/.cursor/projects/**/*.jsonl"]

since = "14d"            # ignore older lines when ts is present
min_count = 3            # “repeated” threshold
long_chars = 160         # “long” when normalized length >= this
long_argv = 14           # … or argv token count >= this (either triggers long)
issue_labels = ["command-idea"]   # gh issue list --label (default command-idea)
ignore = ["^agents ", "^clireview "]   # regexes dropped before counting
```

### Neutral JSONL schema (v1)

Each log line is JSON with:

| Field | Required | Meaning |
|-------|----------|---------|
| `command` | yes | Full command string as run |
| `ts` | no | ISO-8601; if missing, line counts in full file |
| `exit` | no | If present and non-zero, optional `--failed` section later (v2) |

No other fields required. **Consumers** map their own journals into this shape (export script, not clireview).

### Transcript `scan` extraction (v1, conservative)

- Read files matching `scan` globs (cap total bytes per run, e.g. 50 MB).
- Consider a line a command candidate if it matches **generic** patterns only, e.g.:
  - starts with `#` → skip,
  - matches `^(sudo )?[a-zA-Z0-9_./-]+(\s|$)` and length ≤ 500,
  - or JSON object with string field `command` / `text` containing the same.
- Do **not** parse vendor-specific chat schemas in v1 beyond those two rules.

## Normalization (for counting repeats)

Before counting:

1. Strip leading/trailing whitespace.
2. Collapse internal whitespace to single spaces.
3. Optional: replace `\d{4,}` with `#` and long hex/sha with `#` (flag `--no-redact` for debug).

Same normalized string → same bucket.

## Classification

For each bucket with `count >= min_count`:

| Tag | Rule |
|-----|------|
| `repeat` | default |
| `long` | also `long_chars` or `long_argv` |
| `cli` | normalized command starts with `{name} ` |
| `other` | not `cli` |
| `has_issue` | fuzzy match against open `issue_labels` titles (case-insensitive substring or token overlap ≥ 2) |
| `no_issue` | repeated, no match |

**Recommendations (printed, not enforced):**

| Situation | Suggestion line |
|-----------|-----------------|
| `other` + `repeat` | “Often run outside `{name}` — new subcommand, wrapper script, or doc example?” |
| `cli` + `long` + `repeat` | “Often long `{name}` — narrow command, task, or script in repo?” |
| `repeat` once below `min_count` | omitted (one-offs ignored) |
| `has_issue` | “Open issue #N — comment +1 if still happening” |
| `no_issue` + `repeat` | “No open issue — consider filing (template below)” |

Print `gh issue comment` / `gh issue create` **examples** with `-R {gh_repo}`; never call `gh` unless `--open` added in v2.

## Output sections (text)

1. **Summary** — files read, lines in window, bucket count.
2. **Repeated (other)** — top N by count.
3. **Repeated long ({name})** — top N.
4. **Already tracked** — repeat + matched issue.
5. **Not tracked** — repeat + no issue (action list for triage).

`--format json` mirrors the same structure for scripts.

## Module layout

| File | Role |
|------|------|
| `src/clireview/habits.py` | load config, read logs/scan, normalize, classify, render |
| `src/clireview/cli.py` | `habits` subcommand |
| `tests/test_habits.py` | fixtures: tiny jsonl + fake issues function |

Reuse `Project` / `load_project()` from `cli.py`; extend `_table()` parsing for `[tool.clireview.habits]` list keys (or accept TOML via tomllib on 3.11+ and keep regex fallback on 3.9 for strings only).

`review.open_issues` stays the GitHub reader; pass `issue_labels` from habits config.

## Explicit non-goals

- No Unbxd / `unbxd` / region / site / journal path defaults in clireview.
- No `unbxd cli mine` duplicate in another package name.
- No LLM summarization in v1.
- No automatic issue or comment creation in v1.
- No ranking “what to do today” (Slack List / digest is separate).

## Adopters (outside this repo)

Any CLI repo adds `[tool.clireview.habits.logs]` pointing at **their** export. Example: a one-line export from
a personal journal into neutral JSONL — that script lives in the **consumer** repo or private dotfiles, not here.

## Done when

- [ ] `clireview habits` with `--format text|json|markdown` and tests (no network; monkeypatch `open_issues`).
- [ ] README section: config table, JSONL schema, interpretation of `long` vs one-off.
- [ ] Tag **v0.3.0** (minor: new subcommand).
- [ ] Consumer pins tag when ready (separate PR in that repo); update unbxdtools TODO to point at clireview, not a new `unbxd` command.

## Build order

1. `habits.py` + config parsing + jsonl reader + normalize + count.
2. CLI subcommand + text output.
3. `open_issues` matching + markdown/json format.
4. Optional `scan` glob reader (can ship in same PR if small).
5. README + release tag.

## Start here

```bash
cd ~/clireview
# branch: habits
# first commit: habits.py skeleton + test jsonl fixture + habits subcommand stub
```

Board hand-off (consumer repo optional):

```text
agents handoff "plan: clireview docs/plans/habits.md; branch habits; no unbxd in tree"
```
