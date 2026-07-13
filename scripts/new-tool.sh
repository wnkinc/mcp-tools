#!/usr/bin/env bash
# Stamp a new container-first mcp-tools server.
#
#   scripts/new-tool.sh <name> <port> [subdomain]
#   scripts/new-tool.sh weather 8065
#
# Creates tools/<name>/ (FastMCP server wired to the shared serve() helper, env.example,
# Dockerfile) + a per-tool egress allowlist, and inserts a service + state volume into
# docker-compose.yml. It does NOT touch Cloudflare, secrets, or the egress/ingress
# configs -- it prints the exact follow-up edits.
set -euo pipefail

NAME="${1:?usage: new-tool.sh <name> <port> [subdomain]}"
PORT="${2:?usage: new-tool.sh <name> <port> [subdomain]}"
ACL="${NAME//-/_}"   # squid acl names: hyphens -> underscores

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Default subdomain = <name>.<MCP_DOMAIN>, from the environment or the root .env
# (the same deployment-identity file the compose stack interpolates).
if [ -z "${MCP_DOMAIN:-}" ] && [ -f "$ROOT/.env" ]; then
  MCP_DOMAIN="$(grep -E '^MCP_DOMAIN=' "$ROOT/.env" | tail -1 | cut -d= -f2-)"
fi
SUBDOMAIN="${3:-${NAME}.${MCP_DOMAIN:-example.com}}"
DIR="$ROOT/tools/$NAME"
SQUID="$ROOT/security/egress-proxy/squid.compose.conf"
COMPOSE="$ROOT/docker-compose.yml"

[ -e "$DIR" ] && { echo "ERROR: $DIR already exists" >&2; exit 1; }

# Next egress listener port = highest http_port in the compose squid conf + 1.
EPORT="$(grep -oE '^http_port +[0-9]+' "$SQUID" | grep -oE '[0-9]+' | sort -n | tail -1)"
EPORT="$((EPORT + 1))"

mkdir -p "$DIR"

cat > "$DIR/server.py" <<PY
import os
import sys
from pathlib import Path

from fastmcp import FastMCP

# Make the repo root importable regardless of CWD, then load the shared serve()
# helper (applies OAuth + optional guardrail/approval, then runs).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from security.serve import serve  # noqa: E402

mcp = FastMCP(name="${NAME}")


@mcp.tool
def ping() -> str:
    """Health check; replace with real tools."""
    return "pong"


def main() -> None:
    port = int(os.getenv("MCP_PORT", "${PORT}"))
    # Trusted internal tool by default. If this tool returns UNTRUSTED external
    # content, add: untrusted_output=True (guardrail) and/or require_approval=True --
    # and add this tool's profile name to the guardrail service's \`profiles\` list
    # in docker-compose.yml so the screen starts with it automatically.
    serve(mcp, port=port)


if __name__ == "__main__":
    main()
PY

cat > "$DIR/requirements.txt" <<'TXT'
fastmcp==3.4.2
TXT

cat > "$DIR/env.example" <<ENV
# Container sets MCP_HOST/MCP_PORT/MCP_TRANSPORT; this file is secrets + posture only.
MCP_AUTH_ENABLED=1
# MCP_PUBLIC_URL is stamped by the tunnel overlay from the root .env's MCP_DOMAIN;
# set it here only when running outside compose.
#MCP_PUBLIC_URL=https://${SUBDOMAIN}
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
MCP_ALLOWED_GOOGLE_EMAILS=
ENV

sed -e "s|__NAME__|${NAME}|g" -e "s|__PORT__|${PORT}|g" \
    "$ROOT/scripts/templates/Dockerfile.template" > "$DIR/Dockerfile"

# Deploy manifest: what the gatekeeper's deploy_status (and the chat-driven deploy
# flow) reads to describe this tool -- fill in summary, secrets, and notes.
cat > "$DIR/deploy.json" <<JSON
{
  "title": "${NAME}",
  "profile": "${NAME}",
  "subdomain": "${NAME}",
  "port": ${PORT},
  "summary": "TODO: one sentence on what this tool does.",
  "secrets": [
    {"key": "TODO_API_KEY", "label": "TODO human name", "hint": "TODO where to get it"}
  ],
  "notes": [],
  "depends": []
}
JSON

# Per-tool egress allowlist stub (locked down by default -- add only what it needs).
ALLOW="$ROOT/security/egress-proxy/allowlist/${NAME}.txt"
cat > "$ALLOW" <<TXT
# ${NAME} egress allowlist (squid dstdomain). One host per line; leading dot = subdomains.
# Add ONLY hosts this tool must reach (plus the Google OAuth hosts if it serves Claude:
# accounts.google.com, oauth2.googleapis.com, www.googleapis.com). Discover misses from
# squid TCP_DENIED in the egress log (docker compose exec egress tail /var/log/squid/access.log).
TXT

