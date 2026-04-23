"""Commands for remote SCRATCH timestamp renewal."""

from __future__ import annotations

import typer

from .ssh import run

app = typer.Typer(help="Refresh timestamps under the remote SCRATCH filesystem.")


@app.command()
def renew() -> None:
    """Refresh file and directory timestamps under remote $SCRATCH."""
    cmd = """
if [ -z "$SCRATCH" ]; then
  echo "ERROR: SCRATCH environment variable is not set on the remote host."
  exit 1
fi

if [ ! -d "$SCRATCH" ]; then
  echo "ERROR: SCRATCH is not a directory: $SCRATCH"
  exit 1
fi

find "$SCRATCH" -mindepth 1 \\( -type f -o -type d \\) -exec touch -c {} +
echo "Renewed timestamps under $SCRATCH."
"""
    output = run(cmd, login_shell=True)
    if output:
        typer.echo(output)
