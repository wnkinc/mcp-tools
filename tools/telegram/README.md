# telegram (:8063)

[chigwell/telegram-mcp](https://github.com/chigwell/telegram-mcp) — a Telethon
**user-account** MTProto client exposed as MCP tools — running behind this repo's
shared security stack. The engine is a source checkout pinned by commit + sha256
in the Dockerfile (the PyPI name `telegram-mcp` belongs to an unrelated project;
the engine's own install guard refuses it). It speaks stdio as a child process;
`server.py` fronts it with a fastmcp proxy so `serve()` applies auth and the
guardrail exactly as for a native tool.

## Posture

- **Read-only by default** (`TELEGRAM_EXPOSED_TOOLS=read-only`, enforced as the
  child-env default in `server.py`): the engine's 49 `readOnlyHint` tools — read
  chats/messages/contacts, search. No send, delete, join, or admin ops. Widening
  to `all` is a deliberate `.env` change; pair it with `MCP_REQUIRE_APPROVAL=1`.
- **Guardrail-screened output** (`untrusted_output=True`): message content from
  arbitrary chats is a prompt-injection vector, same class as xmcp's web content.
- **Egress**: MTProto dials Telegram DC IPs directly, so this tool's squid
  listener (:3131) carries the repo's first dst-CIDR allowlist (Telegram's
  published DC ranges) next to the usual domain list; Telethon is pointed at the
  wall via the engine's `TELEGRAM_PROXY_*` http-CONNECT support (`python-socks`).

## Setup (out-of-band, once)

1. API credentials: <https://my.telegram.org> → "API development tools" →
   `TELEGRAM_API_ID` + `TELEGRAM_API_HASH`.
2. Session string (interactive phone login, on a trusted machine):
   `uvx --from git+https://github.com/chigwell/telegram-mcp telegram-mcp-generate-session`
3. `cp env.example .env`, fill in the three values (plus Google OAuth for public).

The session string is full account access — read everything, send as you. It
lives only in the gitignored `.env`.

## Tests

`pytest tools/telegram` — no Telegram, no network: child-env overrides, schema
strip, and proxy forwarding against a dummy stdio child.







Now Part 2, the session string. I've already done the prep for you: cloned the generator to
  ~/tg-session-gen (verified it's the exact same commit we pinned in the Dockerfile, b2f71f1) and
  installed its dependencies. All that's left is the interactive login, which needs a real terminal
  (it draws a QR code and prompts) — so run this in a separate terminal window, not through me:

  cd ~/tg-session-gen
  set -a; source ~/Documents/mcp-tools/tools/telegram/.env; set +a
  uv run session_string_generator.py --qr

  The middle line loads your TELEGRAM_API_ID/TELEGRAM_API_HASH straight from the .env you just
  filled, so the generator won't ask for them.

  What happens next:

  1. It prints a QR code in the terminal. On your phone: Telegram → Settings → Devices → Link Desktop
  Device → scan it. (QR expires after a bit; it redraws automatically, up to 10 times.)
  2. If you have a 2FA cloud password set, it'll prompt for that.
  3. When it asks for an account label, just press Enter (empty = the single-account default we
  scoped).
  4. It prints the session string — a long base64-looking blob. Copy the whole thing into
  tools/telegram/.env as TELEGRAM_SESSION_STRING=<paste> (no quotes needed).
