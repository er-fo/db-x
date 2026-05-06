from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
import json


DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "db-x" / "jobs"


@dataclass(frozen=True)
class JobState:
    instance_id: str
    job_name: str
    repo: str
    branch_name: str
    session_name: str
    mission_path: str
    created_at: str


def save_job_state(job: JobState, state_dir: Path | None = None) -> Path:
    root = (state_dir or DEFAULT_STATE_DIR).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{job.instance_id}.json"
    path.write_text(json.dumps(asdict(job), indent=2) + "\n", encoding="utf-8")
    return path


def load_job_state(instance_id: str, state_dir: Path | None = None) -> JobState | None:
    path = (state_dir or DEFAULT_STATE_DIR).expanduser() / f"{instance_id}.json"
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return JobState(**payload)


def created_at_now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"
