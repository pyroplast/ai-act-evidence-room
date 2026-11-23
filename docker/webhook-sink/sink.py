"""Demo webhook receiver. Verifies X-Evidence-Signature and records every delivery.

Standard library only, so a firm's own receiver can copy ``verify`` as-is.

    GET  /healthz       200
    GET  /deliveries    JSON list of everything received, with "verified" and "reason"
    POST /_control      {"fail_status": 500} makes deliveries fail (0 restores 200)
    POST /<anything>    a delivery: 200 when the signature verifies, 401 when it does
                        not, or the configured fail_status

Environment: SINK_SECRET (required), SINK_LOG (default /data/deliveries.jsonl),
SINK_FAIL_STATUS (default 0), SINK_TOLERANCE_SECONDS (default 300; 0 disables the
timestamp check), SINK_PORT (default 8080).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def verify(
    secret: str, header: str, body: bytes, *, now: int | None = None, tolerance: int | None = 300
) -> tuple[bool, str]:
    """Same check as evidence_room.notify.verify:
    header = "t=<unix>,v1=<hex hmac_sha256(secret, '<t>.' + body)>"."""
    try:
        parts = dict(item.split("=", 1) for item in header.split(","))
        timestamp = int(parts["t"])
        given = parts["v1"]
    except (KeyError, ValueError):
        return False, "malformed signature header"
    mac = hmac.new(secret.encode("utf-8"), f"{timestamp}.".encode() + body, hashlib.sha256)
    if not hmac.compare_digest(mac.hexdigest(), given):
        return False, "signature mismatch"
    if tolerance and now is not None and abs(now - timestamp) > tolerance:
        return False, "timestamp outside tolerance"
    return True, "ok"


class Sink:
    def __init__(
        self, secret: str, log_path: str | Path, fail_status: int = 0, tolerance: int = 300
    ) -> None:
        self.secret = secret
        self.log_path = Path(log_path)
        self.fail_status = fail_status
        self.tolerance = tolerance
        self.lock = threading.Lock()

    def record(self, entry: dict) -> None:
        line = json.dumps(entry, ensure_ascii=False)
        with self.lock:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        print(line, flush=True)

    def received(self) -> list[dict]:
        try:
            with self.log_path.open(encoding="utf-8") as fh:
                return [json.loads(line) for line in fh if line.strip()]
        except FileNotFoundError:
            return []


def make_handler(sink: Sink) -> type[BaseHTTPRequestHandler]:
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
                self._send(200, sink.received())
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if self.path == "/_control":
                sink.fail_status = int(json.loads(body or b"{}").get("fail_status", 0))
                self._send(200, {"fail_status": sink.fail_status})
                return
            ok, reason = verify(
                sink.secret,
                self.headers.get("X-Evidence-Signature", ""),
                body,
                now=int(time.time()),
                tolerance=sink.tolerance,
            )
            status = 401 if not ok else (sink.fail_status or 200)
            try:
                payload = json.loads(body)
            except ValueError:
                payload = None
            sink.record(
                {
                    "received_at": datetime.now(UTC).isoformat(),
                    "path": self.path,
                    "event": self.headers.get("X-Evidence-Event", ""),
                    "event_id": self.headers.get("X-Evidence-Event-Id", ""),
                    "attempt": self.headers.get("X-Evidence-Attempt", ""),
                    "verified": ok,
                    "reason": reason,
                    "responded": status,
                    "payload": payload,
                }
            )
            self._send(status, {"received": True, "verified": ok, "reason": reason})

        def log_message(self, format: str, *args: object) -> None:
            pass

    return Handler


def make_server(sink: Sink, host: str = "0.0.0.0", port: int = 8080) -> ThreadingHTTPServer:  # noqa: S104
    return ThreadingHTTPServer((host, port), make_handler(sink))


if __name__ == "__main__":
    secret = os.environ.get("SINK_SECRET", "")
    if not secret:
        raise SystemExit("SINK_SECRET is required")
    sink = Sink(
        secret,
        os.environ.get("SINK_LOG", "/data/deliveries.jsonl"),
        fail_status=int(os.environ.get("SINK_FAIL_STATUS", "0") or 0),
        tolerance=int(os.environ.get("SINK_TOLERANCE_SECONDS", "300")),
    )
    make_server(sink, port=int(os.environ.get("SINK_PORT", "8080"))).serve_forever()
