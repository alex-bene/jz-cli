"""Raw TCP tunnel to a port on a Jean Zay compute node.

Unlike `jz proxy http`, this is protocol-agnostic: it forwards bytes in both
directions and understands nothing about them. That makes it usable for
websockets, databases, TensorBoard, Jupyter, or any other non-HTTP service — and
there is no allowlist in front of it, so it is an unrestricted pipe to whichever
port you name. Prefer `jz proxy http`/`jz proxy openai` when the traffic is
HTTP, both because they can restrict methods and paths and because they keep the
node's own HTTP framing.

The tunnel is a :class:`jz_cli.proxy.connect.RelayListener` on a fixed local
port: the same accept/pump machinery the HTTP proxies run on an ephemeral port.
"""

from __future__ import annotations

import typer

from .connect import RelayFactory, RelayListener, ssh_relay_stream
from .http import LOCALHOST_TARGETS, build_config
from .nodes import NodeResolutionError


def serve_tcp(
    *,
    remote_host: str,
    remote_port: int,
    bind_host: str,
    local_port: int,
    verbose: bool = False,
    inner_node: str | None = None,
    relay_factory: RelayFactory | None = None,
) -> None:
    """Start a raw TCP tunnel from the local port to the remote service.

    `inner_node` enables the two-hop form (connect to `remote_host` from inside
    that node), matching the HTTP proxy's LOCALHOST_TARGETS semantics.
    """
    try:
        listener = RelayListener(
            host=remote_host,
            remote_port=remote_port,
            relay_factory=relay_factory or ssh_relay_stream,
            bind_host=bind_host,
            port=local_port,
            verbose=verbose,
            inner_node=inner_node,
        )
    except OSError as exc:
        # Port already in use, bad bind address, ...
        typer.echo(f"Could not bind {bind_host}:{local_port}: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Serving raw TCP tunnel on {bind_host}:{local_port} -> {remote_host}:{remote_port}")
    typer.echo("Warning: this forwards any protocol to the remote port without policy checks.")
    try:
        listener.wait()
    except KeyboardInterrupt:
        typer.echo("\nStopping tunnel.")
    finally:
        listener.close()


def tcp(
    node: str | None = typer.Option(None, "--node", help="Specific compute node to target."),
    job_id: int | None = typer.Option(None, "--job-id", help="Resolve the compute node from a Slurm job id."),
    job_name: str | None = typer.Option(None, "--job-name", help="Resolve the compute node from a running job name."),
    local_port: int = typer.Option(8990, "--local-port", help="Local port to bind the tunnel to."),
    bind_host: str = typer.Option("127.0.0.1", "--bind-host", help="Local host/IP address to bind to."),
    remote_host: str | None = typer.Option(
        None,
        "--remote-host",
        help="Host to connect to from the login node (one hop, the default). "
        "Pass 'localhost' to connect from inside the compute node instead, for services bound to its loopback.",
    ),
    remote_port: int = typer.Option(8000, "--remote-port", help="Remote port to tunnel to."),
    verbose: bool = typer.Option(False, "--verbose/--no-verbose", help="Log every tunnel close and open."),
) -> None:
    """Serve a raw TCP tunnel to a port on a compute node."""
    try:
        config = build_config(
            node=node,
            job_id=job_id,
            job_name=job_name,
            remote_host=remote_host,
            remote_port=remote_port,
            verbose=verbose,
        )
    except (NodeResolutionError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc

    typer.echo(f"Resolved remote node: {config.node} ({config.selector})")
    if config.remote_host in LOCALHOST_TARGETS:
        # Same two-hop semantics as the HTTP proxy: localhost targets are made
        # from INSIDE the compute node (services bound to its loopback).
        serve_tcp(
            remote_host="localhost",
            remote_port=config.remote_port,
            bind_host=bind_host,
            local_port=local_port,
            verbose=verbose,
            inner_node=config.node,
        )
    else:
        serve_tcp(
            remote_host=config.remote_host,
            remote_port=config.remote_port,
            bind_host=bind_host,
            local_port=local_port,
            verbose=verbose,
        )
