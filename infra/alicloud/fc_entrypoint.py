#!/usr/bin/env python3
"""FC 3.0 custom container HTTP entrypoint.

FC 3.0 delivers invocations as HTTP POST requests to the container.
This server dynamically imports the handler specified by FC_HANDLER
(e.g. "pipeline_worker.handler") and routes all POSTs to it.
"""
import http.server
import importlib
import json
import os
import sys
import traceback

sys.path.insert(0, "/app")


def _load_handler():
    spec = os.environ.get("FC_HANDLER", "pipeline_worker.handler")
    module_name, func_name = spec.rsplit(".", 1)
    module = importlib.import_module(module_name)
    return getattr(module, func_name)


class Handler(http.server.BaseHTTPRequestHandler):
    _fc_handler = None

    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        body = self.rfile.read(length) if length else b""
        try:
            result = self._fc_handler(body, None)
            if isinstance(result, dict):
                out = json.dumps(result).encode()
            elif isinstance(result, str):
                out = result.encode()
            else:
                out = result or b""
            self.send_response(200)
        except Exception:
            out = json.dumps({"error": traceback.format_exc()[-1000:]}).encode()
            self.send_response(500)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[fc] {fmt % args}\n")


if __name__ == "__main__":
    Handler._fc_handler = staticmethod(_load_handler())
    port = int(os.environ.get("FC_SERVER_PORT", "9000"))
    server = http.server.HTTPServer(("0.0.0.0", port), Handler)
    print(f"FC handler ready on :{port}", flush=True)
    server.serve_forever()
