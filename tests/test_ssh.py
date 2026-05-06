from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from dbx.ssh import RemoteCommandError, build_attach_command, run_remote_shell_command


class SshTests(unittest.TestCase):
    def test_attach_command_targets_tmux_session(self) -> None:
        command = build_attach_command("ubuntu@dbx-job.tail.ts.net", "dbx-job-123")
        self.assertEqual(
            command,
            ["ssh", "-t", "ubuntu@dbx-job.tail.ts.net", "tmux attach -t dbx-job-123"],
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
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=10",
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


if __name__ == "__main__":
    unittest.main()
