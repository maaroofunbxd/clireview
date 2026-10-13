"""clireview.history: command trees read from git, compared merge by merge."""
import subprocess
import textwrap

import pytest

from clireview import Project
from clireview import history as cli_history


def _tree(commands, scripts=None):
    return {"commands": commands, "scripts": scripts or {}}


def _cmd(options=(), args=(), help=""):
    return {"options": sorted(["--help", *options]), "args": list(args), "help": help}


def test_parse_scripts():
    text = '[project]\nname = "x"\n\n[project.scripts]\nmytool = "a.b:main"\n# c\nother = "k.g:main"\n\n[tool.x]\ny = "z"\n'
    assert cli_history.parse_scripts(text) == {"mytool": "a.b:main", "other": "k.g:main"}
    assert cli_history.parse_scripts("[project]\n") == {}


def test_diff_added_removed_options_scripts():
    old = _tree({"mytool a": _cmd(["--x"]), "mytool b": _cmd()}, {"mytool": "m"})
    new = _tree({"mytool a": _cmd(["--y"]), "mytool c": _cmd(["--z"], help="new")}, {"mytool": "m", "other": "g"})
    change = cli_history.diff_trees(old, new)
    assert change["added"] == ["mytool c"] and change["removed"] == ["mytool b"] and change["renamed"] == []
    assert change["options"] == {"mytool a": {"added": ["--y"], "removed": ["--x"]}}
    assert change["scripts_added"] == ["other"] and change["scripts_removed"] == []
    assert cli_history.lines({"change": change}) == [
        "+ mytool c", "- mytool b", "~ mytool a: added --y; removed --x", "+ script other"]


def test_diff_rename_needs_same_params_and_help():
    old = _tree({"mytool j list": _cmd(["--since"], help="Every change"), "mytool j x": _cmd(help="X")})
    new = _tree({"mytool j changes": _cmd(["--since"], help="Every change"), "mytool j y": _cmd(help="Y")})
    change = cli_history.diff_trees(old, new)
    assert change["renamed"] == [["mytool j list", "mytool j changes"]]
    assert change["added"] == ["mytool j y"] and change["removed"] == ["mytool j x"]
    assert "mytool j list -> changes" in cli_history.lines({"change": change})


def test_unchanged_is_empty():
    tree = _tree({"mytool a": _cmd(["--x"])})
    assert cli_history.is_empty(cli_history.diff_trees(tree, tree))


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _write_cli(repo, body):
    pkg = repo / "src" / "mytool_pkg" / "cli"
    pkg.mkdir(parents=True, exist_ok=True)
    (repo / "src" / "mytool_pkg" / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text("")
    (pkg / "main.py").write_text("import typer\napp = typer.Typer()\n" + textwrap.dedent(body))


@pytest.fixture
def project(tmp_path):
    return Project(app="mytool_pkg.cli.main:app", name="mytool", src="src/mytool_pkg", cache_dir=tmp_path / "cache")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "dev")
    _git(repo, "config", "user.email", "dev@example.com")
    (repo / "pyproject.toml").write_text('[project.scripts]\nmytool = "mytool_pkg.cli.main:main"\n')
    _write_cli(repo, '''
        @app.command("one")
        def one(x: bool = typer.Option(False, "--x")) -> None:
            """One."""

        @app.command("two")
        def two() -> None:
            """Two."""
    ''')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "start")
    return repo


def test_history_from_a_real_repo(repo, project):
    # An agent's branch: rename one -> uno (same params, same help), add --y to two; merged with --no-ff.
    _git(repo, "checkout", "-qb", "feat")
    _write_cli(repo, '''
        @app.command("uno")
        def one(x: bool = typer.Option(False, "--x")) -> None:
            """One."""

        @app.command("two")
        def two(y: bool = typer.Option(False, "--y")) -> None:
            """Two."""
    ''')
    _git(repo, "commit", "-qam", "rename\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "feat", "-m", "Merge feat")
    # By hand, straight on main: a docs-only commit (skipped) and a new command with no agent trailer.
    (repo / "README").write_text("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "docs")
    (repo / "src" / "mytool_pkg" / "cli" / "main.py").write_text(
        (repo / "src" / "mytool_pkg" / "cli" / "main.py").read_text() + '\n@app.command("three")\ndef three() -> None:\n    """Three."""\n')
    _git(repo, "commit", "-qam", "three by hand")

    entries = cli_history.history(repo, ref="main", project=project)
    assert [e["subject"] for e in entries] == ["Merge feat", "three by hand"]
    merge, hand = entries
    assert merge["actor"] == "agent:Claude Opus 5.5" and merge["authors"] == ["dev"]
    assert merge["change"]["renamed"] == [["mytool one", "mytool uno"]]
    assert merge["change"]["options"] == {"mytool two": {"added": ["--y"], "removed": []}}
    assert hand["actor"] == "user" and hand["change"]["added"] == ["mytool three"]
    assert list(project.cache_dir.glob("*.json"))  # trees are cached


def test_history_reports_a_cli_that_does_not_import(repo, project):
    (repo / "src" / "mytool_pkg" / "cli" / "main.py").write_text("raise RuntimeError('broken on purpose')\n")
    _git(repo, "commit", "-qam", "break it")
    [entry] = cli_history.history(repo, ref="main", project=project)
    assert "broken on purpose" in entry["errors"]["after"]
    assert any(line.startswith("! could not load") for line in cli_history.lines(entry))
