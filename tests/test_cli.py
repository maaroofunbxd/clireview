"""clireview.cli and click CLIs: the command reads [tool.clireview] and checks a branch against main."""
import json
import subprocess
import textwrap

import pytest

from clireview import cli, history


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _write(repo, body):
    pkg = repo / "src" / "clicktool"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "main.py").write_text("import click\n" + textwrap.dedent(body))


START = '''
    @click.group()
    def main():
        """Clicktool."""

    @main.command()
    @click.option("--x", is_flag=True)
    def one(x):
        """One."""

    @main.group()
    def sub():
        """Sub."""

    @sub.command()
    @click.argument("path")
    def two(path):
        """Two."""
'''


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIREVIEW_CACHE", str(tmp_path / "cache"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "dev")
    _git(repo, "config", "user.email", "dev@example.com")
    (repo / "pyproject.toml").write_text(
        '[project.scripts]\nclicktool = "clicktool.main:main"\n\n'
        '[tool.clireview]\napp = "clicktool.main:main"\nsrc = "src/clicktool"\n\n'
        '[tool.clireview.env]\nCLICKTOOL_OFFLINE = "1"\n')
    _write(repo, START)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "start")
    return repo


def test_load_project_from_pyproject(repo):
    project = cli.load_project(repo)
    assert (project.app, project.src, project.name) == ("clicktool.main:main", "src/clicktool", "clicktool")
    assert project.env == {"CLICKTOOL_OFFLINE": "1"}
    with pytest.raises(SystemExit):
        cli.load_project(repo / "nowhere")


def test_click_tree(repo, tmp_path):
    project = cli.load_project(repo)
    tree = history.load_tree(repo, "HEAD", project)
    assert sorted(tree["commands"]) == ["clicktool one", "clicktool sub two"]
    assert tree["commands"]["clicktool sub two"]["args"] == ["path"]


def test_check_breaking_and_added(repo, capsys):
    _git(repo, "checkout", "-qb", "feat")
    _write(repo, START.replace('@click.option("--x", is_flag=True)', '@click.option("--y", is_flag=True)')
           + '\n    @main.command()\n    def three():\n        """Three."""\n')
    _git(repo, "commit", "-qam", "change")
    assert cli.main(["-C", str(repo), "check", "main"]) == 1
    out = capsys.readouterr().out
    assert "~ clicktool one: removed --x   (breaking)" in out and "+ clicktool three" in out
    assert cli.main(["-C", str(repo), "check", "main", "--allow-breaking"]) == 0
    capsys.readouterr()
    assert cli.main(["-C", str(repo), "--format", "json", "check", "main"]) == 1
    assert json.loads(capsys.readouterr().out)["change"]["added"] == ["clicktool three"]
    cli.main(["-C", str(repo), "--format", "markdown", "check", "main", "--allow-breaking"])
    assert "(breaking)" in capsys.readouterr().out


def test_check_nothing_changed(repo, capsys):
    assert cli.main(["-C", str(repo), "check", "main"]) == 0
    assert "no CLI changes" in capsys.readouterr().out
