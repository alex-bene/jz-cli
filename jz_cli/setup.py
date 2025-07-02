# jz/setupp.py


import typer

from .config import set_value

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
    set_value("remote_user", username)

    account = typer.prompt("Enter your account id to use in Jean Zay")
    set_value("account", account)

    typer.echo("\n✅ Setup complete.")
