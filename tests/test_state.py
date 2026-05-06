from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from dbx.state import JobState, load_job_state, save_job_state


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
        self.assertEqual(loaded.lifecycle_state, "created")
        self.assertIsNone(loaded.pr_url)
        self.assertIsNone(loaded.terminated_at)
