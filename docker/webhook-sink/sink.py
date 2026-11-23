"""Demo webhook receiver: records every delivery it gets. Standard library only.

GET  /healthz      -> 200
GET  /deliveries   -> JSON list of what was received
POST /<anything>   -> recorded; responds 200 (or SINK_FAIL_STATUS, to exercise retries)

Signature verification (X-Evidence-Signature) is added in M3 together with the notifier.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG_PATH = os.environ.get("SINK_LOG", "/data/deliveries.jsonl")
FAIL_STATUS = int(os.environ.get("SINK_FAIL_STATUS", "0") or 0)
_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: object) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send(200, {"status": "ok"})
        elif self.path == "/deliveries":
            try:
                with open(LOG_PATH, encoding="utf-8") as fh:
                    self._send(200, [json.loads(line) for line in fh if line.strip()])
            except FileNotFoundError:
                self._send(200, [])
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8", "replace")
        record = {
            "received_at": datetime.now(UTC).isoformat(),
            "path": self.path,
            "signature": self.headers.get("X-Evidence-Signature", ""),
            "body": body,
        }
        with _lock, open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        print(json.dumps(record), flush=True)
        self._send(FAIL_STATUS or 200, {"received": True})

    def log_message(self, format: str, *args: object) -> None:
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()  # noqa: S104
