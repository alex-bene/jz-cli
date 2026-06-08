"""HTTP transport for SSH-backed local proxy commands."""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, cast
from urllib.parse import urlsplit

import typer

from jz_cli.ssh import run

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}
RESPONSE_HEADER_DENYLIST = HOP_BY_HOP_HEADERS | {"server", "date"}
LOCAL_HEALTH_PATH = "/_jz/health"
INNER_SSH_ARGS = ("-o", "StrictHostKeyChecking=accept-new")

JsonResponse = tuple[int, dict[str, object]]
RequestValidator = Callable[[str, str, bytes], JsonResponse | None]


class NodeResolutionError(RuntimeError):
    """Raised when the target compute node cannot be resolved."""


class RelayError(RuntimeError):
    """Raised when the SSH relay to the upstream service fails."""


def resolve_node(*, node: str | None, job_id: int | None, job_name: str | None) -> tuple[str, str]:
    """Resolve the selected compute node once at startup."""
    selectors = [node is not None, job_id is not None, job_name is not None]
    if sum(selectors) != 1:
        msg = "Provide exactly one of --node, --job-id, or --job-name."
        raise typer.BadParameter(msg)
    if node is not None:
        return f"node={node}", node
    if job_id is not None:
        return f"job_id={job_id}", _resolve_job_id(job_id)
    return f"job_name={job_name}", _resolve_job_name(job_name)


def _resolve_job_id(job_id: int) -> str:
    hostlist_cmd = f'squeue -h -j {job_id} -o %N | awk "NF {{print; exit}}"'
    hostlist = str(run(hostlist_cmd, login_shell=True)).strip()
    return _resolve_single_hostname(_expand_hostlist(hostlist), context=f"job id {job_id}")


def _resolve_job_name(job_name: str) -> str:
    quoted_name = shlex.quote(job_name)
    output = str(run(f'squeue -h -u "$USER" -n {quoted_name} -t R -o "%A|%N"', login_shell=True))
    matches: list[tuple[str, str]] = []
    for raw_line in output.splitlines():
        stripped_line = raw_line.strip()
        if not stripped_line:
            continue
        job_id, _, hostlist = stripped_line.partition("|")
        if not job_id or not hostlist:
            continue
        node = _resolve_single_hostname(_expand_hostlist(hostlist), context=f"job {job_id}")
        matches.append((job_id, node))

    if not matches:
        msg = f"No running jobs found with name '{job_name}'."
        raise NodeResolutionError(msg)
    if len(matches) > 1:
        job_ids = ", ".join(job_id for job_id, _ in matches)
        msg = f"Multiple running jobs found with name '{job_name}': {job_ids}. Use --job-id instead."
        raise NodeResolutionError(msg)
    return matches[0][1]


def _expand_hostlist(hostlist: str) -> list[str]:
    if not hostlist:
        return []
    output = str(run(f"scontrol show hostnames {shlex.quote(hostlist)}", login_shell=True))
    return [line.strip() for line in output.splitlines() if line.strip()]


def _resolve_single_hostname(hostnames: list[str], *, context: str) -> str:
    if not hostnames:
        msg = f"No node resolved for {context}."
        raise NodeResolutionError(msg)
    if len(hostnames) > 1:
        joined = ", ".join(hostnames)
        msg = f"{context} resolves to multiple nodes ({joined}). Pass --node explicitly."
        raise NodeResolutionError(msg)
    return hostnames[0]


@dataclass
class ProxyConfig:
    """Shared runtime settings for the generic HTTP proxy."""

    selector: str
    node: str
    remote_host: str
    remote_port: int
    verbose: bool = False
    allow_methods: tuple[str, ...] = ()
    allow_prefixes: tuple[str, ...] = ()
    extra_validator: RequestValidator | None = None


def validate_request(config: ProxyConfig, method: str, path: str, body: bytes) -> JsonResponse | None:
    """Apply the configured method/path checks plus any extra validation."""
    normalized_method = method.upper()
    if config.allow_methods and normalized_method not in config.allow_methods:
        return 405, {"error": f"Unsupported method: {normalized_method}"}
    if config.allow_prefixes and not any(path.startswith(prefix) for prefix in config.allow_prefixes):
        return 404, {"error": f"Unsupported path: {path}"}
    if config.extra_validator is None:
        return None
    return config.extra_validator(normalized_method, path, body)


def parse_http_response(raw: bytes) -> tuple[int, list[tuple[str, str]], bytes]:
    """Split a curl `-D -` response into status, headers, and body."""
    remaining = raw
    while True:
        header_blob, separator, body = remaining.partition(b"\r\n\r\n")
        if not separator:
            header_blob, separator, body = remaining.partition(b"\n\n")
        if not separator:
            msg = "Upstream response did not contain HTTP headers."
            raise ValueError(msg)

        header_lines = header_blob.decode("iso-8859-1").splitlines()
        if not header_lines:
            msg = "Upstream response headers were empty."
            raise ValueError(msg)

        status_parts = header_lines[0].split(" ", 2)
        if len(status_parts) < 2 or not status_parts[1].isdigit():
            msg = f"Invalid upstream status line: {header_lines[0]}"
            raise ValueError(msg)

        status_code = int(status_parts[1])
        if 100 <= status_code < 200:
            remaining = body
            continue

        headers: list[tuple[str, str]] = []
        for line in header_lines[1:]:
            name, separator, value = line.partition(":")
            if separator:
                headers.append((name.strip(), value.strip()))
        return status_code, headers, body


