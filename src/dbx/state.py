from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
import json


DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "db-x" / "jobs"
DEFAULT_ARTIFACTS_DIR = Path.home() / ".local" / "state" / "db-x" / "artifacts"


@dataclass(frozen=True)
class JobState:
    instance_id: str
    job_name: str
    repo: str
    branch_name: str
    session_name: str
    mission_path: str
    created_at: str
    resume_session_id: str | None = None
    job_root: str = ""
    base_branch: str = "main"
    lifecycle_state: str = "created"
    status: str = "pending"
    pr_url: str | None = None
    last_error: str | None = None
    terminated_at: str | None = None


def save_job_state(job: JobState, state_dir: Path | None = None) -> Path:
    root = (state_dir or DEFAULT_STATE_DIR).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{job.instance_id}.json"
    path.write_text(json.dumps(asdict(job), indent=2) + "\n", encoding="utf-8")
    return path


def save_job_artifact(
    instance_id: str,
    artifact_name: str,
    content: str,
    artifacts_dir: Path | None = None,
) -> Path:
    root = (artifacts_dir or DEFAULT_ARTIFACTS_DIR).expanduser() / instance_id
    root.mkdir(parents=True, exist_ok=True)
    path = root / artifact_name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def load_job_state(instance_id: str, state_dir: Path | None = None) -> JobState | None:
    path = (state_dir or DEFAULT_STATE_DIR).expanduser() / f"{instance_id}.json"
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.setdefault("job_root", "")
    payload.setdefault("base_branch", "main")
    payload.setdefault("lifecycle_state", "created")
    payload.setdefault("status", "pending")
    payload.setdefault("resume_session_id", None)
    payload.setdefault("pr_url", None)
    payload.setdefault("last_error", None)
    payload.setdefault("terminated_at", None)
    return JobState(**payload)


def list_job_states(state_dir: Path | None = None) -> list[JobState]:
    root = (state_dir or DEFAULT_STATE_DIR).expanduser()
    if not root.exists():
        return []
    jobs: list[JobState] = []
    for path in sorted(root.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.setdefault("job_root", "")
        payload.setdefault("lifecycle_state", "created")
        payload.setdefault("status", "pending")
        payload.setdefault("resume_session_id", None)
        payload.setdefault("pr_url", None)
        payload.setdefault("last_error", None)
        payload.setdefault("terminated_at", None)
        jobs.append(JobState(**payload))
    return jobs


def load_job_artifacts(instance_id: str, artifacts_dir: Path | None = None) -> dict[str, str]:
    root = (artifacts_dir or DEFAULT_ARTIFACTS_DIR).expanduser() / instance_id
    if not root.exists():
        return {}
    artifacts: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            artifacts[path.relative_to(root).as_posix()] = path.read_text(encoding="utf-8")
    return artifacts


def created_at_now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"
