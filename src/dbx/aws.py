from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import shlex
import subprocess
import tempfile

from .config import AppConfig


class AwsCliError(RuntimeError):
    pass


@dataclass(frozen=True)
class JobLaunchRequest:
    repo: str
    mission_path: Path
    job_name: str
    branch_name: str
    session_name: str


def run_aws_cli(
    config: AppConfig,
    args: list[str],
    *,
    check: bool = True,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    command = ["aws"]
    if config.aws_profile:
        command.extend(["--profile", config.aws_profile])
    command.extend(["--region", config.aws_region])
    command.extend(args)

    result = subprocess.run(
        command,
        check=False,
        capture_output=capture_output,
        text=True,
    )
    if check and result.returncode != 0:
        raise AwsCliError(result.stderr.strip() or "AWS CLI command failed.")
    return result


def build_user_data(config: AppConfig, request: JobLaunchRequest) -> str:
    mission_text = request.mission_path.read_text(encoding="utf-8")
    mission_marker = "DBX_MISSION_EOF"
    prompt_marker = "DBX_PROMPT_EOF"
    bootstrap_prompt = _build_bootstrap_prompt()
    repo_clone_url = f"https://github.com/{request.repo}.git"
    job_root = f"{config.repo_root.rstrip('/')}/{request.job_name}"
    repo_dir = f"{job_root}/repo"
    mission_file = f"{job_root}/AGENT_MISSION.md"
    status_file = f"{job_root}/STATUS.md"
    prompt_file = f"{job_root}/BOOTSTRAP_PROMPT.txt"
    log_file = f"{job_root}/codex.log"

    script = [
        "#!/bin/bash",
        "set -euo pipefail",
        f"JOB_ROOT={shlex.quote(job_root)}",
        f"REPO_DIR={shlex.quote(repo_dir)}",
        f"MISSION_FILE={shlex.quote(mission_file)}",
        f"STATUS_FILE={shlex.quote(status_file)}",
        f"PROMPT_FILE={shlex.quote(prompt_file)}",
        f"LOG_FILE={shlex.quote(log_file)}",
        f"SESSION_NAME={shlex.quote(request.session_name)}",
        f"REPO_CLONE_URL={shlex.quote(repo_clone_url)}",
        f"BRANCH_NAME={shlex.quote(request.branch_name)}",
        f"BASE_BRANCH={shlex.quote(config.default_base_branch)}",
        "mkdir -p \"$JOB_ROOT\"",
        "cat >\"$MISSION_FILE\" <<'" + mission_marker + "'",
        mission_text,
        mission_marker,
        "cat >\"$PROMPT_FILE\" <<'" + prompt_marker + "'",
        bootstrap_prompt,
        prompt_marker,
        "cat >\"$STATUS_FILE\" <<'STATUS_EOF'",
        f"# {request.job_name}",
        "",
        f"- Repo: `{request.repo}`",
        f"- Branch: `{request.branch_name}`",
        f"- Session: `{request.session_name}`",
        "- State: bootstrapping",
        "",
        "STATUS_EOF",
        "if [ ! -d \"$REPO_DIR/.git\" ]; then",
        "  git clone \"$REPO_CLONE_URL\" \"$REPO_DIR\"",
        "fi",
        "cd \"$REPO_DIR\"",
        "git fetch origin --prune",
        "git checkout -B \"$BRANCH_NAME\" \"origin/$BASE_BRANCH\"",
        "cp \"$MISSION_FILE\" \"$REPO_DIR/AGENT_MISSION.md\"",
        "tmux new-session -d -s \"$SESSION_NAME\" "
        "\"cd '$REPO_DIR' && codex --no-alt-screen \\\"\\$(cat '$PROMPT_FILE')\\\" "
        "2>&1 | tee '$LOG_FILE'; exec bash\"",
        "printf '%s\\n' 'State: running' > /tmp/dbx-status.tmp",
        "cat \"$STATUS_FILE\" | sed 's/State: bootstrapping/State: running/' > /tmp/dbx-status.tmp",
        "mv /tmp/dbx-status.tmp \"$STATUS_FILE\"",
    ]
    return "\n".join(script)


def launch_instance(config: AppConfig, request: JobLaunchRequest) -> dict[str, object]:
    user_data = build_user_data(config, request)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".sh", delete=False
    ) as handle:
        handle.write(user_data)
        user_data_path = handle.name

    try:
        command = [
            "ec2",
            "run-instances",
            "--image-id",
            config.ami_id,
            "--instance-type",
            config.instance_type,
            "--subnet-id",
            config.subnet_id,
            "--security-group-ids",
            config.security_group_id,
            "--instance-initiated-shutdown-behavior",
            "terminate",
            "--tag-specifications",
            (
                "ResourceType=instance,Tags=["
                f"{{Key=Name,Value={request.job_name}}},"
                "{Key=ManagedBy,Value=dbx},"
                f"{{Key=Repo,Value={request.repo}}}"
                "]"
            ),
            "--user-data",
            f"file://{user_data_path}",
            "--output",
            "json",
        ]
        result = run_aws_cli(config, command)
        return json.loads(result.stdout)
    finally:
        Path(user_data_path).unlink(missing_ok=True)


