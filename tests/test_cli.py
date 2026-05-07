from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from dbx import cli
from dbx.state import JobState


class CliTests(unittest.TestCase):
    def test_sessions_prints_simple_ten_row_table_with_latest_user_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / ".codex" / "sessions"
            newest = _write_codex_session(
                root,
                session_id="019dfeb4-2e25-7173-8ab9-006893040db2",
                created_at="2026-05-06T21:11:54Z",
                latest_user_message="no, it shall give me an interactive list",
                mtime=1100,
            )
            oldest = _write_codex_session(
                root,
                session_id="019dfdac-5bea-71f0-91c5-4fdd8826860b",
                created_at="2026-05-05T19:00:00Z",
                latest_user_message="this one should be hidden by the ten row cap",
                mtime=1,
            )
            for index in range(9):
                _write_codex_session(
                    root,
                    session_id=f"019dfaaa-0000-7000-8000-{index:012d}",
                    created_at="2026-05-06T20:00:00Z",
                    latest_user_message=f"middle message {index}",
                    mtime=100 + index,
                )
            with patch("dbx.cli._codex_sessions_root", return_value=root):
                with patch("sys.stdout", new=io.StringIO()) as stdout:
                    exit_code = cli.main(["sessions"])

        self.assertEqual(exit_code, 0)
        output = stdout.getvalue()
        self.assertIn("Created", output)
        self.assertIn("Updated", output)
        self.assertIn("Conversation", output)
        self.assertIn("no, it shall give me an interactive list", output)
        self.assertIn(str(newest), output)
        self.assertNotIn("this one should be hidden", output)
        self.assertNotIn(str(oldest), output)
        self.assertEqual(output.count(".jsonl"), 10)

    def test_sessions_json_is_available_for_automation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / ".codex" / "sessions"
            session_path = _write_codex_session(
                root,
                session_id="019dfeb4-2e25-7173-8ab9-006893040db2",
                created_at="2026-05-06T21:11:54Z",
                latest_user_message="launch this one",
                mtime=100,
            )
            with patch("dbx.cli._codex_sessions_root", return_value=root):
                with patch("sys.stdout", new=io.StringIO()) as stdout:
                    exit_code = cli.main(["sessions", "--json"])

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["latest_user_message"], "launch this one")
        self.assertEqual(payload[0]["path"], str(session_path))

    def test_sessions_ignore_internal_user_role_notifications(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / ".codex" / "sessions"
            _write_codex_session(
                root,
                session_id="019dfeb4-2e25-7173-8ab9-006893040db2",
                created_at="2026-05-06T21:11:54Z",
                latest_user_message="real user request",
                mtime=100,
                extra_user_message="<subagent_notification>{}</subagent_notification>",
            )
            with patch("dbx.cli._codex_sessions_root", return_value=root):
                with patch("sys.stdout", new=io.StringIO()) as stdout:
                    exit_code = cli.main(["sessions"])

        self.assertEqual(exit_code, 0)
        output = stdout.getvalue()
        self.assertIn("real user request", output)
        self.assertNotIn("subagent_notification", output)

    def test_sessions_exclude_subagent_threads(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / ".codex" / "sessions"
            _write_codex_session(
                root,
                session_id="019dfeb4-2e25-7173-8ab9-006893040db2",
                created_at="2026-05-06T21:11:54Z",
                latest_user_message="real user session",
                mtime=100,
            )
            _write_codex_session(
                root,
                session_id="019dfebf-7ab2-74e1-88b1-0c5affae2018",
                created_at="2026-05-06T21:24:14Z",
                latest_user_message="You are reviewing the backend spec-first integration",
                mtime=200,
                source={"subagent": {"thread_spawn": {"parent_thread_id": "parent"}}},
            )
            with patch("dbx.cli._codex_sessions_root", return_value=root):
                with patch("sys.stdout", new=io.StringIO()) as stdout:
                    exit_code = cli.main(["sessions"])

        self.assertEqual(exit_code, 0)
        output = stdout.getvalue()
        self.assertIn("real user session", output)
        self.assertNotIn("You are reviewing", output)
        self.assertEqual(output.count(".jsonl"), 1)

    def test_start_can_pick_resume_session_interactively(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / ".codex" / "sessions"
            picked_session = _write_codex_session(
                root,
                session_id="019dfeb4-2e25-7173-8ab9-006893040db2",
                created_at="2026-05-06T21:11:54Z",
                latest_user_message="resume the deployment planning session",
                mtime=100,
            )
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
        self.assertIn("Created", stderr.getvalue())
        self.assertIn("Conversation", stderr.getvalue())
        self.assertIn("resume the deployment planning session", stderr.getvalue())
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

    def test_start_picker_accepts_arrow_key_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / ".codex" / "sessions"
            _write_codex_session(
                root,
                session_id="019dfeb4-2e25-7173-8ab9-006893040db2",
                created_at="2026-05-06T21:11:54Z",
                latest_user_message="first visible session",
                mtime=200,
            )
            _write_codex_session(
                root,
                session_id="019dfe10-8e20-7580-ba09-86c21ace5c81",
                created_at="2026-05-06T18:13:11Z",
                latest_user_message="second visible session",
                mtime=100,
            )
            config_path = Path(tmpdir) / "config.toml"
            mission_path = Path(tmpdir) / "mission.md"
            config_path.write_text(_sample_config(), encoding="utf-8")
            mission_path.write_text("# Mission\nShip it.\n", encoding="utf-8")
            saved_jobs = []
            stdin = _TTYInput("\x1b[B\n")
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
                                        with patch("sys.stdout", new=io.StringIO()):
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
        self.assertIn("Use ↑/↓", stderr.getvalue())
        self.assertEqual(
            saved_jobs[-1].resume_session_id,
            "019dfe10-8e20-7580-ba09-86c21ace5c81",
        )

    def test_start_picker_accepts_pasted_session_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / ".codex" / "sessions"
            _write_codex_session(
                root,
                session_id="019dfeb4-2e25-7173-8ab9-006893040db2",
                created_at="2026-05-06T21:11:54Z",
                latest_user_message="first visible session",
                mtime=200,
            )
            _write_codex_session(
                root,
                session_id="019dfe10-8e20-7580-ba09-86c21ace5c81",
                created_at="2026-05-06T18:13:11Z",
                latest_user_message="second visible session",
                mtime=100,
            )
            config_path = Path(tmpdir) / "config.toml"
            mission_path = Path(tmpdir) / "mission.md"
            config_path.write_text(_sample_config(), encoding="utf-8")
            mission_path.write_text("# Mission\nShip it.\n", encoding="utf-8")
            saved_jobs = []
            stdin = _TTYInput("019dfe10-8e20-7580-ba09-86c21ace5c81\n")
            with patch("dbx.cli._codex_sessions_root", return_value=root):
                with patch("sys.stdin", new=stdin):
                    with patch("sys.stderr", new=io.StringIO()):
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
                                        with patch("sys.stdout", new=io.StringIO()):
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
        self.assertEqual(
            saved_jobs[-1].resume_session_id,
            "019dfe10-8e20-7580-ba09-86c21ace5c81",
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
                    return_value={"phase": "runtime", "state": "running"},
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
        self.assertEqual(payload["runtime"]["state"], "running")

    def test_start_without_args_runs_resume_wizard_with_configured_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / ".codex" / "sessions"
            _write_codex_session(
                root,
                session_id="019dfeb4-2e25-7173-8ab9-006893040db2",
                created_at="2026-05-06T21:11:54Z",
                latest_user_message="resume default launch",
                mtime=100,
            )
            mission_path = Path(tmpdir) / "mission.md"
            mission_path.write_text("# Mission\nShip it.\n", encoding="utf-8")
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text(
                _sample_config()
                + f'default_repo = "er-fo/db-x"\n'
                + f'default_mission = "{mission_path}"\n',
                encoding="utf-8",
            )
            saved_jobs = []
            stdin = _TTYInput("\n\n\n")
            with patch("dbx.cli._codex_sessions_root", return_value=root):
                with patch("sys.stdin", new=stdin):
                    with patch("sys.stderr", new=io.StringIO()) as stderr:
                        with patch(
                            "dbx.cli.launch_instance",
                            return_value={"Instances": [{"InstanceId": "i-123"}]},
                        ) as launch_instance:
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
                                                ["--config", str(config_path), "start"]
                                            )

        self.assertEqual(exit_code, 0)
        wizard_output = stderr.getvalue()
        self.assertIn("dbx start launch wizard", wizard_output)
        self.assertIn("Step 1/3: Choose launch mode", wizard_output)
        self.assertIn("Resume local Codex session", wizard_output)
        self.assertIn("Step 2/3: Choose Codex session", wizard_output)
        self.assertIn("Currently selected", wizard_output)
        self.assertIn("resume default launch", wizard_output)
        self.assertIn("Step 3/3: Review launch", wizard_output)
        self.assertIn("Repo: er-fo/db-x", wizard_output)
        self.assertIn(f"Mission: {mission_path.resolve()}", wizard_output)
        self.assertIn("AWS: c7i.xlarge in eu-north-1", wizard_output)
        self.assertEqual(saved_jobs[-1].repo, "er-fo/db-x")
        self.assertEqual(saved_jobs[-1].mission_path, str(mission_path.resolve()))
        self.assertEqual(
            saved_jobs[-1].resume_session_id,
            "019dfeb4-2e25-7173-8ab9-006893040db2",
        )
        request = launch_instance.call_args.args[1]
        self.assertEqual(request.repo, "er-fo/db-x")
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["instance_id"], "i-123")

    def test_start_wizard_can_launch_fresh_mission_without_resume_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / ".codex" / "sessions"
            _write_codex_session(
                root,
                session_id="019dfeb4-2e25-7173-8ab9-006893040db2",
                created_at="2026-05-06T21:11:54Z",
                latest_user_message="available but not resumed",
                mtime=100,
            )
            mission_path = Path(tmpdir) / "mission.md"
            mission_path.write_text("# Mission\nShip it.\n", encoding="utf-8")
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text(
                _sample_config()
                + f'default_repo = "er-fo/db-x"\n'
                + f'default_mission = "{mission_path}"\n',
                encoding="utf-8",
            )
            saved_jobs = []
            stdin = _TTYInput("\x1b[B\n\n")
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
                                    "dbx.cli.save_job_state",
                                    side_effect=saved_jobs.append,
                                ):
                                    with patch("sys.stdout", new=io.StringIO()) as stdout:
                                        exit_code = cli.main(
                                            ["--config", str(config_path), "start"]
                                        )

        self.assertEqual(exit_code, 0)
        self.assertIn("Start fresh mission", stderr.getvalue())
        self.assertIsNone(saved_jobs[-1].resume_session_id)
        payload = json.loads(stdout.getvalue())
        self.assertIsNone(payload["resume_session_id"])

    def test_start_wizard_accepts_pasted_session_id_before_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / ".codex" / "sessions"
            _write_codex_session(
                root,
                session_id="019dfeb4-2e25-7173-8ab9-006893040db2",
                created_at="2026-05-06T21:11:54Z",
                latest_user_message="first visible session",
                mtime=200,
            )
            _write_codex_session(
                root,
                session_id="019dfe10-8e20-7580-ba09-86c21ace5c81",
                created_at="2026-05-06T18:13:11Z",
                latest_user_message="pasted session to resume",
                mtime=100,
            )
            mission_path = Path(tmpdir) / "mission.md"
            mission_path.write_text("# Mission\nShip it.\n", encoding="utf-8")
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text(
                _sample_config()
                + f'default_repo = "er-fo/db-x"\n'
                + f'default_mission = "{mission_path}"\n',
                encoding="utf-8",
            )
            saved_jobs = []
            stdin = _TTYInput("\x1b[B\x1b[B\n019dfe10-8e20-7580-ba09-86c21ace5c81\n\n")
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
                                        with patch("sys.stdout", new=io.StringIO()):
                                            exit_code = cli.main(
                                                ["--config", str(config_path), "start"]
                                            )

        self.assertEqual(exit_code, 0)
        self.assertIn("Paste session ID or path", stderr.getvalue())
        self.assertIn("pasted session to resume", stderr.getvalue())
        self.assertEqual(
            saved_jobs[-1].resume_session_id,
            "019dfe10-8e20-7580-ba09-86c21ace5c81",
        )

    def test_start_wizard_cancel_does_not_launch_instance(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / ".codex" / "sessions"
            _write_codex_session(
                root,
                session_id="019dfeb4-2e25-7173-8ab9-006893040db2",
                created_at="2026-05-06T21:11:54Z",
                latest_user_message="do not launch this",
                mtime=100,
            )
            mission_path = Path(tmpdir) / "mission.md"
            mission_path.write_text("# Mission\nShip it.\n", encoding="utf-8")
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text(
                _sample_config()
                + f'default_repo = "er-fo/db-x"\n'
                + f'default_mission = "{mission_path}"\n',
                encoding="utf-8",
            )
            stdin = _TTYInput("q")
            with patch("dbx.cli._codex_sessions_root", return_value=root):
                with patch("sys.stdin", new=stdin):
                    with patch("sys.stderr", new=io.StringIO()) as stderr:
                        with patch("dbx.cli.launch_instance") as launch_instance:
                            exit_code = cli.main(["--config", str(config_path), "start"])

        self.assertEqual(exit_code, 2)
        self.assertIn("No dbx launch selected", stderr.getvalue())
        launch_instance.assert_not_called()

    def test_start_wizard_session_preview_renders_selected_details(self) -> None:
        session_path = Path(
            "/tmp/sessions/2026/05/06/"
            "rollout-2026-05-06T21-11-54-019dfeb4-2e25-7173-8ab9-006893040db2.jsonl"
        )
        session = cli.CodexSession(
            session_id="019dfeb4-2e25-7173-8ab9-006893040db2",
            path=session_path,
            relative_path="2026/05/06/rollout.jsonl",
            created_at="2026-05-06T21:11:54Z",
            updated_at="2026-05-06T21:15:00Z",
            branch="main",
            latest_user_message="make the selector more interactive",
            size_bytes=1234,
        )
        output = io.StringIO()

        cli._render_codex_session_picker(
            [session],
            selected_index=0,
            typed="",
            output_stream=output,
            clear_screen=False,
        )

        rendered = output.getvalue()
        self.assertIn("Currently selected", rendered)
        self.assertIn("make the selector more interactive", rendered)
        self.assertIn("Branch: main", rendered)
        self.assertIn("Session: 019dfeb4-2e25-7173-8ab9-006893040db2", rendered)
        self.assertIn(str(session_path), rendered)

    def test_start_without_args_requires_configured_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text(_sample_config(), encoding="utf-8")
            with patch("sys.stderr", new=io.StringIO()) as stderr:
                exit_code = cli.main(["--config", str(config_path), "start"])

        self.assertEqual(exit_code, 2)
        self.assertIn("default_repo", stderr.getvalue())
        self.assertIn("default_mission", stderr.getvalue())

    def test_start_forwards_resume_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / ".codex" / "sessions"
            _write_codex_session(
                root,
                session_id="019dfdac-5bea-71f0-91c5-4fdd8826860b",
                created_at="2026-05-06T16:23:44Z",
                latest_user_message="resume this work",
                mtime=100,
                branch="feature/base",
            )
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
                    return_value={"phase": "runtime", "state": "running"},
                    create=True,
                ):
                    with patch("dbx.cli._codex_sessions_root", return_value=root):
                        with patch("dbx.cli._upload_resume_session_when_reachable") as upload_session:
                            upload_session.return_value = {
                                "remote_path": (
                                    "/home/ubuntu/.codex/sessions/2026/05/06/"
                                    "rollout-2026-05-06T16-23-44-"
                                    "019dfdac-5bea-71f0-91c5-4fdd8826860b.jsonl"
                                )
                            }
                            with patch(
                                "dbx.cli.save_job_state", side_effect=saved_jobs.append
                            ):
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
        self.assertEqual(request.base_branch, "feature/base")
        self.assertEqual(saved_jobs[-1].base_branch, "feature/base")
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
                                with patch("dbx.cli._persist_remote_artifacts"):
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

    def test_finish_remote_delegates_to_vm_finish_script(self) -> None:
        job_state = _sample_job_state()
        with patch("dbx.cli.run_remote_shell_command") as run_remote:
            with patch(
                "dbx.cli._read_remote_command_output",
                return_value="https://github.com/er-fo/db-x/pull/1\n",
            ):
                result = cli._run_finish_remote(
                    "ubuntu@dbx-job",
                    job_state,
                    "feature/base",
                    no_pr=False,
                    blocked=True,
                )

        self.assertEqual(result["pr_url"], "https://github.com/er-fo/db-x/pull/1")
        remote_script = run_remote.call_args.args[1]
        self.assertIn("/usr/local/bin/dbx-finish-job", remote_script)
        self.assertIn("--mode blocked", remote_script)
        self.assertIn("--base feature/base", remote_script)

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
                                    "status_json": {"phase": "runtime", "state": "running"},
                                    "status_md": "# dbx-job\n",
                                    "blocker": None,
                                    "bootstrap_log": "boot\n",
                                    "codex_log": "codex\n",
                                    "finish_log": "finish\n",
                                },
                            ):
                                with patch("dbx.cli._persist_remote_artifacts"):
                                    with patch("sys.stdout", new=io.StringIO()) as stdout:
                                        exit_code = cli.main(
                                            ["--config", str(config_path), "status", "i-123", "--logs"]
                                        )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["remote_status"]["state"], "running")
        self.assertEqual(payload["logs"]["codex"], "codex\n")

    def test_monitor_once_shows_only_git_status_and_codex_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text(_sample_config(), encoding="utf-8")

            def fake_remote_command(target: str, shell_command: str, **kwargs):
                if "git status --short --branch" in shell_command:
                    return subprocess.CompletedProcess(
                        args=[],
                        returncode=0,
                        stdout="## agent/test...origin/main\n M src/dbx/cli.py\n",
                        stderr="",
                    )
                if "tail -n 80" in shell_command and "logs/codex.log" in shell_command:
                    return subprocess.CompletedProcess(
                        args=[],
                        returncode=0,
                        stdout="codex line 1\ncodex line 2\n",
                        stderr="",
                    )
                raise AssertionError(f"unexpected remote command: {shell_command}")

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
                            "dbx.cli.run_remote_shell_command",
                            side_effect=fake_remote_command,
                        ):
                            with patch("sys.stdout", new=io.StringIO()) as stdout:
                                exit_code = cli.main(
                                    ["--config", str(config_path), "monitor", "i-123", "--once"]
                                )

        self.assertEqual(exit_code, 0)
        output = stdout.getvalue()
        self.assertIn("Git", output)
        self.assertIn("## agent/test...origin/main", output)
        self.assertIn(" M src/dbx/cli.py", output)
        self.assertIn("Codex output", output)
        self.assertIn("codex line 2", output)
        self.assertNotIn("STATUS.md", output)
        self.assertNotIn("bootstrap", output)

    def test_start_monitor_launches_then_monitors_on_stderr(self) -> None:
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
                ):
                    with patch("dbx.cli.save_job_state", side_effect=saved_jobs.append):
                        with patch("dbx.cli.run_monitor", return_value=0) as monitor:
                            with patch("sys.stdout", new=io.StringIO()) as stdout:
                                with patch("sys.stderr", new=io.StringIO()) as stderr:
                                    exit_code = cli.main(
                                        [
                                            "--config",
                                            str(config_path),
                                            "start",
                                            "er-fo/db-x",
                                            str(mission_path),
                                            "--monitor",
                                        ]
                                    )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["instance_id"], "i-123")
        self.assertEqual(stderr.getvalue(), "")
        monitor.assert_called_once()
        self.assertEqual(monitor.call_args.args[1], "i-123")
        self.assertIs(monitor.call_args.kwargs["output_stream"], stderr)
        self.assertTrue(saved_jobs)

    def test_start_monitor_requires_runtime_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            mission_path = Path(tmpdir) / "mission.md"
            config_path.write_text(_sample_config(), encoding="utf-8")
            mission_path.write_text("# Mission\nShip it.\n", encoding="utf-8")
            with patch("sys.stderr", new=io.StringIO()) as stderr:
                exit_code = cli.main(
                    [
                        "--config",
                        str(config_path),
                        "start",
                        "er-fo/db-x",
                        str(mission_path),
                        "--no-wait",
                        "--monitor",
                    ]
                )

        self.assertEqual(exit_code, 2)
        self.assertIn("--monitor requires runtime verification", stderr.getvalue())

    def test_live_render_uses_alternate_screen_sequences_when_clearing(self) -> None:
        output = io.StringIO()

        cli._render_monitor_snapshot(
            job_id="i-123",
            git_status="## main\n",
            codex_log="codex\n",
            output_stream=output,
            clear_screen=True,
            lines=80,
            interval_seconds=2.0,
        )

        rendered = output.getvalue()
        self.assertIn("\033[?1049h", rendered)
        self.assertIn("\033[2J\033[H", rendered)
        self.assertIn("Git", rendered)
        self.assertIn("Codex output", rendered)

    def test_wait_for_runtime_ready_succeeds_only_after_agent_heartbeat(self) -> None:
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
                        with patch(
                            "dbx.cli._remote_file_exists",
                            side_effect=lambda _target, path: True,
                        ):
                            with patch(
                                "dbx.cli._read_remote_json",
                                side_effect=[
                                    {
                                        "phase": "runtime",
                                        "state": "running",
                                        "detail": "agent_heartbeat_received",
                                    },
                                    {
                                        "status": "running",
                                        "started_at": "2026-05-07T10:00:00Z",
                                        "session_name": "dbx-job",
                                    },
                                ],
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

        self.assertEqual(runtime["state"], "running")
        self.assertEqual(runtime["heartbeat"]["status"], "running")
        self.assertEqual(runtime["system_status"], "initializing")
        self.assertEqual(runtime["instance_status"], "initializing")

    def test_wait_for_runtime_ready_retries_after_transient_ssh_error(self) -> None:
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
                with patch("dbx.cli.describe_instance_status", return_value={}):
                    with patch("dbx.cli.build_ssh_target", return_value="ubuntu@dbx-job"):
                        with patch("dbx.cli._remote_file_exists", return_value=True):
                            with patch(
                                "dbx.cli._read_remote_json",
                                side_effect=[
                                    cli.RemoteCommandError("ssh handshake reset"),
                                    {
                                        "phase": "runtime",
                                        "state": "running",
                                        "detail": "agent_heartbeat_received",
                                    },
                                    {
                                        "status": "running",
                                        "started_at": "2026-05-07T10:00:00Z",
                                        "session_name": "dbx-job",
                                    },
                                ],
                            ):
                                with patch(
                                    "dbx.cli._remote_tmux_session_exists",
                                    return_value=True,
                                ):
                                    with patch("dbx.cli.time.sleep") as sleep:
                                        runtime = cli._wait_for_runtime_ready(
                                            config,
                                            _sample_job_state(),
                                            timeout_seconds=2,
                                            poll_interval_seconds=0,
                                        )

        self.assertEqual(runtime["state"], "running")
        self.assertGreaterEqual(sleep.call_count, 1)

    def test_wait_for_runtime_ready_raises_auth_failed_while_runtime_is_starting(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = cli.load_config(_write_sample_config(tmpdir))
            job_state = _sample_job_state()
            paths = cli._remote_paths(job_state)

            def remote_file_exists(_target: str, path: str) -> bool:
                if path == paths["agent_started_json"]:
                    self.fail("agent heartbeat should not be checked after codex auth failure")
                return path in {
                    "/var/lib/cloud/instance/boot-finished",
                    paths["codex_log"],
                }

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
                        with patch(
                            "dbx.cli._remote_file_exists",
                            side_effect=remote_file_exists,
                        ):
                            with patch(
                                "dbx.cli._read_remote_json",
                                return_value={
                                    "phase": "runtime",
                                    "state": "starting",
                                    "detail": "waiting_for_agent_heartbeat",
                                },
                            ):
                                with patch(
                                    "dbx.cli._remote_tmux_session_exists",
                                    return_value=True,
                                ):
                                    with patch(
                                        "dbx.cli._tail_remote_text",
                                        return_value="Error: login required\n",
                                    ):
                                        with patch(
                                            "dbx.cli._capture_remote_artifacts",
                                            return_value={
                                                "status_json": {
                                                    "phase": "runtime",
                                                    "state": "starting",
                                                    "detail": "waiting_for_agent_heartbeat",
                                                },
                                                "codex_log": "Error: login required\n",
                                            },
                                        ):
                                            with patch(
                                                "dbx.cli._persist_remote_artifacts"
                                            ) as persist_artifacts:
                                                with self.assertRaises(
                                                    cli.RuntimeLifecycleError
                                                ) as raised:
                                                    cli._wait_for_runtime_ready(
                                                        config,
                                                        job_state,
                                                        timeout_seconds=1,
                                                        poll_interval_seconds=0,
                                                    )

        self.assertEqual(raised.exception.status, "auth_failed")
        self.assertEqual(raised.exception.detail, "codex_auth_failed")
        self.assertEqual(
            raised.exception.runtime["detail"],
            "codex_auth_failed",
        )
        persist_artifacts.assert_called_once()

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
        self.assertEqual(saved_jobs[-1].status, "ready")
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
                                            with patch("dbx.cli.save_job_state"):
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

    def test_start_terminates_on_blocked_runtime_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            mission_path = Path(tmpdir) / "mission.md"
            config_path.write_text(_sample_config(), encoding="utf-8")
            mission_path.write_text("# Mission\nShip it.\n", encoding="utf-8")
            with patch(
                "dbx.cli.launch_instance",
                return_value={"Instances": [{"InstanceId": "i-123"}]},
            ):
                with patch(
                    "dbx.cli._wait_for_runtime_ready",
                    side_effect=cli.RuntimeLifecycleError(
                        status="blocked",
                        detail="codex_auth_failed",
                        runtime={
                            "status": "blocked",
                            "detail": "codex_auth_failed",
                            "artifacts": {"status_json": {"state": "blocked"}},
                        },
                    ),
                ):
                    with patch(
                        "dbx.cli._terminate_job",
                        return_value={"final_state": "terminated"},
                    ) as terminate_job:
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

        self.assertEqual(exit_code, 1)
        terminate_job.assert_called_once()
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["detail"], "codex_auth_failed")
        self.assertEqual(payload["termination"]["final_state"], "terminated")

    def test_start_terminates_on_auth_failed_runtime_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            mission_path = Path(tmpdir) / "mission.md"
            config_path.write_text(_sample_config(), encoding="utf-8")
            mission_path.write_text("# Mission\nShip it.\n", encoding="utf-8")
            with patch(
                "dbx.cli.launch_instance",
                return_value={"Instances": [{"InstanceId": "i-123"}]},
            ):
                with patch(
                    "dbx.cli._wait_for_runtime_ready",
                    side_effect=cli.RuntimeLifecycleError(
                        status="auth_failed",
                        detail="codex login required",
                        runtime={
                            "status": "auth_failed",
                            "detail": "codex login required",
                            "artifacts": {"codex_log": "Error: login required\n"},
                        },
                    ),
                ):
                    with patch(
                        "dbx.cli._terminate_job",
                        return_value={"final_state": "terminated"},
                    ):
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

        self.assertEqual(exit_code, 1)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "auth_failed")
        self.assertEqual(payload["lifecycle_state"], "terminated")

    def test_start_timeout_terminates_and_preserves_timeout_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            mission_path = Path(tmpdir) / "mission.md"
            config_path.write_text(_sample_config(), encoding="utf-8")
            mission_path.write_text("# Mission\nShip it.\n", encoding="utf-8")
            with patch(
                "dbx.cli.launch_instance",
                return_value={"Instances": [{"InstanceId": "i-123"}]},
            ):
                with patch(
                    "dbx.cli._wait_for_runtime_ready",
                    side_effect=cli.RuntimeLifecycleError(
                        status="timeout",
                        detail="waiting_for_agent_heartbeat",
                        runtime={
                            "status": "timeout",
                            "detail": "waiting_for_agent_heartbeat",
                            "artifacts": {"codex_log": "still starting\n"},
                        },
                    ),
                ):
                    with patch(
                        "dbx.cli._terminate_job",
                        return_value={"final_state": "terminated"},
                    ):
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

        self.assertEqual(exit_code, 1)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "timeout")
        self.assertEqual(payload["lifecycle_state"], "terminated")
        self.assertEqual(payload["termination"]["final_state"], "terminated")

    def test_capture_remote_artifacts_collects_logs_even_when_status_json_is_invalid(self) -> None:
        with patch(
            "dbx.cli._read_remote_json",
            side_effect=cli.RemoteCommandError("Remote JSON artifact was invalid"),
        ):
            with patch(
                "dbx.cli._read_remote_text",
                side_effect=["# status\n", "blocked details\n"],
            ):
                with patch(
                    "dbx.cli._tail_remote_text",
                    side_effect=["boot\n", "codex\n", "finish\n"],
                ):
                    artifacts = cli._capture_remote_artifacts(
                        "ubuntu@dbx-job",
                        _sample_job_state(),
                        include_logs=True,
                    )

        self.assertIsNone(artifacts["status_json"])
        self.assertEqual(artifacts["status_md"], "# status\n")
        self.assertEqual(artifacts["codex_log"], "codex\n")
        self.assertIn("status_json", artifacts["errors"])

    def test_persist_remote_artifacts_redacts_secrets_before_saving(self) -> None:
        artifacts = {
            "status_json": {"phase": "runtime", "state": "blocked", "detail": "auth"},
            "blocker": "token ghp_1234567890abcdefghijklmnopqrstuvwxyz leaks",
            "codex_log": "TAILSCALE_AUTH_KEY=tskey-auth-123\nOPENAI_API_KEY=sk-proj-secret\n",
        }

        with patch("dbx.cli.save_job_artifact") as save_artifact:
            cli._persist_remote_artifacts("i-123", artifacts)

        saved_contents = {call.args[1]: call.args[2] for call in save_artifact.call_args_list}
        self.assertIn("BLOCKER.md", saved_contents)
        self.assertIn("[REDACTED]", saved_contents["BLOCKER.md"])
        self.assertIn("[REDACTED]", saved_contents["codex.log"])
        self.assertNotIn("ghp_", saved_contents["BLOCKER.md"])
        self.assertNotIn("sk-proj-secret", saved_contents["codex.log"])

    def test_list_includes_local_terminated_jobs_not_visible_in_aws(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text(_sample_config(), encoding="utf-8")
            local_job = JobState(
                instance_id="i-local",
                job_name="dbx-local",
                repo="er-fo/db-x",
                branch_name="agent/local",
                session_name="dbx-local",
                mission_path="/tmp/mission.md",
                created_at="2026-05-07T10:00:00Z",
                job_root="/home/ubuntu/work/dbx-local",
                lifecycle_state="terminated",
                status="timeout",
                terminated_at="2026-05-07T10:10:00Z",
            )
            with patch("dbx.cli.list_instances", return_value=[]):
                with patch("dbx.cli.list_job_states", return_value=[local_job]):
                    with patch("sys.stdout", new=io.StringIO()) as stdout:
                        exit_code = cli.main(["--config", str(config_path), "list"])

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload[0]["instance_id"], "i-local")
        self.assertEqual(payload[0]["status"], "timeout")
        self.assertEqual(payload[0]["lifecycle_state"], "terminated")

    def test_status_falls_back_to_local_state_and_saved_artifacts_for_terminated_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text(_sample_config(), encoding="utf-8")
            local_job = JobState(
                instance_id="i-terminated",
                job_name="dbx-local",
                repo="er-fo/db-x",
                branch_name="agent/local",
                session_name="dbx-local",
                mission_path="/tmp/mission.md",
                created_at="2026-05-07T10:00:00Z",
                job_root="/home/ubuntu/work/dbx-local",
                lifecycle_state="terminated",
                status="auth_failed",
                last_error="codex login required",
                terminated_at="2026-05-07T10:10:00Z",
            )
            with patch(
                "dbx.cli.describe_instance",
                side_effect=cli.AwsCliError("Instance not found"),
            ):
                with patch("dbx.cli.load_job_state", return_value=local_job):
                    with patch(
                        "dbx.cli.load_job_artifacts",
                        return_value={
                            "status.json": '{\n  "state": "blocked"\n}\n',
                            "BLOCKER.md": "codex auth failed\n",
                            "codex.log": "Error: login required\n",
                        },
                    ):
                        with patch("sys.stdout", new=io.StringIO()) as stdout:
                            exit_code = cli.main(
                                ["--config", str(config_path), "status", "i-terminated", "--logs"]
                            )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["instance_id"], "i-terminated")
        self.assertEqual(payload["status"], "auth_failed")
        self.assertEqual(payload["lifecycle_state"], "terminated")
        self.assertEqual(payload["remote_status"]["state"], "blocked")
        self.assertIn("codex", payload["logs"])


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
        base_branch="feature/base",
        lifecycle_state="running",
        status="ready",
    )


