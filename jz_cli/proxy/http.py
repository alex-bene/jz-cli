"""Streaming HTTP proxy backed by an SSH relay to a remote service.

Relay mechanics live in :mod:`jz_cli.proxy.connect`: a local TCP listener pumps
each accepted connection to the target service over one SSH exec channel,
relayed by a tiny threaded subprocess relay.

HTTP framing, keep-alive pooling, chunked bodies, and `Expect: 100-continue`
are handled by aiohttp on an ordinary localhost socket, so one SSH channel
serves every request on a pooled upstream connection instead of one channel
per request.
"""

from __future__ import annotations

import asyncio
import posixpath
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlsplit

import typer
from aiohttp import ClientError, ClientSession, ClientTimeout, TCPConnector, web
from multidict import CIMultiDict, CIMultiDictProxy

from .connect import RelayListener, relay_listener
from .nodes import NodeResolutionError, resolve_node

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from aiohttp.typedefs import Handler

LOCAL_HEALTH_PATH = "/_jz/health"
UPSTREAM_KEEPALIVE_TIMEOUT_SECONDS = 75
UPSTREAM_SOCK_READ_TIMEOUT_SECONDS = 900
MAX_CONNECTIONS = 256

# Cap on one request BODY (not the conversation: each proxied turn replays the
# whole history, so this limits the largest single turn's replay). aiohttp's
# library default is 1 MiB, which a single base64-inflated image attachment
# exceeds — mirror the 64 MiB the v0/v1 benchmark proxies allow and make it
# controllable per proxy initialization (CLI: --max-body-mb).
DEFAULT_MAX_BODY_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_BODY_MB = DEFAULT_MAX_BODY_BYTES // (1024 * 1024)

# Per-hop headers describe one connection and must not cross a proxy boundary.
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
# aiohttp computes framing for each hop it speaks: it may stream the request
# body chunked, so the request's Content-Length must be recomputed, not copied.
# Response bytes are passed through verbatim, so responses KEEP the upstream
# Content-Length (only Transfer-Encoding, already hop-by-hop, is reframed).
STREAM_REFRAMING_HEADERS = {"content-length"}

JsonResponse = tuple[HTTPStatus, dict[str, str]]
CLIENT_SESSION_KEY: web.AppKey[ClientSession] = web.AppKey("client_session", ClientSession)


@dataclass
class ProxyConfig:
    """Runtime settings for the generic HTTP proxy."""

    selector: str
    node: str
    remote_host: str
    remote_port: int
    verbose: bool = False
    allow_methods: tuple[str, ...] = ()
    allow_prefixes: tuple[str, ...] = ()
    allow_paths: tuple[str, ...] = ()
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES


def policy_path(target: str) -> str:
    """Return the URL path used for allowlist matching, decoded and dot-normalized.

    `.` (or percent-encoded `%2e`) segments must NOT pass a prefix check that
    the upstream would then see differently: the proxy recomposes the upstream
    URL and both yarl and typical servers normalize dot segments, so
    '/v1/../../admin' or '/v1/%2e%2e/admin' would match the '/v1/' prefix
    here but reach the upstream as '/admin'. Validating the decoded,
    dot-normalized path closes that bypass; it may reject exotic paths that
    legitimately encode '/' inside a segment (none in machine APIs).
    """
    path = posixpath.normpath(unquote(urlsplit(target).path))
    # normpath preserves '//' (and collapses beyond-root dots to '/'); force a
    # single leading slash so a protocol-relative bypass cannot survive.
    return "/" + path.lstrip("/")


def validate_request(config: ProxyConfig, method: str, path: str) -> JsonResponse | None:
    """Apply configured method and path checks."""
    normalized_method = method.upper()
    if config.allow_methods and normalized_method not in config.allow_methods:
        return HTTPStatus.METHOD_NOT_ALLOWED, {"error": f"Unsupported method: {normalized_method}"}
    if (config.allow_prefixes or config.allow_paths) and not (
        path in config.allow_paths or path.startswith(tuple(config.allow_prefixes))
    ):
        return HTTPStatus.NOT_FOUND, {"error": f"Unsupported path: {path}"}
    return None


