from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path
import json
import re
import shlex
import shutil
import subprocess
import sys

from .aws import (
    AwsCliError,
    JobLaunchRequest,
    build_ssh_target,
    describe_instance,
    launch_instance,
    list_instances,
    terminate_instance,
)
from .config import DEFAULT_CONFIG_PATH, AppConfig, load_config
from .ssh import build_attach_command


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "doctor":
        return run_doctor(args.config)

    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        if args.command == "start":
            return run_start(config, args.repo, args.mission)
        if args.command == "list":
            return run_list(config)
        if args.command == "status":
            return run_status(config, args.job_id)
        if args.command == "attach":
            return run_attach(config, args.job_id)
        if args.command == "terminate":
            return run_terminate(config, args.job_id)
    except AwsCliError as exc:
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

    start_parser = subparsers.add_parser("start", help="Launch a new job instance.")
    start_parser.add_argument("repo", help="GitHub repo in owner/name format.")
    start_parser.add_argument("mission", help="Path to a mission markdown file.")

    subparsers.add_parser("list", help="List dbx-managed instances.")

    status_parser = subparsers.add_parser("status", help="Show one job instance.")
    status_parser.add_argument("job_id", help="AWS instance ID.")

    attach_parser = subparsers.add_parser("attach", help="Print the tmux attach command.")
    attach_parser.add_argument("job_id", help="AWS instance ID.")

    terminate_parser = subparsers.add_parser(
        "terminate", help="Terminate a running job instance."
    )
    terminate_parser.add_argument("job_id", help="AWS instance ID.")

    return parser


def run_doctor(config_path: str | None) -> int:
    missing = [tool for tool in ["aws", "gh", "git", "ssh", "tmux", "python3"] if shutil.which(tool) is None]
    report = {
        "config_path": str(Path(config_path).expanduser()) if config_path else str(DEFAULT_CONFIG_PATH),
        "tools": {
            tool: shutil.which(tool) is not None
            for tool in ["aws", "gh", "git", "ssh", "tmux", "python3", "tailscale", "codex"]
        },
    }
    print(json.dumps(report, indent=2))
    return 0 if not missing else 1


def run_start(config: AppConfig, repo: str, mission: str) -> int:
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

    request = JobLaunchRequest(
        repo=repo,
        mission_path=mission_path,
        job_name=job_name,
        branch_name=branch_name,
        session_name=session_name,
    )
    payload = launch_instance(config, request)
    instances = payload.get("Instances", [])
    instance_id = instances[0]["InstanceId"] if instances else "<unknown>"
    output = {
        "job_name": job_name,
        "branch_name": branch_name,
        "session_name": session_name,
        "instance_id": instance_id,
    }
    print(json.dumps(output, indent=2))
    return 0


def run_list(config: AppConfig) -> int:
    instances = list_instances(config)
    simplified = []
    for instance in instances:
        simplified.append(
            {
                "instance_id": instance.get("InstanceId"),
                "state": ((instance.get("State") or {}).get("Name")),
                "name": _find_name(instance),
                "launch_time": instance.get("LaunchTime"),
            }
        )
    print(json.dumps(simplified, indent=2, default=str))
    return 0


def run_status(config: AppConfig, job_id: str) -> int:
    instance = describe_instance(config, job_id)
    summary = {
        "instance_id": instance.get("InstanceId"),
        "name": _find_name(instance),
        "state": ((instance.get("State") or {}).get("Name")),
        "launch_time": instance.get("LaunchTime"),
        "private_dns_name": instance.get("PrivateDnsName"),
        "private_ip": instance.get("PrivateIpAddress"),
    }
    print(json.dumps(summary, indent=2, default=str))
    return 0


def run_attach(config: AppConfig, job_id: str) -> int:
    instance = describe_instance(config, job_id)
    session_name = _find_name(instance)
    if not session_name:
        raise AwsCliError("Could not determine tmux session name from instance tags.")
    target = build_ssh_target(config, instance)
    command = build_attach_command(target, session_name)
    print(" ".join(shlex_quote(part) for part in command))
    return 0


def run_terminate(config: AppConfig, job_id: str) -> int:
    payload = terminate_instance(config, job_id)
    print(json.dumps(payload, indent=2))
    return 0


def _find_name(instance: dict[str, object]) -> str | None:
    tags = instance.get("Tags", [])
    if not isinstance(tags, list):
        return None
    for tag in tags:
        if isinstance(tag, dict) and tag.get("Key") == "Name":
            value = tag.get("Value")
            if isinstance(value, str) and value:
                return value
    return None


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "mission"


def shlex_quote(value: str) -> str:
    return subprocess.list2cmdline([value]) if sys.platform == "win32" else shlex.quote(value)


if __name__ == "__main__":
    raise SystemExit(main())
