from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from dbx.config import load_config, resolve_config_path


class ConfigTests(unittest.TestCase):
    def test_resolve_config_path_prefers_explicit_path(self) -> None:
        resolved = resolve_config_path("~/custom-db-x.toml")
        self.assertTrue(str(resolved).endswith("custom-db-x.toml"))

    def test_load_config_reads_required_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text(
                """
aws_profile = "personal"
aws_region = "eu-north-1"
default_owner = "er-fo"
default_base_branch = "main"
ami_id = "ami-123"
subnet_id = "subnet-123"
security_group_id = "sg-123"
instance_type = "c7i.xlarge"
ssh_user = "ubuntu"
tailscale_domain = "tailnet.ts.net"
repo_root = "/home/ubuntu/work"
job_prefix = "dbx"
""",
                encoding="utf-8",
            )
            config = load_config(str(config_path))

        self.assertEqual(config.aws_profile, "personal")
        self.assertEqual(config.aws_region, "eu-north-1")
        self.assertEqual(config.tailscale_domain, "tailnet.ts.net")


if __name__ == "__main__":
    unittest.main()
