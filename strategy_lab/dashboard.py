"""Serves the page. Reads the recordings; never writes them.

A fresh connection per request, so each page sees everything the Collector has written
since the last one. Reads cannot block the Collector's writes because the database runs in
WAL mode — which matters more than it sounds: a stalled Collector loses Rounds that cannot
be collected again.
"""
from __future__ import annotations

import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .db import connect
from .render import render_page

log = logging.getLogger("dashboard")

DB_PATH = os.environ.get("STRATEGY_LAB_DB", "data/lab.db")
HOST = os.environ.get("STRATEGY_LAB_HOST", "0.0.0.0")
PORT = int(os.environ.get("STRATEGY_LAB_PORT", "8000"))


class Handler(BaseHTTPRequestHandler):
    server_version = "StrategyLab"

    def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        path = self.path.split("?", 1)[0]
        if path in ("/healthz", "/health"):
            return self._send(200, "text/plain; charset=utf-8", b"ok")
        if path != "/":
            return self._send(404, "text/plain; charset=utf-8", b"not found")
        try:
            conn = connect(DB_PATH)
            try:
                body = render_page(conn).encode("utf-8")
            finally:
                conn.close()
        except Exception:
            log.exception("could not render the page")
            return self._send(500, "text/plain; charset=utf-8", b"could not read the recordings")
        self._send(200, "text/html; charset=utf-8", body)

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        log.info("%s - %s", self.address_string(), fmt % args)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("STRATEGY_LAB_LOG", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log.info("dashboard reading %s, listening on %s:%s", DB_PATH, HOST, PORT)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
