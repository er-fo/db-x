from __future__ import annotations

import shlex
import subprocess


def build_attach_command(target: str, session_name: str) -> list[str]:
    remote = f"tmux attach -t {shlex.quote(session_name)}"
    return ["ssh", "-t", target, remote]


def run_ssh_command(target: str, remote_command: str) -> int:
    result = subprocess.run(["ssh", target, remote_command], check=False)
    return result.returncode
