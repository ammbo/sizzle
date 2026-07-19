"""Login worker: interactive browser session for authenticated app capture.

The FC custom-container entrypoint accepts a WebSocket upgrade on :9000 and
proxies the RFB stream from x11vnc. The browser session starts on first
authenticated WebSocket connection (token validated against OSS metadata).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import signal
import socket
import struct
import subprocess
import threading
import time
from urllib.parse import parse_qs, urlparse

import oss2
from oss2.credentials import EnvironmentVariableCredentialsProvider
from playwright.sync_api import sync_playwright

from sizzle.browser_auth import encrypt_state

_WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_session_lock = threading.Lock()
_active: dict | None = None


def _bucket():
    auth = oss2.ProviderAuthV4(EnvironmentVariableCredentialsProvider())
    return oss2.Bucket(
        auth,
        os.environ["OSS_ENDPOINT"],
        os.environ["OSS_BUCKET"],
        region=os.environ.get("ALIBABA_CLOUD_REGION", "ap-southeast-1"),
    )


def _start_xvfb(display: str = ":99", resolution: str = "1280x720x24") -> subprocess.Popen:
    return subprocess.Popen(
        ["Xvfb", display, "-screen", "0", resolution, "-ac"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _start_x11vnc(display: str = ":99", vnc_port: int = 5999) -> subprocess.Popen:
    return subprocess.Popen(
        [
            "x11vnc",
            "-display", display,
            "-rfbport", str(vnc_port),
            "-shared",
            "-forever",
            "-noxdamage",
            "-nopw",
            "-listen", "127.0.0.1",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _cleanup(*procs: subprocess.Popen | None) -> None:
    for proc in procs:
        if proc is None:
            continue
        try:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except ProcessLookupError:
                pass


def _load_session(run_id: str) -> dict:
    raw = _bucket().get_object(f"runs/{run_id}/login_session.json").read()
    return json.loads(raw)


def _update_session(run_id: str, **fields) -> None:
    meta = _load_session(run_id)
    meta.update(fields)
    _bucket().put_object(
        f"runs/{run_id}/login_session.json",
        json.dumps(meta),
        headers={"content-type": "application/json"},
    )


def _signal_exists(run_id: str, name: str) -> bool:
    try:
        _bucket().get_object_meta(f"runs/{run_id}/{name}")
        return True
    except oss2.exceptions.NoSuchKey:
        return False


def _ws_accept_key(key: str) -> str:
    digest = hashlib.sha1((key + _WS_MAGIC).encode()).digest()
    return base64.b64encode(digest).decode()


def _ws_recv_frame(conn: socket.socket) -> tuple[int, bytes] | None:
    header = _recvexact(conn, 2)
    if not header:
        return None
    b1, b2 = header[0], header[1]
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack("!H", _recvexact(conn, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _recvexact(conn, 8))[0]
    mask = _recvexact(conn, 4) if masked else b""
    payload = _recvexact(conn, length) if length else b""
    if masked and payload:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload


def _ws_send_frame(conn: socket.socket, opcode: int, payload: bytes) -> None:
    header = bytearray([0x80 | (opcode & 0x0F)])
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header.extend(struct.pack("!H", n))
    else:
        header.append(127)
        header.extend(struct.pack("!Q", n))
    conn.sendall(header + payload)


def _recvexact(conn: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return b""
        buf.extend(chunk)
    return bytes(buf)


def _proxy_ws_to_vnc(ws: socket.socket, vnc_port: int = 5999) -> None:
    vnc = socket.create_connection(("127.0.0.1", vnc_port), timeout=10)
    vnc.settimeout(None)
    stop = threading.Event()

    def vnc_to_ws() -> None:
        try:
            while not stop.is_set():
                data = vnc.recv(65536)
                if not data:
                    break
                _ws_send_frame(ws, 0x2, data)
        except OSError:
            pass
        finally:
            stop.set()

    t = threading.Thread(target=vnc_to_ws, daemon=True)
    t.start()
    try:
        while not stop.is_set():
            frame = _ws_recv_frame(ws)
            if frame is None:
                break
            opcode, payload = frame
            if opcode in (0x8,):  # close
                break
            if opcode in (0x9,):  # ping
                _ws_send_frame(ws, 0xA, payload)
                continue
            if opcode in (0x1, 0x2) and payload:  # text/binary
                vnc.sendall(payload)
    except OSError:
        pass
    finally:
        stop.set()
        try:
            vnc.close()
        except OSError:
            pass


def _capture_state(run_id: str, app_url: str, ctx) -> str:
    ttl_seconds = int(os.environ.get("BROWSER_AUTH_TTL", "3600"))
    state = ctx.storage_state()
    ciphertext = encrypt_state(json.dumps(state))
    origin = f"{urlparse(app_url).scheme}://{urlparse(app_url).netloc}"
    state_key = f"runs/{run_id}/browser_state.enc"
    blob = json.dumps({
        "run_id": run_id,
        "ciphertext_b64": ciphertext,
        "created_at": time.time(),
        "ttl_seconds": ttl_seconds,
        "origin": origin,
    })
    _bucket().put_object(
        state_key,
        blob,
        headers={"content-type": "application/json"},
    )
    return state_key


def _run_browser_session(run_id: str, app_url: str, session_id: str) -> dict:
    """Start Xvfb/x11vnc/Chromium and poll for complete/cancel signals."""
    display = ":99"
    vnc_port = 5999
    xvfb = _start_xvfb(display)
    time.sleep(1)
    os.environ["DISPLAY"] = display
    x11vnc = _start_x11vnc(display, vnc_port)
    time.sleep(0.5)

    browser = None
    ctx = None
    state_key = ""
    status = "ready"

    try:
        _update_session(run_id, status="ready", vnc_port=vnc_port)

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
            ctx = browser.new_context(viewport={"width": 1280, "height": 720})
            page = ctx.new_page()
            page.goto(app_url, wait_until="domcontentloaded", timeout=60000)

            deadline = time.time() + 300
            while time.time() < deadline:
                if _signal_exists(run_id, "login_cancel"):
                    status = "cancelled"
                    break
                if _signal_exists(run_id, "login_complete"):
                    state_key = _capture_state(run_id, app_url, ctx)
                    status = "captured"
                    break
                time.sleep(1)
            else:
                status = "timeout"

            _update_session(
                run_id,
                status=status,
                **({"state_key": state_key} if state_key else {}),
            )
            try:
                ctx.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass
    except Exception as e:
        status = "error"
        _update_session(run_id, status="error", error=str(e)[:500])
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
        raise
    finally:
        _cleanup(x11vnc, xvfb)

    return {"status": status, "state_key": state_key, "session_id": session_id}


def ensure_session(run_id: str, session_id: str, token: str) -> None:
    """Validate token and ensure a browser session is running for this run."""
    global _active
    meta = _load_session(run_id)
    if meta.get("session_id") != session_id:
        raise PermissionError("session mismatch")
    if not secrets.compare_digest(str(meta.get("auth_token", "")), token):
        raise PermissionError("invalid token")
    if meta.get("status") in ("cancelled", "captured", "timeout", "error"):
        raise RuntimeError(f"session already {meta['status']}")

    with _session_lock:
        if _active and _active.get("run_id") == run_id and _active["thread"].is_alive():
            return
        app_url = meta["app_url"]

        def target() -> None:
            global _active
            try:
                _run_browser_session(run_id, app_url, session_id)
            finally:
                with _session_lock:
                    if _active and _active.get("run_id") == run_id:
                        _active = None

        thread = threading.Thread(target=target, daemon=True, name=f"login-{run_id}")
        _active = {"run_id": run_id, "thread": thread}
        thread.start()

    # Wait until VNC is accepting connections
    deadline = time.time() + 45
    while time.time() < deadline:
        try:
            meta = _load_session(run_id)
        except Exception:
            time.sleep(0.5)
            continue
        if meta.get("status") == "ready":
            # confirm TCP
            try:
                s = socket.create_connection(("127.0.0.1", 5999), timeout=1)
                s.close()
                return
            except OSError:
                pass
        if meta.get("status") in ("error", "cancelled", "timeout"):
            raise RuntimeError(meta.get("error") or meta["status"])
        time.sleep(0.4)
    raise TimeoutError("browser session failed to become ready")


def handle_websocket(conn: socket.socket, path: str, headers: dict[str, str]) -> None:
    """Complete WS handshake, start session if needed, proxy to x11vnc."""
    qs = parse_qs(urlparse(path).query)
    run_id = (qs.get("run_id") or [""])[0]
    session_id = (qs.get("session_id") or [""])[0]
    token = (qs.get("token") or [""])[0]
    if not run_id or not session_id or not token:
        conn.sendall(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
        conn.close()
        return

    try:
        ensure_session(run_id, session_id, token)
    except PermissionError:
        conn.sendall(b"HTTP/1.1 401 Unauthorized\r\nConnection: close\r\n\r\n")
        conn.close()
        return
    except Exception as e:
        body = str(e)[:200].encode()
        conn.sendall(
            b"HTTP/1.1 503 Service Unavailable\r\n"
            b"Content-Type: text/plain\r\n"
            + f"Content-Length: {len(body)}\r\n".encode()
            + b"Connection: close\r\n\r\n"
            + body
        )
        conn.close()
        return

    key = headers.get("sec-websocket-key", "")
    if not key:
        conn.sendall(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
        conn.close()
        return

    accept = _ws_accept_key(key)
    proto = headers.get("sec-websocket-protocol", "")
    extra = ""
    if "binary" in proto:
        extra = "Sec-WebSocket-Protocol: binary\r\n"

    conn.sendall(
        (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n"
            f"{extra}"
            "\r\n"
        ).encode()
    )

    try:
        _proxy_ws_to_vnc(conn)
    finally:
        try:
            conn.close()
        except OSError:
            pass


def handler(event: bytes, context) -> str:
    """Optional event invoke — kept for compatibility; live sessions use WebSocket."""
    payload = json.loads(event) if event else {}
    run_id = payload.get("run_id", "")
    session_id = payload.get("session_id", "")
    token = payload.get("token", "")
    if run_id and session_id and token:
        ensure_session(run_id, session_id, token)
        return json.dumps({"status": "ready", "session_id": session_id})
    return json.dumps({"status": "ok", "mode": "websocket"})
