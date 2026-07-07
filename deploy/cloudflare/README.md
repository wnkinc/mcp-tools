# deploy/cloudflare — the ingress identity, shared by every path

One `pulumi up` creates the stack's Cloudflare Tunnel and the wildcard DNS
record. Both deployment paths consume it — the local runbook writes its
`credsJson` output to `security/ingress/secrets/creds.json`, the AWS stack reads
it via a StackReference — so switching hosts later keeps the same domain,
tunnel, and credentials. `pulumi destroy` removes both resources.

## Quickstart

```bash
cd deploy/cloudflare
python3 -m venv venv && venv/bin/pip install -r requirements.txt
pulumi login --local              # state as a local file; any shared backend works too
pulumi stack init prod            # pick a passphrase (PULUMI_CONFIG_PASSPHRASE)
pulumi config set cloudflareAccountId <id>      # zone Overview page
pulumi config set cloudflareZoneId <zone-id>
export CLOUDFLARE_API_TOKEN=<token with Cloudflare Tunnel:Edit + DNS:Edit>
pulumi up
```

Then hand the outputs to your deployment:

```bash
pulumi stack output tunnelId                     # -> TUNNEL_ID in the root .env (local path)
pulumi stack output credsJson --show-secrets \
  > ../../security/ingress/secrets/creds.json    # local path; the AWS stack reads it itself
```

The AWS stack points at this one with
`pulumi config set cloudflareStack organization/mcp-tools-cloudflare/prod`
(both stacks on the same backend). Full runbooks:
[docs/deploy/local.md](../../docs/deploy/local.md),
[docs/deploy/aws.md](../../docs/deploy/aws.md).
