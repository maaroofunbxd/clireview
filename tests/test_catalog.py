"""`clireview.catalog` and `clireview find`: BM25F command catalog search, templates, usage boost, and fzf browsing."""
import datetime
import json
import textwrap
from typing import Optional

import click
import pytest
import typer

from clireview import catalog, cli


def _make_typer_app() -> typer.Typer:
    app = typer.Typer(help="Sample tool.")
    k8s_app = typer.Typer(help="Cluster operations.")
    runner_app = typer.Typer(help="Pipeline runner.")
    pr_app = typer.Typer(help="PR test image lifecycle.")
    runner_app.add_typer(pr_app, name="pr-test")
    config_app = typer.Typer(help="Config management.")
    feed_app = typer.Typer(help="Catalog feed tools.")
    journal_app = typer.Typer(help="Audit journal.")
    meta_app = typer.Typer(help="CLI metadata.")

    app.add_typer(k8s_app, name="k8s")
    app.add_typer(runner_app, name="pipeline")
    app.add_typer(config_app, name="config")
    app.add_typer(feed_app, name="feed")
    app.add_typer(journal_app, name="journal")
    app.add_typer(meta_app, name="cli")

    @k8s_app.command("restart")
    def k8s_restart(
        deployment: str = typer.Argument(..., help="Deployment name."),
        region: Optional[str] = typer.Option(None, "--region", "-R", help="Target region."),
        cluster: Optional[str] = typer.Option(None, "--cluster", "-c", help="Target cluster."),
        yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
    ) -> None:
        """Rollout-restart a deployment and wait for pods."""

    @pr_app.command("down")
    def pr_down(
        region: Optional[str] = typer.Option(None, "--region", "-R", help="Target region."),
        yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
    ) -> None:
        """Put the deployment image back to the baseline tag."""

    @config_app.command("copy")
    def config_copy(
        source: str = typer.Argument(..., help="Source site."),
        dest: str = typer.Argument(..., help="Destination site."),
    ) -> None:
        """Copy service configuration between two environments."""

    @feed_app.command("upload")
    def feed_upload(
        file: str = typer.Argument(..., help="Feed file."),
        site: Optional[str] = typer.Option(None, "--site", "-s", help="Target site."),
    ) -> None:
        """Upload a catalog feed file."""

    @journal_app.command("show")
    def journal_show(
        run_id: Optional[str] = typer.Argument(None, help="Run identifier."),
        limit: int = typer.Option(10, "--limit", "-n", help="Number of entries."),
    ) -> None:
        """Show details of a recorded journal run."""

    @meta_app.command("find")
    def cli_find(
        words: Optional[list[str]] = typer.Argument(None, help="Search words."),
    ) -> None:
        """Find which command does a task."""

    return app


@pytest.fixture
def sample_app() -> typer.Typer:
    return _make_typer_app()


def test_catalog_templates_and_every_command(sample_app):
    entries = {e["command"]: e for e in catalog.catalog(sample_app, name="mytool")}
    assert "mytool cli find" in entries and "mytool journal show" in entries
    assert entries["mytool k8s restart"]["template"] == "mytool k8s restart DEPLOYMENT [-R REGION] [-c CLUSTER] [options]"
    assert entries["mytool journal show"]["template"] == "mytool journal show [RUN_ID] [options]"
    assert all(e["help"] for e in entries.values() if not e["hidden"])


def test_catalog_accepts_click_group():
    @click.group()
    def grp():
        """Root."""

    @grp.command()
    @click.argument("target")
    @click.option("--region", "-R", help="Region.")
    def deploy(target, region):
        """Deploy a service."""

    entries = {e["command"]: e for e in catalog.catalog(grp, name="ops")}
    assert entries["ops deploy"]["template"] == "ops deploy TARGET [-R REGION]"
    assert entries["ops deploy"]["help"] == "Deploy a service."


@pytest.mark.parametrize(
    "query, best",
    [
        ("restart pod", "mytool k8s restart"),  # name beats help; 'pod' via synonym
        ("rollback image", "mytool pipeline pr-test down"),  # 'rollback' -> 'back'
        ("copy config", "mytool config copy"),
        ("upload feed", "mytool feed upload"),  # any order
        ("MYTOOL Journal SHOW", "mytool journal show"),  # any case; leading root word dropped
        ("restar", "mytool k8s restart"),  # start of a word
        ("pipelne image", "mytool pipeline pr-test down"),  # a typo
        ("how do i find a command", "mytool cli find"),  # filler words dropped
    ],
)
def test_find_ranks_the_right_command_first(sample_app, query, best):
    found = catalog.find(catalog.catalog(sample_app, name="mytool"), query)
    assert found["results"][0]["command"] == best, [e["command"] for e in found["results"]]


