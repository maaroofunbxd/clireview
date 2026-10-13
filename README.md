# clireview

What changed in a [typer](https://typer.tiangolo.com) CLI, merge by merge, read from git, and a weekly review of
those changes marked by `reviewed-*` tags. For a maintainer whose CLI also gets changes from other people or AI
agents: it reads the code at each commit, not the commit messages, so it sees every added, removed or renamed
command and option, whoever wrote it.

A library, no command of its own: the tool that uses it makes a `Project` and builds its own commands on top
(unbxdtools: `unbxd cli changes`, `unbxd cli review`).

## Install

```bash
uv add "clireview @ git+https://github.com/maaroofunbxd/clireview@v0.1.0"
```

Python 3.9 or newer; no dependencies. The CLI being read needs typer in the Python that runs clireview.

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

## Develop

```bash
uv sync
uv run ruff check src tests
uv run pytest -q
```
