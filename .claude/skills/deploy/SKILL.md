---
name: deploy
description: Drive a deployment of this stack end-to-end — local box or AWS VM, behind Cloudflare Tunnel with Google OAuth. Use when the user says "deploy this", "set this up", "make this public", "put this on AWS", or asks to stand up / go live with the MCP tools. Claude does every step it can; the user only decides, approves spend, and does the account/browser steps.
---

# Deploy this stack

You are driving the deployment. The user is the approver and does only the
steps that genuinely require a human (owning accounts, minting tokens,
approving spend). Everything else — installing CLIs, Pulumi stacks, watching
boot logs, writing env files, verification — is your job. Do not hand the
user a list of commands to run; run them.

**Source of truth:** `docs/DEPLOY.md` (the chooser) and the runbook it picks —
`docs/deploy/local.md` or `docs/deploy/aws.md`. Read the chooser and the
chosen runbook fully before acting. This skill is protocol (who does what,
when to stop); the runbooks own the actual commands. If this file and a
runbook disagree on a command, the runbook wins.

## Execution environment (decide silently; never ask)

Notice where you are running before Phase 0. The user is never asked about
any of this — it changes what *you* do, not what they decide.

- **User's machine** (Claude Code in a terminal): everything below applies
  as written, including the `!` prefix for user-run commands.
- **Hosted sandbox** (Cowork, Claude Code on the web): commands run in an
  ephemeral VM that is not the user's machine and does not survive the
  session.
  - **Local path: stop.** The stack must run on the user's own box and you
    cannot bring it up there from here. Say so, point them at Claude Code in
    a terminal on the target machine, and offer the AWS path as the
    alternative you *can* drive.
  - **AWS path: fine** — the VM is created in AWS, not where you're standing
    — but the Pulumi state rule below is mandatory, and the `!` prefix does
    not exist here (the secrets protocol's "user runs it themselves" route
    becomes "user puts the value in the file/console themselves").

**Pulumi state — pick silently, never ask the user:**

1. `pulumi whoami` succeeds → a backend is already configured; use it,
   don't switch it.
2. User's machine, no backend → `pulumi login --local`.
3. Sandbox, AWS path → state must outlive the sandbox: create a small S3
   bucket with the AWS credentials already in hand and
   `pulumi login s3://<bucket>` **before** creating any resources. Never
   sign the user up for Pulumi Cloud to solve this.

Both stacks (`deploy/cloudflare` and `deploy/aws`) must live on the **same
backend** — the AWS stack reads the ingress stack via a StackReference.

## Phase 0 — decisions (one question round)

Ask the user, in a single round, the decisions `docs/DEPLOY.md` lists:

1. **Path** — local box or AWS.
2. **Tools** — xmcp, data, lean, telegram (lean requires data and, on AWS,
   a bigger disk — check the runbook's sizing note).
3. **Guardrail** — default to the path's natural provider (llamafirewall
   local, bedrock on AWS). Only surface this if they ask or pick tools that
   don't need it.
4. **Approvals** — only if the picked tools include a gated one (xmcp,
   telegram): write actions on those tools require out-of-band human
   approval, delivered as an Approve/Deny card to an approval channel
   (Slack or Discord; telegram planned). Ask: set up a channel (a free
   Slack/Discord app, ~5-10 browser minutes, done while other steps run),
   or run this deploy with approvals off (`MCP_REQUIRE_APPROVAL=0` in the
   root `.env`)? There is no third option: gated tools with no channel fail
   every gated call as "approval undeliverable". If they choose off, say
   plainly that write actions on those tools will then run ungated.
   When they pick the channel, explain the point of the control: approval
   is HUMAN-in-the-loop, so it must live on a platform (or at least an
   account) the agent does not operate — if the agent's own tools can read
   the card and press its buttons, the gate can approve itself. Steer them
   to whichever of Slack/Discord their agent doesn't touch.

## Phase 1 — preflight

**You check and install tooling.** Detect what's already present before
installing anything (`pulumi version`, `aws sts get-caller-identity`,
`docker --version`, `session-manager-plugin` — per the runbook's prereq
list). Install what's missing yourself; tell the user what you installed.

**Then give the user ONE short checklist** — only the deployment-level items
a human must produce, nothing else:

- A **domain on Cloudflare** (free plan is fine) and, from its zone Overview
  page, the **Zone ID** and **Account ID**.
- A **Cloudflare API token** with `Cloudflare Tunnel:Edit` + `DNS:Edit` on
  that zone.
