# deploy/aws — the cloud deployment, as code

One `pulumi up` gives you the same stack a local deploy runs — the compose file,
the egress wall, the tunnel ingress — on an EC2 VM, with the guardrail backed by
an Amazon Bedrock Guardrail instead of a local model. Ingress comes from the
shared [deploy/cloudflare](../cloudflare/) stack (run it first); this stack is
compute + guardrail + boot secrets. The full step-by-step runbook (including the
manual pieces: Google OAuth client, per-tool secrets, optional Slack app) lives
at [docs/deploy/aws.md](../../docs/deploy/aws.md).

What the program creates: an EC2 instance (zero-inbound security group; admin
via SSM Session Manager), a Bedrock Guardrail (prompt-attack filter), an
instance role scoped to `ApplyGuardrail` + the two SSM boot secrets.
`pulumi destroy` removes all of it; the tunnel and DNS live on in the
cloudflare stack.

## Quickstart

```bash
cd deploy/cloudflare && pulumi up      # once — see its README
cd ../aws
python3 -m venv venv && venv/bin/pip install -r requirements.txt
pulumi stack init prod
pulumi config set aws:region us-east-1
pulumi config set domain example.com
pulumi config set cloudflareStack organization/mcp-tools-cloudflare/prod
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

Assumptions: both stacks share one Pulumi backend (the StackReference resolves
there), the account has a default VPC (the VM lands there), and first boot takes
several minutes while images build — `docker compose ps` over an SSM session
shows progress.
