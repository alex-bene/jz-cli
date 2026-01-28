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
- **SLURM Job Management**: View your job queue and cancel jobs.
- **IDRIS Resource Management**: Check your resource allocations, project status, and disk quotas.

## Installation

1.  **Clone the repository:**

    ```bash
    git clone https://github.com/your-username/jz-cli.git
    cd jz-cli
    ```

2.  **Install the dependencies:**

    This project uses `uv` for package management.

    ```bash
    pip install uv
    uv pip install -e .
    ```

3.  **Run the setup:**

    ```bash
    jz setup
    ```

    This will prompt you for your Jean Zay username, which is required for all remote operations.

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

# Show or set your remote username
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
   ```

This project uses [Ruff](https://github.com/astral-sh/ruff) for linting and formatting. We use [pre-commit](https://pre-commit.com/) hooks to ensure code quality.

- **Local**: Hooks run before every commit (requires `pre-commit install`).
- **GitHub Actions**: Runs on every push to **auto-fix** issues on all branches.
- **pre-commit.ci**: Runs on every push to **check** code quality (fixes are handled by the GitHub Action).

## License

This project is licensed under the [MIT License](LICENSE).
