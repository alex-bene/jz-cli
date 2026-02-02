"""SLURM-specific commands for jz_cli."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import typer
from rich import print as rprint
from rich.syntax import Syntax

from jz_cli.config import get_value
from jz_cli.ssh import run
from jz_cli.sync import get_remote_base_dir

app = typer.Typer(help="SLURM-specific commands.")


@app.command(context_settings={"ignore_unknown_options": True, "allow_extra_args": True})
def node_run(
    job_id: int | None = typer.Argument(..., help="Job ID to run command on"),
    command: str = typer.Argument(..., help="Command to run on the allocated node"),
) -> None:
    """Run command on allocated node based on job id. (accepts any srun options)."""
    cmd = f"srun --jobid {job_id} --overlap --ntasks=1 {command}"
    typer.echo(run(cmd, login_shell=True))


@app.command(context_settings={"ignore_unknown_options": True, "allow_extra_args": True})
def queue(ctx: typer.Context) -> None:
    """Show job queue for user. (accepts any squeue options)."""
    cmd = 'squeue -u \\$USER -o \\"%10i %9P %16j %2t %10M %10L %5D %15b %14N %R\\"' + " " + " ".join(ctx.args)
    typer.echo(run(cmd, login_shell=True))


@app.command(context_settings={"ignore_unknown_options": True, "allow_extra_args": True})
def info(ctx: typer.Context) -> None:
    """Show cluster info. (accepts any sinfo options)."""
    cmd = 'sinfo -O "Partition:15,Available:12,Time:15,NodeAIOT,GRES,Features:35"' + " ".join(ctx.args)
    typer.echo(run(cmd, login_shell=True))


@app.command(context_settings={"ignore_unknown_options": True, "allow_extra_args": True})
def cancel(
    ctx: typer.Context,
    job_id: int | None = typer.Argument(None, help="Job ID to cancel"),
    all_jobs: bool | None = typer.Option(False, "--all", "-a", help="Cancel all jobs for the user"),
) -> None:
    """Delete job by id or by user. (accepts any squeue options)."""
    cmd = "scancel"
    if job_id is not None:
        cmd += f" {job_id}"
    if job_id is None and all_jobs:
        cmd += " -u \\$USER"
    cmd += " " + " ".join(ctx.args)
    typer.echo(run(cmd, login_shell=True))


@dataclass
class GPUResource:
    """Base class for GPU resource specifications."""

    partition: str = field(init=False, default="")
    account: str = field(init=False, default="")
    cpus_per_task: int = field(init=False, default=-1)
    max_gpus: int = field(init=False, default=-1)
    constraint: str = field(init=False, default="")
    module_load: str = field(init=False, default="")

    def to_sbatch(self) -> str:
        """Return SLURM sbatch directives."""
        out = ""
        if self.partition:
            out += f"#SBATCH --partition={self.partition}\n"
        if self.constraint:
            out += f"#SBATCH -C {self.constraint}\n"
        if self.cpus_per_task > 0:
            out += f"#SBATCH --cpus-per-task={self.cpus_per_task}\n"
        if self.account:
            out += f"#SBATCH --account={self.account}\n"

        return out

    def calc_nodes(self, num_of_gpus: int) -> int:
        """Calculate the number of nodes required for the given number of GPUs."""
        return (num_of_gpus + self.max_gpus - 1) // self.max_gpus


@dataclass
class A100Resource(GPUResource):
    """A100 GPU resource specifications."""

    partition: str = field(init=False, default="gpu_p5")
    constraint: str = field(init=False, default="a100")
    cpus_per_task: int = field(init=False, default=8)
    module_load: str = field(init=False, default="arch/a100")
    max_gpus: int = field(init=False, default=8)

    def __post_init__(self) -> None:
        """Set account after initialization."""
        account = get_value("account")
        self.account = f"{account}@a100"


@dataclass
class H100Resource(GPUResource):
    """H100 GPU resource specifications."""

    partition: str = field(init=False, default="gpu_p6,gpu_p6s")
    constraint: str = field(init=False, default="h100")
    cpus_per_task: int = field(init=False, default=24)
    module_load: str = field(init=False, default="arch/h100")
    max_gpus: int = field(init=False, default=4)

    def __post_init__(self) -> None:
        """Set account after initialization."""
        account = get_value("account")
        self.account = f"{account}@h100"


@dataclass
class V100Resource(GPUResource):
    """V100 GPU resource specifications."""

    partition: str = "gpu_p13"
    gpu_mem: int = 32

    def __post_init__(self) -> None:
        """Set account and other parameters after initialization."""
        account = get_value("account")
        self.account = f"{account}@v100"

        if self.gpu_mem not in [16, 32]:
            msg = "Invalid gpu_mem value, only 16 or 32 are supported"
            raise ValueError(msg)
        if self.partition not in ["gpu_p2", "gpu_p13"]:
            msg = "Invalid partition value, only gpu_p2 or gpu_p13 are supported"
            raise ValueError(msg)
        if self.partition == "gpu_p2" and self.gpu_mem != 32:
            msg = "Invalid gpu_mem value for gpu_p2"
            raise ValueError(msg)

        if self.partition == "gpu_p2":
            self.constraint = ""
            self.cpus_per_task = 3
            self.max_gpus = 8
        elif self.gpu_mem == 16:
            self.partition = "gpu_p13"
            self.constraint = "v100-16g"
            self.cpus_per_task = 10
            self.max_gpus = 4
        elif self.gpu_mem == 32:
            self.partition = "gpu_p13"
            self.constraint = "v100-32g"
            self.cpus_per_task = 10
            self.max_gpus = 4


@app.command()
def batch(
    job_name: str = typer.Option("", "--job-name", help="Job name"),
    time: str = typer.Option("02:00:00", "--time", help="Time limit"),
    hint: str = typer.Option("nomultithread", "--hint", help="Hint for the job"),
    num_of_gpus: int = typer.Option(1, "--num-of-gpus", help="Number of GPUs"),
    module_load: list[str] = typer.Option([], "--module-load", help="Modules to load (can repeat)"),
    script: str = typer.Option(None, "--script", help="Script to run"),
    gpu_type: str = typer.Option("v100-32g", "--gpu-type", help="GPU type (a100, h100, v100-p2, v100-16g, v100-32g)"),
    submit_job: bool = typer.Option(False, "--submit-job", help="Submit the job to the cluster"),
) -> None:
    """Create a SLURM sbatch script."""
    if gpu_type == "a100":
        partition = A100Resource()
    elif gpu_type == "h100":
        partition = H100Resource()
    elif gpu_type == "v100-p2":
        partition = V100Resource(partition="gpu_p2")
    elif gpu_type == "v100-16g":
        partition = V100Resource(gpu_mem=16)
    elif gpu_type == "v100-32g":
        partition = V100Resource(gpu_mem=32)
    else:
        msg = f"Invalid gpu_type: {gpu_type}"
        raise ValueError(msg)

    if module_load:
        module_load = ([partition.module_load] if partition.module_load else []) + module_load
        module_load = "module load " + "\nmodule load ".join(module_load)

    num_of_nodes = partition.calc_nodes(num_of_gpus)
    gpus_per_node = num_of_gpus if num_of_nodes == 1 else partition.max_gpus

    sbatch_script = f"""#!/bin/bash
