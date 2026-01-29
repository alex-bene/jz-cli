"""Main CLI entry point for jz_cli."""

import typer

from .config import app as config_app
from .config import ensure_config
from .idris import app as idris_app
from .setup import app as setup_app
from .slurm import app as slurm_app
from .ssh import app as ssh_app
from .sync import app as sync_app

app = typer.Typer()
app.add_typer(sync_app)
app.add_typer(setup_app)
app.add_typer(config_app, name="config")
app.add_typer(ssh_app, name="ssh")
app.add_typer(idris_app, name="idris")
app.add_typer(slurm_app, name="slurm")


@app.callback()
def main(ctx: typer.Context) -> None:
    """Jz CLI - Manage your Jean Zay projects and files easily from the command line."""
    if ctx.invoked_subcommand != "setup":
        ensure_config()
