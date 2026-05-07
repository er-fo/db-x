from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
import json
import re
import select
import shlex
import shutil
import subprocess
import sys
import termios
import time
import tty
from typing import Callable

from .aws import (
    AwsCliError,
    JobLaunchRequest,
    build_ssh_target,
    describe_instance,
    describe_instance_status,
    get_console_output,
    launch_instance,
    list_instances,
    terminate_instance,
    wait_for_instance_terminated,
)
from .config import DEFAULT_CONFIG_PATH, AppConfig, load_config
from .config import resolve_config_path, write_default_config
from .ssh import RemoteCommandError, build_attach_command, run_remote_shell_command
from .ssh import upload_remote_text
from .state import (
    JobState,
    created_at_now,
    list_job_states,
    load_job_artifacts,
    load_job_state,
    save_job_artifact,
    save_job_state,
)

PICK_SESSION = "__dbx_pick_session__"
SESSION_LIST_LIMIT = 10
DEFAULT_MONITOR_INTERVAL_SECONDS = 2.0
DEFAULT_MONITOR_LOG_LINES = 80
ALT_SCREEN_ENTER = "\033[?1049h"
ALT_SCREEN_EXIT = "\033[?1049l"
CLEAR_SCREEN = "\033[2J\033[H"
SESSION_ID_RE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


@dataclass(frozen=True)
class CodexSession:
    session_id: str
    path: Path
    relative_path: str
    created_at: str
    updated_at: str
    branch: str
    latest_user_message: str
    size_bytes: int

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "path": str(self.path),
            "relative_path": self.relative_path,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "modified_at": self.updated_at,
            "branch": self.branch,
            "latest_user_message": self.latest_user_message,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True)
class StartWizardSelection:
    repo: str
    mission: str
    resume_session_id: str | None
    launch_mode: str


@dataclass(frozen=True)
class WizardMenuOption:
    key: str
    label: str
    detail: str = ""


REMOTE_READY_STATES = {"running", "ready"}
REMOTE_NON_SUCCESS_STATES = {"blocked", "failed", "auth_failed", "timeout"}

_REDACTION_PATTERNS = (
    re.compile(r"tskey-auth-[A-Za-z0-9_-]+"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ASIA[0-9A-Z]{16}"),
    re.compile(r"aws_secret_access_key\s*[=:]\s*[A-Za-z0-9/+=]{20,}", re.IGNORECASE),
    re.compile(r"OPENAI_API_KEY\s*[=:]\s*[^\s]+", re.IGNORECASE),
    re.compile(r"CODEX_(?:AUTH|API|TOKEN)[A-Z_]*\s*[=:]\s*[^\s]+", re.IGNORECASE),
    re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{12,}"),
)

_CODEX_AUTH_FAILURE_PATTERNS = (
    re.compile(r"codex login", re.IGNORECASE),
    re.compile(r"authentication failed", re.IGNORECASE),
    re.compile(r"not logged in", re.IGNORECASE),
    re.compile(r"login required", re.IGNORECASE),
    re.compile(r"invalid api key", re.IGNORECASE),
    re.compile(r"token_expired", re.IGNORECASE),
    re.compile(r"authentication token is expired", re.IGNORECASE),
    re.compile(r"access token could not be refreshed", re.IGNORECASE),
    re.compile(r"refresh token was already used", re.IGNORECASE),
    re.compile(r"please log out and sign in again", re.IGNORECASE),
)


class RuntimeLifecycleError(RuntimeError):
    def __init__(self, *, status: str, detail: str, runtime: dict[str, object]) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail
        self.runtime = runtime


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "doctor":
        return run_doctor(args.config)
    if args.command == "init-config":
        return run_init_config(args.config, args.force)
    if args.command == "sessions":
        return run_sessions(args.limit, json_output=args.json)

    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        if args.command == "start":
            return run_start(
                config,
                args.repo,
                args.mission,
                resume_session_id=args.resume_session,
                pick_session=args.pick_session,
                wait=args.wait,
                timeout_seconds=args.timeout,
                monitor=args.monitor,
            )
        if args.command == "list":
            return run_list(config)
        if args.command == "status":
            return run_status(config, args.job_id, include_logs=args.logs)
        if args.command == "attach":
            return run_attach(config, args.job_id, check=args.check)
        if args.command == "monitor":
            return run_monitor(
                config,
                args.job_id,
                interval_seconds=args.interval,
                lines=args.lines,
                once=args.once,
            )
        if args.command == "finish":
            return run_finish(
                config,
                args.job_id,
                keep_instance=args.keep_instance,
                no_pr=args.no_pr,
                timeout_seconds=args.timeout,
            )
        if args.command == "terminate":
            return run_terminate(
                config,
                args.job_id,
                wait=args.wait,
                force=args.force,
            )
    except (AwsCliError, RemoteCommandError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    parser.error("Missing command")
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dbx",
        description="Disposable AWS devboxes for long-running Codex missions.",
    )
    parser.add_argument(
        "--config",
        help=f"Path to config TOML file. Defaults to {DEFAULT_CONFIG_PATH}.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor", help="Validate local prerequisites.")
    init_config_parser = subparsers.add_parser(
        "init-config", help="Write a starter config file."
    )
    init_config_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing config file.",
    )

    sessions_parser = subparsers.add_parser(
        "sessions", help="List local Codex sessions that dbx can resume."
    )
    sessions_parser.add_argument(
        "--limit",
        type=int,
        help=f"Maximum number of sessions to show, capped at {SESSION_LIST_LIMIT}.",
    )
    sessions_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON instead of the human table.",
    )

    start_parser = subparsers.add_parser("start", help="Launch a new job instance.")
    start_parser.add_argument(
        "repo",
        nargs="?",
        help="GitHub repo in owner/name format. Defaults to config default_repo.",
    )
    start_parser.add_argument(
        "mission",
        nargs="?",
        help="Path to a mission markdown file. Defaults to config default_mission.",
    )
    start_parser.add_argument(
        "--resume-session",
        nargs="?",
        const=PICK_SESSION,
        metavar="SESSION_ID",
        help="Resume an existing Codex session UUID instead of starting a fresh one.",
    )
    start_parser.add_argument(
        "--pick-session",
        action="store_true",
        help="Pick a local Codex session interactively and resume it.",
    )
    start_parser.add_argument(
        "--wait",
        dest="wait",
        action="store_true",
        default=True,
        help="Wait for runtime verification before returning.",
    )
    start_parser.add_argument(
        "--no-wait",
        dest="wait",
        action="store_false",
        help="Return after the EC2 launch API succeeds.",
    )
    start_parser.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="Maximum seconds to wait for runtime verification.",
    )
    start_parser.add_argument(
        "--monitor",
        action="store_true",
        help="After launch, watch remote git status and Codex output.",
    )

    subparsers.add_parser("list", help="List dbx-managed instances.")

    status_parser = subparsers.add_parser("status", help="Show one job instance.")
    status_parser.add_argument("job_id", help="AWS instance ID.")
    status_parser.add_argument(
        "--json",
        action="store_true",
        help="Reserved for compatibility; status output is JSON by default.",
    )
    status_parser.add_argument(
        "--logs",
        action="store_true",
        help="Include recent remote log tails when available.",
    )

    attach_parser = subparsers.add_parser("attach", help="Print the tmux attach command.")
    attach_parser.add_argument("job_id", help="AWS instance ID.")
    attach_parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the remote tmux session before printing the command.",
    )

    monitor_parser = subparsers.add_parser(
        "monitor", help="Watch a devbox's git status and Codex output."
    )
    monitor_parser.add_argument("job_id", help="AWS instance ID.")
    monitor_parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_MONITOR_INTERVAL_SECONDS,
        help="Seconds between live monitor refreshes.",
    )
    monitor_parser.add_argument(
        "--lines",
        type=int,
        default=DEFAULT_MONITOR_LOG_LINES,
        help="Number of Codex log lines to show.",
    )
    monitor_parser.add_argument(
        "--once",
        action="store_true",
        help="Print one monitor snapshot and exit.",
    )

    finish_parser = subparsers.add_parser(
        "finish",
        help="Preserve remote work, open/update a PR, and terminate the instance.",
    )
    finish_parser.add_argument("job_id", help="AWS instance ID.")
    finish_parser.add_argument(
        "--keep-instance",
        action="store_true",
        help="Skip termination after preservation succeeds.",
    )
    finish_parser.add_argument(
        "--no-pr",
        action="store_true",
        help="Push the branch but skip pull request creation/update.",
    )
    finish_parser.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="Maximum seconds to wait for runtime or termination checks.",
    )

    terminate_parser = subparsers.add_parser(
        "terminate", help="Terminate a running job instance."
    )
    terminate_parser.add_argument("job_id", help="AWS instance ID.")
    terminate_parser.add_argument(
        "--wait",
        dest="wait",
        action="store_true",
        default=True,
        help="Wait for the instance to reach terminated state.",
    )
    terminate_parser.add_argument(
        "--no-wait",
        dest="wait",
        action="store_false",
        help="Return after the terminate API call succeeds.",
    )
    terminate_parser.add_argument(
        "--force",
        action="store_true",
        help="Skip best-effort remote artifact collection before termination.",
    )

    return parser


