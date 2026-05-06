from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from dbx import cli
from dbx.state import JobState


class CliTests(unittest.TestCase):
    def test_sessions_lists_local_codex_sessions_with_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / ".codex" / "sessions"
            session_a = root / "2026" / "05" / "06" / (
                "rollout-2026-05-06T21-11-54-"
                "019dfeb4-2e25-7173-8ab9-006893040db2.jsonl"
            )
            session_b = root / "2026" / "05" / "05" / (
                "rollout-2026-05-05T19-00-00-"
                "019dfdac-5bea-71f0-91c5-4fdd8826860b.jsonl"
            )
            session_a.parent.mkdir(parents=True)
            session_b.parent.mkdir(parents=True)
            session_a.write_text("{}\n", encoding="utf-8")
            session_b.write_text("{}\n", encoding="utf-8")
            session_a.touch()
            session_b.touch()
            os.utime(session_a, (200, 200))
            os.utime(session_b, (100, 100))
            with patch("dbx.cli._codex_sessions_root", return_value=root):
                with patch("sys.stdout", new=io.StringIO()) as stdout:
                    exit_code = cli.main(["sessions"])

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(len(payload), 2)
        self.assertEqual(
            payload[0]["session_id"],
            "019dfeb4-2e25-7173-8ab9-006893040db2",
        )
        self.assertTrue(payload[0]["path"].endswith(str(session_a)))
        self.assertEqual(
            payload[1]["relative_path"],
            "2026/05/05/rollout-2026-05-05T19-00-00-019dfdac-5bea-71f0-91c5-4fdd8826860b.jsonl",
        )

    def test_start_can_pick_resume_session_interactively(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / ".codex" / "sessions"
            picked_session = root / "2026" / "05" / "06" / (
                "rollout-2026-05-06T21-11-54-"
                "019dfeb4-2e25-7173-8ab9-006893040db2.jsonl"
            )
            picked_session.parent.mkdir(parents=True)
            picked_session.write_text("{}\n", encoding="utf-8")
            config_path = Path(tmpdir) / "config.toml"
            mission_path = Path(tmpdir) / "mission.md"
            config_path.write_text(_sample_config(), encoding="utf-8")
            mission_path.write_text("# Mission\nShip it.\n", encoding="utf-8")
            saved_jobs = []
            stdin = _TTYInput("1\n")
            with patch("dbx.cli._codex_sessions_root", return_value=root):
                with patch("sys.stdin", new=stdin):
                    with patch("sys.stderr", new=io.StringIO()) as stderr:
                        with patch(
                            "dbx.cli.launch_instance",
                            return_value={"Instances": [{"InstanceId": "i-123"}]},
                        ):
                            with patch(
                                "dbx.cli._wait_for_runtime_ready",
                                return_value={"phase": "runtime", "state": "ready"},
                            ):
                                with patch(
                                    "dbx.cli._upload_resume_session_when_reachable",
                                    return_value={"remote_path": "/home/ubuntu/.codex/sessions/picked.jsonl"},
                                ):
                                    with patch(
                                        "dbx.cli.save_job_state",
                                        side_effect=saved_jobs.append,
                                    ):
                                        with patch("sys.stdout", new=io.StringIO()) as stdout:
                                            exit_code = cli.main(
                                                [
                                                    "--config",
                                                    str(config_path),
                                                    "start",
                                                    "er-fo/db-x",
                                                    str(mission_path),
                                                    "--pick-session",
                                                ]
                                            )

        self.assertEqual(exit_code, 0)
        self.assertIn("Local Codex sessions", stderr.getvalue())
        self.assertIn(str(picked_session), stderr.getvalue())
        self.assertTrue(saved_jobs)
        self.assertEqual(
            saved_jobs[-1].resume_session_id,
            "019dfeb4-2e25-7173-8ab9-006893040db2",
        )
        payload = json.loads(stdout.getvalue())
        self.assertEqual(
            payload["resume_session_id"],
            "019dfeb4-2e25-7173-8ab9-006893040db2",
        )

    def test_start_pick_session_fails_when_not_interactive(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            mission_path = Path(tmpdir) / "mission.md"
            config_path.write_text(_sample_config(), encoding="utf-8")
            mission_path.write_text("# Mission\nShip it.\n", encoding="utf-8")
            with patch("sys.stdin", new=io.StringIO("")):
                with patch("sys.stderr", new=io.StringIO()) as stderr:
                    exit_code = cli.main(
                        [
                            "--config",
                            str(config_path),
                            "start",
                            "er-fo/db-x",
                            str(mission_path),
                            "--pick-session",
                        ]
                    )

        self.assertEqual(exit_code, 2)
        self.assertIn("Cannot pick a Codex session without an interactive terminal", stderr.getvalue())

    def test_doctor_fails_without_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "missing.toml"
            with patch("dbx.cli.shutil.which", return_value="/usr/bin/fake"):
                with patch("sys.stdout", new=io.StringIO()) as stdout:
                    exit_code = cli.main(["--config", str(config_path), "doctor"])
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

    def test_start_waits_for_runtime_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            mission_path = Path(tmpdir) / "mission.md"
            config_path.write_text(_sample_config(), encoding="utf-8")
            mission_path.write_text("# Mission\nShip it.\n", encoding="utf-8")
            saved_jobs = []
            with patch(
                "dbx.cli.launch_instance",
                return_value={"Instances": [{"InstanceId": "i-123"}]},
            ):
                with patch(
                    "dbx.cli._wait_for_runtime_ready",
                    return_value={"phase": "runtime", "state": "ready"},
                    create=True,
                ) as wait:
                    with patch("dbx.cli.save_job_state", side_effect=saved_jobs.append):
                        with patch("sys.stdout", new=io.StringIO()) as stdout:
                            exit_code = cli.main(
                                [
                                    "--config",
                                    str(config_path),
                                    "start",
                                    "er-fo/db-x",
                                    str(mission_path),
                                ]
                            )
        self.assertEqual(exit_code, 0)
        self.assertTrue(saved_jobs)
        self.assertEqual(saved_jobs[-1].job_root, "/home/ubuntu/work/" + saved_jobs[-1].job_name)
        wait.assert_called_once()
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["instance_id"], "i-123")
        self.assertEqual(payload["runtime"]["state"], "ready")

    def test_start_forwards_resume_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            mission_path = Path(tmpdir) / "mission.md"
            config_path.write_text(_sample_config(), encoding="utf-8")
            mission_path.write_text("# Mission\nShip it.\n", encoding="utf-8")
            saved_jobs = []
            with patch(
                "dbx.cli.launch_instance",
                return_value={"Instances": [{"InstanceId": "i-123"}]},
            ) as launch_instance:
                with patch(
                    "dbx.cli._wait_for_runtime_ready",
                    return_value={"phase": "runtime", "state": "ready"},
                    create=True,
                ):
                    with patch(
                        "dbx.cli._find_codex_session_file",
                        return_value=Path("/tmp/session.jsonl"),
                    ):
                        with patch(
                            "dbx.cli._codex_session_relative_path",
                            return_value=(
                                "2026/05/06/rollout-2026-05-06T16-23-44-"
                                "019dfdac-5bea-71f0-91c5-4fdd8826860b.jsonl"
                            ),
                        ):
                            with patch(
                                "dbx.cli._upload_resume_session_when_reachable",
                                return_value={
                                    "remote_path": (
                                        "/home/ubuntu/.codex/sessions/2026/05/06/"
                                        "rollout-2026-05-06T16-23-44-"
                                        "019dfdac-5bea-71f0-91c5-4fdd8826860b.jsonl"
                                    )
                                },
                            ) as upload_session:
                                with patch("dbx.cli.save_job_state", side_effect=saved_jobs.append):
                                    with patch("sys.stdout", new=io.StringIO()) as stdout:
                                        exit_code = cli.main(
                                            [
                                                "--config",
                                                str(config_path),
                                                "start",
                                                "--resume-session",
                                                "019dfdac-5bea-71f0-91c5-4fdd8826860b",
                                                "er-fo/db-x",
                                                str(mission_path),
                                            ]
                                        )

        self.assertEqual(exit_code, 0)
        self.assertTrue(saved_jobs)
        self.assertEqual(
            saved_jobs[-1].resume_session_id,
            "019dfdac-5bea-71f0-91c5-4fdd8826860b",
        )
        payload = json.loads(stdout.getvalue())
        self.assertEqual(
            payload["resume_session_id"],
            "019dfdac-5bea-71f0-91c5-4fdd8826860b",
        )
        request = launch_instance.call_args.args[1]
        self.assertEqual(
            request.resume_session_relative_path,
            "2026/05/06/rollout-2026-05-06T16-23-44-019dfdac-5bea-71f0-91c5-4fdd8826860b.jsonl",
        )
        upload_session.assert_called_once()

    def test_start_fails_when_requested_resume_session_is_not_local(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            mission_path = Path(tmpdir) / "mission.md"
            config_path.write_text(_sample_config(), encoding="utf-8")
            mission_path.write_text("# Mission\nShip it.\n", encoding="utf-8")
            with patch("dbx.cli._find_codex_session_file", return_value=None):
                with patch("sys.stderr", new=io.StringIO()) as stderr:
                    exit_code = cli.main(
                        [
                            "--config",
                            str(config_path),
                            "start",
                            "--resume-session",
                            "missing-session",
                            "er-fo/db-x",
                            str(mission_path),
                        ]
                    )

        self.assertEqual(exit_code, 2)
        self.assertIn("Codex session not found locally", stderr.getvalue())

    def test_resume_session_upload_uses_job_hostname_without_aws_polling(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = cli.load_config(_write_sample_config(tmpdir))
            session_file = Path(tmpdir) / "session.jsonl"
            session_file.write_text("{\"type\":\"session\"}\n", encoding="utf-8")
            with patch("dbx.cli.describe_instance", side_effect=AssertionError("no aws poll")):
                with patch("dbx.cli.time.sleep"):
                    with patch(
                        "dbx.cli.upload_remote_text",
                        side_effect=[cli.RemoteCommandError("not ready"), None],
                    ) as upload:
                        result = cli._upload_resume_session_when_reachable(
                            config,
                            _sample_job_state(),
                            session_file,
                            "2026/05/06/session.jsonl",
                            timeout_seconds=30,
                        )

        self.assertTrue(result["uploaded"])
        self.assertEqual(upload.call_args.args[0], "ubuntu@dbx-job")
        self.assertEqual(
            upload.call_args.args[1],
            "/home/ubuntu/.codex/sessions/2026/05/06/session.jsonl",
        )

    def test_finish_command_dispatches_to_run_finish(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text(_sample_config(), encoding="utf-8")
            with patch("dbx.cli.run_finish", return_value=0, create=True) as run_finish:
                exit_code = cli.main(["--config", str(config_path), "finish", "i-123"])

        self.assertEqual(exit_code, 0)
        run_finish.assert_called_once()

    def test_finish_updates_pr_and_terminates_after_preservation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text(_sample_config(), encoding="utf-8")
            saved_jobs = []
            with patch(
                "dbx.cli.describe_instance",
                return_value={
                    "InstanceId": "i-123",
                    "State": {"Name": "running"},
                    "Tags": [{"Key": "Name", "Value": "dbx-job"}],
                },
            ):
                with patch(
                    "dbx.cli._load_or_infer_job_state",
                    return_value=_sample_job_state(),
                ):
                    with patch("dbx.cli.build_ssh_target", return_value="ubuntu@dbx-job"):
                        with patch(
                            "dbx.cli._capture_remote_artifacts",
                            return_value={
                                "status_json": {"phase": "runtime", "state": "ready"},
                                "status_md": "# dbx-job\n",
                                "blocker": None,
                                "bootstrap_log": "boot\n",
                                "codex_log": "codex\n",
                                "finish_log": "finish\n",
                            },
                        ):
                            with patch(
                                "dbx.cli._run_finish_remote",
                                return_value={"pr_url": "https://github.com/er-fo/db-x/pull/1"},
                            ):
                                with patch(
                                    "dbx.cli._terminate_job",
                                    return_value={"final_state": "terminated"},
                                ) as terminate_job:
                                    with patch(
                                        "dbx.cli.save_job_state",
                                        side_effect=saved_jobs.append,
                                    ):
                                        with patch("sys.stdout", new=io.StringIO()) as stdout:
                                            exit_code = cli.main(
                                                ["--config", str(config_path), "finish", "i-123"]
                                            )

        self.assertEqual(exit_code, 0)
        terminate_job.assert_called_once()
        self.assertEqual(saved_jobs[-1].pr_url, "https://github.com/er-fo/db-x/pull/1")
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["termination"]["final_state"], "terminated")

    def test_status_includes_remote_logs_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text(_sample_config(), encoding="utf-8")
            with patch(
                "dbx.cli.describe_instance",
                return_value={
                    "InstanceId": "i-123",
                    "State": {"Name": "running"},
                    "PrivateDnsName": "dbx-job",
                    "PrivateIpAddress": "10.0.0.1",
                    "Tags": [{"Key": "Name", "Value": "dbx-job"}],
                },
            ):
                with patch(
                    "dbx.cli.load_job_state",
                    return_value=_sample_job_state(),
                ):
                    with patch(
                        "dbx.cli.describe_instance_status",
                        return_value={
                            "SystemStatus": {"Status": "ok"},
                            "InstanceStatus": {"Status": "ok"},
                        },
                    ):
                        with patch("dbx.cli.build_ssh_target", return_value="ubuntu@dbx-job"):
                            with patch(
                                "dbx.cli._capture_remote_artifacts",
                                return_value={
                                    "status_json": {"phase": "runtime", "state": "ready"},
                                    "status_md": "# dbx-job\n",
                                    "blocker": None,
                                    "bootstrap_log": "boot\n",
                                    "codex_log": "codex\n",
                                    "finish_log": "finish\n",
                                },
                            ):
                                with patch("sys.stdout", new=io.StringIO()) as stdout:
                                    exit_code = cli.main(
                                        ["--config", str(config_path), "status", "i-123", "--logs"]
                                    )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["remote_status"]["state"], "ready")
        self.assertEqual(payload["logs"]["codex"], "codex\n")

    def test_wait_for_runtime_ready_reports_but_does_not_block_on_ec2_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = cli.load_config(_write_sample_config(tmpdir))
            with patch(
                "dbx.cli.describe_instance",
                return_value={
                    "InstanceId": "i-123",
                    "State": {"Name": "running"},
                    "Tags": [{"Key": "Name", "Value": "dbx-job"}],
                },
            ):
                with patch(
                    "dbx.cli.describe_instance_status",
                    return_value={
                        "SystemStatus": {"Status": "initializing"},
                        "InstanceStatus": {"Status": "initializing"},
                    },
                ):
                    with patch("dbx.cli.build_ssh_target", return_value="ubuntu@dbx-job"):
                        with patch("dbx.cli._remote_file_exists", return_value=True):
                            with patch(
                                "dbx.cli._read_remote_json",
                                return_value={"phase": "runtime", "state": "ready"},
                            ):
                                with patch(
                                    "dbx.cli._remote_tmux_session_exists",
                                    return_value=True,
                                ):
                                    runtime = cli._wait_for_runtime_ready(
                                        config,
                                        _sample_job_state(),
                                        timeout_seconds=1,
                                        poll_interval_seconds=0,
                                    )

        self.assertEqual(runtime["state"], "ready")
        self.assertEqual(runtime["system_status"], "initializing")
        self.assertEqual(runtime["instance_status"], "initializing")

    def test_attach_check_reports_ready_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text(_sample_config(), encoding="utf-8")
            with patch(
                "dbx.cli.describe_instance",
                return_value={
                    "InstanceId": "i-123",
                    "State": {"Name": "running"},
                    "Tags": [{"Key": "Name", "Value": "dbx-job"}],
                },
            ):
                with patch(
                    "dbx.cli._load_or_infer_job_state",
                    return_value=_sample_job_state(),
                ):
                    with patch("dbx.cli.build_ssh_target", return_value="ubuntu@dbx-job"):
                        with patch("dbx.cli._verify_remote_attach_ready") as verify:
                            with patch("sys.stdout", new=io.StringIO()) as stdout:
                                exit_code = cli.main(
                                    ["--config", str(config_path), "attach", "i-123", "--check"]
                                )

        self.assertEqual(exit_code, 0)
        verify.assert_called_once()
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["ready"])

    def test_terminate_updates_local_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text(_sample_config(), encoding="utf-8")
            saved_jobs = []
            with patch(
                "dbx.cli.describe_instance",
                return_value={
                    "InstanceId": "i-123",
                    "State": {"Name": "running"},
                    "Tags": [{"Key": "Name", "Value": "dbx-job"}],
                },
            ):
                with patch(
                    "dbx.cli._load_or_infer_job_state",
                    return_value=_sample_job_state(),
                ):
                    with patch("dbx.cli.build_ssh_target", return_value="ubuntu@dbx-job"):
                        with patch("dbx.cli._capture_remote_artifacts", return_value={}):
                            with patch("dbx.cli._persist_remote_artifacts"):
                                with patch(
                                    "dbx.cli.terminate_instance",
                                    return_value={"TerminatingInstances": [{"InstanceId": "i-123"}]},
                                ):
                                    with patch(
                                        "dbx.cli.wait_for_instance_terminated",
                                        return_value={"State": {"Name": "terminated"}},
                                    ):
                                        with patch(
                                            "dbx.cli.save_job_state",
                                            side_effect=saved_jobs.append,
                                        ):
                                            with patch("sys.stdout", new=io.StringIO()):
                                                exit_code = cli.main(
                                                    ["--config", str(config_path), "terminate", "i-123"]
                                                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(saved_jobs[-1].status, "terminated")
        self.assertEqual(saved_jobs[-1].lifecycle_state, "terminated")

    def test_terminate_continues_when_remote_artifact_capture_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text(_sample_config(), encoding="utf-8")
            with patch(
                "dbx.cli.describe_instance",
                return_value={
                    "InstanceId": "i-123",
                    "State": {"Name": "running"},
                    "Tags": [{"Key": "Name", "Value": "dbx-job"}],
                },
            ):
                with patch(
                    "dbx.cli._load_or_infer_job_state",
                    return_value=_sample_job_state(),
                ):
                    with patch("dbx.cli.build_ssh_target", return_value="ubuntu@dbx-job"):
                        with patch(
                            "dbx.cli._capture_remote_artifacts",
                            side_effect=cli.RemoteCommandError("bad remote json"),
                        ):
                            with patch(
                                "dbx.cli._console_output_excerpt",
                                return_value="console failure",
                            ):
                                with patch("dbx.cli.save_job_artifact") as save_artifact:
                                    with patch(
                                        "dbx.cli.terminate_instance",
                                        return_value={
                                            "TerminatingInstances": [{"InstanceId": "i-123"}]
                                        },
                                    ) as terminate_instance:
                                        with patch(
                                            "dbx.cli.wait_for_instance_terminated",
                                            return_value={"State": {"Name": "terminated"}},
                                        ):
                                            with patch("sys.stdout", new=io.StringIO()):
                                                exit_code = cli.main(
                                                    [
                                                        "--config",
                                                        str(config_path),
                                                        "terminate",
                                                        "i-123",
                                                    ]
                                                )

        self.assertEqual(exit_code, 0)
        terminate_instance.assert_called_once()
        save_artifact.assert_called_once()

    def test_read_remote_json_reports_invalid_payload_as_remote_error(self) -> None:
        with patch(
            "dbx.cli.run_remote_shell_command",
            return_value=subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="not-json",
                stderr="",
            ),
        ):
            with self.assertRaises(cli.RemoteCommandError):
                cli._read_remote_json("ubuntu@dbx-job", "/tmp/status.json")


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


def _write_sample_config(tmpdir: str) -> str:
    config_path = Path(tmpdir) / "config.toml"
    config_path.write_text(_sample_config(), encoding="utf-8")
    return str(config_path)


def _sample_job_state() -> JobState:
    return JobState(
        instance_id="i-123",
        job_name="dbx-job",
        repo="er-fo/db-x",
        branch_name="agent/test",
        session_name="dbx-job",
        mission_path="/tmp/mission.md",
        created_at="2026-05-06T12:00:00Z",
        job_root="/home/ubuntu/work/dbx-job",
        lifecycle_state="running",
        status="ready",
    )


class _TTYInput(io.StringIO):
    def isatty(self) -> bool:
        return True
