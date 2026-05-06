from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from dbx.aws import JobLaunchRequest, build_user_data
from dbx.config import load_config


class AwsTests(unittest.TestCase):
    def test_build_user_data_contains_clone_and_tmux_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            mission_path = Path(tmpdir) / "mission.md"
            config_path.write_text(_sample_config(), encoding="utf-8")
            mission_path.write_text("# Mission\nShip it.\n", encoding="utf-8")
            config = load_config(str(config_path))
            request = JobLaunchRequest(
                repo="er-fo/db-x",
                mission_path=mission_path,
                job_name="dbx-ship-123",
                branch_name="agent/ship-123",
                session_name="dbx-ship-123",
            )

            script = build_user_data(config, request)

        self.assertIn("git clone", script)
        self.assertIn("tmux new-session -d -s", script)
        self.assertIn("codex --no-alt-screen", script)
        self.assertIn("AGENT_MISSION.md", script)


def _sample_config() -> str:
    return """
aws_region = "eu-north-1"
default_owner = "er-fo"
default_base_branch = "main"
ami_id = "ami-123"
subnet_id = "subnet-123"
security_group_id = "sg-123"
instance_type = "c7i.xlarge"
ssh_user = "ubuntu"
repo_root = "/home/ubuntu/work"
job_prefix = "dbx"
"""
