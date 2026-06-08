"""OpenAI-specific proxy configuration built on the generic SSH HTTP proxy."""

from __future__ import annotations

import json

import typer

from .http import NodeResolutionError, ProxyConfig, resolve_node, serve_proxy

OPENAI_ALLOWED_PREFIXES = ("/v1/", "/health")
OPENAI_ALLOWED_METHODS = ("GET", "POST")


def validate_openai_request(method: str, path: str, body: bytes) -> tuple[int, dict[str, object]] | None:
    """Reject streaming requests until the OpenAI wrapper supports them."""
    if method != "POST" or not path.startswith("/v1/") or not body:
        return None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if isinstance(payload, dict) and payload.get("stream") is True:
        return 501, {"error": "Streaming requests are not supported by this proxy yet."}
    return None


def openai(
    node: str | None = typer.Option(None, "--node", help="Specific compute node to target."),
    job_id: int | None = typer.Option(None, "--job-id", help="Resolve the compute node from a Slurm job id."),
    job_name: str | None = typer.Option(None, "--job-name", help="Resolve the compute node from a running job name."),
    local_port: int = typer.Option(8000, "--local-port", help="Local port to bind the proxy server to."),
    bind_host: str = typer.Option("127.0.0.1", "--bind-host", help="Local host/IP address to bind to."),
    remote_port: int = typer.Option(8000, "--remote-port", help="Remote port where vLLM serves the API."),
    verbose: bool = typer.Option(False, "--verbose/--no-verbose", help="Enable verbose proxy logging."),
) -> None:
    """Serve a local OpenAI-compatible proxy that relays requests through SSH."""
    try:
        selector, resolved_node = resolve_node(node=node, job_id=job_id, job_name=job_name)
    except NodeResolutionError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc

    config = ProxyConfig(
        selector=selector,
        node=resolved_node,
        remote_host="localhost",
        remote_port=remote_port,
        verbose=verbose,
        allow_methods=OPENAI_ALLOWED_METHODS,
        allow_prefixes=OPENAI_ALLOWED_PREFIXES,
        extra_validator=validate_openai_request,
    )
    serve_proxy(config, bind_host=bind_host, local_port=local_port)
