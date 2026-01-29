"""Commands for syncing code to Jean Zay cluster."""

from __future__ import annotations

import subprocess
from pathlib import Path

import typer

from .config import get_value
from .ssh import get_ssh_opts, run

app = typer.Typer(help="Sync local code to Jean Zay cluster.")


def get_remote_base_dir(local_dir: Path) -> Path:
    """Get the remote base directory for syncing based on local directory name."""
    scratch_dir = Path(run("echo \\$SCRATCH", login_shell=True).strip())
    basename = Path(local_dir).resolve().name
    return scratch_dir / "rsync" / basename


@app.command()
def sync(
    local_dir: Path | str = typer.Argument(Path.cwd(), help="Local directory to sync"),
    remote_base_dir: Path | str | None = typer.Option(None, help="Remote base directory"),
    exclude: list[str] = typer.Option(None, "--exclude", "-e", help="Additional rsync exclude patterns (can repeat)"),
    delete: bool = typer.Option(False, "--delete", help="Delete files that are not in the source directory"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
) -> None:
    """Sync local directory to Jean Zay via rsync."""
    remote_user = get_value("remote_user")

    local_dir = Path(local_dir)
    if remote_base_dir is None:
        remote_base_dir = get_remote_base_dir(local_dir)
    remote_base_dir = Path(remote_base_dir)

    default_excludes = [".git", "__pycache__", ".ruff_cache", ".venv", "uv.lock"]
    all_excludes = default_excludes + (exclude or [])
    exclude_opts = " ".join([f"--exclude='{pattern}'" for pattern in all_excludes])
    ssh_opts = get_ssh_opts()
    rsh_opt = f"-e 'ssh {ssh_opts}'"

    cmd = (
        f"rsync -a{'v' if verbose else ''}z {'--delete' if delete else ''} {rsh_opt} {exclude_opts} {local_dir}/ "
        f"{remote_user}:{remote_base_dir}/"
    )
    typer.echo(f"Syncing {local_dir} to {remote_user}:{remote_base_dir} ...")
    if verbose:
        typer.echo(f"Running command:\n{cmd}\n")
    subprocess.run(cmd, check=False, shell=True)  # noqa: S602
