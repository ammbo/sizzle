"""CLI: `sizzle make <repo_url> [--app-url ...]`. Fire-and-forget is the product."""

from __future__ import annotations

from pathlib import Path

import typer

from .config import Config
from .pipeline import make_demo_video

app = typer.Typer(add_completion=False, no_args_is_help=True)


@app.callback()
def _root():
    """sizzle: repo in, demo video out."""


@app.command()
def make(
    repo_url: str = typer.Argument(..., help="Git repo URL to make a demo video for"),
    app_url: str = typer.Option(None, "--app-url", help="Live app URL for the capture agent"),
    duration: int = typer.Option(180, "--duration", help="Target duration in seconds"),
    work_dir: Path = typer.Option(Path("runs"), "--work-dir"),
    dry_run: bool = typer.Option(False, "--dry-run", help="No model calls; stub every lane"),
):
    """Emit a <=180s demo video, a shot manifest, and a cost report. Zero human input."""
    cfg = Config(work_dir=work_dir, target_duration_s=duration, dry_run=dry_run)
    if not dry_run and not cfg.api_key:
        typer.echo("DASHSCOPE_API_KEY is not set (use --dry-run to test the pipeline offline)")
        raise typer.Exit(1)
    manifest = make_demo_video(cfg, repo_url, app_url)
    typer.echo(manifest.final_cut)


if __name__ == "__main__":
    app()