def list_instances(config: AppConfig) -> list[dict[str, object]]:
    result = run_aws_cli(
        config,
        [
            "ec2",
            "describe-instances",
            "--filters",
            "Name=tag:ManagedBy,Values=dbx",
            "Name=instance-state-name,Values=pending,running,stopping,stopped",
            "--output",
            "json",
        ],
    )
    payload = json.loads(result.stdout)
    reservations = payload.get("Reservations", [])
    instances: list[dict[str, object]] = []
    for reservation in reservations:
        instances.extend(reservation.get("Instances", []))
    return instances


def describe_instance(config: AppConfig, instance_id: str) -> dict[str, object]:
    result = run_aws_cli(
        config,
        [
            "ec2",
            "describe-instances",
            "--instance-ids",
            instance_id,
            "--output",
            "json",
        ],
    )
    payload = json.loads(result.stdout)
    reservations = payload.get("Reservations", [])
    if not reservations or not reservations[0].get("Instances"):
        raise AwsCliError(f"Instance {instance_id} was not found.")
    return reservations[0]["Instances"][0]


def terminate_instance(config: AppConfig, instance_id: str) -> dict[str, object]:
    result = run_aws_cli(
        config,
        [
            "ec2",
            "terminate-instances",
            "--instance-ids",
            instance_id,
            "--output",
            "json",
        ],
    )
    return json.loads(result.stdout)


def build_ssh_target(config: AppConfig, instance: dict[str, object]) -> str:
    hostname = _find_tag(instance, "Name") or instance.get("PrivateDnsName")
    if not isinstance(hostname, str) or not hostname:
        raise AwsCliError("Could not determine an SSH hostname for the instance.")
    if config.tailscale_domain and "." not in hostname:
        hostname = f"{hostname}.{config.tailscale_domain}"
    return f"{config.ssh_user}@{hostname}"


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


def _build_bootstrap_prompt() -> str:
    return "\n".join(
        [
            "Read AGENT_MISSION.md before doing anything else.",
            "",
            "Use Superpowers for this implementation. Work like a long-running implementation agent, not a one-shot task runner.",
            "",
            "Start by:",
            "1. Inspecting the repository and mission.",
            "2. Writing a brief plan into STATUS.md or the nearest durable project artifact.",
            "3. Implementing in coherent checkpoints.",
            "4. Verifying changes before concluding a checkpoint.",
            "",
            "If you are blocked by missing product intent, credentials, external access, or repeated verification failure:",
            "- update STATUS.md with the blocker and current state",
            "- leave the branch in a resumable state",
            "- stop and wait in tmux",
        ]
    )
