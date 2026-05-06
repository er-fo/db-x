from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import tomllib


DEFAULT_CONFIG_PATH = Path.home() / ".config" / "db-x" / "config.toml"
DEFAULT_CONFIG_TEMPLATE = """aws_profile = "personal"
aws_region = "eu-north-1"
default_owner = "er-fo"
default_base_branch = "main"
ami_id = "ami-xxxxxxxxxxxxxxxxx"
subnet_id = "subnet-xxxxxxxxxxxxxxxxx"
security_group_id = "sg-xxxxxxxxxxxxxxxxx"
instance_type = "c7i.xlarge"
ssh_user = "ubuntu"
tailscale_domain = "tail12345.ts.net"
tailscale_auth_key = "tskey-auth-xxxxxxxx"
tailscale_tags = ["tag:dbx"]
repo_root = "/home/ubuntu/work"
job_prefix = "dbx"
"""


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
    tailscale_auth_key: str | None
    tailscale_tags: tuple[str, ...]
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
        tailscale_auth_key=_optional_str(
            os.environ.get("DBX_TAILSCALE_AUTH_KEY", data.get("tailscale_auth_key"))
        ),
        tailscale_tags=_optional_str_list(data.get("tailscale_tags")),
        repo_root=_required_str(data, "repo_root"),
        job_prefix=_required_str(data, "job_prefix"),
    )


def write_default_config(path: Path, *, force: bool = False) -> None:
    target = path.expanduser()
    if target.exists() and not force:
        raise FileExistsError(f"Refusing to overwrite existing config at {target}.")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")


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


def _optional_str_list(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("Optional config list values must be arrays of strings when provided.")
    cleaned: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("Optional config list values must contain only strings.")
        stripped = item.strip()
        if not stripped:
            raise ValueError("Optional config list values cannot contain empty strings.")
        cleaned.append(stripped)
    return tuple(cleaned)
