# jz/config.py

import json
from pathlib import Path

import typer
from rich import box
from rich.console import Console
from rich.table import Table

APP_NAME = "jz"
CONFIG_FILE = "config.json"


def _config_path() -> Path:
    app_dir = Path(typer.get_app_dir(APP_NAME))
    app_dir.mkdir(parents=True, exist_ok=True)
    return app_dir / CONFIG_FILE


def get_config() -> dict:
    path = _config_path()
    if path.exists():
        with open(path, "r") as f:
            return json.load(f)
    return {}


def save_config(config: dict):
    with open(_config_path(), "w") as f:
        json.dump(config, f, indent=2)


def get_remote_user() -> str:
    config = get_config()
    return config.get("remote_user", "jz")  # Default fallback


def set_remote_user(username: str):
    config = get_config()
    config["remote_user"] = username
    save_config(config)


def ensure_config():
    if not _config_path().exists():
        typer.echo("🚧 No configuration found.")
        typer.echo("🛠  Please run `jz setup` to configure before using the CLI.")
        raise typer.Exit(code=1)


app = typer.Typer(help="Inspect or modify jz configuration.")


@app.command()
def show():
    """
    Display the full current configuration.
    """
    config = get_config()
    if not config:
        typer.echo("No configuration set yet.")
        return

    table = Table("Key", "Value", box=box.MINIMAL)
    for k, v in config.items():
        table.add_row(k, str(v))

    console = Console()
    console.print(table)


@app.command()
def remote_user(
    value: str = typer.Option(
        None, "--set", help="Set remote_user. If not provided, just show it."
    ),
):
    """
    Show or set the remote user (Jean Zay login).
    """
    if value:
        set_remote_user(value)
        typer.echo(f"✅ remote_user set to '{value}'")
    else:
        typer.echo(f"👤 remote_user: {get_remote_user()}")
