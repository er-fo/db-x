from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import gzip
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
    resume_session_relative_path: str | None = None
    base_branch: str = "main"


def run_aws_cli(
    config: AppConfig,
    args: list[str],
    *,
    check: bool = True,
    capture_output: bool = True,
    timeout_seconds: int = 60,
) -> subprocess.CompletedProcess[str]:
    command = ["aws"]
    if config.aws_profile:
        command.extend(["--profile", config.aws_profile])
    command.extend(["--region", config.aws_region])
    command.extend(args)

    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=capture_output,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise AwsCliError(
            f"AWS CLI command timed out after {timeout_seconds}s: {' '.join(command)}"
        ) from exc
    if check and result.returncode != 0:
        raise AwsCliError(result.stderr.strip() or "AWS CLI command failed.")
    return result


def build_user_data(config: AppConfig, request: JobLaunchRequest) -> str:
    mission_text = request.mission_path.read_text(encoding="utf-8")
    mission_marker = "DBX_MISSION_EOF"
    prompt_marker = "DBX_PROMPT_EOF"
    repo_clone_url = f"https://github.com/{request.repo}.git"
    job_root = f"{config.repo_root.rstrip('/')}/{request.job_name}"
    repo_dir = f"{job_root}/repo"
    mission_file = f"{job_root}/AGENT_MISSION.md"
    status_file = f"{job_root}/STATUS.md"
    status_json = f"{job_root}/status.json"
    agent_started_json = f"{job_root}/AGENT_STARTED.json"
    bootstrap_prompt = _build_bootstrap_prompt(
        request.session_name,
        agent_started_json,
    )
    blocker_file = f"{job_root}/BLOCKER.md"
    prompt_file = f"{job_root}/BOOTSTRAP_PROMPT.txt"
    preflight_prompt_file = f"{job_root}/DBX_PREFLIGHT_PROMPT.txt"
    log_dir = f"{job_root}/logs"
    bootstrap_log = f"{log_dir}/bootstrap.log"
    preflight_log = f"{log_dir}/preflight.log"
    log_file = f"{log_dir}/codex.log"
    finish_log = f"{log_dir}/finish.log"
    codex_command = _build_codex_command(request, prompt_file, repo_dir)
    codex_preflight_command = _build_codex_preflight_command(preflight_prompt_file, repo_dir)
    preflight_prompt = _build_preflight_prompt()
    preflight_hooks_json = _build_hook_config("/usr/local/bin/dbx-preflight-hook")
    runtime_hooks_json = _build_hook_config(
        f"/usr/local/bin/dbx-finish-job --mode hook --base {request.base_branch} --shutdown"
    )
    resume_session_remote_path = _remote_codex_session_path(
        config, request.resume_session_relative_path
    )
    tailscale_tags = ",".join(config.tailscale_tags)

    script = [
        "#!/bin/bash",
        "set -euo pipefail",
        f"JOB_ROOT={shlex.quote(job_root)}",
        f"REPO_DIR={shlex.quote(repo_dir)}",
        f"MISSION_FILE={shlex.quote(mission_file)}",
        f"STATUS_FILE={shlex.quote(status_file)}",
        f"STATUS_JSON={shlex.quote(status_json)}",
        f"AGENT_STARTED_JSON={shlex.quote(agent_started_json)}",
        f"BLOCKER_FILE={shlex.quote(blocker_file)}",
        f"PROMPT_FILE={shlex.quote(prompt_file)}",
        f"PREFLIGHT_PROMPT_FILE={shlex.quote(preflight_prompt_file)}",
        f"LOG_DIR={shlex.quote(log_dir)}",
        f"BOOTSTRAP_LOG={shlex.quote(bootstrap_log)}",
        f"PREFLIGHT_LOG={shlex.quote(preflight_log)}",
        f"LOG_FILE={shlex.quote(log_file)}",
        f"FINISH_LOG={shlex.quote(finish_log)}",
        f"SESSION_NAME={shlex.quote(request.session_name)}",
        f"REPO_CLONE_URL={shlex.quote(repo_clone_url)}",
        f"BRANCH_NAME={shlex.quote(request.branch_name)}",
        f"BASE_BRANCH={shlex.quote(request.base_branch)}",
        f"TAILSCALE_AUTH_KEY={shlex.quote(config.tailscale_auth_key or '')}",
        f'TAILSCALE_TAGS={shlex.quote(tailscale_tags)}',
        f"RESUME_SESSION_REMOTE_PATH={shlex.quote(resume_session_remote_path or '')}",
        "DBX_USER=ubuntu",
        "mkdir -p \"$JOB_ROOT\" \"$LOG_DIR\"",
        "touch \"$PREFLIGHT_LOG\" \"$FINISH_LOG\"",
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
        "redact_text_file() {",
        "  local input_file=\"$1\"",
        "  local output_file=\"$2\"",
        "  python3 - \"$input_file\" \"$output_file\" <<'PY'",
        "import re",
        "import sys",
        "from pathlib import Path",
        "",
        "input_path, output_path = sys.argv[1:]",
        "text = Path(input_path).read_text(encoding='utf-8')",
        "patterns = [",
        "    re.compile(r'tskey-auth-[A-Za-z0-9_-]+'),",
        "    re.compile(r'gh[pousr]_[A-Za-z0-9]{20,}'),",
        "    re.compile(r'github_pat_[A-Za-z0-9_]{20,}'),",
        "    re.compile(r'AKIA[0-9A-Z]{16}'),",
        "    re.compile(r'ASIA[0-9A-Z]{16}'),",
        "    re.compile(r'aws_secret_access_key\\s*[=:]\\s*[A-Za-z0-9/+=]{20,}', re.IGNORECASE),",
        "    re.compile(r'sk-(?:proj-)?[A-Za-z0-9_-]{12,}'),",
        "    re.compile(r'OPENAI_API_KEY\\s*[=:]\\s*[^\\s]+', re.IGNORECASE),",
        "    re.compile(r'CODEX_(?:AUTH|API|TOKEN)[A-Z_]*\\s*[=:]\\s*[^\\s]+', re.IGNORECASE),",
        "]",
        "for pattern in patterns:",
        "    text = pattern.sub('[REDACTED]', text)",
        "Path(output_path).write_text(text, encoding='utf-8')",
        "PY",
        "}",
        "write_status \"bootstrap\" \"starting\" \"initializing job root\"",
        "cat >\"$MISSION_FILE\" <<'" + mission_marker + "'",
        mission_text,
        mission_marker,
        "cat >\"$PROMPT_FILE\" <<'" + prompt_marker + "'",
        bootstrap_prompt,
        prompt_marker,
        "cat >\"$PREFLIGHT_PROMPT_FILE\" <<'DBX_PREFLIGHT_PROMPT_EOF'",
        preflight_prompt,
        "DBX_PREFLIGHT_PROMPT_EOF",
        "chown \"$DBX_USER\":\"$DBX_USER\" \"$MISSION_FILE\" \"$PROMPT_FILE\" \"$PREFLIGHT_PROMPT_FILE\"",
        "write_status \"bootstrap\" \"running\" \"cloning repository\"",
        "sudo -u \"$DBX_USER\" -H env GH_PROMPT_DISABLED=1 gh auth status >/dev/null",
        "sudo -u \"$DBX_USER\" -H env GH_PROMPT_DISABLED=1 gh auth setup-git >/dev/null",
        "if [ ! -d \"$REPO_DIR/.git\" ]; then",
        "  sudo -u \"$DBX_USER\" -H git clone \"$REPO_CLONE_URL\" \"$REPO_DIR\"",
        "fi",
        "sudo -u \"$DBX_USER\" -H env REPO_DIR=\"$REPO_DIR\" BRANCH_NAME=\"$BRANCH_NAME\" BASE_BRANCH=\"$BASE_BRANCH\" MISSION_FILE=\"$MISSION_FILE\" bash -lc 'cd \"$REPO_DIR\" && git fetch origin --prune && git checkout -B \"$BRANCH_NAME\" \"origin/$BASE_BRANCH\" && cp \"$MISSION_FILE\" \"$REPO_DIR/AGENT_MISSION.md\"'",
        "sudo -u \"$DBX_USER\" -H env REPO_DIR=\"$REPO_DIR\" python3 - <<'PY'",
        "import json",
        "import os",
        "from pathlib import Path",
        "",
        "repo_dir = os.environ['REPO_DIR']",
        "config_path = Path.home() / '.codex' / 'config.toml'",
        "config_path.parent.mkdir(parents=True, exist_ok=True)",
        "existing = config_path.read_text(encoding='utf-8') if config_path.exists() else ''",
        "entry = '\\n[projects.' + json.dumps(repo_dir) + ']\\ntrust_level = \"trusted\"\\n'",
        "if entry not in existing:",
        "    with config_path.open('a', encoding='utf-8') as handle:",
        "        handle.write(entry)",
        "PY",
        "cat >\"/usr/local/bin/dbx-finish-ready\" <<'DBX_FINISH_READY_EOF'",
        "#!/bin/bash",
        "set -euo pipefail",
        f"JOB_ROOT={shlex.quote(job_root)}",
        f"STATUS_FILE={shlex.quote(status_file)}",
        f"STATUS_JSON={shlex.quote(status_json)}",
        f"BLOCKER_FILE={shlex.quote(blocker_file)}",
        "READY_FILE=\"$JOB_ROOT/DBX_FINISH_READY\"",
        "MODE=\"${1:-complete}\"",
        "shift || true",
        "DETAIL=\"${*:-}\"",
        "case \"$MODE\" in complete|blocked) ;; *) echo \"usage: dbx-finish-ready complete|blocked [detail]\" >&2; exit 2 ;; esac",
        "printf '%s\\n' \"$MODE\" >\"$READY_FILE\"",
        "python3 - \"$STATUS_JSON\" \"$STATUS_FILE\" \"$MODE\" \"$DETAIL\" <<'PY'",
        "import json",
        "import sys",
        "from pathlib import Path",
        "status_json_path, status_file_path, mode, detail = sys.argv[1:]",
        "state = 'complete' if mode == 'complete' else 'blocked'",
        "payload = json.loads(Path(status_json_path).read_text(encoding='utf-8')) if Path(status_json_path).exists() else {}",
        "payload.update({'phase': 'finish-ready', 'state': state, 'detail': detail})",
        "Path(status_json_path).write_text(json.dumps(payload, indent=2) + '\\n', encoding='utf-8')",
        "Path(status_file_path).write_text('\\n'.join([f\"# {payload.get('job_name', 'dbx job')}\", '', f\"- Phase: finish-ready\", f\"- State: {state}\", f\"- Detail: {detail}\", '']) , encoding='utf-8')",
        "PY",
        "if [ \"$MODE\" = \"blocked\" ]; then",
        "  { printf '%s\\n\\n' '# Blocker'; printf '%s\\n' \"${DETAIL:-Agent marked this job blocked.}\"; } >\"$BLOCKER_FILE\"",
        "fi",
        "DBX_FINISH_READY_EOF",
        "chmod 755 /usr/local/bin/dbx-finish-ready",
        "cat >\"/usr/local/bin/dbx-finish-job\" <<'DBX_FINISH_JOB_EOF'",
        "#!/bin/bash",
        "set -euo pipefail",
        f"JOB_ROOT={shlex.quote(job_root)}",
        f"REPO_DIR={shlex.quote(repo_dir)}",
        f"STATUS_FILE={shlex.quote(status_file)}",
        f"STATUS_JSON={shlex.quote(status_json)}",
        f"BLOCKER_FILE={shlex.quote(blocker_file)}",
        f"FINISH_LOG={shlex.quote(finish_log)}",
        f"BRANCH_NAME={shlex.quote(request.branch_name)}",
        f"BASE_BRANCH={shlex.quote(request.base_branch)}",
        "READY_FILE=\"$JOB_ROOT/DBX_FINISH_READY\"",
        "DONE_FILE=\"$JOB_ROOT/DBX_FINISH_DONE\"",
        "LOCK_FILE=\"$JOB_ROOT/dbx-finish.lock\"",
        "PR_BODY_FILE=\"$JOB_ROOT/PR_BODY.md\"",
        "MODE=unknown",
        "NO_PR=0",
        "SHUTDOWN=0",
        "while [ \"$#\" -gt 0 ]; do",
        "  case \"$1\" in",
        "    --mode) MODE=\"$2\"; shift 2 ;;",
        "    --base) BASE_BRANCH=\"$2\"; shift 2 ;;",
        "    --no-pr) NO_PR=1; shift ;;",
        "    --shutdown) SHUTDOWN=1; shift ;;",
        "    *) echo \"unknown dbx-finish-job argument: $1\" >&2; exit 2 ;;",
        "  esac",
        "done",
        "if [ \"$MODE\" = \"hook\" ]; then",
        "  [ -f \"$READY_FILE\" ] || exit 0",
        "  MODE=\"$(cat \"$READY_FILE\")\"",
        "fi",
        "mkdir -p \"$(dirname \"$FINISH_LOG\")\"",
        "exec > >(tee -a \"$FINISH_LOG\") 2>&1",
        "exec 9>\"$LOCK_FILE\"",
        "flock -n 9 || exit 0",
        "case \"$MODE\" in complete|blocked|unknown) ;; *) MODE=unknown ;; esac",
        "DRAFT=0",
        "if [ \"$MODE\" != \"complete\" ]; then DRAFT=1; fi",
        "cd \"$REPO_DIR\"",
        "if [ -n \"$(git status --porcelain)\" ]; then",
        "  git add -A",
        "  git commit -m \"Preserve dbx work for $BRANCH_NAME\"",
        "fi",
        "git push -u origin \"$BRANCH_NAME\"",
        "TITLE=\"$(git log -1 --pretty=%s)\"",
        "if [ \"$DRAFT\" = \"1\" ]; then TITLE=\"BLOCKED: $TITLE\"; fi",
        "cat >\"$PR_BODY_FILE\" <<EOF",
        "## dbx finish",
        "",
        "- Branch: \\`$BRANCH_NAME\\`",
        "- Base: \\`$BASE_BRANCH\\`",
        "- Mode: \\`$MODE\\`",
        "",
        "## Status",
        "EOF",
        "cat \"$STATUS_FILE\" >>\"$PR_BODY_FILE\" 2>/dev/null || true",
        "if [ -f \"$BLOCKER_FILE\" ]; then",
        "  printf '\\n## Blocker\\n\\n' >>\"$PR_BODY_FILE\"",
        "  cat \"$BLOCKER_FILE\" >>\"$PR_BODY_FILE\"",
        "fi",
        "PR_URL=",
        "if [ \"$NO_PR\" = \"0\" ]; then",
        "  PR_URL=\"$(gh pr view \"$BRANCH_NAME\" --json url --jq '.url' 2>/dev/null || true)\"",
        "  if [ -n \"$PR_URL\" ]; then",
        "    gh pr edit \"$BRANCH_NAME\" --title \"$TITLE\" --body-file \"$PR_BODY_FILE\"",
        "  elif [ \"$DRAFT\" = \"1\" ]; then",
        "    gh pr create --draft --base \"$BASE_BRANCH\" --head \"$BRANCH_NAME\" --title \"$TITLE\" --body-file \"$PR_BODY_FILE\"",
        "  else",
        "    gh pr create --base \"$BASE_BRANCH\" --head \"$BRANCH_NAME\" --title \"$TITLE\" --body-file \"$PR_BODY_FILE\"",
        "  fi",
        "  PR_URL=\"$(gh pr view \"$BRANCH_NAME\" --json url --jq '.url')\"",
        "  printf '%s\\n' \"$PR_URL\" >\"$JOB_ROOT/PR_URL\"",
        "fi",
        "python3 - \"$STATUS_JSON\" \"$MODE\" \"$PR_URL\" <<'PY'",
        "import json",
        "import sys",
        "from pathlib import Path",
        "path, mode, pr_url = sys.argv[1:]",
        "payload = json.loads(Path(path).read_text(encoding='utf-8')) if Path(path).exists() else {}",
        "payload.update({'phase': 'finished', 'state': mode, 'pr_url': pr_url or None})",
        "Path(path).write_text(json.dumps(payload, indent=2) + '\\n', encoding='utf-8')",
        "PY",
        "printf '%s\\n' \"$MODE\" >\"$DONE_FILE\"",
        "if [ \"$SHUTDOWN\" = \"1\" ] && [ \"$NO_PR\" = \"0\" ] && [ -n \"$PR_URL\" ]; then",
        "  shutdown -h now",
        "fi",
        "DBX_FINISH_JOB_EOF",
        "chmod 755 /usr/local/bin/dbx-finish-job",
        "cat >\"/usr/local/bin/dbx-preflight-hook\" <<'DBX_PREFLIGHT_HOOK_EOF'",
        "#!/bin/bash",
        "set -euo pipefail",
        f"PREFLIGHT_LOG={shlex.quote(preflight_log)}",
        "printf '%s\\n' 'dbx preflight hook invoked' >>\"$PREFLIGHT_LOG\"",
        "DBX_PREFLIGHT_HOOK_EOF",
        "chmod 755 /usr/local/bin/dbx-preflight-hook",
        "cat >\"/usr/local/bin/dbx-codex-preflight\" <<'DBX_CODEX_PREFLIGHT_EOF'",
        "#!/bin/bash",
        "set +e",
        f"JOB_ROOT={shlex.quote(job_root)}",
        f"REPO_DIR={shlex.quote(repo_dir)}",
        f"STATUS_FILE={shlex.quote(status_file)}",
        f"STATUS_JSON={shlex.quote(status_json)}",
        f"BLOCKER_FILE={shlex.quote(blocker_file)}",
        f"PREFLIGHT_LOG={shlex.quote(preflight_log)}",
        f"PREFLIGHT_PROMPT_FILE={shlex.quote(preflight_prompt_file)}",
        f"SESSION_NAME={shlex.quote(request.session_name)}",
        "PREFLIGHT_SESSION=\"$SESSION_NAME-preflight\"",
        "PREFLIGHT_TIMEOUT_SECONDS=90",
        "PREFLIGHT_TAIL_RAW=\"$JOB_ROOT/preflight-failure.raw.txt\"",
        "PREFLIGHT_TAIL_REDACTED=\"$JOB_ROOT/preflight-failure.redacted.txt\"",
        "write_preflight_blocker() {",
        "  local detail=\"$1\"",
        "  local title='# Codex preflight failed'",
        "  local summary='dbx could not verify Codex runtime health before starting the long-running tmux session.'",
        "  case \"$detail\" in",
        "    codex_auth_failed)",
        "      title='# Codex authentication failed'",
        "      summary='dbx detected a Codex authentication failure during VM preflight.'",
        "      ;;",
        "    codex_hook_config_invalid)",
        "      title='# Codex hook config is invalid'",
        "      summary='dbx detected a Codex hooks configuration failure during VM preflight.'",
        "      ;;",
        "  esac",
        "  tail -n 120 \"$PREFLIGHT_LOG\" >\"$PREFLIGHT_TAIL_RAW\" 2>/dev/null || true",
        "  redact_text_file \"$PREFLIGHT_TAIL_RAW\" \"$PREFLIGHT_TAIL_REDACTED\"",
        "  write_status \"bootstrap\" \"blocked\" \"$detail\"",
        "  cat >\"$BLOCKER_FILE\" <<EOF",
        "$title",
        "",
        "$summary",
        "",
        "## preflight.log tail",
        "EOF",
        "  cat \"$PREFLIGHT_TAIL_REDACTED\" >>\"$BLOCKER_FILE\" 2>/dev/null || true",
        "  rm -f \"$PREFLIGHT_TAIL_RAW\" \"$PREFLIGHT_TAIL_REDACTED\"",
        "}",
        "classify_preflight() {",
        "  python3 - \"$PREFLIGHT_LOG\" <<'PY'",
        "import re",
        "import sys",
        "from pathlib import Path",
        "path = Path(sys.argv[1])",
        "text = path.read_text(encoding='utf-8') if path.exists() else ''",
        "if 'DBX_PREFLIGHT_OK' in text:",
        "    print('ok')",
        "    raise SystemExit(0)",
        "if re.search(r'failed to parse hooks config|invalid generated hooks schema', text, re.IGNORECASE):",
        "    print('codex_hook_config_invalid')",
        "    raise SystemExit(0)",
        "auth_patterns = [",
        "    r'not logged in',",
        "    r'login required',",
        "    r'invalid api key',",
        "    r'token_expired',",
        "    r'authentication token is expired',",
        "    r'access token could not be refreshed',",
        "    r'refresh token was already used',",
        "    r'please log out and sign in again',",
        "]",
        "if any(re.search(pattern, text, re.IGNORECASE) for pattern in auth_patterns):",
        "    print('codex_auth_failed')",
        "    raise SystemExit(0)",
        "if re.search(r'MCP startup incomplete|MCP client .* failed to start|No more recovery steps available', text, re.IGNORECASE):",
        "    print('codex_preflight_failed')",
        "    raise SystemExit(0)",
        "print('pending')",
        "PY",
        "}",
        ": >\"$PREFLIGHT_LOG\"",
        "tmux kill-session -t \"$PREFLIGHT_SESSION\" >/dev/null 2>&1 || true",
        "tmux new-session -d -s \"$PREFLIGHT_SESSION\" -c \"$REPO_DIR\"",
        "tmux pipe-pane -o -t \"$PREFLIGHT_SESSION\":0.0 \"cat >> '$PREFLIGHT_LOG'\"",
        f"tmux send-keys -t \"$PREFLIGHT_SESSION\":0.0 {shlex.quote(codex_preflight_command)} C-m",
        "STARTED_AT=$(date +%s)",
        "while true; do",
        "  RESULT=\"$(classify_preflight)\"",
        "  if [ \"$RESULT\" = \"ok\" ]; then",
        "    tmux kill-session -t \"$PREFLIGHT_SESSION\" >/dev/null 2>&1 || true",
        "    exit 0",
        "  fi",
        "  if [ \"$RESULT\" != \"pending\" ]; then",
        "    tmux kill-session -t \"$PREFLIGHT_SESSION\" >/dev/null 2>&1 || true",
        "    write_preflight_blocker \"$RESULT\"",
        "    exit 1",
        "  fi",
        "  if ! tmux has-session -t \"$PREFLIGHT_SESSION\" >/dev/null 2>&1; then",
        "    write_preflight_blocker codex_preflight_failed",
        "    exit 1",
        "  fi",
        "  NOW=$(date +%s)",
        "  if [ $((NOW - STARTED_AT)) -ge \"$PREFLIGHT_TIMEOUT_SECONDS\" ]; then",
        "    tmux kill-session -t \"$PREFLIGHT_SESSION\" >/dev/null 2>&1 || true",
        "    write_preflight_blocker codex_preflight_failed",
        "    exit 1",
        "  fi",
        "  sleep 2",
        "done",
        "DBX_CODEX_PREFLIGHT_EOF",
        "chmod 755 /usr/local/bin/dbx-codex-preflight",
        "cat >\"/usr/local/bin/dbx-codex-watch\" <<'DBX_CODEX_WATCH_EOF'",
        "#!/bin/bash",
        "set +e",
        f"JOB_ROOT={shlex.quote(job_root)}",
        f"STATUS_FILE={shlex.quote(status_file)}",
        f"STATUS_JSON={shlex.quote(status_json)}",
        f"BLOCKER_FILE={shlex.quote(blocker_file)}",
        f"LOG_FILE={shlex.quote(log_file)}",
        f"BASE_BRANCH={shlex.quote(request.base_branch)}",
        "DONE_FILE=\"$JOB_ROOT/DBX_FINISH_DONE\"",
        "AUTH_TAIL_RAW=\"$JOB_ROOT/codex-auth-failure.raw.txt\"",
        "AUTH_TAIL_REDACTED=\"$JOB_ROOT/codex-auth-failure.redacted.txt\"",
        "codex_auth_failed() {",
        "  python3 - \"$LOG_FILE\" <<'PY'",
        "import re",
        "import sys",
        "from pathlib import Path",
        "path = Path(sys.argv[1])",
        "text = path.read_text(encoding='utf-8') if path.exists() else ''",
        "patterns = [",
        "    r'not logged in',",
        "    r'login required',",
        "    r'invalid api key',",
        "    r'token_expired',",
        "    r'authentication token is expired',",
        "    r'access token could not be refreshed',",
        "    r'refresh token was already used',",
        "    r'please log out and sign in again',",
        "]",
        "raise SystemExit(0 if any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns) else 1)",
        "PY",
        "}",
        "write_auth_failed_status() {",
        "  python3 - \"$STATUS_JSON\" \"$STATUS_FILE\" <<'PY'",
        "import json",
        "import sys",
        "from pathlib import Path",
        "status_json_path, status_file_path = sys.argv[1:]",
        "payload = json.loads(Path(status_json_path).read_text(encoding='utf-8')) if Path(status_json_path).exists() else {}",
        "payload.update({'phase': 'runtime', 'state': 'auth_failed', 'detail': 'codex_auth_failed'})",
        "Path(status_json_path).write_text(json.dumps(payload, indent=2) + '\\n', encoding='utf-8')",
        "Path(status_file_path).write_text(",
        "    '\\n'.join([",
        "        f\"# {payload.get('job_name', 'dbx job')}\",",
        "        '',",
        "        f\"- Repo: `{payload.get('repo', '')}`\",",
        "        f\"- Branch: `{payload.get('branch_name', '')}`\",",
        "        f\"- Session: `{payload.get('session_name', '')}`\",",
        "        '- Phase: runtime',",
        "        '- State: auth_failed',",
        "        '- Detail: codex_auth_failed',",
        "        '',",
        "    ]),",
        "    encoding='utf-8',",
        ")",
        "PY",
        "}",
        "write_auth_failed_blocker() {",
        "  tail -n 80 \"$LOG_FILE\" >\"$AUTH_TAIL_RAW\" 2>/dev/null || true",
        "  python3 - \"$AUTH_TAIL_RAW\" \"$AUTH_TAIL_REDACTED\" <<'PY'",
        "import re",
        "import sys",
        "from pathlib import Path",
        "input_path, output_path = map(Path, sys.argv[1:])",
        "text = input_path.read_text(encoding='utf-8') if input_path.exists() else ''",
        "patterns = [",
        "    re.compile(r'tskey-auth-[A-Za-z0-9_-]+'),",
        "    re.compile(r'gh[pousr]_[A-Za-z0-9]{20,}'),",
        "    re.compile(r'github_pat_[A-Za-z0-9_]{20,}'),",
        "    re.compile(r'AKIA[0-9A-Z]{16}'),",
        "    re.compile(r'ASIA[0-9A-Z]{16}'),",
        "    re.compile(r'aws_secret_access_key\\s*[=:]\\s*[A-Za-z0-9/+=]{20,}', re.IGNORECASE),",
        "    re.compile(r'sk-(?:proj-)?[A-Za-z0-9_-]{12,}'),",
        "    re.compile(r'OPENAI_API_KEY\\s*[=:]\\s*[^\\s]+', re.IGNORECASE),",
        "    re.compile(r'CODEX_(?:AUTH|API|TOKEN)[A-Z_]*\\s*[=:]\\s*[^\\s]+', re.IGNORECASE),",
        "]",
        "for pattern in patterns:",
        "    text = pattern.sub('[REDACTED]', text)",
        "output_path.write_text(text, encoding='utf-8')",
        "PY",
        "  cat >\"$BLOCKER_FILE\" <<'EOF'",
        "# Codex authentication failed",
        "",
        "dbx detected a Codex authentication failure after runtime startup.",
        "",
        "## codex.log tail",
        "EOF",
        "  cat \"$AUTH_TAIL_REDACTED\" >>\"$BLOCKER_FILE\" 2>/dev/null || true",
        "  rm -f \"$AUTH_TAIL_RAW\" \"$AUTH_TAIL_REDACTED\"",
        "}",
        f"{codex_command}",
        "CODEX_EXIT=$?",
        "if [ ! -f \"$DONE_FILE\" ]; then",
        "  if codex_auth_failed; then",
        "    write_auth_failed_status || true",
        "    write_auth_failed_blocker || true",
        "    /usr/local/bin/dbx-finish-job --mode blocked --base \"$BASE_BRANCH\" --shutdown",
        "  else",
        "    /usr/local/bin/dbx-finish-job --mode unknown --base \"$BASE_BRANCH\" --shutdown",
        "  fi",
        "fi",
        "exit \"$CODEX_EXIT\"",
        "DBX_CODEX_WATCH_EOF",
        "chmod 755 /usr/local/bin/dbx-codex-watch",
        "sudo -u \"$DBX_USER\" -H mkdir -p \"/home/$DBX_USER/.codex\"",
        "sudo -u \"$DBX_USER\" -H python3 - <<'PY'",
        "from pathlib import Path",
        "config = Path.home() / '.codex' / 'config.toml'",
        "existing = config.read_text(encoding='utf-8') if config.exists() else ''",
        "if 'codex_hooks = true' not in existing:",
        "    with config.open('a', encoding='utf-8') as handle:",
        "        handle.write('\\n[features]\\ncodex_hooks = true\\n')",
        "PY",
        "sudo -u \"$DBX_USER\" -H tee \"/home/$DBX_USER/.codex/hooks.json\" >/dev/null <<'DBX_HOOKS_EOF'",
        *preflight_hooks_json.splitlines(),
        "DBX_HOOKS_EOF",
        "if [ -n \"$TAILSCALE_AUTH_KEY\" ]; then",
        "  write_status \"bootstrap\" \"running\" \"connecting tailscale\"",
        "  systemctl start tailscaled",
        "  TAILSCALE_UP_ARGS=(--ssh --hostname \"$SESSION_NAME\" --auth-key \"$TAILSCALE_AUTH_KEY\")",
        "  if [ -n \"$TAILSCALE_TAGS\" ]; then",
        "    TAILSCALE_UP_ARGS+=(--advertise-tags \"$TAILSCALE_TAGS\")",
        "  fi",
        "  tailscale up \"${TAILSCALE_UP_ARGS[@]}\"",
        "fi",
        "AUTH_STATUS_RAW=\"$JOB_ROOT/codex-login-status.raw.txt\"",
        "AUTH_STATUS_REDACTED=\"$JOB_ROOT/codex-login-status.redacted.txt\"",
        "set +e",
        "AUTH_STATUS_OUTPUT=\"$(sudo -u \"$DBX_USER\" -H codex login status 2>&1)\"",
        "AUTH_STATUS_EXIT=$?",
        "set -e",
        "printf '%s\\n' \"$AUTH_STATUS_OUTPUT\" > \"$AUTH_STATUS_RAW\"",
        "if [ \"$AUTH_STATUS_EXIT\" -ne 0 ]; then",
        "  redact_text_file \"$AUTH_STATUS_RAW\" \"$AUTH_STATUS_REDACTED\"",
        "  write_status \"bootstrap\" \"blocked\" \"codex_auth_failed\"",
        "  cat >\"$BLOCKER_FILE\" <<EOF",
        "# Codex authentication failed",
        "",
        "Bootstrap could not verify Codex authentication for the ubuntu user.",
        "",
        "## codex login status",
        "EOF",
        "  cat \"$AUTH_STATUS_REDACTED\" >> \"$BLOCKER_FILE\"",
        "  rm -f \"$AUTH_STATUS_RAW\" \"$AUTH_STATUS_REDACTED\"",
        "  exit 0",
        "fi",
        "rm -f \"$AUTH_STATUS_RAW\" \"$AUTH_STATUS_REDACTED\"",
        "write_status \"bootstrap\" \"running\" \"running codex preflight\"",
        "set +e",
        "sudo -u \"$DBX_USER\" -H /usr/local/bin/dbx-codex-preflight",
        "PREFLIGHT_EXIT=$?",
        "set -e",
        "if [ \"$PREFLIGHT_EXIT\" -ne 0 ]; then",
        "  exit 0",
        "fi",
        "sudo -u \"$DBX_USER\" -H tee \"/home/$DBX_USER/.codex/hooks.json\" >/dev/null <<'DBX_RUNTIME_HOOKS_EOF'",
        *runtime_hooks_json.splitlines(),
        "DBX_RUNTIME_HOOKS_EOF",
        "write_status \"bootstrap\" \"running\" \"starting tmux codex session\"",
        "sudo -u \"$DBX_USER\" -H tmux new-session -d -s \"$SESSION_NAME\" -c \"$REPO_DIR\"",
        "sudo -u \"$DBX_USER\" -H tmux pipe-pane -o -t \"$SESSION_NAME\":0.0 \"cat >> '$LOG_FILE'\"",
        "if [ -n \"$RESUME_SESSION_REMOTE_PATH\" ]; then",
        "  write_status \"bootstrap\" \"waiting\" \"waiting for codex resume session upload\"",
        "  for _ in $(seq 1 120); do",
        "    if [ -f \"$RESUME_SESSION_REMOTE_PATH\" ]; then",
        "      break",
        "    fi",
        "    sleep 2",
        "  done",
        "  if [ ! -f \"$RESUME_SESSION_REMOTE_PATH\" ]; then",
        "    write_status \"bootstrap\" \"blocked\" \"codex resume session upload missing\"",
        "    cat >\"$BLOCKER_FILE\" <<EOF",
        "# Resume session upload missing",
        "",
        "The requested Codex resume session was not uploaded before bootstrap timed out.",
        "EOF",
        "    exit 0",
        "  fi",
        "fi",
        "sudo -u \"$DBX_USER\" -H tmux send-keys -t \"$SESSION_NAME\":0.0 /usr/local/bin/dbx-codex-watch C-m",
        "write_status \"runtime\" \"starting\" \"waiting_for_agent_heartbeat\"",
    ]
    return "\n".join(script)