- **AWS path only:** working AWS credentials on this machine. If they need to
  log in interactively, have them run it themselves with the `!` prefix
  (e.g. `! aws configure` or `! aws sso login`).

Do **not** ask for per-tool API keys or the Google OAuth client here. Those
belong to later runbook steps and you only collect them for the tools the
user actually picked, when the runbook reaches them.

## Human-step protocol

When the runbook hits a step only the user can do (Cloudflare dashboard,
Google Cloud console, buying a domain):

- Give the exact click-path from the runbook and say precisely which values
  you need back (names, not secrets, in chat — see below).
- If the user is stuck, offer to help directly: with the Claude Chrome
  extension connected, you can navigate the browser with them and do or
  verify the clicks. Mention this option; don't assume it's available.
- Where possible, parallelize: e.g. the Google OAuth client (AWS runbook
  step 4) can be created while the VM's first boot is still building images.

## Secrets protocol

This governs how you **ask**, not just how you react to a paste.

- Split every request into public identifiers vs secrets. Client IDs,
  account/zone IDs, emails, domains, instance IDs are fine in chat — ask
  for those plainly. Client secrets, API keys, bearer tokens, session
  strings are not; a Telegram session string is full account access.
- When you need a secret, lead with the route that keeps it out of the
  chat and make that the default ask: on the user's machine, the `!`
  prefix (`! pulumi config set --secret ...`) or the user editing the
  `.env` in their own editor while you wait. Never phrase it as "send me
  the secret".
- **But do not refuse pasted secrets.** Some users will want to do
  everything in the chat. Say once that pasting a secret into the
  conversation is not recommended (it persists in the conversation history)
  — then, if they paste it anyway, use it: put it exactly where the runbook
  says (`.env`, `pulumi config set --secret`), never repeat it back, never
  echo it in command output, and move on. It works; it's just not the
  recommended route.
- Never commit a secret. Never print one in logs or summaries.

## Spend gate

Before any command that creates billable resources (on AWS: `pulumi up` in
`deploy/aws`), state plainly what will be created and the recurring cost from
the runbook, and get an explicit yes. The Cloudflare ingress stack and
`pulumi preview` need no gate — they're free/read-only.

## Execution

- Follow the chosen runbook top to bottom. Announce each numbered step as
  you start it.
- After each step, run the runbook's verification (or the obvious one:
  `pulumi stack output`, `docker compose ps`, the `curl ... WWW-Authenticate`
  check) and show the user the evidence before moving on. Never advance past
  a failed check — use the runbook's Troubleshooting section first.
- On AWS, first boot takes several minutes: watch it for real over the SSM
  session (`cloud-init-output.log`, then `docker compose ps`) rather than
  assuming it worked.
- Per-tool secrets step: read `tools/<tool>/env.example` for each chosen
  tool and collect only those values, following the secrets protocol above.
- Approval-channel step (gated tools + channel chosen in Phase 0): drive the
  runbook's Approvals section. Both walkthroughs live in
  `security/approval/service/env.example` — Slack (create app → `chat:write`
  scope → install → signing secret → private channel + invite bot →
  Interactivity Request URL `https://approval.<domain>/slack/interact`) or
  Discord (create app → bot token → public key → channel ID → Interactions
  Endpoint URL `https://approval.<domain>/discord/interact`, set LAST — the
  sidecar must be live when Discord validates it; also set
  `APPROVAL_PROVIDER=discord`). Collect the three values per the secrets
  protocol into `security/approval/service/.env`, and verify:
  the sidecar's `/healthz` (compose-internal, e.g.
  `docker compose exec approval python -c ...`) must show
  `"channel": "configured"`, and a test POST to its `/gate` must return
  `"notified": true` — that proves a card actually landed in their channel;
  have the user Deny it. If they opted out instead, set
  `MCP_REQUIRE_APPROVAL=0` in the root `.env` before `up`.

## Finish

- Run the verification curl for every deployed tool and show the
  `WWW-Authenticate` proof.
- Give the user the connector URLs and walk them through Claude → Settings →
  Connectors → Add custom connector → Google login.
- Tell them the two day-2 commands that matter: how to update the deployment
  and how to tear it down (`Day 2` section of the runbook).

## If the docs are wrong

If a runbook step fails, is out of order, or misled you, note it, work
around it, and keep going. At the end, list every doc problem you hit so it
can be fixed — the runbooks are meant to survive exactly this kind of run.
