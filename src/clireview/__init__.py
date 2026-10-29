"""What changed in a typer or click CLI, merge by merge, read from git (history.py), and the weekly review of it with
reviewed-* tags (review.py). Generic: the CLI to read comes in as a Project, made by the tool that uses it.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

__version__ = "0.3.0"


@dataclass(frozen=True)
class Project:
    app: str  # the typer app or click command, as "package.module:attr" (mytool.cli:app)
    name: str  # the command, the first word of every command path (mytool)
    src: str  # the package's folder in the repo, the part a commit's CLI is loaded from (src/mytool)
    repo: Optional[Path] = None  # the git checkout read when none is given
    cache_dir: Path = Path(os.path.expanduser("~/.cache/clireview/cli-tree"))  # trees, by the hash of src + pyproject.toml
    env: dict = field(default_factory=dict)  # extra variables while a commit's CLI is imported (turn side effects off)
    gh_repo: str = ""  # owner/name on GitHub: where review.open_issues looks

    @property
    def module(self) -> str:
        return self.app.split(":", 1)[0]

    @property
    def attr(self) -> str:
        return self.app.split(":", 1)[1] if ":" in self.app else "app"

    @property
    def path_root(self) -> str:
        """What goes first on sys.path in an extracted commit: the folder holding the top package (src)."""
        return str(Path(self.src).parent)
