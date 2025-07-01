# jz-cli

A command-line interface (CLI) helper for the Jean Zay SLURM cluster at IDRIS.

This CLI simplifies common tasks such as syncing files, managing SSH connections, and interacting with the SLURM scheduler and IDRIS-specific commands.

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

This project is implemented using `typer` for the CLI.