def launch_instance(config: AppConfig, request: JobLaunchRequest) -> dict[str, object]:
    user_data = build_user_data(config, request)
    with tempfile.NamedTemporaryFile(
        mode="wb", suffix=".sh.gz", delete=False
    ) as handle:
        handle.write(gzip.compress(user_data.encode("utf-8")))
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
            f"fileb://{user_data_path}",
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
    return f"{config.ssh_user}@{hostname}"


def _remote_codex_session_path(config: AppConfig, relative_path: str | None) -> str | None:
    if not relative_path:
        return None
    home = "/root" if config.ssh_user == "root" else f"/home/{config.ssh_user}"
    return f"{home}/.codex/sessions/{relative_path}"


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


def _build_bootstrap_prompt(session_name: str, agent_started_json: str) -> str:
    return "\n".join(
        [
            "Continue and complete the task you were doing previously.",
            "",
            "You have been moved onto a disposable VM devbox. The environment looks like this:",
            "",
            "- The repository has been cloned into the current working directory.",
            "- The mission is available in AGENT_MISSION.md.",
            "- This prompt was injected by dbx during VM bootstrap.",
            "- You are running inside tmux on an EC2 instance created for this task.",
            "- The instance is disposable; keep the git branch clean, reviewable, and resumable.",
            "- When you mark the work complete or blocked, dbx will preserve the branch, open or update a pull request, and terminate the VM after PR confirmation.",
            "",
            f"Your first action is mandatory: write {agent_started_json} before reading AGENT_MISSION.md or running any repository command.",
            "",
            "You are authorized to prepare this VM environment as needed:",
            "- inspect the repository and current git state",
            "- install missing project dependencies",
            "- run setup, lint, tests, typechecks, builds, and local smoke checks",
            "- update local configuration required for the task",
            "- create or update files needed to complete the mission",
            "",
            "Work like a long-running implementation agent, not a one-shot task runner.",
            "",
            "Git hygiene is mandatory:",
            "- check git status before starting meaningful work",
            "- keep changes small, coherent, and reviewable",
            "- commit completed checkpoints when they are verified",
            "- never leave unrelated, accidental, generated, or debug-only changes mixed into the work",
            "- before stopping, ensure the repository is clean or explicitly documented in STATUS.md",
            "",
            "Start by:",
            f"1. Writing {agent_started_json}.",
            "   Use JSON with status, started_at, and session_name.",
            "   Set \"status\": \"running\" and use the current ISO-8601 UTC timestamp.",
            f"   Set \"session_name\": {json.dumps(session_name)}.",
            "2. Reading AGENT_MISSION.md.",
            "3. Updating status.json and STATUS.md to phase=runtime, state=running, detail=agent_heartbeat_received.",
            "4. Inspecting the repository, mission file, and git status.",
            "5. Writing a brief plan into STATUS.md or the nearest durable project artifact.",
            "6. Continuing the task in coherent checkpoints.",
            "7. Verifying each checkpoint before moving on.",
            "8. Keeping the branch clean, reviewable, and resumable throughout the work.",
            "",
            "When the work is complete and verified:",
            "- update STATUS.md with the result and verification evidence",
            "- leave git clean or with only intentional committed checkpoints",
            "- run `dbx-finish-ready complete \"short completion summary\"`",
            "- stop normally; the dbx finish lifecycle will open/update the PR and terminate the VM after the PR URL is confirmed",
            "",
            "If you cannot proceed safely because of missing product intent, credentials, external access, unsafe permissions, or repeated verification failure:",
            "- update STATUS.md with the blocker, current state, and next recommended action",
            "- write a concise summary into BLOCKER.md",
            "- leave the repository clean, or explicitly document why uncommitted changes remain",
            "- run `dbx-finish-ready blocked \"short blocker summary\"`",
            "- stop normally; dbx will preserve the branch in a draft PR and terminate after the PR URL is confirmed",
            "",
            "If Codex exits before you mark complete or blocked, dbx treats the stop as unknown, preserves the branch in a draft PR, and terminates only after PR confirmation.",
            "",
            f"The first heartbeat step is mandatory: write {agent_started_json} before reading AGENT_MISSION.md or running any repository command.",
            "Never stop without leaving a clear status note and a clean or explicitly documented git state.",
        ]
    )


