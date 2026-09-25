"""CLI entrypoints for SSH-backed proxy commands."""

from __future__ import annotations

import typer

from .http import serve as http_proxy
from .openai import openai as openai_proxy
from .tcp import tcp as tcp_tunnel

# Subcommands name the protocol: http, openai, tcp.
app = typer.Typer(help="Expose local HTTP or TCP proxies backed by an SSH relay to Jean Zay.")
app.command("http")(http_proxy)
app.command("openai")(openai_proxy)
app.command("tcp")(tcp_tunnel)
