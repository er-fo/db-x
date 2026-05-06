from __future__ import annotations

import unittest

from dbx.ssh import build_attach_command


class SshTests(unittest.TestCase):
    def test_attach_command_targets_tmux_session(self) -> None:
        command = build_attach_command("ubuntu@dbx-job.tail.ts.net", "dbx-job-123")
        self.assertEqual(
            command,
            ["ssh", "-t", "ubuntu@dbx-job.tail.ts.net", "tmux attach -t dbx-job-123"],
        )


if __name__ == "__main__":
    unittest.main()
