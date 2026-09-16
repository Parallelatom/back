"""Serves the page. Reads the recordings; never writes them.

A fresh read-only connection per cache refresh. Reads cannot block the Collector's writes
because the database runs in WAL mode — which matters more than it sounds: a stalled
Collector loses Rounds that cannot be collected again.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .db import connect_readonly
from .render import render_page

log = logging.getLogger("dashboard")

DB_PATH = os.environ.get("STRATEGY_LAB_DB", "data/lab.db")
HOST = os.environ.get("STRATEGY_LAB_HOST", "127.0.0.1")
PORT = int(os.environ.get("STRATEGY_LAB_PORT", "8000"))


class DashboardServer(ThreadingHTTPServer):
    """Bound both socket workers and expensive renders, including simultaneous misses."""

    def __init__(self, address, handler, db_path=DB_PATH, max_workers=4,
                 request_timeout=10, cache_seconds=5, cache_entries=8):
        if max_workers < 1 or request_timeout <= 0 or cache_seconds < 0 or cache_entries < 1:
            raise ValueError("invalid dashboard resource limits")
        self.db_path = db_path
        self.request_timeout = request_timeout
        self.cache_seconds = cache_seconds
        self.cache_entries = cache_entries
        self._slots = threading.BoundedSemaphore(max_workers)
        self._render_lock = threading.Lock()
        self._pages = OrderedDict()
        super().__init__(address, handler)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(self.request_timeout)
        return request, address

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            try:
                request.settimeout(0.1)
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\n"
                                b"Content-Length: 0\r\nRetry-After: 1\r\n\r\n")
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def page(self, include_stale=False, include_partial=False, page=1):
        key = (include_stale, include_partial, page)
        with self._render_lock:
            cached = self._pages.get(key)
            if cached is not None and time.monotonic() - cached[0] < self.cache_seconds:
                self._pages.move_to_end(key)
                return cached[1]
            conn = connect_readonly(self.db_path)
            try:
                # All tables on the page describe the same committed snapshot.
                conn.execute("BEGIN")
                body = render_page(conn, include_stale=include_stale,
                                   include_partial=include_partial, page=page).encode("utf-8")
            finally:
                conn.close()
            self._pages[key] = (time.monotonic(), body)
            self._pages.move_to_end(key)
            while len(self._pages) > self.cache_entries:
                self._pages.popitem(last=False)
            return body


class Handler(BaseHTTPRequestHandler):
    server_version = "StrategyLab"

    def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        parsed = urlsplit(self.path)
        if parsed.path in ("/healthz", "/health"):
            return self._send(200, "text/plain; charset=utf-8", b"ok")
        if parsed.path != "/":
            return self._send(404, "text/plain; charset=utf-8", b"not found")
        query = parse_qs(parsed.query)
        try:
            body = self.server.page(
                include_stale="stale" in query,
                include_partial="partial" in query,
                page=self._page_of(query),
            )
        except Exception:
            log.exception("could not render the page")
            return self._send(500, "text/plain; charset=utf-8", b"could not read the recordings")
        self._send(200, "text/html; charset=utf-8", body)

    @staticmethod
    def _page_of(query) -> int:
        try:
            return max(1, int(query.get("page", ["1"])[0]))
        except (TypeError, ValueError):
            return 1

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
    with DashboardServer((HOST, PORT), Handler) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
