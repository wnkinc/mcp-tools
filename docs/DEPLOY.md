# Deploying mcp-tools — choose your path

One stack, two ways to host it. Every path runs the same docker-compose files —
tools on a sealed internal network, egress through the squid allowlist wall,
Cloudflare Tunnel ingress, Google OAuth in each server — so a deployment can move
between paths later by re-running the other runbook with the same domain.

| | **Local** ([runbook](deploy/local.md)) | **AWS** ([runbook](deploy/aws.md)) |
|---|---|---|
| Host | your own Linux box | EC2 VM (default t3.small), created by `pulumi up` |
| Guardrail | `llamafirewall` (default) — local model, needs a HF token | `bedrock` — Amazon Bedrock Guardrails API, always on |
| Admin access | it's your machine | SSM Session Manager (zero inbound ports) |
| Cost | your hardware + electricity | ~$15/mo (t3.small) + EBS + Bedrock per-scan |

Ingress is identical in both: the [deploy/cloudflare](../deploy/cloudflare/)
Pulumi stack owns the tunnel + wildcard DNS, so a deployment can change hosts
while keeping its domain, tunnel, and credentials.

## Decisions to make first (both paths)

1. **Which tools** — each is a compose profile named after itself; the README's
   ["The tools" table](../README.md#the-tools) is the menu (each tool's
   `deploy.json` manifest carries its sizing/dependency notes). Start small;
   adding a tool later is an `.env` edit + `up`.
2. **Guardrail on or off** — the output screen for the untrusted-output
   tools. Each path picks its natural provider (table above). On the AWS
   path it's always on; locally, on is the default and off skips the HF setup —
   set it off only if you accept unscreened external content reaching your
   model context.
3. **Approvals — the human-in-the-loop layer.** The server-side version of
   Claude's per-tool "always allow / needs approval / blocked": the desktop
   toggle is sticky (approve once and it sticks across every chat) and doesn't
   reliably apply to custom connectors, so the stack owns the gate. It's on by
   default, and every tool starts **`always_allow`** (Claude's own permission
   UI is the first defense line) — you then gate or block individual tools at
   runtime via the gatekeeper's panel or `set_gating`
   ([docs/GATEKEEPER.md](GATEKEEPER.md)); nothing needs a redeploy. A gated
   call posts an Approve/Deny card to your channel: `APPROVAL_PROVIDER` =
   `slack`, `discord`, or `telegram` in the sidecar's `.env`. A channel **must**
   be configured for gated calls to proceed — without one they report the
   approval as undeliverable, never silently run. Pick a platform the agent
   does **not** operate: a card its own tools can read and click is a gate that
   approves itself. `MCP_REQUIRE_APPROVAL=0` in the root `.env` removes the
   layer entirely.

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
