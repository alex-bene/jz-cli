from __future__ import annotations

import asyncio
import contextlib
import gzip
import http.client
import http.server
import io
import os
import socket
import socketserver
import subprocess
import threading
import time
from typing import TYPE_CHECKING, BinaryIO, Self

if TYPE_CHECKING:
    import socketserver
    from collections.abc import Callable
    from pathlib import Path

import pytest
import typer
from multidict import CIMultiDict
from typer.testing import CliRunner

import jz_cli.main as main_module
import jz_cli.proxy.tcp as tcp_mod
import jz_cli.ssh as ssh_module
from jz_cli.proxy.connect import RelayError, RelayListener, build_remote_command
from jz_cli.proxy.http import (
    ProxyConfig,
    _filter_hop_by_hop,
    build_proxy,
    make_client_session,
    policy_path,
    start_new_site,
    validate_request,
)
from jz_cli.proxy.nodes import resolve_node
from jz_cli.proxy.openai import OPENAI_ALLOWED_METHODS, OPENAI_ALLOWED_PATHS, OPENAI_ALLOWED_PREFIXES


class SocketRelay:
    """Relay stand-in that talks to a local TCP server instead of over SSH."""

    def __init__(self, host: str, port: int) -> None:
        """Connect to a local TCP server standing in for the compute node."""
        self._socket = socket.create_connection((host, port), timeout=10)
        self._socket.settimeout(None)
        self._reader: BinaryIO = self._socket.makefile("rb")

    @property
    def stdout(self) -> BinaryIO:
        """Return the remote-to-local byte stream."""
        return self._reader

    def send_all(self, data: bytes) -> None:
        """Write request bytes to the local stand-in server."""
        self._socket.sendall(data)

    def close_input(self) -> None:
        """Half-close the request side."""
        self._socket.shutdown(socket.SHUT_WR)

    def error_text(self, limit: int = 400) -> str:
        """Return no stderr detail: a local socket has no subprocess."""
        return ""

    def close(self) -> None:
        """Close the connection and its reader."""
        self._reader.close()
        self._socket.close()


def local_listener(opened: list[tuple[str, int]] | None = None) -> Callable[[str, int], RelayListener]:
    """Return a RelayListener factory whose channels use plain local sockets."""

    def factory(host: str, port: int) -> RelayListener:
        def relay(_host: str, _port: int) -> SocketRelay:
            if opened is not None:
                opened.append((host, port))
            return SocketRelay(host, port)

        return RelayListener(host=host, remote_port=port, relay_factory=relay)

    return factory


class ProxyServerThread:
    """Run the aiohttp proxy app in a background thread on an ephemeral port."""

    def __init__(self, config: ProxyConfig, listener_factory: Callable[[str, int], RelayListener]) -> None:
        self.port: int | None = None
        self._config = config
        self._listener_factory = listener_factory
        self._started = threading.Event()
        self._cancel: Callable[[], None] = lambda: None

    def _run(self) -> None:
        loop = asyncio.new_event_loop()

        async def main() -> None:
            runner, listener = build_proxy(self._config, listener_factory=self._listener_factory)
            try:
                self.port = await start_new_site(runner, "127.0.0.1", 0)
                self._started.set()
                await asyncio.Event().wait()
            finally:
                await runner.cleanup()
                listener.close()

        task = loop.create_task(main())
        self._cancel = lambda: loop.call_soon_threadsafe(task.cancel)
        try:
            loop.run_until_complete(task)
        except asyncio.CancelledError:
            pass
        finally:
            loop.close()

    def __enter__(self) -> Self:  # noqa: D105
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._started.wait(timeout=5):
            message = "proxy server did not start"
            raise RuntimeError(message)
        return self

    def __exit__(self, *exc: object) -> None:  # noqa: D105
        self._cancel()
        self._thread.join(timeout=5)


