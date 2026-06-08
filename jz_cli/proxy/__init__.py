"""CLI entrypoints for SSH-backed HTTP proxy commands."""

from __future__ import annotations

import typer

from .http import serve as serve_proxy
from .openai import openai as serve_openai_proxy

app = typer.Typer(help="Expose local HTTP proxies backed by an SSH relay to Jean Zay.")
app.command("serve")(serve_proxy)
app.command("openai")(serve_openai_proxy)
