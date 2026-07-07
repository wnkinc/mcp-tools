# Ingress (Cloudflare tunnel)

The tunnel's **routing** lives in the `configs:` block of `docker-compose.tunnel.yml`
at the repo root -- compose interpolates your `MCP_DOMAIN` / `TUNNEL_ID` from the root
`.env` and mounts the rendered config into the cloudflared sidecar, so the committed
routing stays generic while nothing public runs that isn't in the repo.

This directory holds only the **secret half**: the tunnel's credentials JSON, staged at

    security/ingress/secrets/creds.json     # gitignored, never commit

Get it from the shared deploy/cloudflare Pulumi stack (docs/deploy/local.md
step 3):

    cd deploy/cloudflare
    pulumi stack output credsJson --show-secrets > ../../security/ingress/secrets/creds.json

The AWS path skips the manual staging -- its VM pulls the same JSON from SSM at
boot (see deploy/aws/).

Note: only one connector may run per tunnel. If a host `cloudflared` service already
serves this tunnel, stop it before bringing up the overlay -- two connectors with
different configs would split-route the same hostnames.
