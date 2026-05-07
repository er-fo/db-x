# db-x

`db-x` is a disposable AWS devbox runner for long-running Codex + Superpowers work.

It launches a fresh EC2 instance per mission, boots it from a private golden AMI,
runs Codex inside `tmux`, and then terminates the machine when the work is done
or blocked. The goal is simple: keep the autonomy and long-running ergonomics of
a real devbox without leaving compute running between jobs.

## What it does

- Starts a new EC2 job instance from a prebuilt AMI
- Injects a mission file and bootstrap prompt as cloud-init user data
- Uses ordinary SSH over the Tailscale network for private access
- Keeps Codex running in `tmux`
- Clones the repo, creates a branch, and starts Codex on-instance
- Keeps local job metadata so `list` and `status` stay informative
- Preserves remote work, pushes the job branch, opens a PR, and terminates the
  instance with `finish`

## What it does not do yet

- Build the golden AMI for you
- Install Superpowers for you
- Manage Brevo/email alerts
- Upload logs to S3

## Command surface

```bash
dbx init-config
dbx doctor
dbx sessions
dbx start
dbx start er-fo/db-x missions/bootstrap.md
dbx start er-fo/db-x missions/bootstrap.md --pick-session
dbx list
dbx status i-0123456789abcdef0
dbx status i-0123456789abcdef0 --logs
dbx attach i-0123456789abcdef0
dbx attach i-0123456789abcdef0 --check
dbx finish i-0123456789abcdef0
dbx terminate i-0123456789abcdef0
```

Use `dbx start` for the normal guided launch flow. It uses `default_repo` and
`default_mission` from the config, then opens a terminal wizard that lets you
resume a local Codex session, start a fresh mission, paste a session ID/path,
and review the exact repo, mission, AWS target, and resume choice before any EC2
instance is launched. The session step shows a live "Currently selected" preview
with the conversation, branch, session ID, and full path, so the launch purpose
is clear before you press Enter.

`dbx sessions` prints the same simple 10-row table with created time, updated
time, branch, full session path, and latest user message. Subagent sessions are
excluded, so the list shows direct user-submitted conversations.
`dbx start <repo> <mission>` and `--resume-session <session-id>` remain
available for scripted launches.

`dbx attach` prints the exact SSH command to run. It does not execute SSH for you yet.

## Config

`db-x` reads configuration from `~/.config/db-x/config.toml` by default.

Create it with:

```bash
dbx init-config
```

When you pass a custom config path, put `--config` before the subcommand:

```bash
dbx --config ~/.config/db-x/config.toml doctor
```

Example:

```toml
aws_profile = "personal"
aws_region = "eu-north-1"
default_owner = "er-fo"
default_repo = "er-fo/db-x"
default_mission = "missions/bootstrap.md"
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
```

For unattended access to disposable devboxes, use a tagged, ephemeral Tailscale
auth key. The AMI must also contain working SSH authorization for the operator,
because `dbx` uses OpenSSH over the private Tailscale address rather than the
`tailscale ssh` wrapper.

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
