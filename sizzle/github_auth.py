"""GitHub App authentication: detect private repos, generate installation tokens,
provide git credential helpers. Never places tokens in URLs or logs."""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from base64 import b64decode
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import httpx
import jwt

GITHUB_API = "https://api.github.com"


class GitHubAuthError(RuntimeError):
    """Raised when GitHub App auth fails for any recoverable reason."""


class AppNotInstalledError(GitHubAuthError):
    """The GitHub App is not installed on the target repository."""


class InsufficientPermissionsError(GitHubAuthError):
    """The installation exists but lacks access to the specific repository."""


@dataclass
class InstallationToken:
    token: str
    expires_at: str  # ISO 8601
    permissions: dict[str, str]


def _load_private_key() -> str:
    raw = os.environ.get("GITHUB_APP_PRIVATE_KEY", "")
    if raw:
        return raw
    b64 = os.environ.get("GITHUB_APP_PRIVATE_KEY_B64", "")
    if b64:
        return b64decode(b64).decode()
    raise GitHubAuthError(
        "GITHUB_APP_PRIVATE_KEY or GITHUB_APP_PRIVATE_KEY_B64 must be set"
    )


def _app_id() -> int:
    val = os.environ.get("GITHUB_APP_ID", "")
    if not val:
        raise GitHubAuthError("GITHUB_APP_ID must be set")
    return int(val)


def _generate_jwt() -> str:
    """Create a short-lived JWT (10 min max per GitHub) signed with the App's private key."""
    now = int(time.time())
    payload = {
        "iat": now - 60,
        "exp": now + (10 * 60),
        "iss": _app_id(),
    }
    return jwt.encode(payload, _load_private_key(), algorithm="RS256")


def _github_headers(token: str, *, is_jwt: bool = False) -> dict[str, str]:
    prefix = "Bearer" if is_jwt else "token"
    return {
        "Authorization": f"{prefix} {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def is_repo_accessible(repo_url: str) -> bool:
    """Check if a repo is publicly cloneable without credentials.

    Uses git ls-remote which is cheaper than a full clone attempt.
    """
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--exit-code", "--quiet", repo_url],
            capture_output=True,
            timeout=15,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def parse_owner_repo(repo_url: str) -> tuple[str, str]:
    """Extract (owner, repo) from a GitHub URL.

    Raises ValueError for non-GitHub URLs.
    """
    parsed = urlparse(repo_url.rstrip("/"))
    if "github.com" not in (parsed.hostname or ""):
        raise ValueError(f"Not a GitHub URL: {repo_url}")
    parts = parsed.path.strip("/").removesuffix(".git").split("/")
    if len(parts) < 2:
        raise ValueError(f"Cannot parse owner/repo from: {repo_url}")
    return parts[0], parts[1]


def find_installation(owner: str, repo: str) -> int:
    """Find the installation ID for the Sizzle GitHub App on owner/repo.

    Raises AppNotInstalledError if the app is not installed.
    """
    app_jwt = _generate_jwt()

    with httpx.Client(timeout=15) as client:
        resp = client.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/installation",
            headers=_github_headers(app_jwt, is_jwt=True),
        )
        if resp.status_code == 200:
            return resp.json()["id"]
        if resp.status_code == 404:
            raise AppNotInstalledError(
                f"The Sizzle GitHub App is not installed on {owner}/{repo}. "
                f"Install it at https://github.com/apps/sizzle-video-ai/installations/new"
            )
        resp.raise_for_status()
    raise GitHubAuthError(f"Unexpected response: {resp.status_code}")


def create_installation_token(
    installation_id: int, owner: str, repo: str
) -> InstallationToken:
    """Generate a short-lived (1 hour) installation access token scoped to contents:read.

    The token is scoped to the specific repository, not the entire installation.
    """
    app_jwt = _generate_jwt()

    with httpx.Client(timeout=15) as client:
        resp = client.post(
            f"{GITHUB_API}/app/installations/{installation_id}/access_tokens",
            headers=_github_headers(app_jwt, is_jwt=True),
            json={
                "repositories": [repo],
                "permissions": {"contents": "read"},
            },
        )
        if resp.status_code == 201:
            data = resp.json()
            return InstallationToken(
                token=data["token"],
                expires_at=data["expires_at"],
                permissions=data["permissions"],
            )
        if resp.status_code == 422:
            raise InsufficientPermissionsError(
                f"The Sizzle GitHub App installation does not have access to {owner}/{repo}. "
                f"Update the installation to include this repository."
            )
        resp.raise_for_status()
    raise GitHubAuthError(f"Token creation failed: {resp.status_code}")


def clone_with_token(
    repo_url: str,
    dest: Path,
    token: str,
    *,
    clone_args: list[str] | None = None,
) -> None:
    """Clone a repo using a token via GIT_ASKPASS, never placing the token in the URL.

    Creates a temporary script that echoes the token when git asks for a password.
    The script is deleted after the clone completes or fails.
    """
    if clone_args is None:
        clone_args = ["--filter=blob:limit=200k"]

    dest.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".sh", delete=False, prefix="sizzle_askpass_"
    ) as f:
        f.write("#!/bin/sh\necho \"$SIZZLE_GIT_TOKEN\"\n")
        askpass_path = f.name

    try:
        os.chmod(askpass_path, 0o700)

        parsed = urlparse(repo_url)
        authed_url = urlunparse(
            parsed._replace(netloc=f"x-access-token@{parsed.hostname}")
        )

        env = {
            **os.environ,
            "GIT_ASKPASS": askpass_path,
            "GIT_TERMINAL_PROMPT": "0",
            "SIZZLE_GIT_TOKEN": token,
        }

        result = subprocess.run(
            ["git", "clone", *clone_args, authed_url, str(dest)],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        if result.returncode != 0:
            stderr = result.stderr.replace(token, "[REDACTED]")
            raise GitHubAuthError(f"git clone failed: {stderr[:500]}")
    finally:
        try:
            os.unlink(askpass_path)
        except OSError:
            pass