def test_find_partial_and_none():
    entries = [
        {"command": "t a b", "template": "t a b", "help": "Alpha beta.", "options": [], "args": [], "hidden": False},
        {"command": "t c", "template": "t c", "help": "Gamma.", "options": [{"flags": ["--beta"], "help": ""}], "args": [], "hidden": False},
        {"command": "t h", "template": "t h", "help": "Alpha hidden.", "options": [], "args": [], "hidden": True},
    ]
    found = catalog.find(entries, "alpha zeta")
    assert not found["complete"] and [e["command"] for e in found["results"]] == ["t a b"]
    assert catalog.find(entries, "zeta omega")["results"] == []
    assert [e["command"] for e in catalog.find(entries, "beta")["results"]] == ["t a b", "t c"]  # name > option


def test_last_runs_matches_argv_and_habits_command():
    runs = [
        {"ts": "2026-10-10T00:00:00Z", "argv": ["k8s", "restart", "x"]},
        {"ts": "2026-10-11T00:00:00Z", "argv": ["k8s", "restart", "y"]},
        {"ts": "2026-10-12T00:00:00Z", "argv": ["k8s", "restarts"]},
    ]
    used = catalog.last_runs(runs, "mytool k8s restart")
    assert used["count"] == 2 and used["last"]["argv"][-1] == "y"

    habit_runs = [
        {"ts": "2026-10-10T00:00:00Z", "command": "mytool k8s restart api -R eu"},
        {"ts": "2026-10-11T00:00:00Z", "command": "mytool k8s restart web -R us"},
        {"ts": "2026-10-12T00:00:00Z", "command": "mytool config copy a b"},
    ]
    used_habits = catalog.last_runs(habit_runs, "mytool k8s restart")
    assert used_habits["count"] == 2 and used_habits["last"]["command"].endswith("-R us")


def _e(command, help="", options=()):
    return {
        "command": command,
        "template": command,
        "help": help,
        "doc": help,
        "options": [{"flags": [o], "help": ""} for o in options],
        "args": [],
        "hidden": False,
    }


def test_typos():
    assert catalog._typos("pipelne", "pipeline", 1)
    assert catalog._typos("restat", "restart", 1)
    assert not catalog._typos("config", "confirm", 1)
    assert catalog._quality("zeta", "beta") == 0  # no typos in short words


def test_rare_words_beat_common_ones():
    entries = [_e("t a", "common thing"), _e("t b", "common rare thing"), _e("t c", "common stuff")]
    ranked = catalog.Index(entries).rank(["common", "rare"])
    assert ranked[0]["command"] == "t b"
    assert ranked[0]["why"] == [
        {"word": "common", "matched": "common", "field": "help", "how": "exact"},
        {"word": "rare", "matched": "rare", "field": "help", "how": "exact"},
    ]


def test_usage_breaks_ties_and_orders_browsing():
    now = datetime.datetime(2026, 10, 11, tzinfo=datetime.timezone.utc)
    runs = [{"ts": "2026-10-10T00:00:00Z", "argv": ["t", "b", "-R", "x"]}] * 5 + [
        {"ts": "2025-01-01T00:00:00Z", "argv": ["t", "a"]}
    ] * 5
    entries = [_e("t a", "same words"), _e("t b", "same words")]
    index = catalog.Index(entries, runs, now=now)
    assert [e["command"] for e in index.rank(["same"])] == ["t b", "t a"]
    assert [e["command"] for e in index.by_usage()] == ["t b", "t a"]
    u = catalog.usage(runs, now)
    assert u["t b"] > 4 and u["t a"] < 0.01

    habit_runs = [{"ts": "2026-10-10T00:00:00Z", "command": "mytool sub b --flag"}] * 5 + [
        {"ts": "2025-01-01T00:00:00Z", "command": "mytool sub a"}
    ] * 5
    entries2 = [_e("mytool sub a", "same words"), _e("mytool sub b", "same words")]
    index2 = catalog.Index(entries2, habit_runs, now=now)
    assert [e["command"] for e in index2.rank(["same"])] == ["mytool sub b", "mytool sub a"]


