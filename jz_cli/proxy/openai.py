"""OpenAI-specific proxy configuration built on the generic SSH HTTP proxy."""

from __future__ import annotations

import typer

from .http import DEFAULT_MAX_BODY_MB, build_config, serve_proxy
from .nodes import NodeResolutionError

OPENAI_ALLOWED_PREFIXES = ("/v1/",)
OPENAI_ALLOWED_PATHS = ("/health",)
OPENAI_ALLOWED_METHODS = ("GET", "POST", "HEAD")


def openai(
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
    remote_port: int = typer.Option(8000, "--remote-port", help="Remote port where vLLM serves the API."),
    verbose: bool = typer.Option(False, "--verbose/--no-verbose", help="Enable verbose proxy logging."),
    max_body_mb: int = typer.Option(
        DEFAULT_MAX_BODY_MB,
        "--max-body-mb",
        help="Cap on one request body in MiB (each turn replays the whole "
        "conversation, so this limits the largest single turn's replay; a "
        "base64-inflated image attachment exceeds aiohttp's 1 MiB default).",
    ),
) -> None:
    """Serve a local OpenAI-compatible proxy that relays requests through SSH."""
    try:
        config = build_config(
            node=node,
            job_id=job_id,
            job_name=job_name,
            remote_host=remote_host,
            remote_port=remote_port,
            verbose=verbose,
            allow_methods=OPENAI_ALLOWED_METHODS,
            allow_prefixes=OPENAI_ALLOWED_PREFIXES,
            allow_paths=OPENAI_ALLOWED_PATHS,
            max_body_bytes=max_body_mb * 1024 * 1024,
        )
    except (NodeResolutionError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    serve_proxy(config, bind_host=bind_host, local_port=local_port)
