# Deploying mcp-tools — choose your path

One stack, two ways to host it. Every path runs the same docker-compose files —
tools on a sealed internal network, egress through the squid allowlist wall,
Cloudflare Tunnel ingress, Google OAuth in each server — so a deployment can move
between paths later by re-running the other runbook with the same domain.

| | **Local** ([runbook](deploy/local.md)) | **AWS** ([runbook](deploy/aws.md)) |
|---|---|---|
| Host | your own Linux box | EC2 VM (default t3.large), created by `pulumi up` |
| Guardrail default | `llamafirewall` — local model, needs a HF token | `bedrock` — Amazon Bedrock Guardrails API |
| Admin access | it's your machine | SSM Session Manager (zero inbound ports) |
| Cost | your hardware + electricity | ~$60/mo (t3.large) + EBS + Bedrock per-scan |

Ingress is identical in both: the [deploy/cloudflare](../deploy/cloudflare/)
Pulumi stack owns the tunnel + wildcard DNS, so a deployment can change hosts
while keeping its domain, tunnel, and credentials.

## Decisions to make first (both paths)

1. **Which tools** — each is a compose profile: `xmcp`, `data`, `lean`,
   `telegram`. Start small; adding a tool later is an `.env` edit + `up`.
   (`lean` needs `data`, and its 13 GB base image wants a bigger disk.)
2. **Guardrail on or off** — the output screen for the untrusted tools (`xmcp`,
   `telegram`). On is the default and each path picks its natural provider
   (table above). Off skips the HF/Bedrock setup entirely — set it off only if
   you accept unscreened external content reaching your model context.
3. **Approvals: Slack or page-link** — human-in-the-loop for gated tool calls.
   Works out of the box via an approve page link; creating a Slack app adds
   one-click Approve/Deny cards. The Slack app is the only optional setup step.

## What every deployment needs (gathered up front)

- A **domain on Cloudflare** (free plan is fine), plus an **API token**
  (`Cloudflare Tunnel:Edit` + `DNS:Edit`). Each tool gets a subdomain.
- The **[Pulumi CLI](https://www.pulumi.com/docs/install/)** — it provisions
  the ingress stack on both paths (and the VM on AWS). `pulumi login --local`
  keeps state as a file on your machine; any shared backend works too.
- A **Google Cloud OAuth client** (free) — “Sign in with Google” gating every
  tool to your email allowlist. Created by hand in both paths; each runbook
  walks through it.
- **Per-tool secrets** (API keys etc.) — each tool documents its own
  `tools/<tool>/env.example`; deployment docs stay tool-agnostic.

Pick a column, open its runbook, and go top to bottom.
