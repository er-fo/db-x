from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import tomllib


DEFAULT_CONFIG_PATH = Path.home() / ".config" / "db-x" / "config.toml"


@dataclass(frozen=True)
class AppConfig:
    aws_profile: str | None
    aws_region: str
    default_owner: str
    default_base_branch: str
    ami_id: str
    subnet_id: str
    security_group_id: str
    instance_type: str
    ssh_user: str
    tailscale_domain: str | None
    repo_root: str
    job_prefix: str

    @property
    def ssh_target_suffix(self) -> str:
        return self.tailscale_domain or ""


def resolve_config_path(explicit_path: str | None = None) -> Path:
    if explicit_path:
        return Path(explicit_path).expanduser()
    env_path = os.environ.get("DBX_CONFIG_PATH")
    if env_path:
        return Path(env_path).expanduser()
    return DEFAULT_CONFIG_PATH


def load_config(explicit_path: str | None = None) -> AppConfig:
    path = resolve_config_path(explicit_path)
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found at {path}. Create it before using db-x."
        )

    with path.open("rb") as handle:
        data = tomllib.load(handle)

    return AppConfig(
        aws_profile=_optional_str(data.get("aws_profile")),
        aws_region=_required_str(data, "aws_region"),
        default_owner=_required_str(data, "default_owner"),
        default_base_branch=_required_str(data, "default_base_branch"),
        ami_id=_required_str(data, "ami_id"),
        subnet_id=_required_str(data, "subnet_id"),
        security_group_id=_required_str(data, "security_group_id"),
        instance_type=_required_str(data, "instance_type"),
        ssh_user=_required_str(data, "ssh_user"),
        tailscale_domain=_optional_str(data.get("tailscale_domain")),
        repo_root=_required_str(data, "repo_root"),
        job_prefix=_required_str(data, "job_prefix"),
    )


def _required_str(data: dict[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Config key '{key}' must be a non-empty string.")
    return value.strip()


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Optional config values must be strings when provided.")
    cleaned = value.strip()
    return cleaned or None