def _write_codex_session(
    root: Path,
    *,
    session_id: str,
    created_at: str,
    latest_user_message: str,
    mtime: int,
    extra_user_message: str | None = None,
    source: object = "cli",
    branch: str = "main",
) -> Path:
    date_part, time_part = created_at.removesuffix("Z").split("T")
    year, month, day = date_part.split("-")
    filename_time = time_part.replace(":", "-")
    session_path = root / year / month / day / f"rollout-{date_part}T{filename_time}-{session_id}.jsonl"
    session_path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "type": "session_meta",
            "timestamp": created_at,
            "payload": {
                "timestamp": created_at,
                "cwd": "/tmp/repo",
                "git": {"branch": branch},
                "source": source,
            },
        },
        {
            "type": "event_msg",
            "timestamp": created_at,
            "payload": {
                "type": "user_message",
                "message": latest_user_message,
                "text_elements": [],
            },
        },
        {
            "type": "response_item",
            "timestamp": created_at,
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "older user message"}],
            },
        },
        {
            "type": "response_item",
            "timestamp": created_at,
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "<environment_context>ignore this</environment_context>\n"
                            f"{latest_user_message}"
                        ),
                    }
                ],
            },
        },
    ]
    if extra_user_message is not None:
        records.append(
            {
                "type": "response_item",
                "timestamp": created_at,
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": extra_user_message}],
                },
            }
        )
    session_path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    os.utime(session_path, (mtime, mtime))
    return session_path


class _TTYInput(io.StringIO):
    def isatty(self) -> bool:
        return True
