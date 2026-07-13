# Gatekeeper — tool approval modes

Every tool on every connector runs in one of three **modes**. You set them at
runtime, from chat; nothing needs a code change or a redeploy.

| Mode | What it does |
| --- | --- |
| `always_allow` | Runs with no approval. **The default for any tool** until you change it. |
| `needs_approval` | Each call is gated behind an out-of-band human approval (the approval sidecar posts an Approve/Deny card to your channel — Slack, Discord, or Telegram). |
| `blocked` | Disabled. Calls refuse immediately, and the tool disappears from Claude's tool list on the next connector refresh. |

## How it fits together

```
gatekeeper tool ──► approval sidecar (sole authority on modes) ◄── every tool server
   set_gating          /gating  (mode overrides, per source+tool)      reads modes live
   manage_tools        /catalog (each server's tool list + read/write) (15s TTL cache)
   (widget)            /manage/<token>  (the panel's data + save API)
                       state.json on a volume  (choices survive restarts)
```

- **The sidecar is the only place modes live.** There is no allowlist in code. A tool
  with no stored choice is `always_allow` — deliberately ship-open, so a fresh install
  has a working connector; you curate from there.
- **The panel shows what this deployment serves.** Each approval-enabled server
  registers its catalog with the sidecar **at startup** (its container health probe
  hits `serve()`'s `/healthz`, which re-beacons every ~30s) — so a freshly deployed
  tool appears in the panel, and can be pre-gated, before Claude ever connects to it.
  The catalog carries name, description, and a read-only flag from the tool's MCP
  annotations, with the spec default applied: only an explicit `readOnlyHint: true`
  is read-only — the same rule Claude's own connector UI groups by.
- **"Last used" is the Claude-side signal.** The server can't know what's attached to
  a claude.ai account (removal is silent, there's no account API), so each connector
  section carries an honest label instead: the last time a real authenticated client
  did `tools/list` ("last used 2h ago" / "never used"). Startup re-registration
  deliberately doesn't count as use.
- **Forget** removes a connector's stored state (catalog, modes, timestamps) — the
  per-section button in the panel, applied on Save. A still-deployed server simply
  re-registers on its next health probe; one you removed from `COMPOSE_PROFILES` (or
  detached in Claude and no longer want listed) stays gone. Forgetting deletes stored
  modes, so a returning connector starts back at ship-open `always_allow`.
- **Enforcement is immediate (~15s cache); invisibility lags.** A blocked or gated
  tool is enforced within seconds, but Claude keeps showing a blocked tool in its list
  until the connector's cached `tools/list` refreshes.

## The surfaces

- **`manage_tools`** opens the **permissions panel** in chat: one collapsible section
  per connector (telegram, xmcp, …), tools grouped read-only / write-delete, each with
  an always-allow / needs-approval / blocked control. Review, then **Save** —
  one save can span connectors. The human's click is the authorization, so a save takes
  no approval card. Sessions are **one-shot**: after a save the panel locks (its
  snapshot is stale the moment modes change) — call `manage_tools` again for a fresh
  view and another round. (See `security/approval/manage_widget.py` and
  `security/approval/widgets/manage.html`.)
- **`set_gating(tool, mode, source)`** is the conversational path — change one tool by
  name. It is itself **pinned to `needs_approval` in code** (`_PINNED` in the sidecar):
  changing a safety gate always takes a human approval, and no runtime path — tool or
  widget — can lift that pin.

- **`deploy_status()`** is the read-only deployment inventory: deployed tools (live
  startup beacons) with last-used, stale leftovers, and undeployed tools from the
  codebase — each described by its `tools/<name>/deploy.json` manifest (summary, the
  secrets enabling it needs and where to get them, notes like image size). The agent
  uses it to answer "what else could I add?" and to walk a manual enable; chat-driven
  deploys build on it in a later phase.

The gatekeeper itself is **not manageable**: the sidecar refuses every mode write
against the `gatekeeper` source and leaves it out of the panel. Its tools' behavior
is fixed in code — `set_gating` always needs approval, and `manage_tools` only ever
applies what a human reviews and saves.

The panel marks each connector **deployed** (a startup beacon arrived within ~90s) or
**stale**, and Forget only appears on stale ones — forgetting a live tool would be a
self-defeating 30-second blip that only wipes its curation.

## Developing the panel

`python3 scripts/preview-widget.py` serves the real widget at
`http://127.0.0.1:8123` against canned data, with claude.ai and the sidecar stubbed —
edit `manage.html`, reload the browser. `?theme=dark` previews dark mode; Save is
sandboxed.

## Deploy notes

- **State volume.** The sidecar persists modes to `APPROVAL_STATE_FILE`
  (`docker-compose.yml` mounts the `approval-state` volume at `/app/state`). Without it,
  choices are memory-only and reset on restart.
- **Tunnel routing.** The public overlay exposes the sidecar **only** on its
  human-facing paths (`/approve/<token>`, `/manage/<token>`, the signed provider
  webhooks). The internal write endpoints (`/gate`, `/gating`, `/catalog`, minting a
  manage token) stay on the compose network. Changing that routing needs a
  `cloudflared` container recreate (`docker compose ... up -d`).
- **After changing a widget**, the `ui://` resource URI is content-hashed, so refresh
  the gatekeeper connector in claude.ai to pick up the new panel.