def host_header(host: str, port: int) -> str:
    """Build the upstream Host header: bracket IPv6, omit the default port."""
    host_part = f"[{host}]" if ":" in host else host
    return host_part if port == 80 else f"{host_part}:{port}"


def _filter_hop_by_hop(headers: CIMultiDictProxy[str]) -> CIMultiDict[str]:
    """Drop per-hop headers, including names nominated by Connection headers."""
    nominated = {
        token.strip().lower()
        for name, value in headers.items()
        if name.lower() == "connection"
        for token in value.split(",")
        if token.strip()
    }
    denied = HOP_BY_HOP_HEADERS | nominated
    return CIMultiDict((name, value) for name, value in headers.items() if name.lower() not in denied)


def make_client_session() -> ClientSession:
    """Build the upstream HTTP client used by the proxy.

    Retries are disabled: on a relay-reported failure the local handshake has
    already completed, so aiohttp sees ClientOSError/ServerDisconnectedError
    and otherwise relays idempotent requests a second time (found live — one
    dead-upstream GET opened two relay channels and logged the connector's
    root cause twice). aiohttp 3.14 has no public knob; `_retry_connection`
    is the only switch.
    """
    session = ClientSession(
        timeout=ClientTimeout(total=None, sock_read=UPSTREAM_SOCK_READ_TIMEOUT_SECONDS),
        connector=TCPConnector(keepalive_timeout=UPSTREAM_KEEPALIVE_TIMEOUT_SECONDS, limit=MAX_CONNECTIONS),
        auto_decompress=False,
    )
    session._retry_connection = False
    return session


def make_app(config: ProxyConfig, listener: RelayListener) -> web.Application:
    """Build the aiohttp proxy application for one upstream target."""
    upstream_base = f"http://127.0.0.1:{listener.port}"

    async def client_ctx(app: web.Application) -> AsyncIterator[None]:
        """Own one pooled ClientSession per app; pump connections are reused."""
        session = make_client_session()
        app[CLIENT_SESSION_KEY] = session
        yield
        await session.close()

    async def health(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "ok",
                "selector": config.selector,
                "remote_host": config.remote_host,
                "remote_port": config.remote_port,
            }
        )

    async def forward(request: web.Request) -> web.StreamResponse:
        path = policy_path(request.rel_url.raw_path)
        if error := validate_request(config, request.method, path):
            status, payload = error
            return web.json_response(payload, status=status)

        if request.headers.get("Upgrade") is not None:
            return web.json_response(
                {"error": "HTTP upgrades are not supported; use 'jz proxy tcp'."}, status=HTTPStatus.NOT_IMPLEMENTED
            )

        headers = _filter_hop_by_hop(request.headers)
        for name in STREAM_REFRAMING_HEADERS:
            headers.pop(name, None)
        # The upstream must see the remote destination, not our local relay port.
        headers["Host"] = host_header(config.remote_host, config.remote_port)

        session: ClientSession = app[CLIENT_SESSION_KEY]
        # Body handling: known Content-Length bodies are buffered so aiohttp
        # re-emits identity framing upstream — streaming a length-unknown
        # reader would re-frame them as chunked, which chunk-unaware upstreams
        # misread (and the leftover chunk framing then desyncs the pooled
        # connection). Only chunked/unknown-length bodies stay streamed.
        if request.content_length is not None:
            body: bytes | asyncio.StreamReader | None = await request.read()
        elif request.can_read_body:
            body = request.content
        else:
            body = None
        try:
            # Only attach a body when there is one: an empty chunked body
            # (`0\r\n\r\n`) would be misparsed as the next request on a
            # keep-alive upstream connection.
            async with session.request(
                request.method, f"{upstream_base}{request.rel_url.raw_path_qs}", headers=headers, data=body
            ) as upstream:
                response = web.StreamResponse(status=upstream.status, headers=_filter_hop_by_hop(upstream.headers))
                await response.prepare(request)
                try:
                    if request.method != "HEAD":
                        async for chunk in upstream.content.iter_any():
                            await response.write(chunk)
                    await response.write_eof()
                except (ClientError, asyncio.TimeoutError, OSError):
                    # Local client disconnected or upstream died mid-response;
                    # nothing left to report at this point.
                    pass
                return response
        except (ClientError, asyncio.TimeoutError, OSError) as exc:
            # Prefer the connector's root-cause stderr (e.g. 'connection
            # refused') over aiohttp's generic ClientError.
            message = f"Proxy relay failed: {listener.last_error() or exc}"
            typer.echo(message, err=True)
            return web.json_response({"error": message}, status=HTTPStatus.BAD_GATEWAY)

    @web.middleware
    async def reject_connect(request: web.Request, handler: Handler) -> web.StreamResponse:
        """HTTP CONNECT is a raw tunnel: point it at `jz proxy tcp`."""
        if request.method == "CONNECT":
            return web.json_response(
                {"error": "HTTP CONNECT is not supported; use 'jz proxy tcp'."}, status=HTTPStatus.NOT_IMPLEMENTED
            )
        return await handler(request)

    app = web.Application(middlewares=[reject_connect], client_max_size=config.max_body_bytes)
    app.cleanup_ctx.append(client_ctx)
    app.router.add_get(LOCAL_HEALTH_PATH, health)
    app.router.add_route("*", "/{tail:.*}", forward)
    return app


