"""clireview habits: JSONL logs, normalization, issue matching (no network)."""
import json
import textwrap

import pytest

from clireview import cli, habits


@pytest.fixture
def habit_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIREVIEW_CACHE", str(tmp_path / "cache"))
    repo = tmp_path / "repo"
    repo.mkdir()
    log = tmp_path / "commands.jsonl"
    lines = [
        {"command": "curl https://example.com/api", "ts": "2026-10-20T10:00:00Z"},
        {"command": "curl https://example.com/api", "ts": "2026-10-21T10:00:00Z"},
        {"command": "curl https://example.com/api", "ts": "2026-10-22T10:00:00Z"},
        {"command": "clicktool one --x", "ts": "2026-10-22T10:00:00Z"},
        {"command": "clicktool one --x", "ts": "2026-10-22T11:00:00Z"},
        {"command": "clicktool one --x", "ts": "2026-10-22T12:00:00Z"},
        {"command": "clireview check main", "ts": "2026-10-22T12:00:00Z"},
    ]
    long_cmd = "clicktool " + " ".join(f"arg{i}" for i in range(12))
    for _ in range(3):
        lines.append({"command": long_cmd, "ts": "2026-10-22T12:00:00Z"})
    log.write_text("\n".join(json.dumps(o) for o in lines) + "\n")
    (repo / "pyproject.toml").write_text(
        textwrap.dedent(
            f"""
            [project.scripts]
            clicktool = "clicktool.main:main"

            [tool.clireview]
            app = "clicktool.main:main"
            src = "src/clicktool"
            name = "clicktool"
            gh_repo = "owner/clicktool"

            [tool.clireview.habits]
            logs = ["{log}"]
            min_count = 3
            long_argv = 8
            """
        ).strip()
        + "\n"
    )
    (repo / "src" / "clicktool").mkdir(parents=True)
    (repo / "src" / "clicktool" / "__init__.py").write_text("")
    (repo / "src" / "clicktool" / "main.py").write_text("def main(): pass\n")
    return repo


def test_normalize_redacts_numbers_and_hex():
    assert habits.normalize("run 12345 abcdef0123456789abcdef0123456789") == "run # #"


def test_count_and_classify_other(monkeypatch, habit_repo):
    monkeypatch.setattr(
        "clireview.review.open_issues",
        lambda gh, labels: [{"number": 9, "title": "curl example API", "url": "http://x/9"}],
    )
    project = cli.load_project(habit_repo)
    config = habits.load_habits_config(habit_repo)
    code, report = habits.run(project, config)
    assert code == 0
    curl = next(r for r in report["repeated_other"] if r["command"].startswith("curl"))
    assert curl["count"] == 3 and "has_issue" in curl["tags"]
    assert report["summary"]["files_read"] == 1
    assert not any(r["command"].startswith("clireview") for r in report["repeated_other"])


def test_cli_habits_text_and_json(monkeypatch, habit_repo, capsys):
    monkeypatch.setattr(
        "clireview.review.open_issues",
        lambda gh, labels: [],
    )
    assert cli.main(["-C", str(habit_repo), "habits"]) == 0
    out = capsys.readouterr().out
    assert "Repeated (other)" in out and "Summary" in out
    capsys.readouterr()
    assert cli.main(["-C", str(habit_repo), "--format", "json", "habits"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert "summary" in data and data["summary"]["name"] == "clicktool"


def test_scan_extracts_command_lines(tmp_path):
    scan = tmp_path / "t.jsonl"
    scan.write_text('{"text": "make test"}\n# comment\nnot a command\n')
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        '[tool.clireview]\napp = "x:a"\nsrc = "src/x"\n\n'
        f'[tool.clireview.habits]\nscan = ["{scan}"]\nmin_count = 1\n'
    )
    n, cmds, paths = habits.read_scan_files([str(scan)], ())
    assert n == 1 and paths == [str(scan)]
    assert cmds == ["make test"] * 1 or "make test" in cmds


def test_missing_config_exits_2(habit_repo, capsys):
    (habit_repo / "pyproject.toml").write_text(
        '[tool.clireview]\napp = "clicktool.main:main"\nsrc = "src/clicktool"\n'
    )
    assert cli.main(["-C", str(habit_repo), "habits"]) == 2
    assert "logs or scan" in capsys.readouterr().err