def _serve_in_thread(server: socketserver.BaseServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def _read_chunked_body(rfile: BinaryIO) -> bytes:
    """Read a chunked request body."""
    body = b""
    while True:
        size_line = rfile.readline().strip()
        size = int(size_line.split(b";")[0], 16)
        if size == 0:
            while True:
                trailer = rfile.readline()
                if trailer in (b"\r\n", b"\n", b""):
                    return body
        body += rfile.read(size)
        rfile.read(2)


def _read_body_by_framing(handler: http.server.BaseHTTPRequestHandler) -> bytes:
    """Read a request body honoring its framing (Content-Length or chunked)."""
    if handler.headers.get("Transfer-Encoding", "").lower() == "chunked":
        return _read_chunked_body(handler.rfile)
    length = int(handler.headers.get("Content-Length", 0))
    return handler.rfile.read(length) if length else b""


def test_resolve_node_requires_exactly_one_selector() -> None:
    with pytest.raises(typer.BadParameter):
        resolve_node(node=None, job_id=None, job_name=None)

    with pytest.raises(typer.BadParameter):
        resolve_node(node="jzxh033", job_id=123, job_name=None)


def test_validate_request_normalizes_methods_and_paths() -> None:
    config = ProxyConfig(
        selector="node=jzxh033",
        node="jzxh033",
        remote_host="localhost",
        remote_port=8000,
        allow_methods=("GET", "POST"),
        allow_prefixes=("/api/",),
    )

    assert validate_request(config, "GET", "/api/items") is None
    assert validate_request(config, "PATCH", "/api/items") == (405, {"error": "Unsupported method: PATCH"})
    assert validate_request(config, "GET", "/admin") == (404, {"error": "Unsupported path: /admin"})


def test_validate_request_uses_allowlists_only() -> None:
    config = ProxyConfig(
        selector="node=jzxh033",
        node="jzxh033",
        remote_host="localhost",
        remote_port=8000,
        allow_methods=("GET", "POST"),
        allow_prefixes=("/api/",),
    )

    assert validate_request(config, "POST", "/api/items") is None
    assert validate_request(config, "GET", "/api/admin/users") is None


def test_policy_path_uses_raw_path_for_allowlist_matching() -> None:
    assert policy_path("/api/items?q=x") == "/api/items"


def test_policy_path_dot_normalization_closes_prefix_bypass() -> None:
    """Dot segments must not pass a prefix check the upstream sees differently."""
    # A leading-slash bypass variant: beyond-root dots must yield '/' + rest.
    assert policy_path("/v1/../../admin") == "/admin"
    # urlsplit parses a leading '//' as a netloc (which the proxy discards by
    # overriding Host), so path and recomposed upstream agree: not a bypass.
    assert policy_path("//evil/admin") == "/admin"
    # Percent-encoded dots decode before normalization, or the upstream would.
    assert policy_path("/v1/%2e%2e/admin") == "/admin"
    assert policy_path("/v1/%2e/admin") == "/v1/admin"


def test_validate_request_rejects_dot_segment_prefix_bypass() -> None:
    config = ProxyConfig(
        selector="test",
        node="test-node",
        remote_host="localhost",
        remote_port=8000,
        allow_methods=("GET",),
        allow_prefixes=("/v1/",),
    )

    # Previously matched '/v1/' but reached the upstream as '/admin'.
    assert validate_request(config, "GET", "/admin") == (404, {"error": "Unsupported path: /admin"})


def test_hop_by_hop_headers_are_filtered() -> None:
    headers = CIMultiDict(
        [
            ("Host", "localhost:8000"),
            ("Connection", "keep-alive, Custom-Hop"),
            ("Keep-Alive", "timeout=5"),
            ("Custom-Hop", "yes"),
            ("X-Keep", "yes"),
        ]
    )

    kept = dict(_filter_hop_by_hop(headers))

    assert set(kept) == {"Host", "X-Keep"}


def test_upstream_client_preserves_encoded_bodies_without_total_timeout() -> None:
    async def inspect_session() -> tuple[float | None, float | None, bool]:
        session = make_client_session()
        try:
            return session.timeout.total, session.timeout.sock_read, session._auto_decompress
        finally:
            await session.close()

    total, sock_read, auto_decompress = asyncio.run(inspect_session())

    assert total is None
    assert sock_read == 900
    assert auto_decompress is False


def test_openai_config_allows_streaming_requests() -> None:
    config = ProxyConfig(
        selector="node=jzxh033",
        node="jzxh033",
        remote_host="localhost",
        remote_port=8888,
        verbose=True,
        allow_methods=OPENAI_ALLOWED_METHODS,
        allow_prefixes=OPENAI_ALLOWED_PREFIXES,
        allow_paths=OPENAI_ALLOWED_PATHS,
    )

    assert config.remote_port == 8888
    assert validate_request(config, "GET", "/v1/models") is None
    assert validate_request(config, "GET", "/health") is None
    assert validate_request(config, "GET", "/health/admin") == (404, {"error": "Unsupported path: /health/admin"})
    assert validate_request(config, "POST", "/v1/chat/completions") is None
    assert validate_request(config, "DELETE", "/v1/models/foo") == (405, {"error": "Unsupported method: DELETE"})


def test_http_proxy_streams_response_incrementally() -> None:
    captured: dict[str, object] = {}

    class Upstream(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:
            captured["body"] = _read_body_by_framing(self)
            captured["path"] = self.path
            # Like uvicorn behind vLLM: SSE events framed as chunked bodies, so a
            # consumer can tell where the response ends without a connection close.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            def event(data: bytes) -> bytes:
                return b"%x\r\n%s\r\n" % (len(data), data)

            self.wfile.write(event(b"data: one\n\n"))
            self.wfile.flush()
            time.sleep(2.0)
            self.wfile.write(event(b"data: two\n\n") + b"0\r\n\r\n")
            self.wfile.flush()

        def log_message(self, *args: object) -> None:
            return None

    upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    upstream.daemon_threads = True
    _serve_in_thread(upstream)

    config = ProxyConfig(
        selector="test", node="test-node", remote_host="127.0.0.1", remote_port=upstream.server_address[1]
    )

    with ProxyServerThread(config, local_listener()) as proxy:
        conn = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=10)
        conn.request("POST", "/v1/chat/completions", body=b"{}")
        response = conn.getresponse()

        started = time.monotonic()
        first_event = response.read1(64)
        first_event_latency = time.monotonic() - started
        rest = response.read()

        assert response.status == 200
        assert first_event.startswith(b"data: one")
        # Streamed rather than buffered: the first event arrived while the upstream
        # was still sleeping for 2 s before writing the second one.
        assert first_event_latency < 1.0, f"first event took {first_event_latency:.2f}s"
        assert b"data: two" in rest
        assert captured["body"] == b"{}"
        assert captured["path"] == "/v1/chat/completions"

    upstream.shutdown()
    upstream.server_close()


def test_http_proxy_streams_decoded_chunked_bodies() -> None:
    class ChunkedUpstream(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(b"5\r\nhello\r\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

        def log_message(self, *args: object) -> None:
            return None

    upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ChunkedUpstream)
    upstream.daemon_threads = True
    _serve_in_thread(upstream)

    config = ProxyConfig(
        selector="test", node="test-node", remote_host="127.0.0.1", remote_port=upstream.server_address[1]
    )

    with ProxyServerThread(config, local_listener()) as proxy:
        conn = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=10)
        conn.request("GET", "/v1/models")
        response = conn.getresponse()
        body = response.read()

        assert response.status == 200
        assert body == b"hello"

    upstream.shutdown()
    upstream.server_close()


def test_http_proxy_preserves_gzip_encoded_response_bytes() -> None:
    payload = b'{"status":"compressed"}'
    compressed = gzip.compress(payload)

    class GzipUpstream(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(compressed)))
            self.end_headers()
            self.wfile.write(compressed)

        def log_message(self, *args: object) -> None:
            return None

    upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), GzipUpstream)
    upstream.daemon_threads = True
    _serve_in_thread(upstream)

    config = ProxyConfig(
        selector="test", node="test-node", remote_host="127.0.0.1", remote_port=upstream.server_address[1]
    )

    with ProxyServerThread(config, local_listener()) as proxy:
        conn = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=10)
        conn.request("GET", "/v1/models")
        response = conn.getresponse()
        body = response.read()

        assert response.status == 200
        assert response.getheader("Content-Encoding") == "gzip"
        assert body == compressed
        assert gzip.decompress(body) == payload

    upstream.shutdown()
    upstream.server_close()


