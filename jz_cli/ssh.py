"""SSH commands for persistent connection and remote command execution."""

from __future__ import annotations

import shlex
import subprocess
import threading
import time
from pathlib import Path

import typer

from .config import get_value

app = typer.Typer(help="""SSH tool for persistent connection and remote command execution.""")
_MASTER_START_LOCK = threading.Lock()
_MASTER_START_TIMEOUT_SECONDS = 5.0
_MASTER_START_POLL_SECONDS = 0.1


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


def _master_start_error(proc: subprocess.CompletedProcess[str]) -> str:
    """Return a one-line root-cause detail from a failed master-connection ssh."""
    lines = (proc.stderr or "").strip().splitlines()
    return lines[-1] if lines else f"ssh exited with {proc.returncode}"


def start_master_connection(die_if_running: bool = False) -> None:
    """Start the persistent SSH connection, serializing concurrent callers."""
    try:
        with _MASTER_START_LOCK:
            if is_master_connection_active():
                if die_if_running:
                    typer.echo("Master connection is already active.")
                    raise typer.Exit(0)
                return

            remote_user = get_remote_user()
            typer.echo(f"Starting master connection for {remote_user}...")
            cmd = ["ssh", "-M", *get_ssh_args(), "-fN", "-o", "ControlPersist=12h", remote_user]
            # Don't check: an independent CLI invocation may race us. If another
            # process started the master in the meantime, treat it as success.
            proc = subprocess.run(cmd, check=False, capture_output=True, text=True)  # noqa: S603
            if proc.returncode != 0 and not is_master_connection_active():
                # `ssh -f` fails only after authentication completes, so the outcome
                # is already known; fail now instead of polling the full deadline.
                typer.echo(f"Failed to start the master SSH connection: {_master_start_error(proc)}", err=True)
                raise typer.Exit(1)
            # `ssh -f` normally backgrounds only after authenticating, but poll for
            # the control socket anyway instead of trusting a fixed wait.
            deadline = time.monotonic() + _MASTER_START_TIMEOUT_SECONDS
            while True:
                if is_master_connection_active():
                    typer.echo("Master connection started successfully.")
                    return
                if time.monotonic() >= deadline:
                    typer.echo(f"Failed to start the master SSH connection: {_master_start_error(proc)}", err=True)
                    raise typer.Exit(1)
                time.sleep(_MASTER_START_POLL_SECONDS)
    # CalledProcessError omitted deliberately: no subprocess here uses
    # check=True (an independent CLI invocation may race the master start).
    except OSError as exc:
        msg = f"Failed to start the master SSH connection: {exc}"
        typer.echo(msg, err=True)
        raise typer.Exit(1) from exc


def stop_master_connection() -> None:
    """Stop the persistent SSH connection."""
    if not is_master_connection_active():
        typer.echo("Master connection is not active.")
        return

    typer.echo("Stopping master connection...")
    cmd = ["ssh", *get_ssh_args(), "-O", "exit", get_remote_user()]
    subprocess.run(cmd, check=False)  # noqa: S603
    typer.echo("Master connection stopped.")


def run_completed(
    cmd: str,
    *,
    login_shell: bool = False,
    capture_output: bool = True,
    text: bool = True,
    stdin_data: str | bytes | None = None,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    """Run a command on Jean Zay and return the completed process without checking it."""
    start_master_connection()
    remote_cmd = f"bash -l -c {shlex.quote(cmd)}" if login_shell else cmd
    return subprocess.run(  # noqa: S603
        ["ssh", *get_ssh_args(), get_remote_user(), remote_cmd],
        check=False,
        capture_output=capture_output,
        text=text,
        input=stdin_data,
    )


def run(
    cmd: str,
    *,
    login_shell: bool = False,
    check: bool = True,
    capture_output: bool = True,
    text: bool = True,
    stdin_data: str | bytes | None = None,
) -> str | bytes:
    """Run a command on Jean Zay and return its stdout as a string (or bytes)."""
    result = run_completed(
        cmd, login_shell=login_shell, capture_output=capture_output, text=text, stdin_data=stdin_data
    )
    if check:
        result.check_returncode()
    if not capture_output or result.stdout is None:
        return "" if text else b""
    return result.stdout


@app.command("run")
def run_command(
    cmd: str = typer.Argument(help="Command to run"),
    login_shell: bool = typer.Option(False, help="Login to shell (e.g. to load environment variables)"),
) -> None:
    """Run a command to jz. If login_shell is True, the command will be run in a login shell (bash -l -c)."""
    result = run_completed(cmd, login_shell=login_shell)
    typer.echo(result.stdout, nl=False)
    typer.echo(result.stderr, nl=False, err=True)
    if result.returncode != 0:
        raise typer.Exit(result.returncode)


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
