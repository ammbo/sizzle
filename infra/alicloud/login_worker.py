"""Login worker: interactive browser session for authenticated app capture.

Launches Chromium in a virtual framebuffer (Xvfb), exposes it via x11vnc + websockify
for noVNC live-view, waits for the user to complete login, then captures and encrypts
the Playwright storageState.

FC function spec: CPU 1, Memory 2GB, Timeout 600s.
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import oss2
from oss2.credentials import EnvironmentVariableCredentialsProvider
from playwright.sync_api import sync_playwright

from sizzle.browser_auth import encrypt_state


def _bucket():
    auth = oss2.ProviderAuthV4(EnvironmentVariableCredentialsProvider())
    return oss2.Bucket(
        auth,
        os.environ["OSS_ENDPOINT"],
        os.environ["OSS_BUCKET"],
        region=os.environ.get("ALIBABA_CLOUD_REGION", "ap-southeast-1"),
    )


def _start_xvfb(display: str = ":99", resolution: str = "1280x720x24") -> subprocess.Popen:
    """Start Xvfb on the given display."""
    return subprocess.Popen(
        ["Xvfb", display, "-screen", "0", resolution, "-ac"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _start_x11vnc(display: str = ":99", vnc_port: int = 5999) -> tuple[subprocess.Popen, str]:
    """Start x11vnc with a one-time password. Returns (process, password)."""
    password = secrets.token_urlsafe(12)
    proc = subprocess.Popen(
        [
            "x11vnc",
            "-display", display,
            "-rfbport", str(vnc_port),
            "-passwd", password,
            "-shared",
            "-forever",
            "-noxdamage",
            "-nopw",  # don't prompt for password file
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc, password


def _start_websockify(
    listen_port: int = 6080, vnc_port: int = 5999
) -> subprocess.Popen:
    """Start websockify to bridge WebSocket connections to the VNC server."""
    return subprocess.Popen(
        [
            "websockify",
            "--web", "/usr/share/novnc",
            str(listen_port),
            f"localhost:{vnc_port}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _cleanup(*procs: subprocess.Popen) -> None:
    """Kill all child processes."""
    for proc in procs:
        try:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except ProcessLookupError:
                pass


def handler(event: bytes, context) -> str:
    """FC function handler for interactive login sessions.

    Payload: {"run_id": str, "app_url": str, "session_id": str}

    Flow:
    1. Start Xvfb, x11vnc, websockify
    2. Launch Chromium via Playwright (headful, DISPLAY=:99)
    3. Navigate to app_url
    4. Return VNC connection details
    5. Poll OSS for completion signal
    6. Capture storageState, encrypt, store to OSS
    7. Cleanup
    """
    payload = json.loads(event)
    run_id = payload["run_id"]
    app_url = payload["app_url"]
    session_id = payload["session_id"]
    ttl_seconds = int(os.environ.get("BROWSER_AUTH_TTL", "3600"))

    display = ":99"
    vnc_port = 5999
    ws_port = 6080

    xvfb_proc = _start_xvfb(display)
    time.sleep(1)  # let Xvfb initialize

    os.environ["DISPLAY"] = display

    x11vnc_proc, vnc_password = _start_x11vnc(display, vnc_port)
    time.sleep(0.5)

    ws_proc = _start_websockify(ws_port, vnc_port)
    time.sleep(0.5)

    # Store session metadata in OSS so the API layer can read it
    bucket = _bucket()
    session_meta = json.dumps({
        "session_id": session_id,
        "run_id": run_id,
        "vnc_password": vnc_password,
        "ws_port": ws_port,
        "status": "ready",
    })
    bucket.put_object(
        f"runs/{run_id}/login_session.json",
        session_meta,
        headers={"content-type": "application/json"},
    )

    browser = None
    ctx = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=False,
                args=[
                    f"--display={display}",
                    "--window-size=1280,720",
                    "--disable-gpu",
                    "--no-sandbox",
                ],
            )
            ctx = browser.new_context(
                viewport={"width": 1280, "height": 720},
            )
            page = ctx.new_page()
            page.goto(app_url, wait_until="networkidle")

            # Poll for completion signal
            signal_key = f"runs/{run_id}/login_complete"
            deadline = time.time() + 300  # 5 minute timeout for user to log in
            completed = False

            while time.time() < deadline:
                try:
                    bucket.get_object(signal_key)
                    completed = True
                    break
                except oss2.exceptions.NoSuchKey:
                    time.sleep(2)

            if not completed:
                bucket.put_object(
                    f"runs/{run_id}/login_session.json",
                    json.dumps({"session_id": session_id, "status": "timeout"}),
                    headers={"content-type": "application/json"},
                )
                return json.dumps({"status": "timeout", "session_id": session_id})

            # Capture storageState
            state = ctx.storage_state()
            state_json = json.dumps(state)
            ciphertext = encrypt_state(state_json)

            origin = f"{urlparse(app_url).scheme}://{urlparse(app_url).netloc}"

            state_blob = json.dumps({
                "run_id": run_id,
                "ciphertext_b64": ciphertext,
                "created_at": time.time(),
                "ttl_seconds": ttl_seconds,
                "origin": origin,
            })

            state_key = f"runs/{run_id}/browser_state.enc"
            bucket.put_object(
                state_key,
                state_blob,
                headers={"content-type": "application/json"},
            )

            # Update session status
            bucket.put_object(
                f"runs/{run_id}/login_session.json",
                json.dumps({"session_id": session_id, "status": "captured", "state_key": state_key}),
                headers={"content-type": "application/json"},
            )

            ctx.close()
            browser.close()

    except Exception as e:
        if ctx:
            try:
                ctx.close()
            except Exception:
                pass
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        bucket.put_object(
            f"runs/{run_id}/login_session.json",
            json.dumps({"session_id": session_id, "status": "error", "error": str(e)[:500]}),
            headers={"content-type": "application/json"},
        )
        raise
    finally:
        _cleanup(ws_proc, x11vnc_proc, xvfb_proc)

    return json.dumps({"status": "ok", "state_key": state_key})