def test_http_proxy_rejects_disallowed_request_without_opening_relay() -> None:
    opened: list[tuple[str, int]] = []

    config = ProxyConfig(
        selector="test", node="test-node", remote_host="127.0.0.1", remote_port=1, allow_methods=("GET",)
    )

    with ProxyServerThread(config, local_listener(opened)) as proxy:
        conn = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=10)
        conn.request("DELETE", "/v1/models")
        response = conn.getresponse()
        body = response.read()

        assert response.status == 405
        assert b"Unsupported method: DELETE" in body
        assert opened == []


def test_http_proxy_health_endpoint_skips_relay() -> None:
    opened: list[tuple[str, int]] = []

    config = ProxyConfig(selector="job_id=123", node="jzxh033", remote_host="jzxh033", remote_port=8000)

    with ProxyServerThread(config, local_listener(opened)) as proxy:
        conn = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=10)
        conn.request("GET", "/_jz/health")
        response = conn.getresponse()
        body = response.read()

        assert response.status == 200
        assert b'"status": "ok"' in body
        assert b'"selector": "job_id=123"' in body
        assert opened == []


def test_http_proxy_rejects_connect_and_upgrade() -> None:
    opened: list[tuple[str, int]] = []

    config = ProxyConfig(selector="test", node="test-node", remote_host="127.0.0.1", remote_port=1)

    with ProxyServerThread(config, local_listener(opened)) as proxy:
        conn = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=10)
        conn.request("CONNECT", "example.test:443")
        response = conn.getresponse()
        response.read()
        assert response.status == 501

        conn2 = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=10)
        conn2.connect()
        conn2.request("GET", "/", headers={"Upgrade": "websocket", "Connection": "Upgrade"})
        response2 = conn2.getresponse()
        response2.read()
        assert response2.status == 501
        conn2.close()

    assert opened == []