def _build_codex_command(request: JobLaunchRequest, prompt_file: str, repo_dir: str) -> str:
    prompt_expr = f'"$(cat {shlex.quote(prompt_file)})"'
    codex = (
        "codex --no-alt-screen --ask-for-approval never "
        f"--sandbox danger-full-access -C {shlex.quote(repo_dir)}"
    )
    if request.resume_session_id:
        session_id = shlex.quote(request.resume_session_id)
        return f"{codex} resume {session_id} {prompt_expr}"
    return f"{codex} {prompt_expr}"


def _build_codex_preflight_command(prompt_file: str, repo_dir: str) -> str:
    prompt_expr = f'"$(cat {shlex.quote(prompt_file)})"'
    codex = (
        "codex --no-alt-screen --ask-for-approval never "
        f"--sandbox danger-full-access -C {shlex.quote(repo_dir)}"
    )
    return f"{codex} {prompt_expr}"


def _build_preflight_prompt() -> str:
    return "\n".join(
        [
            "You are running a dbx VM bootstrap preflight.",
            "Do not use tools.",
            "Reply with exactly DBX_PREFLIGHT_OK and nothing else.",
        ]
    )


def _build_hook_config(command: str) -> str:
    return json.dumps(
        {
            "hooks": {
                "Stop": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": command,
                            }
                        ]
                    }
                ]
            }
        },
        indent=2,
    )