class ProxyHandler(BaseHTTPRequestHandler):
    """Handle inbound local HTTP requests and relay them over SSH."""

    server_version = "jz-http-proxy/0.1"

    @property
    def config(self) -> ProxyConfig:
        """Return the proxy config shared by the HTTP server."""
        return cast("ProxyConfig", self.server.config)

    def log_message(self, message_format: str, *args: object) -> None:
        """Emit standard HTTP server logs only when verbose mode is enabled."""
        if self.config.verbose:
            super().log_message(message_format, *args)

    def _handle(self) -> None:
        path = urlsplit(self.path).path
        if path == LOCAL_HEALTH_PATH:
            self._send_json(
                200,
                {
                    "status": "ok",
                    "selector": self.config.selector,
                    "remote_host": self.config.remote_host,
                    "remote_port": self.config.remote_port,
                },
            )
            return

        body = self._read_body()
        error = validate_request(self.config, self.command, path, body)
        if error is not None:
            status, payload = error
            self._send_json(status, payload)
            return

        try:
            status, headers, upstream_body = self._relay_request(body)
        except RelayError as exc:
            self._send_json(502, {"error": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001
            self._send_json(502, {"error": f"Proxy relay failed: {exc}"})
            return

        response_headers = [(name, value) for name, value in headers if name.lower() not in RESPONSE_HEADER_DENYLIST]
        response_headers.append(("Content-Length", str(len(upstream_body))))
        self._send_response(status, response_headers, upstream_body)

    def _read_body(self) -> bytes:
        content_length = self.headers.get("Content-Length")
        if content_length is None:
            return b""
        return self.rfile.read(int(content_length))

    def _relay_request(self, body: bytes) -> tuple[int, list[tuple[str, str]], bytes]:
        config = self.config
        headers = {name: value for name, value in self.headers.items() if name.lower() not in HOP_BY_HOP_HEADERS}
        url = f"http://{config.remote_host}:{config.remote_port}{self.path}"
        curl_args = ["curl", "-sS", "-D", "-", "-X", self.command]
        for name, value in headers.items():
            curl_args.extend(["-H", f"{name}: {value}"])
        if body:
            curl_args.extend(["--data-binary", "@-"])
        curl_args.append(url)
        remote_cmd = shlex.join(["ssh", *INNER_SSH_ARGS, config.node, shlex.join(curl_args)])
        result = run(remote_cmd, return_result=True, capture_output=True, text=False, check=False, stdin_data=body)
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            msg = stderr or "Unknown SSH relay error."
            raise RelayError(msg)
        return parse_http_response(result.stdout)

    def _send_response(self, status: int, headers: list[tuple[str, str]], body: bytes) -> None:
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self._send_response(
            status, [("Content-Type", "application/json"), ("Content-Length", str(len(encoded)))], encoded
        )


for _method in ("DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"):
    setattr(ProxyHandler, f"do_{_method}", ProxyHandler._handle)


def serve_proxy(config: ProxyConfig, *, bind_host: str, local_port: int) -> None:
    """Start a local HTTP proxy using the provided generic settings."""
    typer.echo(f"Resolved remote node: {config.node}")
    typer.echo(
        f"Serving local proxy on http://{bind_host}:{local_port} -> http://{config.remote_host}:{config.remote_port}"
    )

    server = ThreadingHTTPServer((bind_host, local_port), ProxyHandler)
    server.config = config
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        typer.echo("\nStopping proxy.")
    finally:
        server.server_close()


def serve(
    node: str | None = typer.Option(None, "--node", help="Specific compute node to target."),
    job_id: int | None = typer.Option(None, "--job-id", help="Resolve the compute node from a Slurm job id."),
    job_name: str | None = typer.Option(None, "--job-name", help="Resolve the compute node from a running job name."),
    local_port: int = typer.Option(8000, "--local-port", help="Local port to bind the proxy server to."),
    bind_host: str = typer.Option("127.0.0.1", "--bind-host", help="Local host/IP address to bind to."),
    remote_host: str = typer.Option("localhost", "--remote-host", help="Remote host or IP address to target."),
    remote_port: int = typer.Option(8000, "--remote-port", help="Remote port to forward HTTP requests to."),
    allow_method: list[str] | None = typer.Option(
        None, "--allow-method", help="Repeatable HTTP method allowlist. If omitted, all methods are allowed."
    ),
    allow_prefix: list[str] | None = typer.Option(
        None, "--allow-prefix", help="Repeatable path prefix allowlist. If omitted, all paths are allowed."
    ),
    verbose: bool = typer.Option(False, "--verbose/--no-verbose", help="Enable verbose proxy logging."),
) -> None:
    """Serve a local HTTP proxy that relays requests through SSH."""
    try:
        selector, resolved_node = resolve_node(node=node, job_id=job_id, job_name=job_name)
    except NodeResolutionError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc

    config = ProxyConfig(
        selector=selector,
        node=resolved_node,
        remote_host=remote_host,
        remote_port=remote_port,
        verbose=verbose,
        allow_methods=tuple({method.strip().upper() for method in allow_method or () if method.strip()}),
        allow_prefixes=tuple({prefix.strip() for prefix in allow_prefix or () if prefix.strip()}),
    )
    serve_proxy(config, bind_host=bind_host, local_port=local_port)
