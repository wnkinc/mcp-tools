"""mcp-tools ingress identity: one Cloudflare Tunnel + one wildcard DNS record.

This stack is deployment-path-agnostic — the local runbook and the AWS stack both
consume it, so a deployment can change hosts (your box today, an EC2 VM tomorrow)
while keeping the same domain, tunnel, and credentials. Routing stays in the
committed docker-compose.tunnel.yml; the resources here are transport identity
only. `pulumi destroy` removes both.

Config (pulumi config set <key> <value>):
  cloudflareAccountId  (required)  zone Overview page
  cloudflareZoneId     (required)  zone of your MCP_DOMAIN
Cloudflare API token (Cloudflare Tunnel:Edit + DNS:Edit on the zone):
  CLOUDFLARE_API_TOKEN env or `pulumi config set cloudflare:apiToken --secret`.

Outputs:
  tunnelId   -> TUNNEL_ID in the root .env
  credsJson  -> (secret) the JSON cloudflared mounts:
                pulumi stack output credsJson --show-secrets \
                  > ../../security/ingress/secrets/creds.json
"""

from __future__ import annotations

import base64

import pulumi
import pulumi_cloudflare as cloudflare
import pulumi_random as random_

cfg = pulumi.Config()
account_id = cfg.require("cloudflareAccountId")
zone_id = cfg.require("cloudflareZoneId")

name = f"mcp-tools-{pulumi.get_stack()}"

tunnel_secret = random_.RandomPassword(f"{name}-tunnel-secret", length=48, special=False)
tunnel_secret_b64 = tunnel_secret.result.apply(lambda s: base64.b64encode(s.encode()).decode())

tunnel = cloudflare.ZeroTrustTunnelCloudflared(
    f"{name}-tunnel",
    account_id=account_id,
    name=name,
    secret=tunnel_secret_b64,
    config_src="local",  # ingress rules come from the repo's compose overlay
)

# One wildcard record covers every current and future tool subdomain. DNS does no
# security work in this stack: the committed tunnel overlay is the allowlist of
# what's actually served, and cloudflared answers 404 for unrouted hostnames.
wildcard = cloudflare.Record(
    f"{name}-wildcard",
    zone_id=zone_id,
    name="*",
    type="CNAME",
    content=tunnel.id.apply(lambda i: f"{i}.cfargotunnel.com"),
    proxied=True,
    ttl=1,  # proxied records are always TTL "auto"
)

pulumi.export("tunnelId", tunnel.id)
pulumi.export(
    "credsJson",
    pulumi.Output.secret(
        pulumi.Output.json_dumps(
            {"AccountTag": account_id, "TunnelSecret": tunnel_secret_b64, "TunnelID": tunnel.id}
        )
    ),
)