# Insert the compose service + state volume into docker-compose.yml.
python3 - "$COMPOSE" "$NAME" "$EPORT" <<'PY'
import sys
path, name, eport = sys.argv[1], sys.argv[2], sys.argv[3]
src = open(path).read()
if f"\n  {name}:\n" in src:
    print(f"  (docker-compose.yml already has a {name} service; skipped)"); sys.exit(0)
service = f"""  {name}:
    build:
      context: .
      dockerfile: tools/{name}/Dockerfile
    image: mcp-{name}
    profiles: ["{name}"]   # opt-in: runs only when listed in COMPOSE_PROFILES (root .env)
    restart: unless-stopped
    env_file:
      - path: tools/{name}/.env
        required: false
    environment:
      MCP_HOST: 0.0.0.0
      MCP_TRANSPORT: http
      MCP_AUTH_ENABLED: "0"
      HTTPS_PROXY: http://egress:{eport}
      HTTP_PROXY: http://egress:{eport}
      NO_PROXY: localhost,127.0.0.1
    volumes:
      - {name}-state:/app/state
    networks:
      - internal
    depends_on:
      egress:
        condition: service_started

"""
if "\nnetworks:\n" not in src or "\nvolumes:\n" not in src:
    print("  (couldn't find networks:/volumes: anchors; add the service manually)"); sys.exit(0)
out, did_svc, did_vol = [], False, False
for line in src.splitlines(keepends=True):
    if not did_svc and line.startswith("networks:"):
        out.append(service); did_svc = True
    out.append(line)
    if not did_vol and line.startswith("volumes:"):
        out.append(f"  {name}-state:\n"); did_vol = True
open(path, "w").write("".join(out))
print("  inserted service + state volume into docker-compose.yml")
PY

cat <<DONE
Created tools/${NAME}/ (server.py, requirements.txt, env.example, Dockerfile) + egress
allowlist, and wired a compose service (MCP :${PORT}, egress via squid :${EPORT}).

Finish wiring it (all in-repo):
  1. Lock deps:  uv pip compile tools/${NAME}/requirements.txt --generate-hashes \\
                   --python-version 3.12 -o tools/${NAME}/requirements.lock
  2. Tests + CI: write tools/${NAME}/test_${ACL}.py (thin server tests; copy an
     existing tool's), then wire CI in .github/:
       - workflows/ci.yml: a pytest matrix entry (tests: tools/${NAME}, lock: its lock)
         and tools/${NAME} in the compose-validate '.env stub' loop
       - dependabot.yml: /tools/${NAME} under the pip 'directories:' list
  3. Egress: add a listener to security/egress-proxy/squid.compose.conf (before 'deny all'):
         http_port ${EPORT} name=${NAME}
         acl port_${ACL} myportname ${NAME}
         acl dom_${ACL}  dstdomain "/etc/squid/allowlist/${NAME}.txt"
         http_access allow port_${ACL} CONNECT dom_${ACL}
     and put this tool's allowed hosts in security/egress-proxy/allowlist/${NAME}.txt
  4. Ingress: in docker-compose.tunnel.yml add a route to the cloudflared config
     (the configs: block, above the 404):
         - hostname: ${NAME}.\${MCP_DOMAIN}
           service: http://${NAME}:${PORT}
     and a service entry flipping its public posture:
         ${NAME}:
           environment:
             MCP_AUTH_ENABLED: "1"
             MCP_PUBLIC_URL: https://${NAME}.\${MCP_DOMAIN}
     DNS: already covered by the wildcard record the deploy/cloudflare stack owns.
     Route changes apply on cloudflared RECREATE (config renders at up), and a
     changed squid.compose.conf needs an egress RESTART (single-file bind mounts
     go stale on git pull):
         docker compose -f docker-compose.yml -f docker-compose.tunnel.yml \\
           up -d --force-recreate cloudflared
         docker compose restart egress
  5. Secrets: cp tools/${NAME}/env.example tools/${NAME}/.env  (fill Google creds; set
     MCP_AUTH_ENABLED=1 for public), and add https://${SUBDOMAIN}/auth/callback to the
     shared Google OAuth client's Authorized redirect URIs.
  6. Enable it: add ${NAME} to COMPOSE_PROFILES in the root .env (and in env.example's
     'Available:' list so downstream deployers can opt in).
  7. Bring it up:  docker compose up -d --build ${NAME}
     (naming the service auto-enables its profile for this command only; step 6 is what
     includes it in a plain 'up')
  8. Add the custom connector https://${SUBDOMAIN}/mcp in Claude (desktop + web).
DONE
