from __future__ import annotations

import shlex
import subprocess


class RemoteCommandError(RuntimeError):
    pass


def build_attach_command(target: str, session_name: str) -> list[str]:
    return ["tailscale", "ssh", target, "tmux", "attach", "-t", session_name]


def run_ssh_command(target: str, remote_command: str) -> int:
    result = subprocess.run(
        ["tailscale", "ssh", target, "bash", "-lc", remote_command],
        check=False,
    )
    return result.returncode


def run_remote_shell_command(
    target: str,
    shell_command: str,
    *,
    capture_output: bool = True,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    command = [
        "tailscale",
        "ssh",
        target,
        "bash",
        "-lc",
        shell_command,
    ]
    result = subprocess.run(
        command,
        check=False,
        capture_output=capture_output,
        text=True,
    )
    if check and result.returncode != 0:
        raise RemoteCommandError(result.stderr.strip() or "Remote SSH command failed.")
    return result