def test_preview_fzf_lines_and_numbered_fallback(monkeypatch):
    e = {
        **_e("mytool k8s restart", "Rollout-restart a deployment.", ["--yes"]),
        "template": "mytool k8s restart DEPLOYMENT",
        "why": [{"word": "restar", "matched": "restart", "field": "name", "how": "prefix"}],
    }
    runs = [{"ts": "2026-10-11T00:00:00Z", "actor": "user", "exit": 0, "argv": ["k8s", "restart", "web"]}]
    text = catalog.preview_text(e, runs, name="mytool")
    assert "Rollout-restart" in text and "restar -> restart (prefix, name)" in text and "mytool k8s restart web" in text
    assert "never run here" in catalog.preview_text(_e("t x"), runs)
    assert catalog.fzf_lines([e])[0].split("\t")[0] == "0"

    monkeypatch.setattr("shutil.which", lambda _: None)
    monkeypatch.setattr("builtins.input", lambda _: "1")
    picked = catalog.browse_fzf([e], runs, header="ranked")
    assert picked == e


@pytest.fixture
def find_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIREVIEW_CACHE", str(tmp_path / "cache"))
    repo = tmp_path / "repo"
    pkg = repo / "src" / "samplecli"
    pkg.mkdir(parents=True)
    log = tmp_path / "commands.jsonl"
    log.write_text(
        json.dumps(
            {
                "ts": "2026-10-11T00:06:45Z",
                "invocation": "b42c87e7cbaa",
                "actor": "agent:test",
                "exit": 0,
                "argv": ["k8s", "restart", "web", "-R", "eu-west-1"],
            }
        )
        + "\n"
    )
    (repo / "pyproject.toml").write_text(
        textwrap.dedent(
            f"""
            [project.scripts]
            samplecli = "samplecli.main:app"

            [tool.clireview]
            app = "samplecli.main:app"
            src = "src/samplecli"
            name = "samplecli"

            [tool.clireview.habits]
            logs = ["{log}"]
            """
        ).strip()
        + "\n"
    )
    (pkg / "__init__.py").write_text("")
    (pkg / "main.py").write_text(
        textwrap.dedent(
            """
            import click

            @click.group()
            def app():
                \"\"\"Sample CLI.\"\"\"

            @app.group()
            def k8s():
                \"\"\"Cluster commands.\"\"\"

            @k8s.command()
            @click.argument("deployment")
            @click.option("-R", "--region", help="Target region.")
            def restart(deployment, region):
                \"\"\"Rollout-restart a deployment and wait for pods.\"\"\"

            @app.command()
            @click.argument("source")
            @click.argument("dest")
            def copy(source, dest):
                \"\"\"Copy configuration between environments.\"\"\"
            """
        ).strip()
        + "\n"
    )
    return repo


def test_clireview_find_cli_text_json_markdown_and_exit_codes(find_repo, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_tty", lambda: False)
    assert cli.main(["-C", str(find_repo), "find", "restart", "pod"]) == 0
    out = capsys.readouterr().out
    assert "samplecli k8s restart DEPLOYMENT [-R REGION]" in out
    assert "samplecli k8s restart web -R eu-west-1" in out

    assert cli.main(["-C", str(find_repo), "--format", "json", "find", "restart", "pod"]) == 0
    found = json.loads(capsys.readouterr().out)
    assert found["results"][0]["command"] == "samplecli k8s restart"
    assert found["results"][0]["runs"]["count"] == 1

    assert cli.main(["-C", str(find_repo), "find", "--format", "markdown", "restart", "pod"]) == 0
    md = capsys.readouterr().out
    assert "`samplecli k8s restart DEPLOYMENT [-R REGION]`" in md

    assert cli.main(["-C", str(find_repo), "find", "zzqx"]) == 1
    capsys.readouterr()

    assert cli.main(["-C", str(find_repo), "find"]) == 2
    capsys.readouterr()


def test_clireview_find_interactive_calls_browse_fzf(find_repo, capsys, monkeypatch):
    seen = {}

    def fake_browse(ordered, runs, header, name="cli"):
        seen.update(first=ordered[0]["command"], header=header, total=len(ordered), name=name)
        return ordered[0]

    monkeypatch.setattr(catalog, "browse_fzf", fake_browse)
    monkeypatch.setattr(cli, "_tty", lambda: True)
    assert cli.main(["-C", str(find_repo), "find", "-i", "restart", "pod"]) == 0
    out = capsys.readouterr().out
    assert seen == {
        "first": "samplecli k8s restart",
        "header": "ranked for: restart pod",
        "total": 2,
        "name": "samplecli",
    }
    assert "samplecli k8s restart DEPLOYMENT" in out
