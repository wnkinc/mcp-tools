"""mcp-tools on AWS: the same docker-compose stack as a local deploy, on one EC2 VM.

Ingress (the Cloudflare Tunnel + wildcard DNS) is the shared deploy/cloudflare
stack — `pulumi up` there first, then point `cloudflareStack` here at it. This
program consumes its `tunnelId`/`credsJson` outputs, so switching a deployment
between local and AWS keeps the same domain, tunnel, and credentials.

What this program owns (and `pulumi destroy` removes):
  - an EC2 instance (default: t3.small, 20 GB gp3) that boots docker, clones the
    repo at a pinned ref, renders the root .env, and brings up
    docker-compose.yml + docker-compose.tunnel.yml — behind a security group
    with zero inbound rules (tunnel + SSM agent dial out; admin access is SSM
    Session Manager)
  - the guardrail's cloud provider: an Amazon Bedrock Guardrail (prompt-attack
    filter only) + an instance role scoped to ApplyGuardrail on it
  - SSM SecureString parameters carrying the two boot secrets (tunnel credentials,
    optional HF token) — secrets stay out of user-data, which is API-readable

What stays manual (see docs/deploy/aws.md): the Google OAuth client, per-tool
.env files (dropped onto the VM over SSM), and the optional Slack app.

Config (pulumi config set <key> <value>):
  domain               (required)  parent domain, on Cloudflare
  cloudflareStack      (required)  StackReference to deploy/cloudflare, e.g.
                                   organization/mcp-tools-cloudflare/prod
                                   (same backend as this stack)
  tools                default "xmcp,telegram" — comma list of tool profiles
  guardrail            default "bedrock" — bedrock | llamafirewall | off
  hfToken              secret; llamafirewall mode only
  repoUrl              default: the upstream repo
  repoRef              default "main" — pin a tag/commit for reproducible deploys
  instanceType         default "t3.small" — fits the light tools; lean wants ≥ 8 GB RAM
  volumeGb             default 20 (lean's 13 GB base image wants ≥ 100)
  aws:region           the deploy region (bedrock guardrail lives here too)
"""

from __future__ import annotations

import json

import pulumi
import pulumi_aws as aws

UPSTREAM_REPO = "https://github.com/wnkinc/claude-custom-connector-server.git"

cfg = pulumi.Config()
domain = cfg.require("domain")
cloudflare_stack = cfg.require("cloudflareStack")
tools = [t.strip() for t in (cfg.get("tools") or "xmcp,telegram").split(",") if t.strip()]
guardrail_mode = cfg.get("guardrail") or "bedrock"
repo_url = cfg.get("repoUrl") or UPSTREAM_REPO
repo_ref = cfg.get("repoRef") or "main"
instance_type = cfg.get("instanceType") or "t3.small"
volume_gb = cfg.get_int("volumeGb") or 20
hf_token = cfg.get_secret("hfToken")

if guardrail_mode not in ("bedrock", "llamafirewall", "off"):
    raise ValueError(f"guardrail must be bedrock|llamafirewall|off, got {guardrail_mode!r}")

region = aws.get_region().name
stack = pulumi.get_stack()
prefix = f"mcp-tools-{stack}"

# --- ingress: consumed from the shared deploy/cloudflare stack ---------------------
cloudflare = pulumi.StackReference(cloudflare_stack)
tunnel_id = cloudflare.require_output("tunnelId")
creds_json = cloudflare.require_output("credsJson")  # secret in the source stack

# Staged for the VM as a SecureString so the secret rides SSM (KMS-encrypted,
# IAM-gated) instead of instance user-data.
creds_param = aws.ssm.Parameter(
    f"{prefix}-tunnel-creds",
    name=f"/{prefix}/tunnel-creds",
    type="SecureString",
    value=creds_json,
)

hf_param = None
if guardrail_mode == "llamafirewall" and hf_token:
    hf_param = aws.ssm.Parameter(
        f"{prefix}-hf-token",
        name=f"/{prefix}/hf-token",
        type="SecureString",
        value=hf_token,
    )

# --- guardrail: Bedrock (the cloud default) ---------------------------------------
guardrail = None
if guardrail_mode == "bedrock":
    withheld = "[guardrail: content withheld -- the screen flagged likely prompt-injection.]"
    guardrail = aws.bedrock.Guardrail(
        f"{prefix}-guardrail",
        name=prefix,
        description="mcp-tools output screen: prompt-attack filter only (parity with PromptGuard).",
        blocked_input_messaging=withheld,
        blocked_outputs_messaging=withheld,
        content_policy_config={
            "filters_configs": [
                {"type": "PROMPT_ATTACK", "input_strength": "HIGH", "output_strength": "NONE"}
            ]
        },
    )

# --- instance identity: SSM admin + exactly the two boot reads + ApplyGuardrail ---
role = aws.iam.Role(
    f"{prefix}-role",
    assume_role_policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "ec2.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ],
        }
    ),
)
aws.iam.RolePolicyAttachment(
    f"{prefix}-ssm-core",
    role=role.name,
    policy_arn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
)


def _inline_policy(args: dict) -> str:
    statements = [
        {
            "Effect": "Allow",
            "Action": "ssm:GetParameter",
            "Resource": [a for a in (args["creds_arn"], args["hf_arn"]) if a],
        }
    ]
    if args["guardrail_arn"]:
        statements.append(
            {
                "Effect": "Allow",
                "Action": "bedrock:ApplyGuardrail",
                "Resource": args["guardrail_arn"],
            }
        )
    return json.dumps({"Version": "2012-10-17", "Statement": statements})


