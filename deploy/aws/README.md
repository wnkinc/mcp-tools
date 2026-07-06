# deploy/aws — the cloud deployment, as code

One `pulumi up` gives you the same stack a local deploy runs — the compose file,
the egress wall, the tunnel ingress — on an EC2 VM, with the guardrail backed by
an Amazon Bedrock Guardrail instead of a local model. The full step-by-step
runbook (including the manual pieces: Google OAuth client, per-tool secrets,
optional Slack app) lives at [docs/deploy/aws.md](../../docs/deploy/aws.md).

What the program creates: Cloudflare Tunnel + wildcard DNS record, an EC2
instance (zero-inbound security group; admin via SSM Session Manager), a Bedrock
Guardrail (prompt-attack filter), an instance role scoped to `ApplyGuardrail` +
the two SSM boot secrets. `pulumi destroy` removes all of it.

## Quickstart

```bash
cd deploy/aws
python3 -m venv venv && venv/bin/pip install -r requirements.txt
pulumi stack init prod
pulumi config set aws:region us-east-1
pulumi config set domain example.com
pulumi config set cloudflareAccountId <id>
pulumi config set cloudflareZoneId <zone-id>
export CLOUDFLARE_API_TOKEN=<token with Tunnel + DNS edit>
pulumi up
```

Config surface (defaults in parentheses): `tools` (`xmcp,data`), `guardrail`
(`bedrock` | `llamafirewall` | `off`), `hfToken` (secret; llamafirewall mode),
`repoUrl` (upstream), `repoRef` (`main` — pin a tag for reproducible deploys),
`instanceType` (`t3.large`), `volumeGb` (`60`; enabling `lean` wants more — its
base image alone is 13 GB).

Outputs: `connectorUrls` (paste into Claude → Settings → Connectors), `connect`
(the SSM session command for dropping per-tool `.env` files onto the VM),
`instanceId`, `tunnelId`, `guardrailId`.

Assumptions: the account has a default VPC (the VM lands there), and first boot
takes several minutes while images build — `docker compose ps` over an SSM
session shows progress.