def test_http_proxy_reports_unreachable_upstream_as_502() -> None:
    config = ProxyConfig(selector="test", node="test-node", remote_host="127.0.0.1", remote_port=1)

    with ProxyServerThread(config, local_listener()) as proxy:
        conn = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=10)
        conn.request("GET", "/v1/models")
        response = conn.getresponse()
        body = response.read()

        # relay port 1 is unreachable: the local listener fails to connect.
        assert response.status == 502
        assert b"Proxy relay failed" in body


def test_http_proxy_keeps_one_relay_across_keep_alive_requests() -> None:
    class Echo(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:
            body = _read_body_by_framing(self)
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            return None

    upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Echo)
    upstream.daemon_threads = True
    _serve_in_thread(upstream)

    opened: list[tuple[str, int]] = []
    config = ProxyConfig(
        selector="test", node="test-node", remote_host="127.0.0.1", remote_port=upstream.server_address[1]
    )

    with ProxyServerThread(config, local_listener(opened)) as proxy:
        conn = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=10)
        for i in range(3):
            payload = f"{{'n': {i}}}".encode()
            conn.request("POST", "/v1/chat/completions", body=payload)
            response = conn.getresponse()
            assert response.read() == payload
        conn.close()

    # One relay channel serves all three pooled requests.
    assert len(opened) == 1

    upstream.shutdown()
    upstream.server_close()


