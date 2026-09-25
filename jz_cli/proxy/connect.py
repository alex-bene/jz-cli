"""Duplex byte transport to a TCP service on a compute node over one SSH channel.

Why an exec channel: Jean Zay refuses SSH port forwarding to compute nodes
("administratively prohibited"), so instead of a local `-L` listener we run a
tiny stdlib relay as the remote command of the persistent login-node connection.
That relay opens a plain TCP socket to the compute node and pumps bytes in both
directions, which forwards SSE and websocket traffic incrementally.

The login node reaches compute-node ports directly (plain TCP), so the default
is one hop: the relay dials `host:remote_port` from the login node. Only
**loopback-bound** services need the two-hop form `--remote-host localhost`
(see INNER_SSH_ARGS below): 127.0.0.1 on the node is unreachable from the login
node by definition, and a nested `ssh <node>` dials it from inside.
(Historical note: the login node's instant 502 against node ports that once
looked like a network intercept is an env-proxy artifact — IDRIS login shells
export http_proxy, and env-honoring tools like curl route through that Squid,
which refuses node addresses. The raw-socket relay never consults the
environment.)

Two details are load-bearing, both learned the hard way:

- The relay reads stdin with `read1`, never `read`. `read(65536)` blocks until
  the buffer is full or EOF, which silently turns every streamed response into a
  buffered one.
- On stdin EOF the relay half-closes the socket (`shutdown(SHUT_WR)`) instead of
  closing it, so the remote sees an end-of-request while we keep reading its
  reply. When the ssh channel dies instead, the relay notices via EPIPE on its
  next stdout write and exits, so no orphaned relay is left behind.

There is deliberately no watchdog here: every caller reads the response to a
known end (HTTP framing, or EOF for the raw tunnel), so a relay never outlives
its reader.

INNER_SSH_ARGS + the `inner_node` parameter implement that two-hop form (kept
opt-in because it costs one extra exec): with ControlMaster=auto +
ControlPersist=4h on the login node, the inner handshake is paid once per node
across all relay channels.
"""

from __future__ import annotations

import contextlib
import errno
import functools
import shlex
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from typing import BinaryIO, Protocol

import typer

from jz_cli.ssh import get_remote_user, get_ssh_args, start_master_connection

READ_SIZE = 65536
RELAY_IDLE_TIMEOUT_SECONDS = 900
ACCEPT_BACKLOG = 64

# Two-hop inner hop (login node -> compute node) for localhost-bound services;
# the inner ControlMaster is reused across relay channels so the inner SSH
# handshake is paid once per node.
INNER_SSH_ARGS = (
    "-o",
    "StrictHostKeyChecking=accept-new",
    "-o",
    "ControlMaster=auto",
    "-o",
    "ControlPersist=4h",
    "-o",
    "ControlPath=~/.ssh/jz-cli-inner-%C",
)


class RelayError(RuntimeError):
    """Raised when the SSH relay to the remote service fails."""


# Runs on the LOGIN node: connect to the target service and pump stdin <-> socket.
#
# The two directions must be able to finish independently: when the request side
# reaches EOF we half-close the socket and keep reading the response. Only an
# actual failure sets `abort`, because a shared "stop" flag would kill the
# response pump the moment the request side finished, truncating every streamed
# reply (the service then dies with EPIPE on its next write).
CONNECTOR_SRC = r"""
import os, socket, sys, threading

# Kill core dumps for this process: the relay inherits the shell's
# `ulimit -c unlimited` on Jean Zay, and any future crash would otherwise
# write a ~300MB core file to $HOME (found live when the pre-2 connector
# aborted at finalization on every relay close). Only the relay is affected —
# real workloads keep their cores for post-mortem debugging.
try:
    import resource
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
except (ImportError, OSError, ValueError):
    # Non-Linux remote or a hardening-raised floor; not worth failing over.
    pass

host, port = sys.argv[1], int(sys.argv[2])
try:
    sock = socket.create_connection((host, port), timeout=30)
except OSError as exc:
    # One clean line, never a traceback: ssh relays this stderr back and the
    # proxy presents it verbatim in 502 bodies.
    print(f"connector connect failed: {exc.__class__.__name__}: {exc}", file=sys.stderr)
    sys.exit(1)
sock.settimeout(float(sys.argv[3]))
sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
abort = threading.Event()


def client_to_socket() -> None:
    try:
        while not abort.is_set():
            # Raw fd read, never sys.stdin.buffer.read1: a daemon thread blocked
            # inside the buffered reader holds its lock, and interpreter
            # finalization then aborts with `Fatal Python error:
            # _enter_buffered_busy` as soon as the response is done. os.read has
            # identical semantics (returns on any data, b"" on EOF) and no lock.
            data = os.read(0, 65536)
            if not data:
                try:
                    sock.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
                return
            sock.sendall(data)
    except Exception as exc:
        print(f"connector upload failed: {exc}", file=sys.stderr)
        abort.set()


def socket_to_client() -> None:
    try:
        while not abort.is_set():
            data = sock.recv(65536)
            if not data:
                return
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
    except Exception as exc:
        print(f"connector download failed: {exc}", file=sys.stderr)
        abort.set()
    finally:
        try:
            sock.close()
        except OSError:
            pass


threading.Thread(target=client_to_socket, daemon=True).start()
socket_to_client()
"""


