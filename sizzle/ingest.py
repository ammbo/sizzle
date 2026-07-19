"""Ingest (PRD §7.1): clone the repo, parse README + manifests, mine commit history for the build's arc."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

MANIFEST_FILES = [
    "package.json",
    "pyproject.toml",
    "Cargo.toml",
    "go.mod",
    "requirements.txt",
    "Gemfile",
]

README_NAMES = ["README.md", "README.rst", "README.txt", "README", "readme.md"]

MAX_README_CHARS = 12_000
MAX_COMMITS = 80


@dataclass
class RepoContext:
    repo_url: str
    name: str
    readme: str = ""
    manifests: dict[str, str] = field(default_factory=dict)
    commit_log: list[str] = field(default_factory=list)
    local_path: Path | None = None
    is_private: bool = False

    def as_prompt_block(self) -> str:
        parts = [f"# Repository: {self.name}\nURL: {self.repo_url}"]
        if self.readme:
            parts.append(f"## README\n{self.readme[:MAX_README_CHARS]}")
        for fname, content in self.manifests.items():
            parts.append(f"## {fname}\n{content[:2000]}")
        if self.commit_log:
            parts.append("## Commit history (oldest first — this is the build's arc)\n" + "\n".join(self.commit_log))
        return "\n\n".join(parts)


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


def ingest(repo_url: str, work_dir: Path, *, github_token: str | None = None) -> RepoContext:
    """Clone (or reuse) the repo and extract narrative raw material."""
    name = repo_url.rstrip("/").removesuffix(".git").split("/")[-1]
    dest = work_dir / "repo" / name

    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        if github_token:
            from .github_auth import clone_with_token

            clone_with_token(repo_url, dest, github_token, clone_args=["--filter=blob:limit=200k"])
        else:
            subprocess.run(
                ["git", "clone", "--filter=blob:limit=200k", repo_url, str(dest)],
                capture_output=True, text=True, check=True,
            )

    ctx = RepoContext(repo_url=repo_url, name=name, local_path=dest, is_private=bool(github_token))

    for rname in README_NAMES:
        p = dest / rname
        if p.exists():
            ctx.readme = p.read_text(errors="replace")
            break

    for fname in MANIFEST_FILES:
        p = dest / fname
        if p.exists():
            content = p.read_text(errors="replace")
            if fname == "package.json":
                try:
                    data = json.loads(content)
                    content = json.dumps(
                        {k: data.get(k) for k in ("name", "description", "scripts", "dependencies") if k in data},
                        indent=2,
                    )
                except json.JSONDecodeError:
                    pass
            ctx.manifests[fname] = content

    log = _git(["log", "--reverse", "--format=%ad %s", "--date=short", f"-{MAX_COMMITS}"], dest)
    ctx.commit_log = [line for line in log.splitlines() if line.strip()]

    return ctx