def test_relay_pump_does_not_half_close_before_reading_the_response() -> None:
    """Regression: an early half-close makes some upstreams abort without replying."""
    created: list[RecordingRelay] = []

    class RecordingRelay:
        """Relay that records the order of calls and serves a canned response."""

        def __init__(self, host: str, port: int) -> None:
            self.calls: list[str] = []
            self._reader = io.BytesIO(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi")
            created.append(self)

        @property
        def stdout(self) -> BinaryIO:
            self.calls.append("read")
            return self._reader

        def error_text(self, limit: int = 400) -> str:
            """Return no stderr detail: the canned relay never fails."""
            return ""

        def send_all(self, data: bytes) -> None:
            self.calls.append("send_all")

        def close_input(self) -> None:
            self.calls.append("close_input")

        def close(self) -> None:
            self.calls.append("close")

    config = ProxyConfig(selector="test", node="test-node", remote_host="127.0.0.1", remote_port=1)

    with ProxyServerThread(
        config,
        lambda _h, _p: RelayListener(host=_h, remote_port=_p, relay_factory=lambda _hh, _pp: RecordingRelay(_hh, 0)),
    ) as proxy:
        conn = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=10)
        conn.request("POST", "/v1/chat/completions", body=b"{}")
        response = conn.getresponse()
        body = response.read()

        assert response.status == 200
        assert body == b"hi"
        relay = created[0]
        # Nothing may half-close the request side before the response is read.
        for call in relay.calls[: relay.calls.index("read")]:
            assert call not in ("close_input", "close")


def test_remote_connector_command_quotes_host_as_one_argument() -> None:
    command = build_remote_command("node; touch /tmp/not-run", 8000)

    assert "'node; touch /tmp/not-run'" in command


def test_proxy_http_help_shows_generic_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(main_module, "ensure_config", lambda: None)

    result = runner.invoke(main_module.app, ["proxy", "http", "--help"])

    assert result.exit_code == 0
    assert "--allow-method" in result.stdout
    assert "--remote-host" in result.stdout
    assert "--allow-prefix" in result.stdout
    assert "--deny-method" not in result.stdout
    assert "--deny-prefix" not in result.stdout


def test_proxy_openai_help_shows_openai_wrapper_command(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(main_module, "ensure_config", lambda: None)

    result = runner.invoke(main_module.app, ["proxy", "openai", "--help"])

    assert result.exit_code == 0
    assert "--remote-port" in result.stdout
    assert "--allow-prefix" not in result.stdout


def test_proxy_tcp_help_shows_tunnel_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(main_module, "ensure_config", lambda: None)

    result = runner.invoke(main_module.app, ["proxy", "tcp", "--help"])

    assert result.exit_code == 0
    assert "--remote-port" in result.stdout
    assert "--local-port" in result.stdout
    assert "--allow-prefix" not in result.stdout


def test_ssh_run_programmatic_defaults_use_none_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_start_master_connection() -> None:
        return None

    def fake_subprocess_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(ssh_module, "start_master_connection", fake_start_master_connection)
    monkeypatch.setattr(ssh_module.subprocess, "run", fake_subprocess_run)

    result = ssh_module.run("echo ok")

    # stdout is returned as-is; callers trim when they need to.
    assert result == "ok\n"
    assert captured["kwargs"]["input"] is None


def test_start_master_connection_polls_for_delayed_control_socket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    active_checks = iter([False, False, False, True])
    sleeps: list[float] = []

    def fake_is_master_connection_active() -> bool:
        return next(active_checks)

    def fake_subprocess_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ssh_module, "is_master_connection_active", fake_is_master_connection_active)
    monkeypatch.setattr(ssh_module, "get_remote_user", lambda: "user@login")
    monkeypatch.setattr(ssh_module, "get_ssh_args", lambda: ["-S", str(tmp_path / "jz-test.sock")])
    monkeypatch.setattr(ssh_module.subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(ssh_module.time, "sleep", lambda seconds: sleeps.append(seconds))

    ssh_module.start_master_connection()

    assert sleeps == [ssh_module._MASTER_START_POLL_SECONDS, ssh_module._MASTER_START_POLL_SECONDS]


def test_relay_open_failure_surfaces_detail_in_502() -> None:
    """A failing relay factory yields a 502 carrying the root-cause detail."""

    def broken_relay(_host: str, _port: int) -> SocketRelay:
        msg = "boom fake refusal"
        raise RelayError(msg)

    config = ProxyConfig(selector="node=jzxh033", node="jzxh033", remote_host="fake-host", remote_port=8000)
    listener = RelayListener(host="fake-host", remote_port=8000, relay_factory=broken_relay)
    try:
        with ProxyServerThread(config, lambda _host, _port: listener) as proxy:
            conn = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=30)
            conn.request("GET", "/v1/models")
            resp = conn.getresponse()
            body = resp.read()
            conn.close()

            assert resp.status == 502
            assert b"boom fake refusal" in body
    finally:
        listener.close()


