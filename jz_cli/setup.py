# jz/setupp.py


import typer

from .config import set_remote_user

app = typer.Typer(help="First-time configuration for the jz CLI.")


@app.command()
def setup():
    """
    Interactive setup wizard for jz.
    """
    typer.echo("🛠  Welcome to jz setup!\n")

    username = typer.prompt(
        "Enter your remote user for Jean Zay (it will be used as `ssh [remote_user]`)"
    )
    set_remote_user(username)

    typer.echo("\n✅ Setup complete.")
    typer.echo(f"Saved remote_user = '{username}'")
