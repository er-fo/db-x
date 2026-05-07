from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from dbx.state import JobState, list_job_states, load_job_artifacts, load_job_state, save_job_artifact
from dbx.state import save_job_state


class StateTests(unittest.TestCase):
    def test_job_state_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            job = JobState(
                instance_id="i-123",
                job_name="dbx-job",
                repo="er-fo/db-x",
                branch_name="agent/test",
                session_name="dbx-job",
                mission_path="/tmp/mission.md",
                job_root="/home/ubuntu/work/dbx-job",
                base_branch="feature/base",
                created_at="2026-05-06T12:00:00Z",
                lifecycle_state="running",
                status="ready",
                pr_url="https://github.com/er-fo/db-x/pull/1",
                last_error=None,
                terminated_at=None,
            )
            save_job_state(job, state_dir=state_dir)
            loaded = load_job_state("i-123", state_dir=state_dir)

        self.assertEqual(loaded, job)

    def test_load_job_state_backfills_new_optional_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            legacy_path = state_dir / "i-123.json"
            legacy_path.write_text(
                """{
  "instance_id": "i-123",
  "job_name": "dbx-job",
  "repo": "er-fo/db-x",
  "branch_name": "agent/test",
  "session_name": "dbx-job",
  "mission_path": "/tmp/mission.md",
  "created_at": "2026-05-06T12:00:00Z"
}
""",
                encoding="utf-8",
            )

            loaded = load_job_state("i-123", state_dir=state_dir)

        self.assertEqual(loaded.job_root, "")
        self.assertEqual(loaded.base_branch, "main")
        self.assertEqual(loaded.lifecycle_state, "created")
        self.assertIsNone(loaded.pr_url)
        self.assertIsNone(loaded.terminated_at)

    def test_list_job_states_returns_saved_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            first = JobState(
                instance_id="i-123",
                job_name="dbx-job-1",
                repo="er-fo/db-x",
                branch_name="agent/one",
                session_name="dbx-job-1",
                mission_path="/tmp/one.md",
                job_root="/home/ubuntu/work/dbx-job-1",
                created_at="2026-05-06T12:00:00Z",
            )
            second = JobState(
                instance_id="i-456",
                job_name="dbx-job-2",
                repo="er-fo/db-x",
                branch_name="agent/two",
                session_name="dbx-job-2",
                mission_path="/tmp/two.md",
                job_root="/home/ubuntu/work/dbx-job-2",
                created_at="2026-05-07T12:00:00Z",
            )
            save_job_state(first, state_dir=state_dir)
            save_job_state(second, state_dir=state_dir)

            jobs = list_job_states(state_dir=state_dir)

        self.assertEqual([job.instance_id for job in jobs], ["i-123", "i-456"])

    def test_load_job_artifacts_reads_saved_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            artifacts_dir = Path(tmpdir)
            save_job_artifact("i-123", "status.json", "{}\n", artifacts_dir=artifacts_dir)
            save_job_artifact("i-123", "logs/codex.log", "ready\n", artifacts_dir=artifacts_dir)

            artifacts = load_job_artifacts("i-123", artifacts_dir=artifacts_dir)

        self.assertEqual(artifacts["status.json"], "{}\n")
        self.assertEqual(artifacts["logs/codex.log"], "ready\n")