def run_doctor(config_path: str | None) -> int:
    tools = {
        tool: shutil.which(tool) is not None
        for tool in ["aws", "gh", "git", "ssh", "tmux", "python3", "tailscale", "codex"]
    }
    missing = [tool for tool in ["aws", "git", "ssh", "tmux", "python3"] if not tools[tool]]
    config_ok = False
    config_error = None
    aws_identity = None
    aws_error = None
    if not missing:
        try:
            config = load_config(config_path)
            config_ok = True
            aws_identity = _aws_identity(config)
        except (FileNotFoundError, ValueError) as exc:
            config_error = str(exc)
        except AwsCliError as exc:
            config_ok = True
            aws_error = str(exc)
    report = {
        "config_path": str(Path(config_path).expanduser()) if config_path else str(DEFAULT_CONFIG_PATH),
        "tools": tools,
        "config_ok": config_ok,
        "config_error": config_error,
        "aws_identity": aws_identity,
        "aws_error": aws_error,
    }
    print(json.dumps(report, indent=2))
    return 0 if not missing and config_ok and aws_error is None else 1


def run_init_config(config_path: str | None, force: bool) -> int:
    target = resolve_config_path(config_path)
    try:
        write_default_config(target, force=force)
    except FileExistsError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps({"config_path": str(target), "written": True}, indent=2))
    return 0


def run_sessions(limit: int | None = None, *, json_output: bool = False) -> int:
    sessions = _discover_codex_sessions(limit=_session_display_limit(limit))
    if json_output:
        print(json.dumps([session.to_dict() for session in sessions], indent=2))
    else:
        print(_format_codex_sessions(sessions), end="")
    return 0


def _resolve_start_inputs(
    config: AppConfig,
    repo: str | None,
    mission: str | None,
) -> tuple[str | None, str | None]:
    resolved_repo = repo or config.default_repo
    resolved_mission = mission or config.default_mission
    return resolved_repo, resolved_mission


