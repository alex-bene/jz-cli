"""SSH commands for persistent connection and remote command execution."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import typer

from .config import get_value

app = typer.Typer(help="""SSH tool for persistent connection and remote command execution.""")


def get_remote_user() -> str:
    """Get the remote user from configuration."""
    return get_value("remote_user")


def _get_socket_path() -> Path:
    # Using a more standard location for sockets, e.g. in the app's config dir
    app_dir = Path(typer.get_app_dir("jz"))
    app_dir.mkdir(parents=True, exist_ok=True)
    return app_dir / f"ssh-{get_remote_user()}.sock"


def get_ssh_opts() -> str:
    """Get SSH options for persistent connection."""
    socket_path = _get_socket_path()
    return f"-S {socket_path}"


def is_master_connection_active() -> bool:
    """Check if the master SSH connection is active."""
    socket_path = _get_socket_path()
    if not socket_path.exists():
        return False

    remote_user = get_remote_user()
    cmd = ["ssh", "-S", socket_path, "-O", "check", remote_user]
    result = subprocess.run(cmd, check=False, capture_output=True)  # noqa: S603
    return result.returncode == 0


def start_master_connection(die_if_running: bool = False) -> None:
    """Start the master SSH connection."""
    if is_master_connection_active():
        if die_if_running:
            typer.echo("Master connection is already active.")
            raise typer.Exit(0)
        return

    remote_user = get_remote_user()
    typer.echo(f"Starting master connection for {remote_user}...")
    cmd = ["ssh", "-M", *get_ssh_opts().split(), "-fN", "-o", "ControlPersist=12h", remote_user]
    subprocess.run(cmd, check=True)  # noqa: S603

    # Wait a moment for the connection to be established
    time.sleep(1)
    if not is_master_connection_active():
        typer.echo("Failed to start master connection.", err=True)
        raise typer.Exit(1)
    typer.echo("Master connection started successfully.")


def stop_master_connection() -> None:
    """Stop the master SSH connection."""
    if not is_master_connection_active():
        typer.echo("Master connection is not active.")
        return

    typer.echo("Stopping master connection...")
    cmd = ["ssh", *get_ssh_opts().split(), "-O", "exit", get_remote_user()]
    subprocess.run(cmd, check=False)  # noqa: S603
    typer.echo("Master connection stopped.")


@app.command()
def run(
    cmd: str = typer.Argument(help="Command to run"),
    login_shell: bool | None = typer.Option(False, help="Login to shell (e.g. to load environment variables)"),
) -> str:
    """Run a command to jz. If login_shell is True, the command will be run in a login shell (bash -l -c)."""
    start_master_connection()
    result = subprocess.run(  # noqa: S603
        ["ssh", *get_ssh_opts().split(), get_remote_user(), f'bash -l -c "{cmd}"' if login_shell else cmd],
        check=False,
        capture_output=True,
        text=True,
    )
    result.check_returncode()
    return result.stdout.strip()


@app.command()
def start() -> None:
    """Start the persistent SSH connection."""
    start_master_connection(die_if_running=True)


@app.command()
def stop() -> None:
    """Stop the persistent SSH connection."""
    stop_master_connection()


@app.command()
def status() -> None:
    """Check the status of the persistent SSH connection."""
    if is_master_connection_active():
        typer.echo("SSH master connection is active.")
    else:
        typer.echo("SSH master connection is not active.")
