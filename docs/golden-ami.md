# Golden AMI Notes

The first version of `db-x` assumes a manually prepared private encrypted AMI.

The AMI should contain:

- Ubuntu 24.04
- `codex`
- `git`
- `gh`
- `tmux`
- `tailscale`
- `node`, `npm`, `npx`
- `jq`, `curl`, `python3`
- Codex logged in through your ChatGPT subscription
- GitHub auth for repo clone/push/PR flows
- Superpowers installed into the agent skill path

Recommended instance settings:

- Region: `eu-north-1`
- Size: 4 vCPU / 16 GB RAM class
- Shutdown behavior: terminate
- No public SSH exposure
- Tailscale for access

Keep the AMI private. Treat it as sensitive because it contains authenticated
developer tooling.
