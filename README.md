# jz-cli

[![pre-commit.ci status](https://results.pre-commit.ci/badge/github/alex-bene/jz-cli/main.svg)](https://results.pre-commit.ci/latest/github/alex-bene/jz-cli/main)
[![Development Status](https://img.shields.io/badge/status-beta-orange)](https://github.com/alex-bene/jz-cli)
[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A command-line interface (CLI) helper for the Jean Zay SLURM cluster at IDRIS.

This [`typer`](https://typer.tiangolo.com/)-based CLI simplifies common tasks such as syncing files, managing SSH connections, and interacting with the SLURM scheduler and IDRIS-specific commands.

## Features

- **File Synchronization**: Sync your local project directory with the Jean Zay cluster using `rsync`.
- **Persistent SSH Connections**: Maintain a persistent SSH connection for faster access and command execution.
- **HTTP Proxies Over SSH**: Expose local HTTP endpoints that relay requests to services running on Jean Zay compute nodes, with optional method/path restrictions.
- **TCP Tunnels Over SSH**: Expose a raw TCP tunnel to any port on a Jean Zay compute node for non-HTTP services.
- **SCRATCH Timestamp Renewal**: Refresh timestamps under your remote `$SCRATCH`.
- **SLURM Job Management**: View your job queue and cancel jobs.
- **IDRIS Resource Management**: Check your resource allocations, project status, and disk quotas.

## Installation

1.  **Install the dependencies:**

    Install with uv (recommended; installs it in an isolated environment).

    ```bash
    uv tool install --python 3.12 git+https://github.com/alex-bene/jz-cli/
    ```

    or with pip

    ```bash
    pip install git+https://github.com/alex-bene/jz-cli/
    ```

2.  **Run the setup:**

    ```bash
    jz setup
    ```

    This will prompt you for your Jean Zay username and account id. SSH commands use the configured `remote_user` as the SSH target, and sync commands use it for rsync destinations.

## Usage

The `jz` CLI has several subcommands, each with its own set of options.

### `jz sync`

Sync a local directory to the Jean Zay cluster.

```bash
# Sync the current directory
jz sync

# Sync a specific directory
jz sync /path/to/your/project

# Exclude certain files or directories
jz sync -e ".env" -e "data/"

# See more options
jz sync --help
```

### `jz ssh`

Manage the persistent SSH connection.

```bash
# Start the master connection
jz ssh start

# Check the status of the connection
jz ssh status

# Run a command on the remote server
jz ssh run "ls -l"

# Stop the master connection
jz ssh stop
```

### `jz proxy`

Expose a local HTTP or TCP endpoint that relays over ordinary SSH to a service running on a Jean Zay compute node.

```bash
# Expose a generic HTTP service running on the compute node
jz proxy http --job-id 123456 --remote-port 8080 --local-port 8000

# Restrict the proxy to a subset of paths
jz proxy http --node jzxh033 --remote-port 8080 --allow-prefix /api/

# Target a host other than the compute node, reachable from the login node
jz proxy http --node jzxh033 --remote-host 10.0.0.12 --remote-port 8080

# Allow only GET and POST through the generic proxy
jz proxy http --node jzxh033 --remote-port 8080 --allow-method GET --allow-method POST

# Use the OpenAI-specific wrapper for vLLM or another OpenAI-compatible server
jz proxy openai --job-name vllm-serve --remote-port 8888 --local-port 8000

# Reach a service bound to localhost on the compute node (two-hop form):
# the relay connect is made from INSIDE the compute node
jz proxy openai --node jzxh033 --remote-host localhost --remote-port 8888

# Raw TCP tunnel for non-HTTP services (websockets, databases, TensorBoard, ...)
jz proxy tcp --job-id 123456 --remote-port 8888 --local-port 8990
```

Notes:

- Responses stream incrementally, including `text/event-stream` and chunked upstream responses. `jz proxy openai` supports `"stream": true`.
- Upstream HTTP keep-alive connections are reused: one SSH channel serves every request on a pooled connection, instead of one channel (SSH handshake) per request.
- `jz proxy http` and `jz proxy openai` understand HTTP and can restrict what passes (see below). `jz proxy tcp` is a raw byte tunnel with no policy checks: it will forward any protocol to the port you name, so prefer an HTTP proxy when the traffic is HTTP.
- Use exactly one of `--node`, `--job-id`, or `--job-name`.
- `--allow-method` is repeatable. If omitted, all supported HTTP methods are allowed; protocol upgrades and `CONNECT` require `jz proxy tcp`.
- `--allow-prefix` is repeatable. If omitted, all paths are allowed.
- `jz proxy openai` allows only `GET`, `POST`, and `HEAD`, and only forwards `/v1/` and `/health`.
- Request bodies are capped at `--max-body-mb` (default 64 MiB). The cap is per proxy and on a single request body; an OpenAI turn can replay the whole conversation, so a base64-inflated image attachment makes one turn's body much larger than the text you send.
- `--remote-host` defaults to the resolved compute node and is connected to from the login node (one hop). The service must listen on an interface reachable from the login node; a server bound only to `127.0.0.1` on the compute node is reachable only via the two-hop form: pass `--remote-host localhost` (accepted by all three subcommands) and the relay connect is made from inside the compute node instead. Applies to `jz proxy http`, `jz proxy openai`, and `jz proxy tcp` alike.
- Every HTTP proxy also exposes a local health check at `/_jz/health`.
- This avoids SSH port forwarding, which Jean Zay disables.
- `--job-id` is safer than `--job-name` if multiple similarly named jobs may be running.
- The target node is resolved once at startup, so restart the proxy if the job ends or moves.
- Idle relay channels close after 15 minutes without remote network activity; active SSE and other streaming responses reset that inactivity timer. Idle pooled connections are reconnected transparently.
- HTTP requests use the remote destination in the forwarded `Host` header, rather than the proxy's local address.

### `jz scratch`

Refresh timestamps under your remote `$SCRATCH`.

```bash
# Run one immediate renewal
jz scratch renew
```

`jz scratch renew` connects over SSH, verifies that `$SCRATCH` is set and points to a remote directory, then runs `touch -c` on each file and directory below it.

### `jz slurm`

Interact with the SLURM scheduler.

```bash
# Show your job queue
jz slurm queue

# Cancel a specific job
jz slurm cancel 12345

# Cancel all your jobs
jz slurm cancel --all
```

### `jz idris`

Use IDRIS-specific commands.

```bash
# Check your resource allocations
jz idris allocations

# View your projects
jz idris projects

# Check your resource consumption
jz idris consumption

# Check your disk quota
jz idris disk-quota
```

### `jz config`

Manage the CLI configuration.

```bash
# Show the current configuration
jz config show

# Show or set your remote_user
jz config remote-user
jz config remote-user --set my-new-username
```

## Development

To contribute to this project, please ensure you have `uv` installed.

1. Clone the repository:
   ```bash
   git clone https://github.com/alex-bene/jz-cli.git
   cd jz-cli
   ```

2. Install dependencies and pre-commit hooks:
   ```bash
   uv sync
   uv run pre-commit install
   ```

3. Run checks manually (optional):
   ```bash
   uv run ruff check
   uv run ruff format
   uv run pytest
   ```

The proxy suite lives under [`tests/`](tests/).

This project uses [Ruff](https://github.com/astral-sh/ruff) for linting and formatting. We use [pre-commit](https://pre-commit.com/) hooks to ensure code quality.

- **Local**: Hooks run before every commit (requires `pre-commit install`).
- **GitHub Actions**: Runs on every push to **auto-fix** issues on all branches.
- **pre-commit.ci**: Runs on every push to **check** code quality (fixes are handled by the GitHub Action).

## License

This project is licensed under the [MIT License](LICENSE).
