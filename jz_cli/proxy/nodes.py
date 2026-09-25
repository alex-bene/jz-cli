"""Compute-node resolution shared by every SSH-backed proxy command."""

from __future__ import annotations

import shlex

import typer

from jz_cli.ssh import run_completed


class NodeResolutionError(RuntimeError):
    """Raised when the target compute node cannot be resolved."""


def _run_for_output(cmd: str, *, context: str) -> str:
    """Run a resolution command on Jean Zay, mapping failure to NodeResolutionError."""
    result = run_completed(cmd, login_shell=True)
    if result.returncode != 0:
        lines = (result.stderr or "").strip().splitlines()
        detail = lines[-1] if lines else f"command failed with exit code {result.returncode}"
        msg = f"Could not resolve node ({context}): {detail}"
        raise NodeResolutionError(msg)
    return str(result.stdout)


def resolve_node(*, node: str | None, job_id: int | None, job_name: str | None) -> tuple[str, str]:
    """Resolve the selected compute node once at startup."""
    selectors = [node is not None, job_id is not None, job_name is not None]
    if sum(selectors) != 1:
        msg = "Provide exactly one of --node, --job-id, or --job-name."
        raise typer.BadParameter(msg)
    if node is not None:
        return f"node={node}", node
    if job_id is not None:
        return f"job_id={job_id}", _resolve_job_id(job_id)
    return f"job_name={job_name}", _resolve_job_name(job_name)


def _resolve_job_id(job_id: int) -> str:
    hostlist_cmd = f'squeue -h -j {job_id} -o %N | awk "NF {{print; exit}}"'
    hostlist = _run_for_output(hostlist_cmd, context=f"job id {job_id}").strip()
    return _resolve_single_hostname(_expand_hostlist(hostlist), context=f"job id {job_id}")


def _resolve_job_name(job_name: str) -> str:
    quoted_name = shlex.quote(job_name)
    output = _run_for_output(f'squeue -h -u "$USER" -n {quoted_name} -t R -o "%A|%N"', context=f"job name '{job_name}'")
    matches: list[tuple[str, str]] = []
    for raw_line in output.splitlines():
        stripped_line = raw_line.strip()
        if not stripped_line:
            continue
        job_id, _, hostlist = stripped_line.partition("|")
        if not job_id or not hostlist:
            continue
        node = _resolve_single_hostname(_expand_hostlist(hostlist), context=f"job {job_id}")
        matches.append((job_id, node))

    if not matches:
        msg = f"No running jobs found with name '{job_name}'."
        raise NodeResolutionError(msg)
    if len(matches) > 1:
        job_ids = ", ".join(job_id for job_id, _ in matches)
        msg = f"Multiple running jobs found with name '{job_name}': {job_ids}. Use --job-id instead."
        raise NodeResolutionError(msg)
    return matches[0][1]


def _expand_hostlist(hostlist: str) -> list[str]:
    if not hostlist:
        return []
    output = _run_for_output(f"scontrol show hostnames {shlex.quote(hostlist)}", context=f"hostlist '{hostlist}'")
    return [line.strip() for line in output.splitlines() if line.strip()]


def _resolve_single_hostname(hostnames: list[str], *, context: str) -> str:
    if not hostnames:
        msg = f"No node resolved for {context}."
        raise NodeResolutionError(msg)
    if len(hostnames) > 1:
        joined = ", ".join(hostnames)
        msg = f"{context} resolves to multiple nodes ({joined}). Pass --node explicitly."
        raise NodeResolutionError(msg)
    return hostnames[0]
