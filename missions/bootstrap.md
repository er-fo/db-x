# Mission: Bootstrap db-x

## Goal

Turn `db-x` into a usable disposable devbox launcher for long-running Codex work on AWS.

## Constraints

- Keep secrets and concrete infrastructure identifiers out of the repository.
- Prefer small, testable increments.
- Preserve the disposable instance model.

## Definition of done

- `dbx doctor` validates local readiness honestly.
- `dbx start` launches from a configured AMI and records local job state.
- `dbx attach` gives a correct tmux attach command for a running instance.
