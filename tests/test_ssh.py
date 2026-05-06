from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from dbx.ssh import (
    RemoteCommandError,
    build_attach_command,
    run_remote_shell_command,
    upload_remote_text,
)


class SshTests(unittest.TestCase):
    def test_attach_command_targets_tmux_session(self) -> None:
        command = build_attach_command("ubuntu@dbx-job.tail.ts.net", "dbx-job-123")
        self.assertEqual(
            command,
            [
                "ssh",
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
                "ubuntu@dbx-job.tail.ts.net",
                "tmux",
                "attach",
                "-t",
                "dbx-job-123",
            ],
        )

    def test_run_remote_shell_command_uses_bash_login_shell(self) -> None:
        with patch(
            "dbx.ssh.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr=""),
        ) as run:
            result = run_remote_shell_command(
                "ubuntu@dbx-job.tail.ts.net",
                "cat /tmp/status.json",
            )

        self.assertEqual(result.stdout, "ok")
        run.assert_called_once_with(
            [
                "ssh",
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
                "ubuntu@dbx-job.tail.ts.net",
                "bash",
                "-lc",
                "cat /tmp/status.json",
            ],
            check=False,
            capture_output=True,
            text=True,
        )

    def test_run_remote_shell_command_raises_on_nonzero_exit(self) -> None:
        with patch(
            "dbx.ssh.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout="",
                stderr="permission denied",
            ),
        ):
            with self.assertRaises(RemoteCommandError):
                run_remote_shell_command("ubuntu@dbx-job.tail.ts.net", "false")

    def test_upload_remote_text_creates_parent_and_streams_content(self) -> None:
        with patch(
            "dbx.ssh.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        ) as run:
            upload_remote_text(
                "ubuntu@dbx-job.tail.ts.net",
                "/home/ubuntu/.codex/sessions/2026/05/06/session.jsonl",
                "{\"type\":\"session\"}\n",
            )

        run.assert_called_once_with(
            [
                "ssh",
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
                "ubuntu@dbx-job.tail.ts.net",
                "bash",
                "-lc",
                "mkdir -p /home/ubuntu/.codex/sessions/2026/05/06 && cat > /home/ubuntu/.codex/sessions/2026/05/06/session.jsonl",
            ],
            check=False,
            capture_output=True,
            input="{\"type\":\"session\"}\n",
            text=True,
        )


if __name__ == "__main__":
    unittest.main()
