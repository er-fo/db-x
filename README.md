# db-x

`db-x` is a disposable AWS devbox runner for long-running Codex + Superpowers work.

It launches a fresh EC2 instance per mission, boots it from a private golden AMI,
runs Codex inside `tmux`, and then terminates the machine when the work is done
or blocked. The goal is simple: keep the autonomy and long-running ergonomics of
a real devbox without leaving compute running between jobs.

## What it does

- Starts a new EC2 job instance from a prebuilt AMI
- Injects a mission file as cloud-init user data
- Uses Tailscale + SSH for private access
- Keeps Codex running in `tmux`
- Provides local commands to launch, inspect, attach, and terminate jobs

## What it does not do yet

- Build the golden AMI for you
- Install Superpowers for you
- Manage Brevo/email alerts
- Upload logs to S3

## Command surface

```bash
dbx doctor
dbx start er-fo/db-x missions/bootstrap.md
dbx list
dbx status i-0123456789abcdef0
dbx attach i-0123456789abcdef0
dbx terminate i-0123456789abcdef0
```

## Config

`db-x` reads configuration from `~/.config/db-x/config.toml` by default.

Example:

```toml
aws_profile = "personal"
aws_region = "eu-north-1"
default_owner = "er-fo"
default_base_branch = "main"
ami_id = "ami-xxxxxxxxxxxxxxxxx"
subnet_id = "subnet-xxxxxxxxxxxxxxxxx"
security_group_id = "sg-xxxxxxxxxxxxxxxxx"
instance_type = "c7i.xlarge"
ssh_user = "ubuntu"
tailscale_domain = "tail12345.ts.net"
repo_root = "/home/ubuntu/work"
job_prefix = "dbx"
```

## Security model

- Do not commit live credentials, AMI IDs, account IDs, or Tailscale auth keys.
- The golden AMI is assumed to be private and encrypted.
- The instance should use `instance-initiated-shutdown-behavior=terminate`.
- The instance should not receive broad AWS permissions by default.

## Development

```bash
python3.11 -m unittest
PYTHONPATH=src python3.11 -m dbx --help
```