#SBATCH --job-name={job_name}             # Name of job
#SBATCH --time={time}                     # maximum execution time requested (HH:MM:SS)
#SBATCH --hint={hint}
#SBATCH --output=job_logs/%x_%j.out       # name of output file
#SBATCH --error=job_logs/%x_%j.err        # name of error file
#SBATCH --ntasks={num_of_gpus}            # total number of MPI tasks (= total number of GPUs here)
#SBATCH --gres=gpu:{gpus_per_node}        # number of GPUs per node (max 8 with gpu_p2, gpu_p5)
#SBATCH --ntasks-per-node={gpus_per_node} # number of MPI tasks per node (= number of GPUs per node)
#SBATCH --nodes={num_of_nodes}            # number of nodes
{partition.to_sbatch()}

# Cleans out the modules loaded in interactive and inherited by default
module purge
{module_load}

cd {get_remote_base_dir(Path.cwd())}

# Echo of launched commands
set -x

srun {script}
"""

    # Confirm SBATCH script
    rprint(Syntax(sbatch_script, "bash", line_numbers=True))
    typer.confirm("Is the above SBATCH script correct?", abort=True)

    # Create file in jz with timestamp
    current_datetime = str(datetime.now().strftime("%Y%m%d%H%M%S"))  # noqa: DTZ005
    filepath = get_remote_base_dir(Path.cwd()) / f"sbatch_{current_datetime}.slurm"
    run(f"cat > {filepath} <<EOF\n{sbatch_script}\nEOF")

    # Make sure the file exists
    cmd_check_file = f"""
if [ -f {filepath} ]; then
  echo "success"
fi
"""
    success = run(cmd_check_file, login_shell=True) == "success"
    if not success:
        typer.echo("❌ Failed to create file.")
        raise typer.Exit(1)
    typer.echo(f"✅ File created at {filepath}")

    if submit_job:
        cmd_submit = f"sbatch {filepath}"
        typer.echo(f"Submitting job to cluster with command:\n{cmd_submit}")
        run(cmd_submit, login_shell=True)
