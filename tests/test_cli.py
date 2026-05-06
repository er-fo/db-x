from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dbx import cli


class CliTests(unittest.TestCase):
    def test_doctor_fails_without_config(self) -> None:
        with patch("dbx.cli.shutil.which", return_value="/usr/bin/fake"):
            with patch("sys.stdout", new=io.StringIO()) as stdout:
                exit_code = cli.main(["doctor"])
        self.assertEqual(exit_code, 1)
        self.assertIn('"config_ok": false', stdout.getvalue())

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

    def test_doctor_reports_config_and_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text(_sample_config(), encoding="utf-8")
            with patch("dbx.cli.shutil.which", return_value="/usr/bin/fake"):
                with patch(
                    "dbx.cli._aws_identity",
                    return_value={"Account": "123456789012", "Arn": "arn:aws:iam::123:user/me"},
                ):
                    with patch("sys.stdout", new=io.StringIO()) as stdout:
                        exit_code = cli.main(["--config", str(config_path), "doctor"])
        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["config_ok"])
        self.assertEqual(payload["aws_identity"]["Account"], "123456789012")

    def test_init_config_writes_default_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            with patch("sys.stdout", new=io.StringIO()) as stdout:
                exit_code = cli.main(["--config", str(config_path), "init-config"])
            contents = config_path.read_text(encoding="utf-8")
        self.assertEqual(exit_code, 0)
        self.assertIn('aws_region = "eu-north-1"', contents)
        self.assertIn('"written": true', stdout.getvalue())


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