def build_remote_command(host: str, remote_port: int, *, inner_node: str | None = None) -> str:
    """Build a shell-safe command for the login-node connector.

    `inner_node` enables the two-hop form for services bound to the node's
    loopback (e.g. stock Jupyter/TensorBoard): 127.0.0.1 on the node is
    unreachable from the login node, so the connector runs ON the node via an
    inner `ssh <node>` whose ControlMaster is reused across relay execs (the
    handshake is paid once per node, not once per relay).
    """
    if not 1 <= remote_port <= 65535:
        msg = f"Invalid remote port: {remote_port}"
        raise RelayError(msg)
    target = "localhost" if inner_node else host
    connector_cmd = shlex.join(
        ["python3", "-c", CONNECTOR_SRC, target, str(remote_port), str(RELAY_IDLE_TIMEOUT_SECONDS)]
    )
    if inner_node is None:
        return connector_cmd
    return shlex.join(["ssh", *INNER_SSH_ARGS, inner_node, connector_cmd])


class Relay(Protocol):
    """Duplex stream interface shared by HTTP and raw TCP forwarding."""

    @property
    def stdout(self) -> BinaryIO:
        """Return the remote-to-local byte stream."""
        ...

    def send_all(self, data: bytes) -> None:
        """Write bytes to the remote service."""
        ...

    def close_input(self) -> None:
        """Half-close the local-to-remote direction."""
        ...

    def close(self) -> None:
        """Close both relay directions."""
        ...

    def error_text(self, limit: int = 400) -> str:
        """Return the tail of the relay's stderr, for error reporting."""
        ...


class RelayStream:
    """One duplex pipe to ``host:remote_port`` over a dedicated SSH exec channel.

    `host` is resolved from the login node, so it is normally the compute node
    itself; an arbitrary host reachable from the login node also works.

    Read from :attr:`stdout` (with ``read1``), or use :meth:`send_all` and
    :meth:`close_input` to write the request side.
    """

    def __init__(self, *, host: str, remote_port: int, inner_node: str | None = None) -> None:
        """Start the login-node relay that connects to `host:remote_port`.

        `inner_node` wraps the relay in an inner `ssh <inner_node>` so the
        service is reached from inside that node (localhost-bound services).
        """
        try:
            start_master_connection()
        except typer.Exit as exc:
            # Already reported by the SSH layer. Convert (don't pass through):
            # typer.Exit IS a RuntimeError, and letting it escape here would kill
            # the accept thread and silently stop the proxy from accepting.
            msg = "Could not start the SSH master connection to open the relay."
            raise RelayError(msg) from exc
        except Exception as exc:
            msg = f"Could not start the SSH master connection: {exc}"
            raise RelayError(msg) from exc
        remote_cmd = build_remote_command(host, remote_port, inner_node=inner_node)
        # stderr goes to a temp file rather than a pipe: nobody reads it while the
        # stream is live, and an unread pipe can block the ssh process. It has to
        # outlive this constructor, so a context manager does not apply.
        self._stderr = tempfile.TemporaryFile()  # noqa: SIM115
        self._process = subprocess.Popen(  # noqa: S603
            ["ssh", *get_ssh_args(), get_remote_user(), remote_cmd],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
        )
        if self._process.stdin is None or self._process.stdout is None:  # pragma: no cover - Popen contract
            msg = "Failed to open pipes for the SSH relay."
            raise RelayError(msg)
        # A spawn-level failure (bad ssh options, authentication aborted) shows up
        # instantly; report it now instead of as a confusing downstream EOF.
        if self._process.poll() is not None:
            detail = self.error_text() or "unknown error"
            self.close()
            msg = f"SSH relay exited immediately: {detail}"
            raise RelayError(msg)

    @property
    def stdout(self) -> BinaryIO:
        """Return the remote-to-local byte stream. Read it with ``read1``."""
        stdout = self._process.stdout
        if stdout is None:  # pragma: no cover - closed after close()
            msg = "Relay output is already closed."
            raise RelayError(msg)
        return stdout

    def send_all(self, data: bytes) -> None:
        """Write request bytes to the remote service."""
        stdin = self._process.stdin
        if stdin is None:  # pragma: no cover - closed after close_input()
            msg = "Relay input is already closed."
            raise RelayError(msg)
        try:
            stdin.write(data)
            stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            msg = f"SSH relay closed while sending the request: {exc}"
            raise RelayError(msg) from exc

    def close_input(self) -> None:
        """Half-close the request side so the remote sees an end of request."""
        stdin = self._process.stdin
        if stdin is None:
            return
        with contextlib.suppress(OSError, ValueError):
            stdin.close()

    def error_text(self, limit: int = 400) -> str:
        """Return the tail of the relay's stderr, for error reporting."""
        try:
            self._stderr.seek(0)
            data = self._stderr.read()
        except (OSError, ValueError):
            return ""
        return data.decode("utf-8", errors="replace").strip()[-limit:]

    def close(self) -> None:
        """Tear down the relay, killing the ssh channel if it is still alive."""
        self.close_input()
        stdout = self._process.stdout
        if stdout is not None:
            with contextlib.suppress(OSError):
                stdout.close()
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=5)
        with contextlib.suppress(OSError):
            self._stderr.close()


