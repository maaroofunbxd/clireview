# clireview

What changed in a [typer](https://typer.tiangolo.com) or [click](https://click.palletsprojects.com) CLI, merge by merge, read from git, and a weekly review of
those changes marked by `reviewed-*` tags. For a maintainer whose CLI also gets changes from other people or AI
agents: it reads the code at each commit, not the commit messages, so it sees every added, removed or renamed
command and option, whoever wrote it.

A command, `clireview`, for a check in CI and a look back; and a library for a tool that builds its own commands
on top (for example `mytool cli changes` and `mytool cli review`).

## The command

In the repo of the CLI, say which app to read in `pyproject.toml`:

```toml
[tool.clireview]
app = "mytool.cli:app"   # a typer app or a click command/group, "module:attr"
src = "src/mytool"       # the package folder, loaded at each commit
# name = "mytool"        # default: the console script that runs app's module
# gh_repo = "owner/mytool"

[tool.clireview.env]     # set while an old commit's CLI is imported
MYTOOL_NO_SIDE_EFFECTS = "1"
```

```bash
uvx --from "git+https://github.com/maaroofunbxd/clireview" clireview check origin/main   # this branch against main
clireview changes --since "4 weeks ago"   # every merge that changed the CLI, and who made it
clireview status                          # is a review due (reviewed-* tags)
clireview habits                          # repeated shell from your JSONL logs (see below)
```

`check` prints what the branch changes in the CLI (from its merge base with main) and exits 1 when something can
break a caller: a removed or renamed command, a removed option, changed arguments, a removed console script.
`--allow-breaking` reports and exits 0; `--format markdown` or `--format json` for a PR comment or a script
(`--format` goes before the command). Exit 2: git failed or the CLI did not import.

### `habits` — repeated commands (v0.3.0)

Read-only report of command lines that show up often in **your** logs or transcript scan paths. No product
CLI is built in: point at neutral JSONL from an export script in the consumer repo.

```toml
[tool.clireview]
name = "mytool"
gh_repo = "owner/mytool"   # optional: match open issues

[tool.clireview.habits]
logs = ["~/.local/mytool/commands.jsonl"]
scan = ["~/.cursor/projects/**/*.jsonl"]   # optional; conservative line extraction
since = "14d"
min_count = 3              # “repeated” threshold
long_chars = 160           # “long” normalized command
long_argv = 14             # … or this many argv tokens (either triggers long)
issue_labels = ["command-idea"]
ignore = ["^agents ", "^clireview "]
```

Each log line is JSON: `{"command": "…", "ts": "2026-10-01T12:00:00Z"}` (`ts` optional). Commands are
normalized (whitespace collapsed; long numbers and hex redacted unless `--no-redact`) before counting.
**Long** means the habit is a heavy one-liner or many tokens — worth a subcommand or script. **Other**
means raw shell outside `{name}` — worth a wrapper or doc. One-offs below `min_count` are omitted.
Exit **0** when the report prints; **2** when config or inputs are unusable. Prints `gh issue` examples
only; it does not call `gh`.

```bash
clireview habits --since 14d --format json
```

### In CI

```yaml
# .github/workflows/cli-check.yml
on: pull_request
jobs:
  cli:
    runs-on: ubuntu-latest
    permissions: { pull-requests: write }
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      - uses: astral-sh/setup-uv@v6
      - run: uv sync
      - name: CLI changes
        env:
          GH_TOKEN: ${{ github.token }}
          ALLOW: ${{ contains(github.event.pull_request.labels.*.name, 'breaking-ok') && '--allow-breaking' || '' }}
        run: |
          code=0
          uv run --with "clireview @ git+https://github.com/maaroofunbxd/clireview" \
            clireview --format markdown check origin/${{ github.base_ref }} $ALLOW > cli.md || code=$?
          gh pr comment ${{ github.event.number }} --edit-last --body-file cli.md || \
            gh pr comment ${{ github.event.number }} --body-file cli.md
          exit $code
```

The CLI's own dependencies must be installed (`uv sync`), since each commit's code is imported.

## Install as a library

```bash
uv add "clireview @ git+https://github.com/maaroofunbxd/clireview@v0.3.0"
```

Python 3.9 or newer; no dependencies. The CLI being read needs its own dependencies (typer or click) in the Python
that runs clireview.

## Use

```python
from datetime import date
from pathlib import Path
from clireview import Project, history, review

cli = Project(
    app="mytool.cli:app",          # the typer app, "module:attr"
    name="mytool",                 # the first word of every command path
    src="src/mytool",              # the package folder in the repo, loaded at each commit
    repo=Path("~/mytool").expanduser(),
    env={"MYTOOL_NO_SIDE_EFFECTS": "1"},  # set while an old commit's CLI is imported
    gh_repo="owner/mytool",        # for review.open_issues
)

# Every merge on main since a date that changed the CLI, oldest first.
for entry in history.history(cli.repo, "main", since="2026-10-01", project=cli):
    print(entry["sha"][:8], entry["actor"], entry["subject"])
    for line in history.lines(entry):
        print("   ", line)

# The weekly review: due?, what changed since the last reviewed-* tag, then tag it.
st = review.status(cli.repo)                       # {due, last, days, merges}
start = review.start_of(cli.repo, base=st["last"]["sha"] if st["last"] else None)
change = review.net_change(cli.repo, start, project=cli)
print(review.breaking(change), review.added(change))
review.tag_review(cli.repo, review.tag_name(cli.repo, date.today()), "HEAD", "note", push=False)
```

### history.py

- `load_tree(repo, sha, project)`: the command tree at a commit: `git archive` of `project.src` into a temp dir,
  imported by a fresh Python, every command walked (options, arguments, first help line), plus the console scripts
  from `pyproject.toml`. Cached under `project.cache_dir` by the hash of `src` and `pyproject.toml`.
- `diff_trees(old, new)`: commands added / removed / renamed (same parameters and help under a new name), options
  per command, console scripts.
- `history(repo, ref, since=, base=, project=)`: one entry per first-parent commit that changed the CLI, with its
  authors and `actor`: `agent:<name>` when a commit carries a `Co-Authored-By:` trailer naming an AI agent, else
  `user`.
- `lines(entry)`: the change as short lines (`+ added`, `- removed`, `a -> b`, `~ options`).

### review.py

A review is an annotated tag `reviewed-YYYY-MM-DD` on the commit of main that was reviewed; its message is the note.

- `last_review(repo)`, `status(repo, every_days=7)`: when the last one was, and whether one is due (that old and
  something merged since).
- `merges(repo, base=)`: first-parent commits since, with `actor` and `Requested-by:` trailers.
- `start_of`, `net_change`, `breaking`, `added`: the CLI at the start against now, and what in it can break a caller.
- `tag_name`, `tag_review`: the next free tag name for a day; create and push it.
- `open_issues(gh_repo, labels)`: open GitHub issues with those labels (needs `gh`).

### habits.py

- `load_habits_config(repo)`, `run(project, config)`: read JSONL `logs` and optional `scan` globs, normalize,
  classify repeats vs `{name}`, match `open_issues` titles, render text/json/markdown via the CLI.

## Develop

```bash
uv sync
uv run ruff check src tests
uv run pytest -q
```
