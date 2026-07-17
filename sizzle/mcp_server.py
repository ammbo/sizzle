"""MCP interface (PRD §11): a single tool, `make_demo_video(repo_url, app_url)`."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .config import Config
from .pipeline import make_demo_video as _make

mcp = FastMCP("sizzle")


@mcp.tool()
def make_demo_video(repo_url: str, app_url: str | None = None, dry_run: bool = False) -> dict:
    """Produce a <=3-minute demo video for a repo. Returns the shot manifest,
    including the final cut path and the cost report."""
    cfg = Config(dry_run=dry_run)
    manifest = _make(cfg, repo_url, app_url)
    return manifest.model_dump()


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
