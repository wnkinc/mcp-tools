#!/usr/bin/env python3
"""The deploy reconciler: the ONLY thing that turns a chat-approved deploy request
into a running container -- and it lives on the HOST, outside every container.

Why host-side: for a container to start/stop/build other containers it would need
the Docker socket, which is root-equivalent on the host -- the one thing this
stack's sealed-container model forbids. So the network side (approval sidecar) can
only WRITE A REQUEST, and this script, running where the operator's own hands run
`docker compose`, applies it.

Protocol (deploy/host/control/, each file has exactly ONE writer):
  request.json    written by the approval sidecar (uid 999) after a human approves
                  the gatekeeper's deploy_tool call: {"id", "action", "tool", ...}.
                  Overwritten per request, never deleted.
  status.json     written here: {"last_id", "tool", "action", "phase", "detail",
                  "updated"} -- phase is applying | done | failed. A request is
                  consumed when its id becomes last_id.
  inventory.json  written here each pass: {"tools": {name: {"deployed", "secrets_ready",
                  "missing_secrets"}}, "updated"} -- existence/emptiness checks only,
                  secret VALUES never leave the host.
  staging.json    the ONE exception to single-writer, and the one non-world-readable
                  file (uid 999, group <run-as>, mode 660): the sidecar writes a
                  secrets handoff {"id", "tool", "values"} from the in-chat secrets
                  widget, and this script consumes it -- validates the keys against
                  the tool's manifest, writes tools/<tool>/.env (0600), then blanks
                  the file to {} so values never rest here longer than one pass.

Validation is hard and local: only tools shipping a tools/<name>/deploy.json
manifest, only action "deploy", one request in flight at a time. The substrate
(approval, egress, guardrail, gatekeeper, cloudflared) has no manifest and can
never be targeted.

Usage:
  reconcile.py --init --repo /path/to/mcp-tools        (sudo; creates control files)
  reconcile.py --once --repo ...                        (one pass: inventory + apply)
  reconcile.py --watch --repo ...                       (loop; the systemd unit runs this)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# The approval sidecar's uid inside its container (security/approval/service/
# Dockerfile creates the system user "app"); request.json must be writable by it.
SIDECAR_UID = 999

COMPOSE_FILES = ["docker-compose.yml", "docker-compose.tunnel.yml"]
COMPOSE_TIMEOUT = 5400  # lean's first build pulls a ~13GB base image

PROFILE_RE = re.compile(r"^COMPOSE_PROFILES=(.*)$", re.MULTILINE)


def log(msg: str) -> None:
    print(f"reconcile: {msg}", flush=True)


def control_dir(repo: Path) -> Path:
    return repo / "deploy" / "host" / "control"


def read_json(path: Path) -> dict | None:
    """None on absent or torn file (plain overwrite-in-place writers): retry next pass."""
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data))


def load_manifests(repo: Path) -> dict[str, dict]:
    manifests = {}
    for path in sorted((repo / "tools").glob("*/deploy.json")):
        try:
            m = json.loads(path.read_text())
            manifests[m["profile"]] = m
        except (ValueError, KeyError):
            log(f"skipping malformed manifest {path}")
    return manifests


def env_values_from_text(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip()
    return values


def env_values(env_path: Path) -> dict[str, str]:
    """Naive KEY=value parse of a tool .env -- enough to check staging."""
    if not env_path.exists():
        return {}
    return env_values_from_text(env_path.read_text())


def set_env_value(text: str, key: str, value: str) -> str:
    """KEY=value into env-file text: replace the (possibly commented-out) line if
    present, else append. Replacement goes through a lambda (not a plain
    replacement string) so backslashes in the value land literally."""
    pattern = re.compile(rf"^#?\s*{re.escape(key)}=.*$", re.MULTILINE)
    if pattern.search(text):
        return pattern.sub(lambda _m: f"{key}={value}", text, count=1)
    return text.rstrip("\n") + f"\n{key}={value}\n"


def ensure_env(repo: Path, tool: str) -> Path:
    """tools/<tool>/.env, created 0600 from its env.example if absent."""
    env_path = repo / "tools" / tool / ".env"
    if not env_path.exists():
        template = repo / "tools" / tool / "env.example"
        env_path.write_text(template.read_text() if template.exists() else "")
        os.chmod(env_path, 0o600)
    return env_path


def shared_identity_values(repo: Path, exclude_tool: str) -> dict[str, str]:
    """The deployment-wide values (MCP-auth identity) from the first sibling tool
    .env that carries them all -- the source both kinds of seeding draw from."""
    for sibling in sorted((repo / "tools").glob("*/.env")):
        if sibling.parent.name == exclude_tool:
            continue
        values = env_values(sibling)
        if all(values.get(k) for k in SHARED_IDENTITY_KEYS):
            return values
    return {}


def resolve_default(secret: dict, shared: dict) -> str:
    """A manifest secret's default_from value, adapted where shapes differ
    (an email allowlist is a comma list; a single-user email is its first entry)."""
    source = secret.get("default_from", "")
    value = shared.get(source, "")
    if source == "MCP_ALLOWED_GOOGLE_EMAILS" and value:
        value = value.split(",")[0].strip()
    return value


def secrets_state(repo: Path, manifest: dict) -> tuple[bool, list[str]]:
    """(ready, missing_keys): every manifest secret has a non-empty value staged
    OR a resolvable default (default_from a deployment-wide value) -- those are
    filled at deploy time, so the user is never asked for what the stack knows."""
    values = env_values(repo / "tools" / manifest["profile"] / ".env")
    shared = shared_identity_values(repo, manifest["profile"])
    missing = [
        s["key"]
        for s in manifest.get("secrets", [])
        if not values.get(s["key"]) and not resolve_default(s, shared)
    ]
    return (not missing, missing)


def materialize_env(repo: Path, tool: str, manifest: dict) -> None:
    """Ensure tools/<tool>/.env exists and carries everything not user-supplied:
    the template, the shared MCP-auth identity, and manifest default_from fills."""
    env_path = ensure_env(repo, tool)
    text = seed_shared_identity(repo, tool, env_path.read_text())
    have = env_values_from_text(text)
    shared = shared_identity_values(repo, tool)
    filled = []
    for secret in manifest.get("secrets", []):
        key = secret["key"]
        if have.get(key):
            continue
        value = resolve_default(secret, shared)
        if not value:
            continue
        text = set_env_value(text, key, value)
        filled.append(key)
    env_path.write_text(text)
    os.chmod(env_path, 0o600)
    if filled:
        log(f"defaulted {', '.join(filled)} in tools/{tool}/.env from the shared identity")


def compose_cmd(repo: Path) -> list[str]:
    cmd = ["docker", "compose"]
    for f in COMPOSE_FILES:
        cmd += ["-f", str(repo / f)]
    return cmd


def running_services(repo: Path) -> set[str]:
    try:
        # S603: fixed argv -- docker compose with repo-local -f paths, no shell.
        out = subprocess.run(  # noqa: S603
            compose_cmd(repo) + ["ps", "--services", "--status", "running"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=repo,
            check=True,
        ).stdout
        return {s.strip() for s in out.splitlines() if s.strip()}
    except (subprocess.SubprocessError, OSError) as exc:
        log(f"compose ps failed: {exc}")
        return set()


def write_inventory(repo: Path, manifests: dict) -> None:
    running = running_services(repo)
    tools = {}
    for name, m in manifests.items():
        ready, missing = secrets_state(repo, m)
        tools[name] = {
            "deployed": name in running,
            "secrets_ready": ready,
            "missing_secrets": missing,
        }
    write_json(control_dir(repo) / "inventory.json", {"tools": tools, "updated": time.time()})


def add_profile(env_text: str, name: str) -> str:
    """COMPOSE_PROFILES gains `name` (idempotent); the line must already exist."""
    match = PROFILE_RE.search(env_text)
    if not match:
        raise ValueError("root .env has no COMPOSE_PROFILES line")
    profiles = [p.strip() for p in match.group(1).split(",") if p.strip()]
    if name not in profiles:
        profiles.append(name)
    return PROFILE_RE.sub(f"COMPOSE_PROFILES={','.join(profiles)}", env_text, count=1)


def process_request(repo: Path, req: dict, manifests: dict) -> dict:
    """Validate and apply one request; returns the final status record."""
    status = {
        "last_id": req.get("id", ""),
        "tool": req.get("tool", ""),
        "action": req.get("action", ""),
        "updated": time.time(),
    }

    def fail(detail: str) -> dict:
        log(f"request {req.get('id')} REFUSED: {detail}")
        return {**status, "phase": "failed", "detail": detail, "updated": time.time()}

    tool = req.get("tool", "")
    if req.get("action") != "deploy":
        return fail(f"unsupported action {req.get('action')!r} (this phase deploys only)")
    if tool not in manifests:
        return fail(f"{tool!r} is not a shipped tool (no tools/{tool}/deploy.json)")
    ready, missing = secrets_state(repo, manifests[tool])
    if not ready:
        return fail(f"secrets not staged for {tool}: missing {', '.join(missing)}")
    # Fill defaults (shared identity + manifest default_from) so a tool whose every
    # secret is deployment-derivable deploys with no staging step at all.
    materialize_env(repo, tool, manifests[tool])

    write_json(
        control_dir(repo) / "status.json",
        {
            **status,
            "phase": "applying",
            "detail": "compose up --build running",
            "updated": time.time(),
        },
    )
    env_path = repo / ".env"
    env_path.write_text(add_profile(env_path.read_text(), tool))
    log(f"applying: deploy {tool}")
    try:
        # S603: fixed argv; `tool` is validated against the shipped manifests above.
        result = subprocess.run(  # noqa: S603
            compose_cmd(repo) + ["up", "-d", "--build", tool],
            capture_output=True,
            text=True,
            timeout=COMPOSE_TIMEOUT,
            cwd=repo,
        )
    except subprocess.TimeoutExpired:
        return fail(f"compose up timed out after {COMPOSE_TIMEOUT}s")
    if result.returncode != 0:
        tail = (result.stderr or result.stdout).strip()[-500:]
        return fail(f"compose up exited {result.returncode}: {tail}")
    # `up -d` exiting 0 only means the container was CREATED -- a crash-looping tool
    # would otherwise report "done". Wait for its healthcheck (all tools probe
    # serve()'s /healthz) before believing it.
    err = wait_healthy(repo, tool)
    if err:
        return fail(err)
    log(f"deploy {tool}: done")
    return {
        **status,
        "phase": "done",
        "detail": f"{tool} is up and healthy",
        "updated": time.time(),
    }


HEALTHY_TIMEOUT = 180  # image start + first healthcheck window


def wait_healthy(repo: Path, tool: str) -> str | None:
    """None when the service reaches running+healthy; else a failure detail with logs.
    Reads HEALTHY_TIMEOUT at call time (tests shrink it via monkeypatch)."""
    timeout = HEALTHY_TIMEOUT
    deadline = time.time() + timeout
    last = "no status"
    while time.time() < deadline:
        try:
            # S603: fixed argv; `tool` was validated against the shipped manifests.
            out = subprocess.run(  # noqa: S603
                compose_cmd(repo) + ["ps", "--format", "json", tool],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=repo,
            ).stdout.strip()
            rec = json.loads(out.splitlines()[0]) if out else {}
            state, health = rec.get("State", ""), rec.get("Health", "")
            last = f"state={state or '?'} health={health or 'none'}"
            if state == "running" and health in ("healthy", ""):
                return None
        except (subprocess.SubprocessError, OSError, ValueError):
            pass
        time.sleep(min(5, max(0.05, timeout / 10)))
    # S603: fixed argv, validated tool name.
    logs = subprocess.run(  # noqa: S603
        compose_cmd(repo) + ["logs", "--tail", "12", tool],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=repo,
    ).stdout.strip()[-600:]
    return f"{tool} did not become healthy within {int(timeout)}s ({last}). Logs: {logs}"


# Every tool carries the same MCP-auth identity (the shared Google client that
# gates who may CONNECT Claude, and the operator's email allowlist) -- it is not a
# per-tool secret, so the staging form doesn't ask for it. Seed it from another
# tool's .env so a freshly staged tool can actually start (serve() fails closed
# without the allowlist).
SHARED_IDENTITY_KEYS = ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "MCP_ALLOWED_GOOGLE_EMAILS")


def seed_shared_identity(repo: Path, tool: str, text: str) -> str:
    """Fill empty shared-identity keys from the first sibling .env that has them all."""
    have = env_values_from_text(text)
    missing = [k for k in SHARED_IDENTITY_KEYS if not have.get(k)]
    if not missing:
        return text
    shared = shared_identity_values(repo, tool)
    if not shared:
        log(
            f"WARNING: no sibling .env carries the shared identity; tools/{tool}/.env is missing {missing}"
        )
        return text
    for key in missing:
        text = set_env_value(text, key, shared[key])
    log(f"seeded shared MCP-auth identity into tools/{tool}/.env")
    return text


def apply_staging(repo: Path, manifests: dict) -> None:
    """Consume a secrets handoff: manifest-validated keys -> tools/<tool>/.env (0600),
    then blank the handoff. Values are never logged and never rest here past one pass."""
    path = control_dir(repo) / "staging.json"
    staging = read_json(path)
    if not staging or not staging.get("id"):
        return
    tool, values = staging.get("tool", ""), staging.get("values") or {}
    manifest = manifests.get(tool)
    allowed = {s["key"] for s in (manifest or {}).get("secrets", [])}
    if manifest is None or not values or not set(values) <= allowed:
        log(f"staging {staging.get('id')} REFUSED (unknown tool or keys); discarded")
        write_json(path, {})
        return
    env_path = ensure_env(repo, tool)
    text = env_path.read_text()
    for key, value in values.items():
        text = set_env_value(text, key, value)
    env_path.write_text(text)
    os.chmod(env_path, 0o600)
    # User values are in; now fill everything the deployment already knows
    # (shared identity + manifest default_from) around them.
    materialize_env(repo, tool, manifest)
    write_json(path, {})  # consumed: values no longer at rest in the control dir
    log(f"staged {len(values)} secret value(s) into tools/{tool}/.env")


def run_once(repo: Path) -> None:
    manifests = load_manifests(repo)
    apply_staging(repo, manifests)
    write_inventory(repo, manifests)
    req = read_json(control_dir(repo) / "request.json")
    if not req or not req.get("id"):
        return
    prior = read_json(control_dir(repo) / "status.json") or {}
    if prior.get("last_id") == req["id"]:
        return  # already consumed
    final = process_request(repo, req, manifests)
    write_json(control_dir(repo) / "status.json", final)
    write_inventory(repo, manifests)  # reflect the new reality immediately


def init(repo: Path, run_as: str) -> None:
    """Create the control dir + single-writer files with the right owners (run as root).

    request.json belongs to the sidecar container's uid; status/inventory to the
    reconciler's user. Both are world-readable, nothing here is secret in this phase.
    """
    import pwd
    import shutil

    cdir = control_dir(repo)
    cdir.mkdir(parents=True, exist_ok=True)
    pw = pwd.getpwnam(run_as) if not run_as.isdigit() else None
    uid = pw.pw_uid if pw else int(run_as)
    gid = pw.pw_gid if pw else int(run_as)
    for name, owner in (
        ("request.json", SIDECAR_UID),
        ("status.json", uid),
        ("inventory.json", uid),
    ):
        path = cdir / name
        if not path.exists():
            path.write_text("{}")
        os.chown(path, owner, -1)
        os.chmod(path, 0o644)
    # staging.json carries secret values in transit: sidecar-owned so it can write,
    # run-as GROUP so the reconciler can read and blank it, and NOT world-readable.
    staging = cdir / "staging.json"
    if not staging.exists():
        staging.write_text("{}")
    os.chown(staging, SIDECAR_UID, gid)
    os.chmod(staging, 0o660)
    os.chown(cdir, uid, -1)
    # S103: 755 is the point -- the sidecar container (uid 999) must traverse the
    # dir to reach its request.json; the files above carry the single-writer perms.
    os.chmod(cdir, 0o755)  # noqa: S103
    print(f"initialized {cdir} (request.json -> uid {SIDECAR_UID}; rest -> {run_as})")
    if shutil.which("docker") is None:
        print("WARNING: docker not found on PATH for this user")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--once", action="store_true", help="one pass: inventory + apply")
    parser.add_argument("--watch", action="store_true", help="loop forever (systemd mode)")
    parser.add_argument("--interval", type=float, default=15.0)
    parser.add_argument("--init", action="store_true", help="create control files (run as root)")
    parser.add_argument(
        "--user",
        default=os.environ.get("SUDO_USER", "root"),
        help="--init: who owns status/inventory (the unit's User=)",
    )
    args = parser.parse_args()

    repo = args.repo.resolve()
    if args.init:
        init(repo, args.user)
        return
    if not (args.once or args.watch):
        parser.error("pick one of --init, --once, --watch")
    if not control_dir(repo).is_dir():
        sys.exit(f"control dir missing -- run: sudo {sys.argv[0]} --init --repo {repo}")
    log(f"repo {repo} ({'watch' if args.watch else 'once'})")
    while True:
        run_once(repo)
        if not args.watch:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
