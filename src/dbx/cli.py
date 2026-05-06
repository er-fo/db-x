from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
import json
import re
import shlex
import shutil
import subprocess
import sys
import time

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
    load_job_state,
    save_job_artifact,
    save_job_state,
)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "doctor":
        return run_doctor(args.config)
    if args.command == "init-config":
        return run_init_config(args.config, args.force)

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
                wait=args.wait,
                timeout_seconds=args.timeout,
            )
        if args.command == "list":
            return run_list(config)
        if args.command == "status":
            return run_status(config, args.job_id, include_logs=args.logs)
        if args.command == "attach":
            return run_attach(config, args.job_id, check=args.check)
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

    start_parser = subparsers.add_parser("start", help="Launch a new job instance.")
    start_parser.add_argument("repo", help="GitHub repo in owner/name format.")
    start_parser.add_argument("mission", help="Path to a mission markdown file.")
    start_parser.add_argument(
        "--resume-session",
        help="Resume an existing Codex session UUID instead of starting a fresh one.",
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


def run_start(
    config: AppConfig,
    repo: str,
    mission: str,
    *,
    resume_session_id: str | None = None,
    wait: bool = True,
    timeout_seconds: int = 600,
) -> int:
    mission_path = Path(mission).expanduser().resolve()
    if not mission_path.exists():
        print(f"Mission file not found: {mission_path}", file=sys.stderr)
        return 2
    if "/" not in repo:
        repo = f"{config.default_owner}/{repo}"

    slug = _slugify(mission_path.stem)
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    job_name = f"{config.job_prefix}-{slug}-{timestamp}"
    branch_name = f"agent/{slug}-{timestamp}"
    session_name = job_name
    job_root = _job_root(config, job_name)
    resume_session_file = None
    resume_session_relative_path = None
    if resume_session_id:
        resume_session_file = _find_codex_session_file(resume_session_id)
        if resume_session_file is None:
            print(f"Codex session not found locally: {resume_session_id}", file=sys.stderr)
            return 2
        resume_session_relative_path = _codex_session_relative_path(resume_session_file)

    request = JobLaunchRequest(
        repo=repo,
        mission_path=mission_path,
        job_name=job_name,
        branch_name=branch_name,
        session_name=session_name,
        resume_session_id=resume_session_id,
        resume_session_relative_path=resume_session_relative_path,
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
        lifecycle_state="launching",
        status="starting",
    )
    save_job_state(job_state)
    resume_session_upload = None
    if resume_session_file and resume_session_relative_path:
        resume_session_upload = _upload_resume_session_when_reachable(
            config,
            job_state,
            resume_session_file,
            resume_session_relative_path,
            timeout_seconds=timeout_seconds,
        )

    runtime = None
    if wait:
        runtime = _wait_for_runtime_ready(config, job_state, timeout_seconds=timeout_seconds)
        job_state = replace(
            job_state,
            lifecycle_state="running",
            status=str(runtime.get("state", "ready")),
            last_error=None,
        )
        save_job_state(job_state)

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
    print(json.dumps(output, indent=2))
    return 0


def run_list(config: AppConfig) -> int:
    instances = list_instances(config)
    simplified = []
    for instance in instances:
        instance_id = instance.get("InstanceId")
        local_state = (
            load_job_state(instance_id) if isinstance(instance_id, str) and instance_id else None
        )
        simplified.append(
            {
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
        )
    print(json.dumps(simplified, indent=2, default=str))
    return 0


def _find_codex_session_file(session_id: str) -> Path | None:
    root = Path.home() / ".codex" / "sessions"
    if not root.exists():
        return None
    matches = list(root.rglob(f"*{session_id}.jsonl"))
    if not matches:
        return None
    return max(matches, key=lambda path: path.stat().st_mtime)


def _codex_session_relative_path(session_file: Path) -> str:
    root = Path.home() / ".codex" / "sessions"
    return session_file.expanduser().resolve().relative_to(root.expanduser().resolve()).as_posix()


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
    last_problem = "waiting for instance to run"

    while time.time() < deadline:
        instance = describe_instance(config, job_state.instance_id)
        instance_state = ((instance.get("State") or {}).get("Name"))
        if instance_state != "running":
            last_problem = f"instance state is {instance_state}"
            time.sleep(poll_interval_seconds)
            continue
        try:
            target = build_ssh_target(config, instance)
            upload_remote_text(target, remote_path, content)
            return {"remote_path": remote_path, "uploaded": True}
        except (AwsCliError, RemoteCommandError) as exc:
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
    print(json.dumps(summary, indent=2, default=str))
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

    finish_result = _run_finish_remote(
        target,
        job_state,
        config.default_base_branch,
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

    print(json.dumps(output, indent=2, default=str))
    return 0


def run_terminate(
    config: AppConfig,
    job_id: str,
    *,
    wait: bool = True,
    force: bool = False,
) -> int:
    payload = _terminate_job(config, job_id, wait=wait, force=force)
    print(json.dumps(payload, indent=2, default=str))
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

    while time.time() < deadline:
        instance = describe_instance(config, job_state.instance_id)
        instance_state = ((instance.get("State") or {}).get("Name"))
        if instance_state != "running":
            last_problem = f"instance state is {instance_state}"
            time.sleep(poll_interval_seconds)
            continue

        status = describe_instance_status(config, job_state.instance_id) or {}
        system_ok = ((status.get("SystemStatus") or {}).get("Status")) == "ok"
        instance_ok = ((status.get("InstanceStatus") or {}).get("Status")) == "ok"
        if not (system_ok and instance_ok):
            last_problem = "EC2 status checks are not yet passing"
            time.sleep(poll_interval_seconds)
            continue

        target = build_ssh_target(config, instance)
        try:
            if not _remote_file_exists(target, "/var/lib/cloud/instance/boot-finished"):
                last_problem = "cloud-init has not finished"
                time.sleep(poll_interval_seconds)
                continue

            remote_status = _read_remote_json(target, _remote_paths(job_state)["status_json"])
            if str(remote_status.get("state")) != "ready":
                last_problem = f"remote runtime state is {remote_status.get('state')}"
                time.sleep(poll_interval_seconds)
                continue

            if not _remote_tmux_session_exists(target, job_state.session_name):
                last_problem = "tmux session is not ready"
                time.sleep(poll_interval_seconds)
                continue

            if not _remote_file_exists(target, _remote_paths(job_state)["codex_log"]):
                last_problem = "codex log has not been created"
                time.sleep(poll_interval_seconds)
                continue

            return {
                "phase": remote_status.get("phase", "runtime"),
                "state": remote_status.get("state", "ready"),
                "ssh_target": target,
                "cloud_init_complete": True,
                "tmux_session": job_state.session_name,
                "codex_log_ready": True,
                "system_status": (status.get("SystemStatus") or {}).get("Status"),
                "instance_status": (status.get("InstanceStatus") or {}).get("Status"),
            }
        except RemoteCommandError as exc:
            last_problem = str(exc)
            time.sleep(poll_interval_seconds)

    console_output = _console_output_excerpt(config, job_state.instance_id)
    raise AwsCliError(
        f"Timed out waiting for runtime verification on {job_state.instance_id}. "
        f"Last problem: {last_problem}. Console output: {console_output}"
    )


def _build_status_summary(
    config: AppConfig,
    job_id: str,
    *,
    include_logs: bool = False,
) -> dict[str, object]:
    instance = describe_instance(config, job_id)
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
) -> dict[str, object]:
    instance = describe_instance(config, job_id)
    local_state = _load_or_infer_job_state(config, instance, job_id)

    if not force:
        try:
            target = build_ssh_target(config, instance)
            artifacts = _capture_remote_artifacts(target, local_state, include_logs=True)
            _persist_remote_artifacts(job_id, artifacts)
        except (AwsCliError, RemoteCommandError):
            console_output = _console_output_excerpt(config, job_id)
            if console_output:
                save_job_artifact(job_id, "console-output.txt", console_output + "\n")

    payload = terminate_instance(config, job_id)
    if wait:
        final_instance = wait_for_instance_terminated(config, job_id)
        payload["final_state"] = ((final_instance.get("State") or {}).get("Name"))

    updated_state = replace(
        local_state,
        lifecycle_state="terminated",
        status="terminated",
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
    artifacts: dict[str, object] = {
        "status_json": _read_remote_json(target, paths["status_json"]),
        "status_md": _read_remote_text(target, paths["status_md"]),
        "blocker": _read_remote_text(target, paths["blocker"], optional=True),
    }
    if include_logs:
        artifacts["bootstrap_log"] = _tail_remote_text(target, paths["bootstrap_log"])
        artifacts["codex_log"] = _tail_remote_text(target, paths["codex_log"])
        artifacts["finish_log"] = _tail_remote_text(target, paths["finish_log"], optional=True)
    return artifacts


def _persist_remote_artifacts(job_id: str, artifacts: dict[str, object]) -> None:
    if "status_json" in artifacts and artifacts["status_json"] is not None:
        save_job_artifact(
            job_id,
            "status.json",
            json.dumps(artifacts["status_json"], indent=2) + "\n",
        )
    if "status_md" in artifacts and isinstance(artifacts["status_md"], str):
        save_job_artifact(job_id, "STATUS.md", artifacts["status_md"])
    if "blocker" in artifacts and isinstance(artifacts["blocker"], str):
        save_job_artifact(job_id, "BLOCKER.md", artifacts["blocker"])
    if "bootstrap_log" in artifacts and isinstance(artifacts["bootstrap_log"], str):
        save_job_artifact(job_id, "bootstrap.log", artifacts["bootstrap_log"])
    if "codex_log" in artifacts and isinstance(artifacts["codex_log"], str):
        save_job_artifact(job_id, "codex.log", artifacts["codex_log"])
    if "finish_log" in artifacts and isinstance(artifacts["finish_log"], str):
        save_job_artifact(job_id, "finish.log", artifacts["finish_log"])


def _run_finish_remote(
    target: str,
    job_state: JobState,
    base_branch: str,
    *,
    no_pr: bool,
    blocked: bool,
) -> dict[str, object]:
    paths = _remote_paths(job_state)
    commit_message = _finish_commit_message(job_state.job_name)
    pr_title_prefix = "BLOCKED: " if blocked else ""
    pr_draft_line = "gh pr create --draft" if blocked else "gh pr create"
    script = "\n".join(
        [
            "set -euo pipefail",
            f"JOB_ROOT={shlex.quote(job_state.job_root)}",
            f"REPO_DIR={shlex.quote(paths['repo_dir'])}",
            f"STATUS_FILE={shlex.quote(paths['status_md'])}",
            f"STATUS_JSON={shlex.quote(paths['status_json'])}",
            f"BLOCKER_FILE={shlex.quote(paths['blocker'])}",
            f"FINISH_LOG={shlex.quote(paths['finish_log'])}",
            f"BRANCH_NAME={shlex.quote(job_state.branch_name)}",
            f"BASE_BRANCH={shlex.quote(base_branch)}",
            f"NO_PR={'1' if no_pr else '0'}",
            "PR_BODY_FILE=\"$JOB_ROOT/PR_BODY.md\"",
            "mkdir -p \"$(dirname \"$FINISH_LOG\")\"",
            "exec > >(tee -a \"$FINISH_LOG\") 2>&1",
            "cd \"$REPO_DIR\"",
            "if [ -n \"$(git status --porcelain)\" ]; then",
            "  git add -A",
            f"  git commit -m {shlex.quote(commit_message)}",
            "fi",
            "git push -u origin \"$BRANCH_NAME\"",
            "TITLE=\"" + pr_title_prefix + "$(git log -1 --pretty=%s)\"",
            "cat >\"$PR_BODY_FILE\" <<'EOF'",
            "## dbx finish",
            "",
            f"- Job: `{job_state.job_name}`",
            f"- Branch: `{job_state.branch_name}`",
            "",
            "## Status",
            "EOF",
            "cat \"$STATUS_FILE\" >> \"$PR_BODY_FILE\"",
            "if [ -f \"$BLOCKER_FILE\" ]; then",
            "  printf '\\n## Blocker\\n\\n' >> \"$PR_BODY_FILE\"",
            "  cat \"$BLOCKER_FILE\" >> \"$PR_BODY_FILE\"",
            "fi",
            "if [ \"$NO_PR\" = \"0\" ]; then",
            "  EXISTING_PR_URL=\"$(gh pr view \"$BRANCH_NAME\" --json url --jq '.url' 2>/dev/null || true)\"",
            "  if [ -n \"$EXISTING_PR_URL\" ]; then",
            "    gh pr edit \"$BRANCH_NAME\" --title \"$TITLE\" --body-file \"$PR_BODY_FILE\"",
            "  else",
            f"    {pr_draft_line} --base \"$BASE_BRANCH\" --head \"$BRANCH_NAME\" --title \"$TITLE\" --body-file \"$PR_BODY_FILE\"",
            "  fi",
            "fi",
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
        lifecycle_state="discovered",
        status=str(((instance.get("State") or {}).get("Name")) or "unknown"),
    )


def _remote_paths(job_state: JobState) -> dict[str, str]:
    job_root = job_state.job_root
    return {
        "repo_dir": f"{job_root}/repo",
        "status_json": f"{job_root}/status.json",
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
    return str(status_json.get("state")) == "blocked"


def _console_output_excerpt(config: AppConfig, job_id: str, limit: int = 400) -> str:
    output = get_console_output(config, job_id).strip()
    if not output:
        return ""
    compact = " ".join(output.split())
    return compact[:limit]


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


if __name__ == "__main__":
    raise SystemExit(main())
