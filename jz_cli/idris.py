# jz/idris.py


from typing import Optional

import typer

from .ssh import run

app = typer.Typer(help="IDRIS-specific commands.")


@app.command()
def allocations(
    summary: bool = typer.Option(False, "--summary", "-s", help="Summarize output"),
):
    """Indicate the CPU and/or GPU hours allocations."""
    typer.echo(run("idracct" + (" -s" if summary else ""), login_shell=True))


@app.command()
def projects():
    """Display the projects or change the default project."""
    typer.echo(run("idrproj", login_shell=True))


@app.command()
def consumption(
    short: bool = typer.Option(
        False,
        "--short",
        "-s",
        help="Display the status of your accounts without the disclaimer",
    ),
    accounts: Optional[list[str]] = typer.Option(
        None, "--accounts", "-A", help="Display the status of specific accounts"
    ),
):
    """Verify the consumption status of your project."""
    cmd = "idr_compuse"
    if short:
        cmd += " -s"
    if accounts is not None:
        cmd += f" -A {','.join(accounts)}"
    typer.echo(run(cmd, login_shell=True))


@app.command()
def disk_quota(
    project: Optional[str] = typer.Option(
        None,
        "--project",
        "-p",
        help="Show the given project. When not provided, shows your active project. Mutually exclusive with the --all-projects flag.",
    ),
    all_projects: Optional[bool] = typer.Option(
        False,
        "--all-projects",
        "-a",
        help="Show all projects. Mutually exclusive with the --project flag.",
    ),
    space: Optional[list[str]] = typer.Option(
        None, "--space", "-s", help="Filter the output for the given disk space(s)."
    ),
    json: Optional[bool] = typer.Option(
        False, "--json", "-j", help="Display a json formatted data"
    ),
):
    """Show quota disk infos (home/work/store)."""
    cmd = "idr_quota_user"
    if project is not None:
        cmd += f" -p {project}"
    if all_projects:
        cmd += " -a"
    if space is not None:
        space: str = " ".join(space)
        space = space.replace(",", " ")
        space = [s.strip() for s in space.split()]
        cmd += f" -s {' '.join(space)}"
    if json:
        cmd += " -j"
    typer.echo(run(cmd, login_shell=True))
