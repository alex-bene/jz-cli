# jz/idris.py


from typing import Optional

import typer

from .ssh import run

app = typer.Typer(help="SLURM-specific commands.")


@app.command(
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True}
)
def queue(ctx: typer.Context):
    """Show job queue for user. (accepts any squeue options)"""
    cmd = "squeue -u \\$USER " + " ".join(ctx.args)
    print(cmd)
    typer.echo(run(cmd, login_shell=True))


@app.command(
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True}
)
def cancel(
    ctx: typer.Context,
    job_id: Optional[int] = typer.Argument(None, help="Job ID to cancel"),
    all: Optional[bool] = typer.Option(
        False, "--all", "-a", help="Cancel all jobs for the user"
    ),
):
    """Delete job by id or by user. (accepts any squeue options)"""
    cmd = "scancel"
    if job_id is not None:
        cmd += f" {job_id}"
    if job_id is None and all:
        cmd += " -u \\$USER"
    cmd += " " + " ".join(ctx.args)
    typer.echo(run(cmd, login_shell=True))