async def start_new_site(runner: web.AppRunner, bind_host: str, local_port: int) -> int:
    """Start the runner on a TCP site and return the actual bound port."""
    await runner.setup()
    site = web.TCPSite(runner, bind_host, local_port, reuse_address=True)
    await site.start()
    if not runner.addresses:
        msg = "Proxy server did not bind to a socket."
        raise RuntimeError(msg)
    return runner.addresses[0][1]


LOCALHOST_TARGETS = ("localhost", "127.0.0.1", "::1")


def build_proxy(
    config: ProxyConfig, *, listener_factory: Callable[[str, int], RelayListener] | None = None
) -> tuple[web.AppRunner, RelayListener]:
    """Create the runner plus its relay listener without serving."""
    # An injected factory overrides the whole listener (tests, benchmarks).
    if listener_factory is not None:
        listener = listener_factory(config.remote_host, config.remote_port)
    elif config.remote_host in LOCALHOST_TARGETS:
        # localhost targets are made from INSIDE the compute node (two-hop):
        # for services bound to the node's loopback only. (Note: the login node
        # usually REACHES node ports fine over plain TCP — the infamous instant
        # 502 against node:8000 is an env-proxy artifact: IDRIS login shells
        # export http_proxy=prodprox.idris.fr:3128, and env-honoring tools like
        # curl ask that Squid, which refuses node addresses. Our raw-socket
        # relay never consults env proxies.)
        listener = RelayListener(
            host="localhost", remote_port=config.remote_port, inner_node=config.node, verbose=config.verbose
        )
    else:
        listener = relay_listener(config.remote_host, config.remote_port, verbose=config.verbose)
    return web.AppRunner(make_app(config, listener), access_log=None), listener


async def serve_async(
    config: ProxyConfig,
    *,
    bind_host: str,
    local_port: int,
    listener_factory: Callable[[str, int], RelayListener] | None = None,
) -> None:
    """Run the proxy until cancelled, then tear everything down."""
    try:
        runner, listener = build_proxy(config, listener_factory=listener_factory)
    except (OSError, ValueError) as exc:
        # Mostly a failed relay-listener bind; nothing to clean up yet.
        typer.echo(f"Could not start the relay listener: {exc}", err=True)
        raise typer.Exit(1) from exc
    try:
        try:
            local_port_bound = await start_new_site(runner, bind_host, local_port)
        except OSError as exc:
            # Port already in use, bad bind address, ...
            typer.echo(f"Could not bind {bind_host}:{local_port}: {exc}", err=True)
            raise typer.Exit(1) from exc
        typer.echo(f"Resolved remote node: {config.node} ({config.selector})")
        typer.echo(
            f"Serving local proxy on http://{bind_host}:{local_port_bound} -> "
            f"http://{config.remote_host}:{config.remote_port}"
        )
        await asyncio.Event().wait()  # Sleep forever; exit via KeyboardInterrupt.
    finally:
        try:
            await runner.cleanup()
        finally:
            listener.close()


