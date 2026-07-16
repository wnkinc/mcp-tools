#!/bin/bash
# EC2 user-data for the mcp-tools VM. __main__.py renders this template by
# replacing the __UPPERCASE__ tokens.
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -yq docker.io docker-compose-v2 git python3-boto3
systemctl enable --now docker

git clone --depth 1 --branch __REPO_REF__ __REPO_URL__ /opt/mcp-tools
cd /opt/mcp-tools

# Tunnel credentials: SSM -> the gitignored path the compose overlay mounts.
# The read goes through python3-boto3: Ubuntu 24.04 dropped the `awscli` apt
# package, and the v2 bundle would mean curl|unzip-ing an installer at boot.
mkdir -p security/ingress/secrets
python3 -c "import boto3; print(boto3.client('ssm', region_name='__REGION__').get_parameter(Name='__CREDS_PARAM__', WithDecryption=True)['Parameter']['Value'])" \
  > security/ingress/secrets/creds.json
chown 1000:1000 security/ingress/secrets/creds.json
chmod 600 security/ingress/secrets/creds.json

# Profiles are tools only: the guardrail service carries the untrusted tools'
# profile names in compose, so it starts with them automatically.
cat > .env <<'ENVEOF'
COMPOSE_PROFILES=__PROFILES__
MCP_DOMAIN=__DOMAIN__
TUNNEL_ID=__TUNNEL_ID__
HOST_UID=1000
HOST_GID=1000
GUARDRAIL_ENABLED=1
GUARDRAIL_PROVIDER=bedrock
BEDROCK_GUARDRAIL_ID=__GUARDRAIL_ID__
BEDROCK_GUARDRAIL_VERSION=DRAFT
AWS_REGION=__REGION__
ENVEOF

# Per-tool secrets (tools/<tool>/.env) arrive later over SSM; required:false in
# compose lets the stack come up while a tool waits for its secrets.
docker compose -f docker-compose.yml -f docker-compose.tunnel.yml up -d --build
