from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from dbx.aws import AwsCliError, JobLaunchRequest, build_ssh_target, build_user_data
from dbx.aws import run_aws_cli
from dbx.config import load_config


class AwsTests(unittest.TestCase):
    def test_run_aws_cli_reports_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text(_sample_config(), encoding="utf-8")
            config = load_config(str(config_path))

        with patch(
            "dbx.aws.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["aws"], timeout=60),
        ):
            with self.assertRaises(AwsCliError):
                run_aws_cli(config, ["sts", "get-caller-identity"])

    def test_build_ssh_target_keeps_tailscale_hostname_short(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text(_sample_config(), encoding="utf-8")
            config = load_config(str(config_path))
            config = replace(config, tailscale_domain="tail123.ts.net")

        target = build_ssh_target(
            config,
            {"Tags": [{"Key": "Name", "Value": "dbx-job"}]},
        )

        self.assertEqual(target, "ubuntu@dbx-job")

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

    def test_build_user_data_writes_explicit_status_and_log_artifacts(self) -> None:
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

        self.assertIn("status.json", script)
        self.assertIn("logs/bootstrap.log", script)
        self.assertIn("logs/codex.log", script)
        self.assertIn("logs/finish.log", script)
        self.assertIn("AGENT_STARTED.json", script)
        self.assertIn("BLOCKER.md", script)
        self.assertIn("trap '", script)
        self.assertIn("TAILSCALE_UP_ARGS=(--ssh", script)
        self.assertIn("TAILSCALE_AUTH_KEY", script)
        self.assertIn("TAILSCALE_TAGS=tag:dbx", script)
        self.assertIn('--advertise-tags "$TAILSCALE_TAGS"', script)
        self.assertIn("--hostname \"$SESSION_NAME\"", script)
        self.assertIn("gh auth setup-git", script)
        self.assertIn("sudo -u \"$DBX_USER\" -H git clone", script)
        self.assertIn("sudo -u \"$DBX_USER\" -H codex login status", script)
        self.assertIn("codex_auth_failed", script)
        self.assertIn("[projects.", script)
        self.assertIn("trust_level = \"trusted\"", script)
        self.assertIn("sudo -u \"$DBX_USER\" -H tmux new-session", script)
        self.assertIn("tmux pipe-pane -o", script)
        self.assertNotIn("| tee '$LOG_FILE'", script)

    def test_build_user_data_waits_for_agent_heartbeat_before_ready(self) -> None:
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

        self.assertIn('write_status "runtime" "starting" "waiting_for_agent_heartbeat"', script)
        self.assertNotIn('write_status "runtime" "ready" "tmux session started"', script)
        self.assertIn("write AGENT_STARTED.json", script)
        self.assertIn('"status": "running"', script)
        self.assertIn('"session_name": "dbx-ship-123"', script)

    def test_build_user_data_is_valid_bash(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            mission_path = Path(tmpdir) / "mission.md"
            script_path = Path(tmpdir) / "user-data.sh"
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

            script_path.write_text(build_user_data(config, request), encoding="utf-8")

            result = subprocess.run(
                ["bash", "-n", str(script_path)],
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_build_user_data_uses_codex_resume_when_requested(self) -> None:
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
                resume_session_id="019dfdac-5bea-71f0-91c5-4fdd8826860b",
                resume_session_relative_path=(
                    "2026/05/06/rollout-2026-05-06T16-23-44-"
                    "019dfdac-5bea-71f0-91c5-4fdd8826860b.jsonl"
                ),
            )

            script = build_user_data(config, request)

        self.assertIn(
            'codex --no-alt-screen --ask-for-approval never --sandbox danger-full-access -C /home/ubuntu/work/dbx-ship-123/repo resume 019dfdac-5bea-71f0-91c5-4fdd8826860b',
            script,
        )
        self.assertIn(
            '"$(cat /home/ubuntu/work/dbx-ship-123/BOOTSTRAP_PROMPT.txt)"',
            script,
        )
        self.assertNotIn('"$(cat "$PROMPT_FILE")"', script)
        self.assertIn(
            "RESUME_SESSION_REMOTE_PATH=/home/ubuntu/.codex/sessions/2026/05/06/rollout-2026-05-06T16-23-44-019dfdac-5bea-71f0-91c5-4fdd8826860b.jsonl",
            script,
        )
        self.assertIn("waiting for codex resume session upload", script)

    def test_build_user_data_installs_auto_finish_lifecycle(self) -> None:
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
                base_branch="feature/base",
            )

            script = build_user_data(config, request)

        self.assertIn("BASE_BRANCH=feature/base", script)
        self.assertIn("/usr/local/bin/dbx-finish-ready", script)
        self.assertIn("/usr/local/bin/dbx-finish-job", script)
        self.assertIn("/usr/local/bin/dbx-codex-watch", script)
        self.assertIn("codex_hooks = true", script)
        self.assertIn("hooks.json", script)
        self.assertIn('"event": "Stop"', script)
        self.assertIn("DBX_FINISH_READY", script)
        self.assertIn("flock -n 9", script)
        self.assertIn("git status --porcelain", script)
        self.assertIn("gh pr create --draft", script)
        self.assertIn("gh pr create --base \"$BASE_BRANCH\"", script)
        self.assertIn("shutdown -h now", script)
        self.assertIn("dbx-codex-watch", script)
        self.assertIn("dbx-finish-ready complete", script)
        self.assertIn("dbx-finish-ready blocked", script)
        self.assertLess(
            script.index("[ -f \"$READY_FILE\" ] || exit 0"),
            script.index("exec 9>\"$LOCK_FILE\""),
        )
        self.assertIn("DBX_FINISH_DONE", script)


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
tailscale_auth_key = "tskey-auth-123"
tailscale_tags = ["tag:dbx"]
repo_root = "/home/ubuntu/work"
job_prefix = "dbx"
"""