def serve_proxy(
    config: ProxyConfig,
    *,
    bind_host: str,
    local_port: int,
    listener_factory: Callable[[str, int], RelayListener] | None = None,
) -> None:
    """Start a local HTTP proxy using the provided settings (blocking)."""
    try:
        asyncio.run(serve_async(config, bind_host=bind_host, local_port=local_port, listener_factory=listener_factory))
    except KeyboardInterrupt:
        typer.echo("\nStopping proxy.")


def build_config(
    *,
    node: str | None,
    job_id: int | None,
    job_name: str | None,
    remote_host: str | None,
    remote_port: int,
    verbose: bool,
    allow_methods: tuple[str, ...] = (),
    allow_prefixes: tuple[str, ...] = (),
    allow_paths: tuple[str, ...] = (),
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
) -> ProxyConfig:
    """Resolve the target node and assemble the proxy config.

    Raises NodeResolutionError when the node cannot be resolved; the CLI maps
    it to a clean exit. Port validation lives here so bogus invocations fail
    fast instead of sitting there until the first connection is attempted.
    """
    if not 1 <= remote_port <= 65535:
        detail = f"remote port must be in 1..65535, got {remote_port}"
        raise ValueError(detail)
    if max_body_bytes < 1:
        detail = f"request body cap must be at least 1 byte, got {max_body_bytes}"
        raise ValueError(detail)
    selector, resolved_node = resolve_node(node=node, job_id=job_id, job_name=job_name)
    return ProxyConfig(
        selector=selector,
        node=resolved_node,
        remote_host=remote_host or resolved_node,
        remote_port=remote_port,
        verbose=verbose,
        allow_methods=allow_methods,
        allow_prefixes=allow_prefixes,
        allow_paths=allow_paths,
        max_body_bytes=max_body_bytes,
    )


def serve(
    node: str | None = typer.Option(None, "--node", help="Specific compute node to target."),
    job_id: int | None = typer.Option(None, "--job-id", help="Resolve the compute node from a Slurm job id."),
    job_name: str | None = typer.Option(None, "--job-name", help="Resolve the compute node from a running job name."),
    local_port: int = typer.Option(8000, "--local-port", help="Local port to bind the proxy server to."),
    bind_host: str = typer.Option("127.0.0.1", "--bind-host", help="Local host/IP address to bind to."),
    remote_host: str | None = typer.Option(
        None,
        "--remote-host",
        help="Host to connect to from the login node (one hop, the default). "
        "Pass 'localhost' to connect from inside the compute node instead, for services bound to its loopback.",
    ),
    remote_port: int = typer.Option(8000, "--remote-port", help="Remote port to forward HTTP requests to."),
    allow_method: list[str] | None = typer.Option(
        None, "--allow-method", help="Repeatable HTTP method allowlist. If omitted, all supported methods are allowed."
    ),
    allow_prefix: list[str] | None = typer.Option(
        None, "--allow-prefix", help="Repeatable path prefix allowlist. If omitted, all paths are allowed."
    ),
    verbose: bool = typer.Option(False, "--verbose/--no-verbose", help="Enable verbose proxy logging."),
    max_body_mb: int = typer.Option(
        DEFAULT_MAX_BODY_MB,
        "--max-body-mb",
        help="Cap on one request body in MiB (each turn replays the whole "
        "conversation, so this limits the largest single turn's replay; a "
        "base64-inflated image attachment exceeds aiohttp's 1 MiB default).",
    ),
) -> None:
    """Serve a local HTTP proxy that relays requests through SSH."""
    try:
        config = build_config(
            node=node,
            job_id=job_id,
            job_name=job_name,
            remote_host=remote_host,
            remote_port=remote_port,
            verbose=verbose,
            allow_methods=tuple({m.strip().upper() for m in allow_method or () if m.strip()}),
            allow_prefixes=tuple({p.strip() for p in allow_prefix or () if p.strip()}),
            max_body_bytes=max_body_mb * 1024 * 1024,
        )
    except (NodeResolutionError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    serve_proxy(config, bind_host=bind_host, local_port=local_port)
