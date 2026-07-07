# AWS deployment runbook

Goal: the same stack the local runbook builds — tools behind the egress wall,
Cloudflare Tunnel ingress, Google OAuth per tool — on an EC2 VM that
`pulumi up` creates end-to-end, with the guardrail backed by an Amazon Bedrock
Guardrail. `example.com` stands for your domain throughout.

The VM accepts **zero inbound connections**: the tunnel and SSM agent both dial
out. Admin access is `aws ssm start-session` — there is no SSH key to manage.

Prereqs on your workstation:

- An AWS account + credentials configured (`aws sts get-caller-identity`
  works), with a default VPC in your target region (AWS accounts have one
  unless someone deleted it).
- [Pulumi CLI](https://www.pulumi.com/docs/install/) + a backend
  (`pulumi login`), Python 3.11+.
- [AWS Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html)
  for the `aws ssm start-session` step.
- A domain on Cloudflare, its **Zone ID** and **Account ID** (both on the
  zone's Overview page), and an **API token** with `Cloudflare Tunnel:Edit` +
  `DNS:Edit` on that zone.

## 1. Ingress: the shared Cloudflare stack

The tunnel + wildcard DNS record are their own stack, shared by every
deployment path (a deployment moving between local and AWS keeps the same
domain, tunnel, and credentials):

```bash
cd deploy/cloudflare
python3 -m venv venv && venv/bin/pip install -r requirements.txt
pulumi stack init prod
pulumi config set cloudflareAccountId <account-id>
pulumi config set cloudflareZoneId <zone-id>
export CLOUDFLARE_API_TOKEN=<token>
pulumi up
```

The AWS stack reads its outputs itself — nothing to copy by hand.

## 2. Configure the compute stack

```bash
cd ../aws
python3 -m venv venv && venv/bin/pip install -r requirements.txt
pulumi stack init prod
pulumi config set aws:region us-east-1
pulumi config set domain example.com
pulumi config set cloudflareStack organization/mcp-tools-cloudflare/prod
pulumi config set tools xmcp,data            # your pick of xmcp,data,lean,telegram
```

(`cloudflareStack` is the step-1 stack's full name on your shared Pulumi
backend; `organization` is the literal org name on the local/self-managed
backend.)

Guardrail: `bedrock` is the default — the Guardrail resource, IAM permission,
and `.env` wiring all come out of `pulumi up`. Alternatives:

```bash
pulumi config set guardrail llamafirewall    # local model on the VM instead
pulumi config set --secret hfToken hf_...    #   + HF token (see the local runbook
                                             #     for the model-access request)
pulumi config set guardrail off              # unscreened; your call
```

Sizing: defaults are `t3.large` + 60 GB gp3 — right for `xmcp,data`. Enabling
`lean` wants `volumeGb` ≥ 100 (13 GB base image) and benefits from more CPU.
Running your own fork / a pinned version: `pulumi config set repoUrl <fork>`,
`pulumi config set repoRef <tag-or-commit>`.

## 3. `pulumi up`

Creates: the Bedrock Guardrail (prompt-attack filter only), an instance role
(SSM + `ApplyGuardrail` + the two boot-secret reads), a zero-inbound security
group, and the VM — whose first boot installs docker, clones the repo, renders
the root `.env`, fetches the tunnel credentials from SSM, and runs
`docker compose -f docker-compose.yml -f docker-compose.tunnel.yml up -d --build`.

First boot takes several minutes (image builds). Watch it:

```bash
pulumi stack output connect          # prints the SSM session command; run it, then:
sudo tail -f /var/log/cloud-init-output.log
cd /opt/mcp-tools && sudo docker compose ps
```

## 4. Google OAuth client (~5–10 min, manual)

In the [Google Cloud Console](https://console.cloud.google.com/):

1. **Create / pick a project** (e.g. `mcp-tools`).
2. **OAuth consent screen:** User type **External**; app name + your emails;
   scopes `openid` + `.../auth/userinfo.email`. Add every allowed email as a
   **Test user**; leave status **Testing**.
3. **Credentials → Create OAuth client ID:** type **Web application**; one
   redirect URI per tool: `https://<tool>.example.com/auth/callback`. Copy the
   **Client ID** and **Client secret**.

One client covers all tools; a new tool adds one more redirect URI.

## 5. Per-tool secrets (over the SSM session)

For each tool, on the VM:

```bash
cd /opt/mcp-tools
sudo cp tools/<tool>/env.example tools/<tool>/.env
sudo nano tools/<tool>/.env
```

Fill the tool's own values (documented in its `env.example`) plus the auth trio
from step 4: `MCP_AUTH_ENABLED=1`, `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`,
`MCP_ALLOWED_GOOGLE_EMAILS=<your email>`. Then restart with the new secrets:

```bash
sudo docker compose -f docker-compose.yml -f docker-compose.tunnel.yml up -d
```

## 6. Approvals (Slack optional)

Approval-gated calls work out of the box via the approve-page link at
`https://approval.example.com`. For one-click Slack cards, on the VM:
`sudo cp security/approval/service/env.example security/approval/service/.env`,
follow the Slack-app steps inside it (Interactivity Request URL:
`https://approval.example.com/slack/interact`), and `up -d` again.

## 7. Verify + connect Claude

From anywhere:

```bash
curl -sD - -o /dev/null https://xmcp.example.com/mcp | grep -i www-authenticate
# must print: WWW-Authenticate: Bearer ... resource_metadata=...
```

Then Claude → Settings → Connectors → Add custom connector → each URL from
`pulumi stack output connectorUrls` → Connect → Google login.

## Day 2

- **Update the deployment:** bump `repoRef` (or push to your fork's branch) and
  `pulumi up` — user-data changes replace the VM (state volumes are on the VM,
  so exported lake data etc. rebuilds; treat the VM as disposable). For a
  code-only refresh in place: SSM in, `git -C /opt/mcp-tools pull`, `up -d
  --build`.
- **Tear down the compute:** `pulumi destroy` in `deploy/aws` (VM, guardrail,
  IAM, parameters). The tunnel + DNS live on in `deploy/cloudflare` — a local
  deployment can pick them right up, and `pulumi destroy` there retires them
  for good.
- **Costs:** t3.large ≈ $60/mo + EBS (60 GB gp3 ≈ $5/mo) + Bedrock Guardrails
  per-scan (prompt-attack policy, fractions of a cent per screened result).

## Troubleshooting

- **Step 1 fails on Cloudflare** — the API token needs both
  `Cloudflare Tunnel:Edit` (account-scoped) and `DNS:Edit` on the zone.
- **Step 3 fails resolving `cloudflareStack`** — both stacks must live on the
  same Pulumi backend, and the name must be fully qualified
  (`organization/mcp-tools-cloudflare/<stack>`).
- **Stack up, connector says "couldn't connect"** — on the VM check
  `sudo docker compose ps` (is `cloudflared` up?) and
  `sudo docker compose logs cloudflared` (creds/route errors appear here).
- **Guardrail container unhealthy in bedrock mode** — its startup warmup
  validates region, guardrail id, IAM, and the egress path in one call;
  `sudo docker compose logs guardrail` names the failing piece. The egress
  allowlist is region-pinned at boot; if you changed regions by hand, fix
  `security/egress-proxy/allowlist/guardrail.txt` and
  `sudo docker compose restart egress guardrail`.
- **Auth/OAuth issues** — identical to local; see the
  [local runbook's troubleshooting](local.md#troubleshooting).