def test_serve_tcp_localhost_target_goes_two_hop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tunnel CLI matches the HTTP proxy's LOCALHOST_TARGETS semantics."""
    captured: dict = {}

    def fake_listener(**kwargs) -> object:
        captured.update(kwargs)

        class FakeListener:
            def __init__(self) -> None:
                self.closed = False

            def wait(self) -> None:
                return None

            def close(self) -> None:
                self.closed = True

        return FakeListener()

    monkeypatch.setattr("jz_cli.proxy.tcp.RelayListener", lambda **kw: fake_listener(**kw))
    monkeypatch.setattr(
        "jz_cli.proxy.tcp.build_config",
        lambda **kw: ProxyConfig(selector="t", node="jzxh182", remote_host="localhost", remote_port=8000),
    )

    tcp_mod.tcp(node=None, job_id=None, job_name=None)
    assert captured["host"] == "localhost"
    assert captured["inner_node"] == "jzxh182"

    # Named hosts stay one-hop from the login node.
    captured.clear()
    monkeypatch.setattr(
        "jz_cli.proxy.tcp.build_config",
        lambda **kw: ProxyConfig(selector="t", node="jzxh182", remote_host="jzxh182", remote_port=8000),
    )
    tcp_mod.tcp(node=None, job_id=None, job_name=None)
    assert captured["host"] == "jzxh182"
    assert captured["inner_node"] is None


def test_relay_listener_honors_fixed_port() -> None:
    """The tunnel CLI binds an explicit local port instead of an ephemeral one."""
    upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _EchoHandler)
    upstream.daemon_threads = True
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    try:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            fixed_port = probe.getsockname()[1]

        listener = RelayListener(host="127.0.0.1", remote_port=0, port=fixed_port, relay_factory=_LocalEchoRelay)
        try:
            assert listener.port == fixed_port
            client = socket.create_connection(("127.0.0.1", fixed_port), timeout=10)
            client.sendall(b"ping")
            assert client.recv(16) == b"ping"
            client.close()
        finally:
            listener.close()
    finally:
        upstream.shutdown()
        upstream.server_close()


class _EchoHandler(http.server.BaseHTTPRequestHandler):
    """Minimal echo server for the fixed-port tunnel test."""

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        """Reply with a tiny body."""
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        """Silence request logging."""


class _LocalEchoRelay:
    """Relay stand-in that echoes request bytes straight back."""

    def __init__(self, _host: str, _port: int) -> None:
        """Prepare a pipe that echoes whatever it receives."""
        read_fd, write_fd = os.pipe()
        self._reader = os.fdopen(read_fd, "rb")
        self._write_fd = write_fd

    @property
    def stdout(self) -> BinaryIO:
        """Return the echo reader end."""
        return self._reader

    def send_all(self, data: bytes) -> None:
        """Echo the bytes back."""
        os.write(self._write_fd, data)

    def _close_write(self) -> None:
        """Close the write end if it is still open."""
        if self._write_fd >= 0:
            fd, self._write_fd = self._write_fd, -1
            with contextlib.suppress(OSError):
                os.close(fd)

    def close_input(self) -> None:
        """Close the write end so the reader sees EOF."""
        self._close_write()

    def error_text(self, limit: int = 400) -> str:
        """Return no stderr detail: the echo relay has no subprocess."""
        return ""

    def close(self) -> None:
        """Close both pipe ends."""
        with contextlib.suppress(OSError, ValueError):
            self._reader.close()
        self._close_write()