RelayFactory = Callable[[str, int], Relay]


class RelayListener:
    """Local TCP server that forwards each accepted connection to the target.

    HTTP clients and connection pools (aiohttp) speak to ordinary localhost
    sockets; every accepted connection gets a dedicated SSH relay channel with
    the same `read1`/half-close semantics as :class:`RelayStream`. The pump is
    scoped to the upstream connection, not the request, so an HTTP connection
    pool can keep-alive over one SSH channel. The raw TCP tunnel CLI reuses the
    same machinery on a fixed port.

    `inner_node` passes the two-hop semantics through to every relay channel:
    when set (e.g. to the resolved node), the service is reached from inside
    that node. `host` names the destination as seen from the last hop.
    """

    def __init__(
        self,
        *,
        host: str,
        remote_port: int,
        inner_node: str | None = None,
        relay_factory: RelayFactory | None = None,
        bind_host: str = "127.0.0.1",
        port: int = 0,
        verbose: bool = False,
    ) -> None:
        """Bind a local port (ephemeral by default) and start accepting relays."""
        # inner_node must take precedence over an explicit relay_factory: when
        # set, every channel needs the nested-ssh wrapper, and the or-expression
        # used to short-circuit on the (always truthy) default factory,
        # silently dropping the two-hop semantics (found live vs a vLLM job).
        self._relay_factory = (
            functools.partial(ssh_relay_stream_with_inner, inner_node)
            if inner_node
            else (relay_factory or ssh_relay_stream)
        )
        # Derive the socket family from the bind address: AF_INET6 makes
        # --bind-host ::1 work; the AF_INET default path is unaffected.
        family = socket.AF_INET6 if ":" in bind_host else socket.AF_INET
        self._host = host
        self._remote_port = remote_port
        self._verbose = verbose
        self._relays: list[Relay] = []
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._last_error: tuple[float, str] | None = None
        self._accept_backlog: socket.socket = socket.socket(family, socket.SOCK_STREAM)
        self._accept_backlog.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._accept_backlog.bind((bind_host, port))
        self._accept_backlog.listen(ACCEPT_BACKLOG)
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()

    @property
    def port(self) -> int:
        """Return the local port the listener is bound to."""
        return self._accept_backlog.getsockname()[1]

    def wait(self) -> None:
        """Block until close(); used by the blocking raw-tunnel CLI."""
        self._closed.wait()

    def last_error(self, max_age_seconds: float = 30.0) -> str:
        """Return the most recent relay failure detail, if it is recent enough."""
        if self._last_error is None:
            return ""
        recorded_at, detail = self._last_error
        return detail if time.monotonic() - recorded_at <= max_age_seconds else ""

    def _accept_loop(self) -> None:
        while True:
            try:
                conn, _addr = self._accept_backlog.accept()
            except OSError as exc:
                if self._closed.is_set() or exc.errno == errno.EBADF:
                    # The listener socket was closed on shutdown; stop accepting.
                    return
                # Transient accept failure (e.g. ECONNABORTED when a client
                # resets mid-handshake): log and keep serving rather than
                # silently killing the proxy forever.
                typer.echo(f"accept failed ({exc.__class__.__name__}: {exc}); continuing", err=True)
                time.sleep(0.05)
                continue
            # Forwarded byte streams race the delayed-ACK timer otherwise: without
            # NODELAY, Nagle stalls each small forwarded segment by ~40 ms.
            with contextlib.suppress(OSError):
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            try:
                relay = self._relay_factory(self._host, self._remote_port)
            except (RelayError, OSError) as exc:
                typer.echo(f"Could not open relay to {self._host}:{self._remote_port}: {exc}", err=True)
                with self._lock:
                    self._last_error = (time.monotonic(), str(exc))
                conn.close()
                continue
            if self._closed.is_set():
                # close() ran while the factory was in flight (it can take a
                # fresh-master handshake); its snapshot missed this relay, so
                # tear it down here instead of leaking the channel until the
                # 900s connector timeout.
                with contextlib.suppress(RelayError, OSError, ValueError):
                    relay.close()
                conn.close()
                return
            with self._lock:
                self._relays.append(relay)
            if self._verbose:
                typer.echo(f"relay open -> {self._host}:{self._remote_port}")
            pump_thread = threading.Thread(target=self._pump, args=(conn, relay), daemon=True)
            pump_thread.start()

    def _pump(self, conn: socket.socket, relay: Relay) -> None:
        local_name = f"{self._host}:{self._remote_port}"
        upload = threading.Thread(target=self._pump_upload, args=(conn, relay), daemon=True)
        upload.start()
        try:
            stream = relay.stdout
            while True:
                data = stream.read1(READ_SIZE)
                if not data:
                    self._relay_ended(relay, local_name)
                    return
                conn.sendall(data)
        except (RelayError, OSError, ValueError) as exc:
            if not self._closed.is_set():
                self._relay_ended(relay, local_name, exc)
        finally:
            with contextlib.suppress(OSError):
                conn.shutdown(socket.SHUT_RDWR)
            upload.join(timeout=5)
            relay.close()
            with contextlib.suppress(OSError):
                conn.close()
            with self._lock:
                if relay in self._relays:
                    self._relays.remove(relay)
            if self._verbose:
                typer.echo(f"relay closed -> {local_name}")

    def _relay_ended(self, relay: Relay, local_name: str, exc: Exception | None = None) -> None:
        """Distinguish a relay failure (worth reporting) from a client hangup."""
        detail = relay.error_text().strip()
        if detail:
            # The connector's own stderr is the root cause (e.g. 'connection
            # refused'); surface it instead of a bare reset.
            typer.echo(f"Relay to {local_name} failed: {detail}", err=True)
            with self._lock:
                self._last_error = (time.monotonic(), detail)
        elif exc is None:
            return  # Orderly EOF: the response reached its end. Nothing to report.
        else:
            with self._lock:
                self._last_error = (time.monotonic(), str(exc))
            # A broken local connection is a normal client disconnect: verbose only.
            if self._verbose:
                typer.echo(f"Relay download failed for {local_name}: {exc}", err=True)

    def _pump_upload(self, conn: socket.socket, relay: Relay) -> None:
        try:
            while True:
                data = conn.recv(READ_SIZE)
                if not data:
                    return
                relay.send_all(data)
        except (RelayError, OSError, ValueError) as exc:
            # Mostly normal client disconnects mid-request: verbose only.
            if self._verbose:
                typer.echo(f"Relay upload failed for {self._host}:{self._remote_port}: {exc}", err=True)
        finally:
            # Half-close so the remote service still sees an orderly end of input.
            with contextlib.suppress(RelayError, OSError, ValueError):
                relay.close_input()

    def close(self) -> None:
        """Stop accepting and tear down any active relay channels."""
        self._closed.set()
        with contextlib.suppress(OSError):
            self._accept_backlog.close()
        with self._lock:
            relays = list(self._relays)
            self._relays.clear()
        for relay in relays:
            relay.close()
        # The accept thread exits on the closed socket on its own.
        if self._accept_thread is not threading.current_thread():
            self._accept_thread.join(timeout=5)


def relay_listener(host: str, remote_port: int, *, verbose: bool = False) -> RelayListener:
    """Open a local relay listener to `host:remote_port`. Used as the default."""
    return RelayListener(host=host, remote_port=remote_port, verbose=verbose)


def ssh_relay_stream(host: str, remote_port: int) -> RelayStream:
    """Open a relay stream over SSH. Used as the default factory by the proxies."""
    return RelayStream(host=host, remote_port=remote_port)


def ssh_relay_stream_with_inner(inner_node: str, host: str, remote_port: int) -> RelayStream:
    """Open a relay stream reaching the service from inside `inner_node`."""
    return RelayStream(host=host, remote_port=remote_port, inner_node=inner_node)
