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


def env_values(env_path: Path) -> dict[str, str]:
    """Naive KEY=value parse of a tool .env -- enough to check staging."""
    values = {}
    if not env_path.exists():
        return values
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip()
    return values


def secrets_state(repo: Path, manifest: dict) -> tuple[bool, list[str]]:
    """(ready, missing_keys): every manifest secret has a non-empty value staged."""
    values = env_values(repo / "tools" / manifest["profile"] / ".env")
    missing = [s["key"] for s in manifest.get("secrets", []) if not values.get(s["key"])]
    return (not missing, missing)


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
    log(f"deploy {tool}: done")
    return {**status, "phase": "done", "detail": f"{tool} is up", "updated": time.time()}


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
    env_path = repo / "tools" / tool / ".env"
    if not env_path.exists():
        template = repo / "tools" / tool / "env.example"
        env_path.write_text(template.read_text() if template.exists() else "")
        os.chmod(env_path, 0o600)
    text = env_path.read_text()
    for key, value in values.items():
        pattern = re.compile(rf"^#?\s*{re.escape(key)}=.*$", re.MULTILINE)
        if pattern.search(text):
            # lambda (not a plain replacement string) so backslashes in the value
            # land literally; default args bind the loop vars (B023).
            text = pattern.sub(lambda _m, k=key, v=value: f"{k}={v}", text, count=1)
        else:
            text = text.rstrip("\n") + f"\n{key}={value}\n"
    env_path.write_text(text)
    os.chmod(env_path, 0o600)
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
