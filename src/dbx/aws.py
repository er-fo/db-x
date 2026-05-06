from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
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
    payload = {
        "repo": request.repo,
        "branch_name": request.branch_name,
        "job_name": request.job_name,
        "session_name": request.session_name,
        "repo_root": config.repo_root,
        "mission": mission_text,
    }
    script = [
        "#!/bin/bash",
        "set -euo pipefail",
        f"cat >/tmp/dbx-job.json <<'JSON'",
        json.dumps(payload, indent=2),
        "JSON",
        "echo 'dbx bootstrap payload written to /tmp/dbx-job.json'",
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
