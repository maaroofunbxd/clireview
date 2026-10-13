"""clireview.review: the weekly review of a CLI and its reviewed-* tags."""
import datetime
import subprocess

import pytest

from clireview import history as cli_history
from clireview import review as cli_review

NOW = datetime.datetime(2026, 10, 20, 9, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    for k, v in (("GIT_AUTHOR_NAME", "t"), ("GIT_COMMITTER_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@t"), ("GIT_COMMITTER_EMAIL", "t@t")):
        monkeypatch.setenv(k, v)
    path = tmp_path / "r"
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    git(path, "commit", "-q", "--allow-empty", "-m", "init")
    return path


def git(repo, *args, date="2026-10-10T10:00:00Z"):
    env = {"GIT_COMMITTER_DATE": date, "GIT_AUTHOR_DATE": date}
    import os

    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env={**os.environ, **env}).stdout


def merge(repo, branch, message, date="2026-10-15T10:00:00Z"):
    git(repo, "checkout", "-qb", branch, "main")
    git(repo, "commit", "-q", "--allow-empty", "-m", message, date=date)
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", branch, "-m", f"Merge {branch}.", date=date)


def test_no_review_yet_is_due_once_something_is_merged(repo):
    assert cli_review.last_review(repo) is None
    st = cli_review.status(repo, NOW)
    assert st["due"] and st["last"] is None and st["merges"] == 1


def test_a_review_tag_resets_the_clock_and_merges_start_after_it(repo):
    git(repo, "tag", "-a", "reviewed-2026-10-10", "HEAD", "-m", "kept all", date="2026-10-10T10:00:00Z")
    last = cli_review.last_review(repo)
    assert last["tag"] == "reviewed-2026-10-10" and last["note"] == "kept all"
    assert last["sha"] == git(repo, "rev-parse", "HEAD").strip()
    assert not cli_review.status(repo, NOW)["due"]  # nothing merged since

    merge(repo, "a", "a thing\n\nRequested-by: work-3bba (work)\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>")
    merge(repo, "b", "b thing")
    st = cli_review.status(repo, NOW)
    assert st["due"] and st["days"] == 9 and st["merges"] == 2
    assert not cli_review.status(repo, datetime.datetime(2026, 10, 15, tzinfo=datetime.timezone.utc))["due"]  # 5 days

    merges = cli_review.merges(repo, base=last["sha"])
    assert [m["subject"] for m in merges] == ["Merge b.", "Merge a."]
    assert merges[1]["requested_by"] == ["work-3bba (work)"] and merges[1]["actor"] == "agent:Claude Opus 5.5"
    assert merges[0]["requested_by"] == [] and merges[0]["actor"] == "user"


def test_tag_name_counts_up_on_the_same_day_and_tag_review_writes_the_note(repo):
    day = datetime.date(2026, 10, 20)
    assert cli_review.tag_name(repo, day) == "reviewed-2026-10-20"
    cli_review.tag_review(repo, "reviewed-2026-10-20", git(repo, "rev-parse", "HEAD").strip(), "reverted x", push=False)
    assert cli_review.tag_name(repo, day) == "reviewed-2026-10-20-2"
    assert cli_review.last_review(repo)["note"] == "reverted x"


def test_breaking_and_added_from_a_net_change():
    def cmd(options=(), args=(), help=""):
        return {"options": sorted(["--help", *options]), "args": list(args), "help": help}

    old = {"commands": {"mytool a": cmd(["--x"]), "mytool pvc cp": cmd(help="copy"), "mytool gone": cmd(help="gone")}, "scripts": {"old": "m"}}
    new = {"commands": {"mytool a": cmd(["--y"]), "mytool k8s pvc cp": cmd(help="copy"), "mytool new": cmd(help="new")}, "scripts": {}}
    change = cli_history.diff_trees(old, new)
    assert cli_review.breaking(change) == ["- mytool gone", "mytool pvc cp -> mytool k8s pvc cp", "~ mytool a: removed --x",
                                           "- script old"]
    assert cli_review.added(change) == ["+ mytool new", "~ mytool a: added --y"]
