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
                created_at="2026-05-06T12:00:00Z",
            )
            save_job_state(job, state_dir=state_dir)
            loaded = load_job_state("i-123", state_dir=state_dir)

        self.assertEqual(loaded, job)
