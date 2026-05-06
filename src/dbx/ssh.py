from __future__ import annotations

import posixpath
import shlex
import subprocess


class RemoteCommandError(RuntimeError):
    pass


SSH_OPTIONS = [
    "-o",
    "ProxyCommand=tailscale nc %h %p",
    "-o",
    "BatchMode=yes",
    "-o",
    "StrictHostKeyChecking=accept-new",
    "-o",
    "ServerAliveInterval=15",
    "-o",
    "ConnectTimeout=20",
]


def build_attach_command(target: str, session_name: str) -> list[str]:
    return ["ssh", *SSH_OPTIONS, target, "tmux", "attach", "-t", session_name]


def run_ssh_command(target: str, remote_command: str) -> int:
    result = subprocess.run(
        ["ssh", *SSH_OPTIONS, target, "bash", "-lc", remote_command],
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
        "ssh",
        *SSH_OPTIONS,
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


def upload_remote_text(target: str, remote_path: str, content: str) -> None:
    remote_dir = posixpath.dirname(remote_path)
    command = [
        "ssh",
        *SSH_OPTIONS,
        target,
        "bash",
        "-lc",
        f"mkdir -p {shlex.quote(remote_dir)} && cat > {shlex.quote(remote_path)}",
    ]
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        input=content,
        text=True,
    )
    if result.returncode != 0:
        raise RemoteCommandError(result.stderr.strip() or "Remote upload failed.")