aws.iam.RolePolicy(
    f"{prefix}-boot-and-guardrail",
    role=role.name,
    policy=pulumi.Output.all(
        creds_arn=creds_param.arn,
        hf_arn=hf_param.arn if hf_param else "",
        guardrail_arn=guardrail.guardrail_arn if guardrail else "",
    ).apply(_inline_policy),
)
profile = aws.iam.InstanceProfile(f"{prefix}-profile", role=role.name)

# --- the VM ------------------------------------------------------------------------
default_vpc = aws.ec2.get_vpc(default=True)
sg = aws.ec2.SecurityGroup(
    f"{prefix}-sg",
    vpc_id=default_vpc.id,
    description="mcp-tools: zero inbound (tunnel + SSM dial out); all egress",
    egress=[
        {
            "protocol": "-1",
            "from_port": 0,
            "to_port": 0,
            "cidr_blocks": ["0.0.0.0/0"],
            "ipv6_cidr_blocks": ["::/0"],
        }
    ],
)

ami_id = aws.ssm.get_parameter(
    name="/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
).value


def _user_data(a: dict) -> str:
    profiles = ",".join(tools + (["guardrail"] if guardrail_mode != "off" else []))
    env = [
        f"COMPOSE_PROFILES={profiles}",
        f"MCP_DOMAIN={domain}",
        f"TUNNEL_ID={a['tunnel_id']}",
        "HOST_UID=1000",
        "HOST_GID=1000",
        f"GUARDRAIL_ENABLED={'1' if guardrail_mode != 'off' else '0'}",
    ]
    if guardrail_mode == "bedrock":
        env += [
            "GUARDRAIL_PROVIDER=bedrock",
            f"BEDROCK_GUARDRAIL_ID={a['guardrail_id']}",
            "BEDROCK_GUARDRAIL_VERSION=DRAFT",
            f"AWS_REGION={region}",
        ]
    elif guardrail_mode == "llamafirewall":
        env.append("GUARDRAIL_PROVIDER=llamafirewall")
    env_body = "\n".join(env)

    # SSM reads go through python3-boto3: Ubuntu 24.04 dropped the `awscli` apt
    # package, and the v2 bundle would mean curl|unzip-ing an installer at boot.
    ssm_read = (
        "python3 -c \"import boto3; print(boto3.client('ssm', region_name='{region}')"
        ".get_parameter(Name='{name}', WithDecryption=True)['Parameter']['Value'])\""
    )

    hf_fetch = ""
    if a["hf_param"]:
        hf_fetch = (
            f'echo "HF_TOKEN=$({ssm_read.format(region=region, name=a["hf_param"])})" >> .env\n'
        )

    return f"""#!/bin/bash
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -yq docker.io docker-compose-v2 git python3-boto3
systemctl enable --now docker

git clone --depth 1 --branch {repo_ref} {repo_url} /opt/mcp-tools
cd /opt/mcp-tools

# Pin the guardrail's egress allowlist to this deploy's bedrock endpoint.
sed -i -E 's/bedrock-runtime\\.[a-z0-9-]+\\.amazonaws\\.com/bedrock-runtime.{region}.amazonaws.com/' security/egress-proxy/allowlist/guardrail.txt

# Tunnel credentials: SSM -> the gitignored path the compose overlay mounts.
mkdir -p security/ingress/secrets
{ssm_read.format(region=region, name=a["creds_param"])} \\
  > security/ingress/secrets/creds.json
chown 1000:1000 security/ingress/secrets/creds.json
chmod 600 security/ingress/secrets/creds.json

cat > .env <<'ENVEOF'
{env_body}
ENVEOF
{hf_fetch}
# Per-tool secrets (tools/<tool>/.env) arrive later over SSM; required:false in
# compose lets the stack come up while a tool waits for its secrets.
docker compose -f docker-compose.yml -f docker-compose.tunnel.yml up -d --build
"""


user_data = pulumi.Output.all(
    tunnel_id=tunnel_id,
    creds_param=creds_param.name,
    guardrail_id=guardrail.guardrail_id if guardrail else "",
    hf_param=hf_param.name if hf_param else "",
).apply(_user_data)

instance = aws.ec2.Instance(
    f"{prefix}-vm",
    ami=ami_id,
    instance_type=instance_type,
    vpc_security_group_ids=[sg.id],
    iam_instance_profile=profile.name,
    user_data=user_data,
    user_data_replace_on_change=True,  # boot script drift -> fresh VM, never a half-state
    metadata_options={
        "http_tokens": "required",
        # The guardrail container reaches IMDS through the egress proxy (one extra
        # network hop), so the default hop limit of 1 would drop the responses.
        "http_put_response_hop_limit": 2,
    },
    root_block_device={"volume_size": volume_gb, "volume_type": "gp3"},
    tags={"Name": prefix, "project": "mcp-tools"},
)

pulumi.export("instanceId", instance.id)
pulumi.export(
    "connect",
    pulumi.Output.concat("aws ssm start-session --target ", instance.id, " --region ", region),
)
pulumi.export("tunnelId", tunnel_id)
pulumi.export(
    "guardrailId", guardrail.guardrail_id if guardrail else "(guardrail: " + guardrail_mode + ")"
)
pulumi.export("connectorUrls", [f"https://{t}.{domain}/mcp" for t in tools])
pulumi.export("approvalUrl", f"https://approval.{domain}")