def run_start(
    config: AppConfig,
    repo: str | None,
    mission: str | None,
    *,
    resume_session_id: str | None = None,
    pick_session: bool = False,
    wait: bool = True,
    timeout_seconds: int = 600,
    monitor: bool = False,
) -> int:
    if monitor and not wait:
        print("--monitor requires runtime verification; remove --no-wait.", file=sys.stderr)
        return 2

    should_run_wizard = (
        repo is None and mission is None and resume_session_id is None and not pick_session
    )
    repo, mission = _resolve_start_inputs(config, repo, mission)
    if repo is None or mission is None:
        print(
            "Config default_repo and default_mission are required when running "
            "'dbx start' without explicit repo and mission.",
            file=sys.stderr,
        )
        return 2
    mission_path = Path(mission).expanduser().resolve()
    if not mission_path.exists():
        print(f"Mission file not found: {mission_path}", file=sys.stderr)
        return 2
    if "/" not in repo:
        repo = f"{config.default_owner}/{repo}"
    if should_run_wizard:
        try:
            selection = _run_start_wizard(
                config,
                repo,
                str(mission_path),
                wait=wait,
                input_stream=sys.stdin,
                output_stream=sys.stderr,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        repo = selection.repo
        mission = selection.mission
        mission_path = Path(mission)
        resume_session_id = selection.resume_session_id
    if resume_session_id == PICK_SESSION:
        resume_session_id = None
        pick_session = True
    if pick_session and resume_session_id:
        print(
            "Use either --pick-session or --resume-session SESSION_ID, not both.",
            file=sys.stderr,
        )
        return 2
    if pick_session:
        try:
            selected_session = _pick_codex_session()
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        resume_session_id = selected_session.session_id

    slug = _slugify(mission_path.stem)
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    job_name = f"{config.job_prefix}-{slug}-{timestamp}"
    branch_name = f"agent/{slug}-{timestamp}"
    session_name = job_name
    job_root = _job_root(config, job_name)
    resume_session_file = None
    resume_session_relative_path = None
    base_branch = config.default_base_branch
    if resume_session_id:
        resume_session_file = _find_codex_session_file(resume_session_id)
        if resume_session_file is None:
            print(f"Codex session not found locally: {resume_session_id}", file=sys.stderr)
            return 2
        resume_session_relative_path = _codex_session_relative_path(resume_session_file)
        resume_session = _load_codex_session_file(resume_session_file)
        if resume_session and resume_session.branch and resume_session.branch != "-":
            base_branch = resume_session.branch

    request = JobLaunchRequest(
        repo=repo,
        mission_path=mission_path,
        job_name=job_name,
        branch_name=branch_name,
        session_name=session_name,
        resume_session_id=resume_session_id,
        resume_session_relative_path=resume_session_relative_path,
        base_branch=base_branch,
    )
    payload = launch_instance(config, request)
    instances = payload.get("Instances", [])
    instance_id = instances[0]["InstanceId"] if instances else "<unknown>"
    job_state = JobState(
        instance_id=instance_id,
        job_name=job_name,
        repo=repo,
        branch_name=branch_name,
        session_name=session_name,
        mission_path=str(mission_path),
        created_at=created_at_now(),
        resume_session_id=resume_session_id,
        job_root=job_root,
        base_branch=base_branch,
        lifecycle_state="launching",
        status="starting",
    )
    save_job_state(job_state)
    resume_session_upload = None
    if resume_session_file and resume_session_relative_path:
        try:
            resume_session_upload = _upload_resume_session_when_reachable(
                config,
                job_state,
                resume_session_file,
                resume_session_relative_path,
                timeout_seconds=timeout_seconds,
            )
        except AwsCliError as exc:
            status, detail = _classify_resume_upload_failure(exc)
            lifecycle_state, termination = _terminate_after_start_failure(
                config,
                job_state,
                status=status,
                detail=detail,
            )
            _print_json(
                _build_start_failure_output(
                    job_state,
                    status=status,
                    detail=detail,
                    lifecycle_state=lifecycle_state,
                    termination=termination,
                )
            )
            return 1

    runtime = None
    if wait:
        try:
            runtime = _wait_for_runtime_ready(
                config,
                job_state,
                timeout_seconds=timeout_seconds,
            )
            job_state = replace(
                job_state,
                lifecycle_state="running",
                status=str(runtime.get("state", "running")),
                last_error=None,
            )
            save_job_state(job_state)
        except RuntimeLifecycleError as exc:
            lifecycle_state, termination = _terminate_after_start_failure(
                config,
                job_state,
                status=exc.status,
                detail=exc.detail,
            )
            _print_json(
                _build_start_failure_output(
                    job_state,
                    status=exc.status,
                    detail=exc.detail,
                    lifecycle_state=lifecycle_state,
                    termination=termination,
                    resume_session_upload=resume_session_upload,
                    artifacts=(
                        exc.runtime.get("artifacts")
                        if isinstance(exc.runtime.get("artifacts"), dict)
                        else {}
                    ),
                )
            )
            return 1

    output = {
        "job_name": job_name,
        "branch_name": branch_name,
        "session_name": session_name,
        "instance_id": instance_id,
        "resume_session_id": resume_session_id,
    }
    if resume_session_upload is not None:
        output["resume_session_upload"] = resume_session_upload
    if runtime is not None:
        output["runtime"] = runtime
    _print_json(output)
    if monitor:
        return run_monitor(config, instance_id, output_stream=sys.stderr)
    return 0


def _classify_resume_upload_failure(exc: AwsCliError) -> tuple[str, str]:
    detail = _redact_text(str(exc))
    text = str(exc).lower()
    status = "timeout" if "timed out" in text or "timeout" in text else "blocked"
    return status, detail


def _terminate_after_start_failure(
    config: AppConfig,
    job_state: JobState,
    *,
    status: str,
    detail: str,
) -> tuple[str, dict[str, object]]:
    lifecycle_state = "terminated"
    try:
        termination = _terminate_job(
            config,
            job_state.instance_id,
            wait=True,
            force=False,
            status_override=status,
            last_error=detail,
        )
    except (AwsCliError, RemoteCommandError) as terminate_exc:
        lifecycle_state = "termination_failed"
        termination = {"error": _redact_text(str(terminate_exc))}
        save_job_state(
            replace(
                job_state,
                lifecycle_state=lifecycle_state,
                status=status,
                last_error=detail,
            )
        )
    return lifecycle_state, termination


def _build_start_failure_output(
    job_state: JobState,
    *,
    status: str,
    detail: str,
    lifecycle_state: str,
    termination: dict[str, object],
    resume_session_upload: dict[str, object] | None = None,
    artifacts: dict[str, object] | None = None,
) -> dict[str, object]:
    output = {
        "job_name": job_state.job_name,
        "branch_name": job_state.branch_name,
        "session_name": job_state.session_name,
        "instance_id": job_state.instance_id,
        "resume_session_id": job_state.resume_session_id,
        "status": status,
        "detail": detail,
        "lifecycle_state": lifecycle_state,
        "termination": termination,
    }
    if resume_session_upload is not None:
        output["resume_session_upload"] = resume_session_upload
    if artifacts:
        output["artifacts"] = _artifact_excerpts(artifacts)
    return output


def run_list(config: AppConfig) -> int:
    instances = list_instances(config)
    by_instance_id: dict[str, dict[str, object]] = {}
    for instance in instances:
        instance_id = instance.get("InstanceId")
        local_state = (
            load_job_state(instance_id) if isinstance(instance_id, str) and instance_id else None
        )
        by_instance_id[str(instance_id)] = {
            "instance_id": instance_id,
            "state": ((instance.get("State") or {}).get("Name")),
            "name": _find_name(instance),
            "launch_time": instance.get("LaunchTime"),
            "repo": local_state.repo if local_state else None,
            "branch_name": local_state.branch_name if local_state else None,
            "mission_path": local_state.mission_path if local_state else None,
            "resume_session_id": local_state.resume_session_id if local_state else None,
            "lifecycle_state": local_state.lifecycle_state if local_state else None,
            "status": local_state.status if local_state else None,
            "pr_url": local_state.pr_url if local_state else None,
        }

    for local_state in list_job_states():
        if local_state.instance_id in by_instance_id:
            continue
        if (
            local_state.lifecycle_state != "terminated"
            and local_state.status not in REMOTE_NON_SUCCESS_STATES
        ):
            continue
        by_instance_id[local_state.instance_id] = {
            "instance_id": local_state.instance_id,
            "state": None,
            "name": local_state.job_name,
            "launch_time": local_state.created_at,
            "repo": local_state.repo,
            "branch_name": local_state.branch_name,
            "mission_path": local_state.mission_path,
            "resume_session_id": local_state.resume_session_id,
            "lifecycle_state": local_state.lifecycle_state,
            "status": local_state.status,
            "pr_url": local_state.pr_url,
            "terminated_at": local_state.terminated_at,
        }

    simplified = sorted(
        by_instance_id.values(),
        key=lambda item: str(item.get("launch_time") or ""),
        reverse=True,
    )
    _print_json(simplified)
    return 0


def _codex_sessions_root() -> Path:
    return Path.home() / ".codex" / "sessions"


def _discover_codex_sessions(limit: int | None = None) -> list[CodexSession]:
    root = _codex_sessions_root()
    if not root.exists():
        return []

    candidates: list[tuple[float, Path, str, str, int]] = []
    for path in root.rglob("*.jsonl"):
        session_id = _extract_session_id(path)
        if not session_id:
            continue
        stat = path.stat()
        try:
            relative_path = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            relative_path = path.name
        candidates.append((stat.st_mtime, path, session_id, relative_path, stat.st_size))

    candidates.sort(key=lambda candidate: candidate[0], reverse=True)
    sessions: list[CodexSession] = []
    for mtime, path, session_id, relative_path, size_bytes in candidates:
        session = _load_codex_session(
            path=path,
            session_id=session_id,
            relative_path=relative_path,
            updated_at=datetime.fromtimestamp(mtime, UTC).isoformat(),
            size_bytes=size_bytes,
        )
        if session is None or not session.latest_user_message:
            continue
        sessions.append(session)
        if limit is not None and len(sessions) >= limit:
            break
    return sessions


def _session_display_limit(limit: int | None) -> int:
    if limit is None:
        return SESSION_LIST_LIMIT
    return max(1, min(limit, SESSION_LIST_LIMIT))


def _load_codex_session(
    *,
    path: Path,
    session_id: str,
    relative_path: str,
    updated_at: str,
    size_bytes: int,
) -> CodexSession | None:
    created_at = updated_at
    branch = "-"
    latest_user_message = ""

    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue
                if record.get("type") == "session_meta":
                    if _is_subagent_source(payload.get("source")):
                        return None
                    created_at = _normalize_timestamp(
                        _optional_text(payload.get("timestamp"))
                        or _optional_text(record.get("timestamp"))
                        or created_at
                    )
                    branch = _extract_branch(payload) or branch
                if record.get("type") == "event_msg" and payload.get("type") == "user_message":
                    message = _extract_event_user_message(payload)
                    if message:
                        latest_user_message = message
    except OSError:
        pass

    return CodexSession(
        session_id=session_id,
        path=path,
        relative_path=relative_path,
        created_at=created_at,
        updated_at=updated_at,
        branch=branch,
        latest_user_message=latest_user_message,
        size_bytes=size_bytes,
    )


def _extract_session_id(path: Path) -> str | None:
    match = SESSION_ID_RE.search(path.name)
    return match.group(1).lower() if match else None


def _extract_branch(payload: dict[str, object]) -> str | None:
    git = payload.get("git")
    if not isinstance(git, dict):
        return None
    for key in ("branch", "current_branch", "ref"):
        value = git.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _is_subagent_source(source: object) -> bool:
    return isinstance(source, dict) and "subagent" in source


def _extract_event_user_message(payload: dict[str, object]) -> str:
    message = payload.get("message")
    if isinstance(message, str):
        return _clean_user_message(message)
    text_elements = payload.get("text_elements")
    if isinstance(text_elements, list):
        parts = [part for part in text_elements if isinstance(part, str)]
        return _clean_user_message(" ".join(parts))
    return ""


def _clean_user_message(text: str) -> str:
    cleaned = re.sub(r"<environment_context>.*?</environment_context>", " ", text, flags=re.S)
    cleaned = re.sub(r"<turn_aborted>.*?</turn_aborted>", " ", cleaned, flags=re.S)
    cleaned = re.sub(r"<subagent_notification>.*?</subagent_notification>", " ", cleaned, flags=re.S)
    cleaned = re.sub(r"<image\b[^>]*>.*?</image>", " ", cleaned, flags=re.S)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def _optional_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _normalize_timestamp(value: str) -> str:
    try:
        return _parse_timestamp(value).isoformat()
    except ValueError:
        return value


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _format_codex_sessions(
    sessions: list[CodexSession],
    *,
    selected_index: int | None = None,
) -> str:
    if not sessions:
        return f"No local Codex sessions found under {_codex_sessions_root()}.\n"

    lines = [f"{'':<2}{'#':<3}{'Created':<15}{'Updated':<15}{'Branch':<12}Conversation"]
    for index, session in enumerate(sessions, start=1):
        marker = ">" if selected_index == index - 1 else " "
        created = _relative_time(session.created_at)
        updated = _relative_time(session.updated_at)
        branch = _truncate(session.branch or "-", 11)
        conversation = _truncate(session.latest_user_message or "(no user message)", 72)
        lines.append(f"{marker} {index:<3}{created:<15}{updated:<15}{branch:<12}{conversation}")
        lines.append(f"   {session.path}")
    return "\n".join(lines) + "\n"


def _relative_time(value: str) -> str:
    try:
        then = _parse_timestamp(value)
    except ValueError:
        return "-"
    now = datetime.now(UTC)
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    seconds = max(0, int((now - then).total_seconds()))
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hours ago"
    days = hours // 24
    return f"{days} days ago"


def _truncate(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    if width <= 3:
        return value[:width]
    return value[: width - 3] + "..."


def _pick_codex_session() -> CodexSession:
    if not sys.stdin.isatty():
        raise ValueError("Cannot pick a Codex session without an interactive terminal.")

    sessions = _discover_codex_sessions(limit=SESSION_LIST_LIMIT)
    if not sessions:
        raise ValueError(f"No local Codex sessions found under {_codex_sessions_root()}.")

    return _run_codex_session_picker(sessions, sys.stdin, sys.stderr)


def _run_start_wizard(
    config: AppConfig,
    repo: str,
    mission: str,
    *,
    wait: bool,
    input_stream,
    output_stream,
) -> StartWizardSelection:
    if not _stream_is_tty(input_stream):
        raise ValueError("Cannot run the dbx start wizard without an interactive terminal.")

    sessions = _discover_codex_sessions(limit=SESSION_LIST_LIMIT)
    while True:
        launch_mode = _run_start_mode_picker(
            sessions,
            repo=repo,
            mission=mission,
            input_stream=input_stream,
            output_stream=output_stream,
        )
        if launch_mode == "cancel":
            raise ValueError("No dbx launch selected.")

        selected_session = None
        if launch_mode == "resume":
            try:
                selected_session = _run_codex_session_picker(
                    sessions,
                    input_stream,
                    output_stream,
                    heading="dbx start launch wizard",
                    step_label="Step 2/3: Choose Codex session",
                )
            except ValueError:
                raise ValueError("No dbx launch selected.") from None
        elif launch_mode == "paste":
            selected_session = _run_pasted_session_picker(
                sessions,
                input_stream=input_stream,
                output_stream=output_stream,
            )

        review_choice = _run_start_review_picker(
            config,
            repo=repo,
            mission=mission,
            selected_session=selected_session,
            wait=wait,
            input_stream=input_stream,
            output_stream=output_stream,
        )
        if review_choice == "back":
            continue
        if review_choice == "cancel":
            raise ValueError("No dbx launch selected.")
        return StartWizardSelection(
            repo=repo,
            mission=mission,
            resume_session_id=selected_session.session_id if selected_session else None,
            launch_mode=launch_mode,
        )


def _run_start_mode_picker(
    sessions: list[CodexSession],
    *,
    repo: str,
    mission: str,
    input_stream,
    output_stream,
) -> str:
    options: list[WizardMenuOption] = []
    if sessions:
        options.append(
            WizardMenuOption(
                "resume",
                "Resume local Codex session",
                "Continue work from one of the latest direct Codex sessions.",
            )
        )
    options.extend(
        [
            WizardMenuOption(
                "fresh",
                "Start fresh mission",
                "Launch a clean devbox using the configured repo and mission.",
            ),
            WizardMenuOption(
                "paste",
                "Paste session ID or path",
                "Resume a local session by UUID or .jsonl path.",
            ),
            WizardMenuOption("cancel", "Cancel", "Leave AWS untouched."),
        ]
    )
    selected = _run_wizard_menu(
        step_label="Step 1/3: Choose launch mode",
        options=options,
        body_lines=[
            "Default launch target",
            f"Repo: {repo}",
            f"Mission: {mission}",
        ],
        input_stream=input_stream,
        output_stream=output_stream,
    )
    return selected.key


def _run_codex_session_picker(
    sessions: list[CodexSession],
    input_stream,
    output_stream,
    *,
    heading: str = "Local Codex sessions",
    step_label: str | None = None,
) -> CodexSession:
    selected_index = 0
    typed = ""
    use_screen = _stream_is_tty(output_stream)

    with _raw_terminal(input_stream), _live_screen(output_stream, use_screen):
        while True:
            _render_codex_session_picker(
                sessions,
                selected_index=selected_index,
                typed=typed,
                output_stream=output_stream,
                clear_screen=use_screen,
                heading=heading,
                step_label=step_label,
            )
            key = _read_picker_key(input_stream)
            if key == "down":
                selected_index = min(selected_index + 1, len(sessions) - 1)
                typed = ""
            elif key == "up":
                selected_index = max(selected_index - 1, 0)
                typed = ""
            elif key == "enter":
                if typed:
                    matched = _match_session_selection(typed, sessions)
                    if matched is not None:
                        return matched
                    print(
                        "No matching session. Use arrows, a number, or paste a "
                        "session ID/path.",
                        file=output_stream,
                    )
                    typed = ""
                    continue
                return sessions[selected_index]
            elif key in {"escape", "q"} and not typed:
                raise ValueError("No Codex session selected.")
            elif key == "backspace":
                typed = typed[:-1]
            elif len(key) == 1 and key.isprintable():
                typed += key


def _render_codex_session_picker(
    sessions: list[CodexSession],
    *,
    selected_index: int,
    typed: str,
    output_stream,
    clear_screen: bool,
    heading: str = "Local Codex sessions",
    step_label: str | None = None,
) -> None:
    _clear_live_screen(output_stream, clear_screen)
    output_stream.write(f"{heading}\n")
    if step_label:
        output_stream.write(f"{step_label}\n")
    output_stream.write("Use ↑/↓ to choose, Enter to select, q to cancel.\n")
    output_stream.write("You can also paste a session ID or path, then press Enter.\n\n")
    output_stream.write(_format_codex_sessions(sessions, selected_index=selected_index))
    if sessions:
        output_stream.write("\n")
        output_stream.write(_format_selected_session_preview(sessions[selected_index]))
    if typed:
        output_stream.write(f"\nSelection: {typed}\n")
    output_stream.flush()


def _format_selected_session_preview(session: CodexSession) -> str:
    return "\n".join(
        [
            "Currently selected",
            f"Conversation: {session.latest_user_message or '(no user message)'}",
            f"Branch: {session.branch or '-'}",
            f"Created: {_relative_time(session.created_at)}",
            f"Updated: {_relative_time(session.updated_at)}",
            f"Session: {session.session_id}",
            f"Path: {session.path}",
            "",
        ]
    )


def _run_pasted_session_picker(
    sessions: list[CodexSession],
    *,
    input_stream,
    output_stream,
) -> CodexSession:
    typed = ""
    error = ""
    use_screen = _stream_is_tty(output_stream)

    with _raw_terminal(input_stream), _live_screen(output_stream, use_screen):
        while True:
            _render_pasted_session_picker(
                typed=typed,
                error=error,
                output_stream=output_stream,
                clear_screen=use_screen,
            )
            key = _read_picker_key(input_stream)
            if key == "enter":
                selected = _resolve_session_selection(typed, sessions)
                if selected is not None:
                    return selected
                error = (
                    "No matching local Codex session. Paste a session UUID or "
                    ".jsonl path."
                )
                typed = ""
            elif key in {"escape", "q"} and not typed:
                raise ValueError("No dbx launch selected.")
            elif key == "backspace":
                typed = typed[:-1]
                error = ""
            elif len(key) == 1 and key.isprintable():
                typed += key
                error = ""


def _render_pasted_session_picker(
    *,
    typed: str,
    error: str,
    output_stream,
    clear_screen: bool,
) -> None:
    _clear_live_screen(output_stream, clear_screen)
    output_stream.write("dbx start launch wizard\n")
    output_stream.write("Step 2/3: Paste session ID or path\n")
    output_stream.write("Paste a Codex session UUID or .jsonl path, then press Enter.\n")
    output_stream.write("Use q or Esc to cancel before typing.\n\n")
    output_stream.write(f"Selection: {typed}\n")
    if error:
        output_stream.write(f"\n{error}\n")
    output_stream.flush()


def _run_start_review_picker(
    config: AppConfig,
    *,
    repo: str,
    mission: str,
    selected_session: CodexSession | None,
    wait: bool,
    input_stream,
    output_stream,
) -> str:
    options = [
        WizardMenuOption("launch", "Launch", "Create the EC2 devbox now."),
        WizardMenuOption("back", "Back", "Return to launch mode selection."),
        WizardMenuOption("cancel", "Cancel", "Leave AWS untouched."),
    ]
    selected = _run_wizard_menu(
        step_label="Step 3/3: Review launch",
        options=options,
        body_lines=_start_review_lines(
            config,
            repo=repo,
            mission=mission,
            selected_session=selected_session,
            wait=wait,
        ),
        input_stream=input_stream,
        output_stream=output_stream,
    )
    return selected.key


def _start_review_lines(
    config: AppConfig,
    *,
    repo: str,
    mission: str,
    selected_session: CodexSession | None,
    wait: bool,
) -> list[str]:
    resume_line = "Resume: fresh Codex mission"
    conversation_lines: list[str] = []
    if selected_session:
        resume_line = f"Resume: {selected_session.session_id}"
        conversation_lines = [
            f"Conversation: {selected_session.latest_user_message}",
            f"Session path: {selected_session.path}",
        ]
    return [
        resume_line,
        *conversation_lines,
        f"Repo: {repo}",
        f"Mission: {mission}",
        f"AWS: {config.instance_type} in {config.aws_region}",
        f"Base branch: {config.default_base_branch}",
        f"Wait for runtime: {'yes' if wait else 'no'}",
    ]


def _run_wizard_menu(
    *,
    step_label: str,
    options: list[WizardMenuOption],
    body_lines: list[str],
    input_stream,
    output_stream,
) -> WizardMenuOption:
    selected_index = 0
    use_screen = _stream_is_tty(output_stream)

    with _raw_terminal(input_stream), _live_screen(output_stream, use_screen):
        while True:
            _render_wizard_menu(
                step_label=step_label,
                options=options,
                selected_index=selected_index,
                body_lines=body_lines,
                output_stream=output_stream,
                clear_screen=use_screen,
            )
            key = _read_picker_key(input_stream)
            if key == "down":
                selected_index = min(selected_index + 1, len(options) - 1)
            elif key == "up":
                selected_index = max(selected_index - 1, 0)
            elif key == "enter":
                return options[selected_index]
            elif key in {"escape", "q"}:
                return WizardMenuOption("cancel", "Cancel")


def _render_wizard_menu(
    *,
    step_label: str,
    options: list[WizardMenuOption],
    selected_index: int,
    body_lines: list[str],
    output_stream,
    clear_screen: bool,
) -> None:
    _clear_live_screen(output_stream, clear_screen)
    output_stream.write("dbx start launch wizard\n")
    output_stream.write(f"{step_label}\n")
    output_stream.write("Use ↑/↓ to choose, Enter to continue, q to cancel.\n\n")
    for index, option in enumerate(options):
        marker = ">" if selected_index == index else " "
        output_stream.write(f"{marker} {option.label}\n")
        if option.detail:
            output_stream.write(f"  {option.detail}\n")
    if body_lines:
        output_stream.write("\n")
        for line in body_lines:
            output_stream.write(f"{line}\n")
    output_stream.flush()


def _clear_live_screen(output_stream, clear_screen: bool) -> None:
    if clear_screen:
        output_stream.write(ALT_SCREEN_ENTER)
        output_stream.write(CLEAR_SCREEN)


@contextmanager
def _live_screen(output_stream, enabled: bool):
    if not enabled:
        yield
        return

    output_stream.write(ALT_SCREEN_ENTER)
    output_stream.flush()
    try:
        yield
    finally:
        output_stream.write(ALT_SCREEN_EXIT)
        output_stream.flush()


@contextmanager
def _raw_terminal(input_stream):
    try:
        fd = input_stream.fileno()
        original = termios.tcgetattr(fd)
    except (AttributeError, OSError, termios.error, ValueError):
        yield
        return

    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, original)


def _read_monitor_key(input_stream, interval_seconds: float) -> str | None:
    try:
        readable, _, _ = select.select([input_stream], [], [], interval_seconds)
    except (OSError, ValueError):
        time.sleep(interval_seconds)
        return None
    if not readable:
        return None
    return _read_picker_key(input_stream)


def _read_picker_key(input_stream) -> str:
    char = input_stream.read(1)
    if char in {"\n", "\r"}:
        return "enter"
    if char in {"\x7f", "\b"}:
        return "backspace"
    if char == "\x1b":
        suffix = input_stream.read(2)
        if suffix in {"[A", "OA"}:
            return "up"
        if suffix in {"[B", "OB"}:
            return "down"
        return "escape"
    if char == "":
        return "enter"
    if char in {"q", "Q"}:
        return "q"
    return char


def _match_session_selection(selection: str, sessions: list[CodexSession]) -> CodexSession | None:
    cleaned = selection.strip()
    if not cleaned:
        return None
    if cleaned.isdigit():
        index = int(cleaned)
        if 1 <= index <= len(sessions):
            return sessions[index - 1]
    match = SESSION_ID_RE.search(cleaned)
    if match:
        session_id = match.group(1).lower()
        for session in sessions:
            if session.session_id == session_id:
                return session
    for session in sessions:
        if cleaned in {str(session.path), session.relative_path}:
            return session
    return None


def _resolve_session_selection(selection: str, sessions: list[CodexSession]) -> CodexSession | None:
    matched = _match_session_selection(selection, sessions)
    if matched is not None:
        return matched

    cleaned = selection.strip()
    if not cleaned:
        return None

    match = SESSION_ID_RE.search(cleaned)
    if match:
        session_file = _find_codex_session_file(match.group(1).lower())
        if session_file is not None:
            return _load_codex_session_file(session_file)

    pasted_path = Path(cleaned).expanduser()
    if pasted_path.exists() and pasted_path.is_file() and _is_codex_session_path(pasted_path):
        return _load_codex_session_file(pasted_path)
    return None


def _is_codex_session_path(session_file: Path) -> bool:
    try:
        session_file.resolve().relative_to(_codex_sessions_root().resolve())
    except ValueError:
        return False
    return True


def _stream_is_tty(stream) -> bool:
    return bool(getattr(stream, "isatty", lambda: False)())


def _find_codex_session_file(session_id: str) -> Path | None:
    root = _codex_sessions_root()
    if not root.exists():
        return None
    matches = list(root.rglob(f"*{session_id.lower()}.jsonl"))
    if not matches:
        return None
    return max(matches, key=lambda path: path.stat().st_mtime)


def _load_codex_session_file(session_file: Path) -> CodexSession | None:
    session_id = _extract_session_id(session_file)
    if session_id is None:
        return None
    try:
        stat = session_file.stat()
    except OSError:
        return None
    return _load_codex_session(
        path=session_file,
        session_id=session_id,
        relative_path=_codex_session_relative_path(session_file),
        updated_at=datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
        size_bytes=stat.st_size,
    )


def _codex_session_relative_path(session_file: Path) -> str:
    root = _codex_sessions_root()
    try:
        return session_file.expanduser().resolve().relative_to(root.expanduser().resolve()).as_posix()
    except ValueError:
        return session_file.name


def _upload_resume_session_when_reachable(
    config: AppConfig,
    job_state: JobState,
    session_file: Path,
    relative_path: str,
    *,
    timeout_seconds: int,
    poll_interval_seconds: int = 3,
) -> dict[str, object]:
    deadline = time.time() + timeout_seconds
    remote_path = _remote_codex_session_path(config, relative_path)
    content = session_file.read_text(encoding="utf-8")
    target = f"{config.ssh_user}@{job_state.session_name}"
    last_problem = "waiting for SSH upload target"

    while time.time() < deadline:
        try:
            upload_remote_text(target, remote_path, content)
            return {"remote_path": remote_path, "uploaded": True}
        except RemoteCommandError as exc:
            last_problem = str(exc)
            time.sleep(poll_interval_seconds)

    raise AwsCliError(
        f"Timed out uploading Codex resume session to {job_state.instance_id}. "
        f"Last problem: {last_problem}"
    )


def _remote_codex_session_path(config: AppConfig, relative_path: str) -> str:
    home = "/root" if config.ssh_user == "root" else f"/home/{config.ssh_user}"
    return f"{home}/.codex/sessions/{relative_path}"


def run_status(config: AppConfig, job_id: str, *, include_logs: bool = False) -> int:
    summary = _build_status_summary(config, job_id, include_logs=include_logs)
    _print_json(summary)
    return 0


def run_attach(config: AppConfig, job_id: str, *, check: bool = False) -> int:
    instance = describe_instance(config, job_id)
    local_state = _load_or_infer_job_state(config, instance, job_id)
    session_name = local_state.session_name or _find_name(instance)
    if not session_name:
        raise AwsCliError("Could not determine tmux session name from instance tags.")
    target = build_ssh_target(config, instance)
    command = build_attach_command(target, session_name)
    if check:
        _verify_remote_attach_ready(target, local_state)
        print(
            json.dumps(
                {
                    "attach_command": " ".join(shlex_quote(part) for part in command),
                    "ssh_target": target,
                    "session_name": session_name,
                    "ready": True,
                },
                indent=2,
            )
        )
        return 0
    print(" ".join(shlex_quote(part) for part in command))
    return 0


def run_monitor(
    config: AppConfig,
    job_id: str,
    *,
    interval_seconds: float = DEFAULT_MONITOR_INTERVAL_SECONDS,
    lines: int = DEFAULT_MONITOR_LOG_LINES,
    once: bool = False,
    input_stream=None,
    output_stream=None,
) -> int:
    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stdout
    instance = describe_instance(config, job_id)
    job_state = _load_or_infer_job_state(config, instance, job_id)
    target = build_ssh_target(config, instance)
    live = _stream_is_tty(input_stream) and _stream_is_tty(output_stream) and not once
    if not _stream_is_tty(input_stream):
        once = True

    try:
        with _raw_terminal(input_stream), _live_screen(output_stream, live):
            while True:
                git_status, codex_log = _monitor_remote_snapshot(
                    target,
                    job_state,
                    lines=lines,
                )
                runtime_issue = _runtime_issue_from_codex_log(codex_log)
                if runtime_issue is not None:
                    updated_state = _apply_runtime_issue_to_job_state(job_state, runtime_issue)
                    if updated_state != job_state:
                        save_job_state(updated_state)
                        job_state = updated_state
                _render_monitor_snapshot(
                    job_id=job_id,
                    git_status=git_status,
                    codex_log=codex_log,
                    runtime_issue=runtime_issue,
                    output_stream=output_stream,
                    clear_screen=live,
                    lines=lines,
                    interval_seconds=interval_seconds,
                )
                if once:
                    return 0
                key = _read_monitor_key(input_stream, interval_seconds)
                if key in {"q", "Q", "escape"}:
                    return 0
    except KeyboardInterrupt:
        return 0


def run_finish(
    config: AppConfig,
    job_id: str,
    *,
    keep_instance: bool = False,
    no_pr: bool = False,
    timeout_seconds: int = 600,
) -> int:
    instance = describe_instance(config, job_id)
    job_state = _load_or_infer_job_state(config, instance, job_id)
    target = build_ssh_target(config, instance)
    artifacts = _capture_remote_artifacts(target, job_state, include_logs=True)
    _persist_remote_artifacts(job_id, artifacts)
    blocked = bool(artifacts.get("blocker")) or _artifact_state_is_blocked(artifacts)
    if _runtime_issue_from_artifacts(artifacts) is not None:
        blocked = True

    finish_result = _run_finish_remote(
        target,
        job_state,
        job_state.base_branch or config.default_base_branch,
        no_pr=no_pr,
        blocked=blocked,
    )
    artifacts = _capture_remote_artifacts(target, job_state, include_logs=True)
    _persist_remote_artifacts(job_id, artifacts)

    updated_state = replace(
        job_state,
        lifecycle_state="preserved",
        status="blocked" if blocked else "preserved",
        pr_url=finish_result.get("pr_url"),
        last_error=None,
    )
    save_job_state(updated_state)

    output: dict[str, object] = {
        "instance_id": job_id,
        "branch_name": updated_state.branch_name,
        "pr_url": finish_result.get("pr_url"),
        "blocked": blocked,
        "artifacts_saved": True,
    }

    if not keep_instance:
        output["termination"] = _terminate_job(config, job_id, wait=True, force=False)
    else:
        output["termination"] = {"skipped": True}

    _print_json(output)
    return 0


def run_terminate(
    config: AppConfig,
    job_id: str,
    *,
    wait: bool = True,
    force: bool = False,
) -> int:
    payload = _terminate_job(config, job_id, wait=wait, force=force)
    _print_json(payload)
    return 0


def _wait_for_runtime_ready(
    config: AppConfig,
    job_state: JobState,
    *,
    timeout_seconds: int = 600,
    poll_interval_seconds: int = 5,
) -> dict[str, object]:
    deadline = time.time() + timeout_seconds
    last_problem = "waiting for EC2 launch"
    last_detail = "waiting_for_ec2_launch"
    paths = _remote_paths(job_state)

    while time.time() < deadline:
        try:
            instance = describe_instance(config, job_state.instance_id)
            instance_state = ((instance.get("State") or {}).get("Name"))
            if instance_state != "running":
                last_problem = f"instance state is {instance_state}"
                last_detail = str(instance_state or "instance_not_running")
                time.sleep(poll_interval_seconds)
                continue

            status = describe_instance_status(config, job_state.instance_id) or {}
            target = build_ssh_target(config, instance)
            if not _remote_file_exists(target, "/var/lib/cloud/instance/boot-finished"):
                last_problem = "cloud-init has not finished"
                last_detail = "cloud_init_incomplete"
                time.sleep(poll_interval_seconds)
                continue

            remote_status = _read_remote_json(target, paths["status_json"])
            remote_state = str(remote_status.get("state") or "")
            remote_detail = str(remote_status.get("detail") or "")
            tmux_ready = _remote_tmux_session_exists(target, job_state.session_name)
            codex_log_ready = False
            if tmux_ready:
                codex_log_ready = _remote_file_exists(target, paths["codex_log"])
            if codex_log_ready:
                codex_log = _tail_remote_text(target, paths["codex_log"], optional=True)
                if isinstance(codex_log, str) and _log_has_codex_auth_failure(codex_log):
                    artifacts = _capture_remote_artifacts(target, job_state, include_logs=True)
                    _persist_remote_artifacts(job_state.instance_id, artifacts)
                    raise RuntimeLifecycleError(
                        status="auth_failed",
                        detail="codex_auth_failed",
                        runtime={
                            "status": "auth_failed",
                            "detail": "codex_auth_failed",
                            "ssh_target": target,
                            "artifacts": artifacts,
                        },
                    )
            if remote_state in REMOTE_NON_SUCCESS_STATES:
                artifacts = _capture_remote_artifacts(target, job_state, include_logs=True)
                _persist_remote_artifacts(job_state.instance_id, artifacts)
                raise RuntimeLifecycleError(
                    status=remote_state,
                    detail=remote_detail or remote_state,
                    runtime={
                        "status": remote_state,
                        "detail": remote_detail or remote_state,
                        "ssh_target": target,
                        "artifacts": artifacts,
                    },
                )
            if remote_state not in REMOTE_READY_STATES:
                last_problem = (
                    f"remote runtime state is {remote_state}"
                    if remote_state
                    else "remote runtime state is unavailable"
                )
                last_detail = remote_detail or remote_state or "runtime_not_ready"
                time.sleep(poll_interval_seconds)
                continue

            if not tmux_ready:
                last_problem = "tmux session is not ready"
                last_detail = "tmux_session_not_ready"
                time.sleep(poll_interval_seconds)
                continue

            if not codex_log_ready:
                last_problem = "codex log has not been created"
                last_detail = "codex_log_missing"
                time.sleep(poll_interval_seconds)
                continue

            if not _remote_file_exists(target, paths["agent_started_json"]):
                last_problem = "agent heartbeat has not been written"
                last_detail = "waiting_for_agent_heartbeat"
                time.sleep(poll_interval_seconds)
                continue

            heartbeat = _read_remote_json(target, paths["agent_started_json"])
            heartbeat_status = str(heartbeat.get("status") or "")
            if heartbeat_status not in REMOTE_READY_STATES:
                last_problem = (
                    f"agent heartbeat status is {heartbeat_status}"
                    if heartbeat_status
                    else "agent heartbeat status is unavailable"
                )
                last_detail = heartbeat_status or "waiting_for_agent_heartbeat"
                time.sleep(poll_interval_seconds)
                continue

            return {
                "phase": remote_status.get("phase", "runtime"),
                "state": remote_state or heartbeat_status,
                "detail": remote_detail or "agent_heartbeat_received",
                "ssh_target": target,
                "heartbeat": heartbeat,
                "cloud_init_complete": True,
                "tmux_session": job_state.session_name,
                "codex_log_ready": True,
                "system_status": (status.get("SystemStatus") or {}).get("Status"),
                "instance_status": (status.get("InstanceStatus") or {}).get("Status"),
            }
        except RuntimeLifecycleError:
            raise
        except (AwsCliError, RemoteCommandError) as exc:
            last_problem = str(exc)
            last_detail = "transient_remote_error"
            time.sleep(poll_interval_seconds)

    console_output = _console_output_excerpt(config, job_state.instance_id)
    artifacts = _best_effort_runtime_artifacts(config, job_state)
    if console_output:
        artifacts["console_output_excerpt"] = console_output
        save_job_artifact(
            job_state.instance_id,
            "console-output.txt",
            console_output + "\n",
        )
    raise RuntimeLifecycleError(
        status="timeout",
        detail=last_detail,
        runtime={
            "status": "timeout",
            "detail": last_detail,
            "last_problem": last_problem,
            "artifacts": artifacts,
        },
    )


def _build_status_summary(
    config: AppConfig,
    job_id: str,
    *,
    include_logs: bool = False,
) -> dict[str, object]:
    try:
        instance = describe_instance(config, job_id)
    except AwsCliError as exc:
        return _build_local_status_summary(job_id, include_logs=include_logs, aws_error=str(exc))

    local_state = _load_or_infer_job_state(config, instance, job_id)
    instance_status = describe_instance_status(config, job_id) or {}
    summary: dict[str, object] = {
        "instance_id": instance.get("InstanceId"),
        "name": _find_name(instance),
        "state": ((instance.get("State") or {}).get("Name")),
        "launch_time": instance.get("LaunchTime"),
        "private_dns_name": instance.get("PrivateDnsName"),
        "private_ip": instance.get("PrivateIpAddress"),
        "repo": local_state.repo,
        "branch_name": local_state.branch_name,
        "mission_path": local_state.mission_path,
        "resume_session_id": local_state.resume_session_id,
        "job_root": local_state.job_root,
        "lifecycle_state": local_state.lifecycle_state,
        "status": local_state.status,
        "pr_url": local_state.pr_url,
        "last_error": local_state.last_error,
        "system_status": (instance_status.get("SystemStatus") or {}).get("Status"),
        "instance_status": (instance_status.get("InstanceStatus") or {}).get("Status"),
    }

    try:
        target = build_ssh_target(config, instance)
        summary["ssh_target"] = target
        artifacts = _capture_remote_artifacts(target, local_state, include_logs=include_logs)
        _persist_remote_artifacts(job_id, artifacts)
        summary["remote_status"] = artifacts.get("status_json")
        summary["status_markdown"] = artifacts.get("status_md")
        summary["blocker"] = artifacts.get("blocker")
        runtime_issue = _runtime_issue_from_artifacts(artifacts)
        if runtime_issue is not None:
            summary["status"] = runtime_issue["status"]
            summary["last_error"] = runtime_issue["detail"]
            summary["runtime_issue"] = runtime_issue
            summary["blocker"] = summary["blocker"] or runtime_issue["blocker"]
            updated_state = _apply_runtime_issue_to_job_state(local_state, runtime_issue)
            if updated_state != local_state:
                save_job_state(updated_state)
        if isinstance(artifacts.get("errors"), dict) and artifacts["errors"]:
            summary["artifact_errors"] = artifacts["errors"]
        if include_logs:
            summary["logs"] = {
                "bootstrap": artifacts.get("bootstrap_log"),
                "codex": artifacts.get("codex_log"),
                "finish": artifacts.get("finish_log"),
            }
    except (AwsCliError, RemoteCommandError) as exc:
        summary["ssh_error"] = str(exc)
        summary["console_output_excerpt"] = _console_output_excerpt(config, job_id)

    return summary


def _terminate_job(
    config: AppConfig,
    job_id: str,
    *,
    wait: bool,
    force: bool,
    status_override: str | None = None,
    last_error: str | None = None,
) -> dict[str, object]:
    instance = describe_instance(config, job_id)
    local_state = _load_or_infer_job_state(config, instance, job_id)
    artifacts: dict[str, object] = {}

    if not force:
        try:
            target = build_ssh_target(config, instance)
            artifacts = _capture_remote_artifacts(target, local_state, include_logs=True)
            _persist_remote_artifacts(job_id, artifacts)
        except (AwsCliError, RemoteCommandError):
            console_output = _console_output_excerpt(config, job_id)
            if console_output:
                save_job_artifact(job_id, "console-output.txt", console_output + "\n")
                artifacts["console_output_excerpt"] = console_output

    payload = terminate_instance(config, job_id)
    if wait:
        final_instance = wait_for_instance_terminated(config, job_id)
        payload["final_state"] = ((final_instance.get("State") or {}).get("Name"))
    if artifacts:
        payload["artifacts"] = _artifact_excerpts(artifacts)

    updated_state = replace(
        local_state,
        lifecycle_state="terminated",
        status=status_override or local_state.status,
        last_error=last_error,
        terminated_at=created_at_now(),
    )
    save_job_state(updated_state)
    return payload


def _capture_remote_artifacts(
    target: str,
    job_state: JobState,
    *,
    include_logs: bool,
) -> dict[str, object]:
    paths = _remote_paths(job_state)
    artifacts: dict[str, object] = {"errors": {}}
    artifacts["status_json"] = _capture_artifact(
        artifacts,
        "status_json",
        lambda: _read_remote_json(target, paths["status_json"]),
    )
    artifacts["status_md"] = _capture_artifact(
        artifacts,
        "status_md",
        lambda: _read_remote_text(target, paths["status_md"]),
    )
    artifacts["blocker"] = _capture_artifact(
        artifacts,
        "blocker",
        lambda: _read_remote_text(target, paths["blocker"], optional=True),
    )
    if include_logs:
        artifacts["bootstrap_log"] = _capture_artifact(
            artifacts,
            "bootstrap_log",
            lambda: _tail_remote_text(target, paths["bootstrap_log"]),
        )
        artifacts["codex_log"] = _capture_artifact(
            artifacts,
            "codex_log",
            lambda: _tail_remote_text(target, paths["codex_log"]),
        )
        artifacts["finish_log"] = _capture_artifact(
            artifacts,
            "finish_log",
            lambda: _tail_remote_text(target, paths["finish_log"], optional=True),
        )
    return artifacts


def _monitor_remote_snapshot(
    target: str,
    job_state: JobState,
    *,
    lines: int,
) -> tuple[str, str]:
    paths = _remote_paths(job_state)
    return (
        _remote_git_status(target, paths["repo_dir"]),
        _tail_remote_text(target, paths["codex_log"], optional=True, lines=lines)
        or "(codex log is not available yet)\n",
    )


def _remote_git_status(target: str, repo_dir: str) -> str:
    try:
        return _read_remote_command_output(
            target,
            f"cd {shlex.quote(repo_dir)} && git status --short --branch",
        )
    except RemoteCommandError as exc:
        return f"(git status is not available yet: {exc})\n"


def _render_monitor_snapshot(
    *,
    job_id: str,
    git_status: str,
    codex_log: str,
    runtime_issue: dict[str, str] | None,
    output_stream,
    clear_screen: bool,
    lines: int,
    interval_seconds: float,
) -> None:
    _clear_live_screen(output_stream, clear_screen)
    output_stream.write(f"dbx monitor {job_id}\n")
    output_stream.write(f"Refreshing every {interval_seconds:g}s. q/Ctrl-C exits.\n\n")
    if runtime_issue is not None:
        output_stream.write(
            f"Runtime issue: {runtime_issue['status']} ({runtime_issue['detail']})\n\n"
        )
    output_stream.write("Git\n")
    output_stream.write(git_status.rstrip() or "(clean)")
    output_stream.write("\n\n")
    output_stream.write(f"Codex output (last {lines} lines)\n")
    output_stream.write(codex_log.rstrip() or "(no codex output yet)")
    output_stream.write("\n")
    output_stream.flush()


def _persist_remote_artifacts(job_id: str, artifacts: dict[str, object]) -> None:
    if "status_json" in artifacts and artifacts["status_json"] is not None:
        save_job_artifact(
            job_id,
            "status.json",
            _redact_text(json.dumps(artifacts["status_json"], indent=2) + "\n"),
        )
    if "status_md" in artifacts and isinstance(artifacts["status_md"], str):
        save_job_artifact(job_id, "STATUS.md", _redact_text(artifacts["status_md"]))
    if "blocker" in artifacts and isinstance(artifacts["blocker"], str):
        save_job_artifact(job_id, "BLOCKER.md", _redact_text(artifacts["blocker"]))
    if "bootstrap_log" in artifacts and isinstance(artifacts["bootstrap_log"], str):
        save_job_artifact(job_id, "bootstrap.log", _redact_text(artifacts["bootstrap_log"]))
    if "codex_log" in artifacts and isinstance(artifacts["codex_log"], str):
        save_job_artifact(job_id, "codex.log", _redact_text(artifacts["codex_log"]))
    if "finish_log" in artifacts and isinstance(artifacts["finish_log"], str):
        save_job_artifact(job_id, "finish.log", _redact_text(artifacts["finish_log"]))
    errors = artifacts.get("errors")
    if isinstance(errors, dict):
        for name, message in errors.items():
            if isinstance(message, str) and message:
                save_job_artifact(
                    job_id,
                    f"{name}.error.txt",
                    _redact_text(message) + "\n",
                )


def _run_finish_remote(
    target: str,
    job_state: JobState,
    base_branch: str,
    *,
    no_pr: bool,
    blocked: bool,
) -> dict[str, object]:
    paths = _remote_paths(job_state)
    mode = "blocked" if blocked else "complete"
    no_pr_arg = " --no-pr" if no_pr else ""
    script = "\n".join(
        [
            "set -euo pipefail",
            f"/usr/local/bin/dbx-finish-job --mode {shlex.quote(mode)} --base {shlex.quote(base_branch)}{no_pr_arg}",
        ]
    )
    run_remote_shell_command(target, script)
    pr_url = None
    if not no_pr:
        pr_url = _read_remote_command_output(
            target,
            f"cd {shlex.quote(paths['repo_dir'])} && gh pr view {shlex.quote(job_state.branch_name)} --json url --jq '.url'",
        ).strip()
    return {"pr_url": pr_url or None}


def _verify_remote_attach_ready(target: str, job_state: JobState) -> None:
    if not _remote_tmux_session_exists(target, job_state.session_name):
        raise RemoteCommandError(
            f"Remote tmux session {job_state.session_name} is not available for attach."
        )


def _load_or_infer_job_state(
    config: AppConfig,
    instance: dict[str, object],
    job_id: str,
) -> JobState:
    local_state = load_job_state(job_id)
    if local_state is not None:
        return local_state

    job_name = _find_name(instance) or job_id
    repo = _find_tag(instance, "Repo") or ""
    branch_name = ""
    session_name = job_name
    launch_time = instance.get("LaunchTime")
    created_at = str(launch_time) if launch_time is not None else created_at_now()
    return JobState(
        instance_id=job_id,
        job_name=job_name,
        repo=repo,
        branch_name=branch_name,
        session_name=session_name,
        mission_path="",
        created_at=created_at,
        job_root=_job_root(config, job_name),
        base_branch=config.default_base_branch,
        lifecycle_state="discovered",
        status=str(((instance.get("State") or {}).get("Name")) or "unknown"),
    )


def _remote_paths(job_state: JobState) -> dict[str, str]:
    job_root = job_state.job_root
    return {
        "repo_dir": f"{job_root}/repo",
        "status_json": f"{job_root}/status.json",
        "agent_started_json": f"{job_root}/AGENT_STARTED.json",
        "status_md": f"{job_root}/STATUS.md",
        "blocker": f"{job_root}/BLOCKER.md",
        "bootstrap_log": f"{job_root}/logs/bootstrap.log",
        "codex_log": f"{job_root}/logs/codex.log",
        "finish_log": f"{job_root}/logs/finish.log",
    }


def _read_remote_json(target: str, path: str) -> dict[str, object]:
    output = _read_remote_command_output(target, f"cat {shlex.quote(path)}")
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        excerpt = output[:200].replace("\n", "\\n")
        raise RemoteCommandError(
            f"Remote JSON artifact was invalid at {path}: {excerpt}"
        ) from exc
    return payload if isinstance(payload, dict) else {}


def _read_remote_text(target: str, path: str, *, optional: bool = False) -> str | None:
    result = run_remote_shell_command(
        target,
        f"cat {shlex.quote(path)}",
        check=not optional,
    )
    if optional and result.returncode != 0:
        return None
    return result.stdout


def _tail_remote_text(target: str, path: str, *, optional: bool = False, lines: int = 40) -> str | None:
    result = run_remote_shell_command(
        target,
        f"tail -n {lines} {shlex.quote(path)}",
        check=not optional,
    )
    if optional and result.returncode != 0:
        return None
    return result.stdout


def _remote_file_exists(target: str, path: str) -> bool:
    result = run_remote_shell_command(
        target,
        f"test -f {shlex.quote(path)}",
        check=False,
    )
    return result.returncode == 0


def _remote_tmux_session_exists(target: str, session_name: str) -> bool:
    result = run_remote_shell_command(
        target,
        f"tmux has-session -t {shlex.quote(session_name)}",
        check=False,
    )
    return result.returncode == 0


def _read_remote_command_output(target: str, shell_command: str) -> str:
    return run_remote_shell_command(target, shell_command).stdout


def _artifact_state_is_blocked(artifacts: dict[str, object]) -> bool:
    status_json = artifacts.get("status_json")
    if not isinstance(status_json, dict):
        return False
    return str(status_json.get("state")) in REMOTE_NON_SUCCESS_STATES


def _console_output_excerpt(config: AppConfig, job_id: str, limit: int = 400) -> str:
    output = get_console_output(config, job_id).strip()
    if not output:
        return ""
    compact = " ".join(output.split())
    return _redact_text(compact[:limit])


def _job_root(config: AppConfig, job_name: str) -> str:
    return f"{config.repo_root.rstrip('/')}/{job_name}"


def _finish_commit_message(job_name: str) -> str:
    return "\n".join(
        [
            f"Preserve dbx job state for {job_name}",
            "",
            "Constraint: dbx finish auto-commits dirty remote work before push, PR, and teardown.",
            "Rejected: Refuse dirty work preservation | Conflicts with the disposable runner workflow selected for dbx finish.",
            "Confidence: medium",
            "Scope-risk: moderate",
            "Directive: Review this auto-generated checkpoint commit before merge.",
            "Tested: Remote finish preservation runs during dbx finish.",
            "Not-tested: Commit content quality still depends on the remote working tree state.",
        ]
    )


def _find_name(instance: dict[str, object]) -> str | None:
    return _find_tag(instance, "Name")


def _find_tag(instance: dict[str, object], key: str) -> str | None:
    tags = instance.get("Tags", [])
    if not isinstance(tags, list):
        return None
    for tag in tags:
        if isinstance(tag, dict) and tag.get("Key") == key:
            value = tag.get("Value")
            if isinstance(value, str) and value:
                return value
    return None


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "mission"


def shlex_quote(value: str) -> str:
    return subprocess.list2cmdline([value]) if sys.platform == "win32" else shlex.quote(value)


def _aws_identity(config: AppConfig) -> dict[str, object]:
    result = subprocess.run(
        _aws_identity_command(config),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AwsCliError(result.stderr.strip() or "Could not query AWS caller identity.")
    return json.loads(result.stdout)


def _aws_identity_command(config: AppConfig) -> list[str]:
    command = ["aws"]
    if config.aws_profile:
        command.extend(["--profile", config.aws_profile])
    command.extend(
        [
            "--region",
            config.aws_region,
            "sts",
            "get-caller-identity",
            "--output",
            "json",
        ]
    )
    return command


def _capture_artifact(
    artifacts: dict[str, object],
    name: str,
    reader: Callable[[], object],
) -> object:
    try:
        return reader()
    except (AwsCliError, RemoteCommandError) as exc:
        errors = artifacts.setdefault("errors", {})
        if isinstance(errors, dict):
            errors[name] = _redact_text(str(exc))
        return None


def _best_effort_runtime_artifacts(
    config: AppConfig,
    job_state: JobState,
) -> dict[str, object]:
    try:
        instance = describe_instance(config, job_state.instance_id)
        target = build_ssh_target(config, instance)
        artifacts = _capture_remote_artifacts(target, job_state, include_logs=True)
        _persist_remote_artifacts(job_state.instance_id, artifacts)
        return artifacts
    except (AwsCliError, RemoteCommandError):
        return {}


def _log_has_codex_auth_failure(text: str) -> bool:
    return any(pattern.search(text) for pattern in _CODEX_AUTH_FAILURE_PATTERNS)


def _runtime_issue_from_codex_log(codex_log: str | None) -> dict[str, str] | None:
    if not isinstance(codex_log, str) or not _log_has_codex_auth_failure(codex_log):
        return None
    return {
        "status": "auth_failed",
        "detail": "codex_auth_failed",
        "blocker": _codex_auth_failure_blocker(codex_log),
    }


def _runtime_issue_from_artifacts(artifacts: dict[str, object]) -> dict[str, str] | None:
    codex_log = artifacts.get("codex_log")
    if not isinstance(codex_log, str):
        return None
    return _runtime_issue_from_codex_log(codex_log)


def _apply_runtime_issue_to_job_state(
    job_state: JobState, runtime_issue: dict[str, str]
) -> JobState:
    return replace(
        job_state,
        status=runtime_issue["status"],
        last_error=runtime_issue["detail"],
    )


def _codex_auth_failure_blocker(codex_log: str) -> str:
    excerpt = _redact_text(codex_log.strip()) or "Codex reported an authentication failure."
    return "\n".join(
        [
            "# Codex authentication failed",
            "",
            "dbx detected a Codex authentication failure after the runtime was marked ready.",
            "",
            "## codex.log tail",
            excerpt,
            "",
        ]
    )


def _redact_text(text: str) -> str:
    redacted = text
    for pattern in _REDACTION_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def _redact_value(value: object) -> object:
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        return {key: _redact_value(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value


def _artifact_excerpts(artifacts: dict[str, object]) -> dict[str, object]:
    excerpts: dict[str, object] = {}
    for key in ("status_json", "status_md", "blocker", "bootstrap_log", "codex_log", "finish_log"):
        value = artifacts.get(key)
        if isinstance(value, dict):
            excerpts[key] = value
        elif isinstance(value, str) and value:
            excerpts[key] = _redact_text(value[:200])
    errors = artifacts.get("errors")
    if isinstance(errors, dict) and errors:
        excerpts["errors"] = {name: _redact_text(str(message)) for name, message in errors.items()}
    if isinstance(artifacts.get("console_output_excerpt"), str):
        excerpts["console_output_excerpt"] = _redact_text(
            str(artifacts["console_output_excerpt"])[:200]
        )
    return excerpts


def _print_json(payload: object) -> None:
    print(json.dumps(_redact_value(payload), indent=2, default=str))


def _build_local_status_summary(
    job_id: str,
    *,
    include_logs: bool,
    aws_error: str,
) -> dict[str, object]:
    local_state = load_job_state(job_id)
    if local_state is None:
        raise AwsCliError(aws_error)
    artifacts = load_job_artifacts(job_id)
    remote_status = None
    if "status.json" in artifacts:
        try:
            parsed = json.loads(artifacts["status.json"])
            remote_status = parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            remote_status = None
    summary: dict[str, object] = {
        "instance_id": local_state.instance_id,
        "name": local_state.job_name,
        "state": None,
        "launch_time": local_state.created_at,
        "repo": local_state.repo,
        "branch_name": local_state.branch_name,
        "mission_path": local_state.mission_path,
        "resume_session_id": local_state.resume_session_id,
        "job_root": local_state.job_root,
        "lifecycle_state": local_state.lifecycle_state,
        "status": local_state.status,
        "pr_url": local_state.pr_url,
        "last_error": local_state.last_error,
        "terminated_at": local_state.terminated_at,
        "aws_error": _redact_text(aws_error),
        "remote_status": remote_status,
        "status_markdown": artifacts.get("STATUS.md"),
        "blocker": artifacts.get("BLOCKER.md"),
    }
    if include_logs:
        summary["logs"] = {
            "bootstrap": artifacts.get("bootstrap.log"),
            "codex": artifacts.get("codex.log"),
            "finish": artifacts.get("finish.log"),
        }
    return summary


if __name__ == "__main__":
    raise SystemExit(main())
