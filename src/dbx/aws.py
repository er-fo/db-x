from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import shlex
import subprocess
import tempfile
import time

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
    resume_session_id: str | None = None


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
    status_json = f"{job_root}/status.json"
    blocker_file = f"{job_root}/BLOCKER.md"
    prompt_file = f"{job_root}/BOOTSTRAP_PROMPT.txt"
    log_dir = f"{job_root}/logs"
    bootstrap_log = f"{log_dir}/bootstrap.log"
    log_file = f"{log_dir}/codex.log"
    finish_log = f"{log_dir}/finish.log"
    codex_command = _build_codex_command(request)

    script = [
        "#!/bin/bash",
        "set -euo pipefail",
        f"JOB_ROOT={shlex.quote(job_root)}",
        f"REPO_DIR={shlex.quote(repo_dir)}",
        f"MISSION_FILE={shlex.quote(mission_file)}",
        f"STATUS_FILE={shlex.quote(status_file)}",
        f"STATUS_JSON={shlex.quote(status_json)}",
        f"BLOCKER_FILE={shlex.quote(blocker_file)}",
        f"PROMPT_FILE={shlex.quote(prompt_file)}",
        f"LOG_DIR={shlex.quote(log_dir)}",
        f"BOOTSTRAP_LOG={shlex.quote(bootstrap_log)}",
        f"LOG_FILE={shlex.quote(log_file)}",
        f"FINISH_LOG={shlex.quote(finish_log)}",
        f"SESSION_NAME={shlex.quote(request.session_name)}",
        f"REPO_CLONE_URL={shlex.quote(repo_clone_url)}",
        f"BRANCH_NAME={shlex.quote(request.branch_name)}",
        f"BASE_BRANCH={shlex.quote(config.default_base_branch)}",
        f"TAILSCALE_AUTH_KEY={shlex.quote(config.tailscale_auth_key or '')}",
        "DBX_USER=ubuntu",
        "mkdir -p \"$JOB_ROOT\" \"$LOG_DIR\"",
        "touch \"$FINISH_LOG\"",
        "chown -R \"$DBX_USER\":\"$DBX_USER\" \"$JOB_ROOT\"",
        "exec > >(tee -a \"$BOOTSTRAP_LOG\") 2>&1",
        "write_status() {",
        "  local phase=\"$1\"",
        "  local state=\"$2\"",
        "  local detail=\"${3:-}\"",
        "  python3 - \"$STATUS_JSON\" \"$STATUS_FILE\" \"$SESSION_NAME\" \"$phase\" \"$state\" \"$detail\" <<'PY'",
        "import json",
        "import sys",
        "from pathlib import Path",
        "",
        "status_json_path, status_file_path, session_name, phase, state, detail = sys.argv[1:]",
        "payload = {",
        "    'job_name': session_name,",
        "    'phase': phase,",
        "    'state': state,",
        "    'repo': " + repr(request.repo) + ",",
        "    'branch_name': " + repr(request.branch_name) + ",",
        "    'session_name': " + repr(request.session_name) + ",",
        "    'detail': detail,",
        "}",
        "Path(status_json_path).write_text(json.dumps(payload, indent=2) + '\\n', encoding='utf-8')",
        "Path(status_file_path).write_text(",
        "    '\\n'.join([",
        "        '# " + request.job_name + "',",
        "        '',",
        "        '- Repo: `" + request.repo + "`',",
        "        '- Branch: `" + request.branch_name + "`',",
        "        '- Session: `" + request.session_name + "`',",
        "        f'- Phase: {phase}',",
        "        f'- State: {state}',",
        "        f'- Detail: {detail}',",
        "        '',",
        "    ]),",
        "    encoding='utf-8',",
        ")",
        "PY",
        "}",
        "on_error() {",
        "  local exit_code=$?",
        "  write_status \"bootstrap\" \"failed\" \"bootstrap_failed_exit_${exit_code}\"",
        "  if [ ! -f \"$BLOCKER_FILE\" ]; then",
        "    cat >\"$BLOCKER_FILE\" <<EOF",
        "# Bootstrap blocker",
        "",
        "Cloud-init failed before the runtime reached a healthy state.",
        "Check logs/bootstrap.log and the EC2 console output for details.",
        "EOF",
        "  fi",
        "  exit \"$exit_code\"",
        "}",
        "trap 'on_error' ERR",
        "write_status \"bootstrap\" \"starting\" \"initializing job root\"",
        "cat >\"$MISSION_FILE\" <<'" + mission_marker + "'",
        mission_text,
        mission_marker,
        "cat >\"$PROMPT_FILE\" <<'" + prompt_marker + "'",
        bootstrap_prompt,
        prompt_marker,
        "chown \"$DBX_USER\":\"$DBX_USER\" \"$MISSION_FILE\" \"$PROMPT_FILE\"",
        "write_status \"bootstrap\" \"running\" \"cloning repository\"",
        "sudo -u \"$DBX_USER\" -H env GH_PROMPT_DISABLED=1 gh auth status >/dev/null",
        "sudo -u \"$DBX_USER\" -H env GH_PROMPT_DISABLED=1 gh auth setup-git >/dev/null",
        "if [ ! -d \"$REPO_DIR/.git\" ]; then",
        "  sudo -u \"$DBX_USER\" -H git clone \"$REPO_CLONE_URL\" \"$REPO_DIR\"",
        "fi",
        "sudo -u \"$DBX_USER\" -H env REPO_DIR=\"$REPO_DIR\" BRANCH_NAME=\"$BRANCH_NAME\" BASE_BRANCH=\"$BASE_BRANCH\" MISSION_FILE=\"$MISSION_FILE\" bash -lc 'cd \"$REPO_DIR\" && git fetch origin --prune && git checkout -B \"$BRANCH_NAME\" \"origin/$BASE_BRANCH\" && cp \"$MISSION_FILE\" \"$REPO_DIR/AGENT_MISSION.md\"'",
        "if [ -n \"$TAILSCALE_AUTH_KEY\" ]; then",
        "  write_status \"bootstrap\" \"running\" \"connecting tailscale\"",
        "  systemctl start tailscaled",
        "  tailscale up --ssh --hostname \"$SESSION_NAME\" --auth-key \"$TAILSCALE_AUTH_KEY\"",
        "fi",
        "write_status \"bootstrap\" \"running\" \"starting tmux codex session\"",
        "sudo -u \"$DBX_USER\" -H tmux new-session -d -s \"$SESSION_NAME\" "
        f"\"cd '$REPO_DIR' && {codex_command} "
        "2>&1 | tee '$LOG_FILE'; exec bash\"",
        "write_status \"runtime\" \"ready\" \"tmux session started\"",
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


def describe_instance_status(config: AppConfig, instance_id: str) -> dict[str, object] | None:
    result = run_aws_cli(
        config,
        [
            "ec2",
            "describe-instance-status",
            "--include-all-instances",
            "--instance-ids",
            instance_id,
            "--output",
            "json",
        ],
    )
    payload = json.loads(result.stdout)
    statuses = payload.get("InstanceStatuses", [])
    if not statuses:
        return None
    return statuses[0]


def get_console_output(config: AppConfig, instance_id: str) -> str:
    result = run_aws_cli(
        config,
        [
            "ec2",
            "get-console-output",
            "--latest",
            "--instance-id",
            instance_id,
            "--output",
            "json",
        ],
    )
    payload = json.loads(result.stdout)
    output = payload.get("Output")
    return output if isinstance(output, str) else ""


def wait_for_instance_terminated(
    config: AppConfig,
    instance_id: str,
    *,
    timeout_seconds: int = 600,
    poll_interval_seconds: int = 5,
) -> dict[str, object]:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        instance = describe_instance(config, instance_id)
        state = ((instance.get("State") or {}).get("Name"))
        if state == "terminated":
            return instance
        time.sleep(poll_interval_seconds)
    raise AwsCliError(f"Timed out waiting for instance {instance_id} to terminate.")


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
            "- update STATUS.md and status.json with the blocker and current state",
            "- write a concise summary into BLOCKER.md",
            "- leave the branch in a resumable state",
            "- stop and wait in tmux",
        ]
    )


def _build_codex_command(request: JobLaunchRequest) -> str:
    prompt_expr = '\\"\\$(cat \\"$PROMPT_FILE\\")\\"'
    if request.resume_session_id:
        session_id = shlex.quote(request.resume_session_id)
        return f"codex --no-alt-screen resume {session_id} {prompt_expr}"
    return f"codex --no-alt-screen {prompt_expr}"
