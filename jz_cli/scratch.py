"""Commands for remote SCRATCH timestamp renewal."""

from __future__ import annotations

import typer

from .ssh import run_completed

app = typer.Typer(help="Refresh timestamps under the remote SCRATCH filesystem.")


@app.command()
def renew() -> None:
    """Refresh file and directory timestamps under remote $SCRATCH."""
    # No \( ... \) grouping needed in the find below: every entry under
    # $SCRATCH is a file or a directory, so the old filter was a no-op.
    cmd = """
if [ -z "$SCRATCH" ]; then
  echo "ERROR: SCRATCH environment variable is not set on the remote host."
  exit 1
fi

if [ ! -d "$SCRATCH" ]; then
  echo "ERROR: SCRATCH is not a directory: $SCRATCH"
  exit 1
fi

find "$SCRATCH" -mindepth 1 -exec touch -c {} +
echo "Renewed timestamps under $SCRATCH."
    """
    result = run_completed(cmd, login_shell=True)
    if result.stdout:
        typer.echo(result.stdout, nl=False)
    if result.returncode != 0:
        # The script's own ERROR lines diagnose the failure; surface them
        # instead of a CalledProcessError traceback.
        typer.echo(result.stderr, nl=False, err=True)
        raise typer.Exit(result.returncode or 1)
