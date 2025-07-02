# jz/sync.py

import os
import subprocess
from typing import List, Optional

import typer

from .config import get_value
from .ssh import get_ssh_opts, run

app = typer.Typer(help="Sync local code to Jean Zay cluster.")


def get_remote_base_dir(local_dir: str) -> str:
    scratch_dir = run("echo \\$SCRATCH", login_shell=True)
    basename = os.path.basename(os.path.abspath(local_dir))
    return f"{scratch_dir}/rsync/{basename}"


@app.command()
def sync(
    local_dir: str = typer.Argument(os.getcwd(), help="Local directory to sync"),
    remote_base_dir: Optional[str] = typer.Option(None, help="Remote base directory"),
    exclude: List[str] = typer.Option(
        None, "--exclude", "-e", help="Additional rsync exclude patterns (can repeat)"
    ),
    delete: bool = typer.Option(
        True, "--delete", help="Delete files that are not in the source directory"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
):
    """Sync local directory to Jean Zay via rsync."""
    remote_user = get_value("remote_user")

    if remote_base_dir is None:
        remote_base_dir = get_remote_base_dir(local_dir)

    default_excludes = [
        ".git",
        "__pycache__",
        ".venv",
        ".python-version",
        "uv.lock",
    ]
    all_excludes = default_excludes + (exclude or [])
    exclude_opts = " ".join([f"--exclude='{pattern}'" for pattern in all_excludes])
    ssh_opts = get_ssh_opts()
    rsh_opt = f"-e 'ssh {ssh_opts}'"

    cmd = f"rsync -a{'v' if verbose else ''}z {'--delete' if delete else ''} {rsh_opt} {exclude_opts} {local_dir}/ {remote_user}:{remote_base_dir}/"
    typer.echo(f"Syncing {local_dir} to {remote_user}:{remote_base_dir} ...")
    if verbose:
        typer.echo(f"Running command:\n{cmd}\n")
    subprocess.run(cmd, shell=True)
