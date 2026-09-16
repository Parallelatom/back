"""HTTP resource limits and cached reads of real SQLite recordings."""
import http.client
import socket
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from strategy_lab import dashboard
from strategy_lab.db import connect, initialise


@pytest.fixture
def database(tmp_path):
    path = str(tmp_path / "lab.db")
    conn = connect(path)
    initialise(conn)
    yield path
    conn.close()


def test_cache_is_shared_bounded_expires_and_keeps_filters_separate(database, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(dashboard, "time", SimpleNamespace(monotonic=lambda: now[0]))
    calls = []

    def render(conn, **options):
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM rounds")
        calls.append(options)
        return str(len(calls))

    monkeypatch.setattr(dashboard, "render_page", render)
    with dashboard.DashboardServer(("127.0.0.1", 0), dashboard.Handler,
                                   db_path=database, cache_entries=2) as server:
        with ThreadPoolExecutor(max_workers=4) as pool:
            assert list(pool.map(lambda _: server.page(), range(4))) == [b"1"] * 4
        assert server.page(include_stale=True) == b"2"
        assert server.page() == b"1"
        assert server.page(page=2) == b"3"
        assert server.page(include_stale=True) == b"4"  # least-recent entry was evicted
        now[0] = 6.0
        assert server.page(include_stale=True) == b"5"


def get(server, path="/"):
    conn = http.client.HTTPConnection(*server.server_address, timeout=2)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def test_busy_server_refuses_excess_connections_then_recovers(database, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def render(conn, **options):
        entered.set()
        assert release.wait(2)
        return "rendered"

    monkeypatch.setattr(dashboard, "render_page", render)
    with dashboard.DashboardServer(("127.0.0.1", 0), dashboard.Handler,
                                   db_path=database, max_workers=1) as server:
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
        thread.start()
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                first = pool.submit(get, server)
                try:
                    assert entered.wait(2)
                    assert get(server)[0] == 503
                finally:
                    release.set()
                assert first.result(timeout=2) == (200, b"rendered")
        finally:
            server.shutdown()
            thread.join(2)


def test_incomplete_request_is_closed_after_socket_timeout(database):
    with dashboard.DashboardServer(("127.0.0.1", 0), dashboard.Handler,
                                   db_path=database, request_timeout=0.1) as server:
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
        thread.start()
        try:
            with socket.create_connection(server.server_address, timeout=2) as client:
                client.sendall(b"GET / HTTP/1.1\r\n")
                assert client.recv(1024) == b""
            assert get(server, "/healthz") == (200, b"ok")
            assert get(server, "/missing")[0] == 404
            assert get(server, "/?page=bad")[0] == 200
        finally:
            server.shutdown()
            thread.join(2)
