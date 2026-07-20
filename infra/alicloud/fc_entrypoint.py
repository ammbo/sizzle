#!/usr/bin/env python3
"""FC 3.0 custom container HTTP entrypoint.

FC 3.0 delivers invocations as HTTP POST requests to the container.
This server dynamically imports the handler specified by FC_HANDLER
(e.g. "pipeline_worker.handler") and routes all POSTs to it.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

sys.path.insert(0, "/app")


def _load_handler() -> Callable:
    spec = os.environ.get("FC_HANDLER", "pipeline_worker.handler")
    module_name, func_name = spec.rsplit(".", 1)
    module = importlib.import_module(module_name)
    return getattr(module, func_name)


class Handler(BaseHTTPRequestHandler):
    _fc_handler: Callable | None = None

    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        body = self.rfile.read(length) if length else b""
        sys.stderr.write(f"[fc] POST {self.path} body_len={length}\n")
        try:
            result = self._fc_handler(body, None)
            if isinstance(result, dict):
                out = json.dumps(result).encode()
            elif isinstance(result, str):
                out = result.encode()
            else:
                out = result or b""
            self.send_response(200)
            sys.stderr.write(f"[fc] POST {self.path} -> 200 response_len={len(out)}\n")
        except Exception:
            tb = traceback.format_exc()
            sys.stderr.write(f"[fc] POST {self.path} -> 500 error:\n{tb}\n")
            out = json.dumps({"error": tb[-1000:]}).encode()
            self.send_response(500)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        # Health / readiness for FC custom container ping
        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_one_request(self):
        try:
            self.raw_requestline = self.rfile.readline(65537)
            if not self.raw_requestline:
                self.close_connection = True
                return
            if not self.parse_request():
                return

            mname = "do_" + self.command
            if not hasattr(self, mname):
                self.send_error(501, f"Unsupported method ({self.command})")
                return
            getattr(self, mname)()
        except Exception:
            self.close_connection = True
            raise

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[fc] {fmt % args}\n")


if __name__ == "__main__":
    Handler._fc_handler = staticmethod(_load_handler())
    port = int(os.environ.get("FC_SERVER_PORT", "9000"))
    # Threading server so WS proxy + browser thread can coexist with health checks
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.request_queue_size = 32
    print(f"FC handler ready on :{port}", flush=True)
    server.serve_forever()
