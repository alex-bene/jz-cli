"""SSH commands for persistent connection and remote command execution."""

from __future__ import annotations

import shlex
import subprocess
import time
from pathlib import Path

import typer

from .config import get_value

app = typer.Typer(help="""SSH tool for persistent connection and remote command execution.""")


def get_remote_user() -> str:
    """Get the remote user from configuration."""
    return get_value("remote_user")


def get_ssh_opts() -> str:
    """Expose SSH control-socket options for callers such as rsync."""
    return " ".join(shlex.quote(arg) for arg in get_ssh_args())


def _get_socket_path() -> Path:
    app_dir = Path(typer.get_app_dir("jz"))
    app_dir.mkdir(parents=True, exist_ok=True)
    return app_dir / f"ssh-{get_remote_user()}.sock"


def get_ssh_args() -> list[str]:
    """Get SSH arguments for the persistent control socket."""
    return ["-S", str(_get_socket_path())]


def is_master_connection_active() -> bool:
    """Check the status of the persistent SSH master connection."""
    socket_path = _get_socket_path()
    if not socket_path.exists():
        return False

    remote_user = get_remote_user()
    cmd = ["ssh", "-S", str(socket_path), "-O", "check", remote_user]
    result = subprocess.run(cmd, check=False, capture_output=True)  # noqa: S603
    return result.returncode == 0


def start_master_connection(die_if_running: bool = False) -> None:
    """Start the persistent SSH connection."""
    if is_master_connection_active():
        if die_if_running:
            typer.echo("Master connection is already active.")
            raise typer.Exit(0)
        return

    remote_user = get_remote_user()
    typer.echo(f"Starting master connection for {remote_user}...")
    cmd = ["ssh", "-M", *get_ssh_args(), "-fN", "-o", "ControlPersist=12h", remote_user]
    subprocess.run(cmd, check=True)  # noqa: S603

    # Wait a moment for the connection to be established
    time.sleep(1)
    if not is_master_connection_active():
        typer.echo("Failed to start master connection.", err=True)
        raise typer.Exit(1)
    typer.echo("Master connection started successfully.")


def stop_master_connection() -> None:
    """Stop the persistent SSH connection."""
    if not is_master_connection_active():
        typer.echo("Master connection is not active.")
        return

    typer.echo("Stopping master connection...")
    cmd = ["ssh", *get_ssh_args(), "-O", "exit", get_remote_user()]
    subprocess.run(cmd, check=False)  # noqa: S603
    typer.echo("Master connection stopped.")


def run(
    cmd: str,
    *,
    login_shell: bool = False,
    check: bool = True,
    capture_output: bool = True,
    text: bool = True,
    return_result: bool = False,
    stdin_data: str | bytes | None = None,
) -> str | bytes | subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    """Run a command on Jean Zay programmatically."""
    start_master_connection()
    remote_cmd = f"bash -l -c {shlex.quote(cmd)}" if login_shell else cmd
    result = subprocess.run(  # noqa: S603
        ["ssh", *get_ssh_args(), get_remote_user(), remote_cmd],
        check=False,
        capture_output=capture_output,
        text=text,
        input=stdin_data,
    )
    if check:
        result.check_returncode()
    if return_result:
        return result
    if result.stdout is None:
        return ""
    if text:
        return result.stdout.strip()
    return result.stdout


@app.command("run")
def run_command(
    cmd: str = typer.Argument(help="Command to run"),
    login_shell: bool = typer.Option(False, help="Login to shell (e.g. to load environment variables)"),
) -> str | bytes | subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    """Run a command to jz. If login_shell is True, the command will be run in a login shell (bash -l -c)."""
    return run(
        cmd, login_shell=login_shell, check=True, capture_output=True, text=True, return_result=False, stdin_data=None
    )


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
