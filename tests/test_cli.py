from __future__ import annotations

import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dbx import cli


class CliTests(unittest.TestCase):
    def test_doctor_succeeds_when_core_tools_exist(self) -> None:
        with patch("dbx.cli.shutil.which", return_value="/usr/bin/fake"):
            with patch("sys.stdout", new=io.StringIO()) as stdout:
                exit_code = cli.main(["doctor"])
        self.assertEqual(exit_code, 0)
        self.assertIn('"aws": true', stdout.getvalue())

    def test_start_requires_existing_mission_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text(_sample_config(), encoding="utf-8")
            with patch("sys.stderr", new=io.StringIO()) as stderr:
                exit_code = cli.main(
                    ["--config", str(config_path), "start", "er-fo/db-x", "missing.md"]
                )
        self.assertEqual(exit_code, 2)
        self.assertIn("Mission file not found", stderr.getvalue())


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
