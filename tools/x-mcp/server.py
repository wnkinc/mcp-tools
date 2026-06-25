import copy
import hashlib
import hmac
import html
import http.server
import json
import logging
import os
import secrets
import socketserver
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

import sys

import httpx
import requests
from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware
from fastmcp.tools.tool import ToolResult
from oauthlib.oauth1 import Client as OAuth1Client
from requests_oauthlib import OAuth1Session

# PATCHED (mcp-tools): make the repo-root `shared/` package importable regardless
# of the process CWD (systemd runs us from the tool dir), then pull in the shared
# Google-OAuth provider used by every public-facing mcp-tools server.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from shared.auth import build_oauth_provider  # noqa: E402
from security.guardrail.middleware import GuardrailMiddleware  # noqa: E402

HTTP_METHODS = {
    "get",
    "post",
    "put",
    "patch",
    "delete",
    "options",
    "head",
    "trace",
}

# THREAT-MODEL L1 (minimize the grant): code-enforced read-only default. The safe
# grant lives HERE, in version control -- not solely in a gitignored `.env`. An
# empty/missing `X_API_TOOL_ALLOWLIST` falls back to exactly these read ops (NOT
# "expose everything"), so a misconfigured deploy fails closed to read-only instead
# of silently exposing all 165 X operations (68 of them writes). `.env` may still
# set `X_API_TOOL_ALLOWLIST` to NARROW or customize this.
DEFAULT_READ_ALLOWLIST = {
    "searchPostsRecent",
    "getPostsById",
    "getPostsByIds",
    "getUsersByUsername",
    "getUsersByUsernames",
    "getUsersById",
    "searchUsers",
    "getPostsCountsRecent",
}

LOGGER = logging.getLogger("xmcp.x_api")
OAUTH_LOGGER = logging.getLogger("xmcp.oauth1")

REQUEST_TOKEN_URL = "https://api.x.com/oauth/request_token"
AUTHORIZE_URL = "https://api.x.com/oauth/authorize"
ACCESS_TOKEN_URL = "https://api.x.com/oauth/access_token"


def is_truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_csv_env(key: str) -> set[str]:
    raw = os.getenv(key, "")
    if not raw.strip():
        return set()
    return {item.strip() for item in raw.split(",") if item.strip()}


def should_join_query_param(param: dict) -> bool:
    if param.get("in") != "query":
        return False
    schema = param.get("schema", {})
    if schema.get("type") != "array":
        return False
    return param.get("explode") is False


def collect_comma_params(spec: dict) -> set[str]:
    comma_params: set[str] = set()
    components = spec.get("components", {}).get("parameters", {})
    for param in components.values():
        if isinstance(param, dict) and should_join_query_param(param):
            name = param.get("name")
            if isinstance(name, str):
                comma_params.add(name)

    for item in spec.get("paths", {}).values():
        if not isinstance(item, dict):
            continue
        for method, operation in item.items():
            if method.lower() not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            for param in operation.get("parameters", []):
                if not isinstance(param, dict) or "$ref" in param:
                    continue
                if should_join_query_param(param):
                    name = param.get("name")
                    if isinstance(name, str):
                        comma_params.add(name)

    return comma_params


def load_openapi_spec() -> dict:
    url = "https://api.x.com/2/openapi.json"
    LOGGER.info("Fetching OpenAPI spec from %s", url)
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return response.json()


def _get_env_int(key: str, default: int) -> int:
    raw = os.getenv(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"{key} must be an integer value.")


def _callback_url(host: str, port: int, path: str) -> str:
    return f"http://{host}:{port}{path}"


def _wait_for_callback(host: str, port: int, path: str, timeout_seconds: int) -> tuple[str, str]:
    params: dict[str, str | None] = {"oauth_token": None, "oauth_verifier": None}
    event = threading.Event()

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != path:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Not found.")
                return
            query = urllib.parse.parse_qs(parsed.query)
            params["oauth_token"] = (query.get("oauth_token") or [None])[0]
            params["oauth_verifier"] = (query.get("oauth_verifier") or [None])[0]
            event.set()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OAuth complete. You may close this tab.")

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            OAUTH_LOGGER.debug("OAuth1 callback: " + format, *args)

    class _Server(socketserver.TCPServer):
        allow_reuse_address = True

    server = _Server((host, port), _Handler)
    server.timeout = 1

    deadline = time.time() + timeout_seconds
    try:
        while time.time() < deadline:
            server.handle_request()
            if event.is_set():
                break
    finally:
        server.server_close()

    oauth_token = params.get("oauth_token")
    oauth_verifier = params.get("oauth_verifier")
    if not oauth_token or not oauth_verifier:
        raise TimeoutError("OAuth callback not received before timeout.")
    return oauth_token, oauth_verifier


def run_oauth1_flow() -> tuple[str, str]:
    consumer_key = os.getenv("X_OAUTH_CONSUMER_KEY")
    consumer_secret = os.getenv("X_OAUTH_CONSUMER_SECRET")
    if not consumer_key or not consumer_secret:
        raise RuntimeError(
            "Missing X_OAUTH_CONSUMER_KEY or X_OAUTH_CONSUMER_SECRET for OAuth1 flow."
        )

    callback_host = os.getenv("X_OAUTH_CALLBACK_HOST", "127.0.0.1")
    callback_port = _get_env_int("X_OAUTH_CALLBACK_PORT", 8976)
    callback_path = os.getenv("X_OAUTH_CALLBACK_PATH", "/oauth/callback")
    callback_timeout = _get_env_int("X_OAUTH_CALLBACK_TIMEOUT", 300)

    callback_url = _callback_url(callback_host, callback_port, callback_path)

    oauth = OAuth1Session(
        client_key=consumer_key,
        client_secret=consumer_secret,
        callback_uri=callback_url,
    )
    request_token = oauth.fetch_request_token(REQUEST_TOKEN_URL)
    resource_owner_key = request_token.get("oauth_token")
    resource_owner_secret = request_token.get("oauth_token_secret")
    if not resource_owner_key or not resource_owner_secret:
        raise RuntimeError("Failed to obtain OAuth request token.")

    authorization_url = oauth.authorization_url(AUTHORIZE_URL)
    OAUTH_LOGGER.info("Opening browser for OAuth1 consent.")
    webbrowser.open(authorization_url)

    oauth_token, oauth_verifier = _wait_for_callback(
        callback_host, callback_port, callback_path, callback_timeout
    )
    if oauth_token != resource_owner_key:
        raise RuntimeError("OAuth callback token does not match request token.")

    oauth = OAuth1Session(
        client_key=consumer_key,
        client_secret=consumer_secret,
        resource_owner_key=resource_owner_key,
        resource_owner_secret=resource_owner_secret,
        verifier=oauth_verifier,
    )
    access_token = oauth.fetch_access_token(ACCESS_TOKEN_URL)
    access_key = access_token.get("oauth_token")
    access_secret = access_token.get("oauth_token_secret")
    if not access_key or not access_secret:
        raise RuntimeError("Failed to obtain OAuth access token.")
    return access_key, access_secret


def load_env() -> None:
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(env_path, override=True)


def setup_logging() -> bool:
    debug_enabled = is_truthy(os.getenv("X_API_DEBUG", "1"))
    if debug_enabled:
        logging.basicConfig(level=logging.INFO)
        LOGGER.setLevel(logging.INFO)
    return debug_enabled


def should_exclude_operation(path: str, operation: dict) -> bool:
    if "/webhooks" in path or "/stream" in path:
        return True

    tags = [tag.lower() for tag in operation.get("tags", []) if isinstance(tag, str)]
    if "stream" in tags or "webhooks" in tags:
        return True

    if operation.get("x-twitter-streaming") is True:
        return True

    return False


def filter_openapi_spec(spec: dict) -> dict:
    filtered = copy.deepcopy(spec)
    paths = filtered.get("paths", {})
    new_paths = {}
    allow_tags = {tag.lower() for tag in parse_csv_env("X_API_TOOL_TAGS")}
    # Default-deny: an empty/missing allowlist falls back to the read-only default,
    # never "expose everything". See DEFAULT_READ_ALLOWLIST.
    allow_ops = parse_csv_env("X_API_TOOL_ALLOWLIST") or set(DEFAULT_READ_ALLOWLIST)
    deny_ops = parse_csv_env("X_API_TOOL_DENYLIST")
    # Write-guard: non-GET (mutate) operations are NEVER exposed unless writes are
    # explicitly opted in, regardless of what the allowlist contains. Makes "this is
    # a read-only tool" true by construction, not by trusting the allowlist string.
    allow_writes = is_truthy(os.getenv("X_API_ALLOW_WRITES"))

    n_write_blocked = 0
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue

        new_item = {}
        for key, value in item.items():
            if key.lower() in HTTP_METHODS:
                if should_exclude_operation(path, value):
                    continue
                if key.lower() != "get" and not allow_writes:
                    n_write_blocked += 1
                    continue
                operation_id = value.get("operationId")
                operation_tags = [
                    tag.lower() for tag in value.get("tags", []) if isinstance(tag, str)
                ]
                if allow_tags and not (set(operation_tags) & allow_tags):
                    continue
                if allow_ops and operation_id not in allow_ops:
                    continue
                if deny_ops and operation_id in deny_ops:
                    continue
                new_item[key] = value
            else:
                new_item[key] = value

        if any(method.lower() in HTTP_METHODS for method in new_item.keys()):
            new_paths[path] = new_item

    filtered["paths"] = new_paths
    n_tools = sum(
        1
        for item in new_paths.values()
        for method in item
        if method.lower() in HTTP_METHODS
    )
    used_default = not parse_csv_env("X_API_TOOL_ALLOWLIST")
    LOGGER.warning(
        "X grant: %d tools exposed, writes=%s, allowlist=%s (%d write ops blocked)",
        n_tools,
        "ON" if allow_writes else "OFF",
        "DEFAULT(read-only)" if used_default else "env",
        n_write_blocked,
    )
    return filtered


def print_tool_list(spec: dict) -> None:
    tools: list[str] = []
    for path, item in spec.get("paths", {}).items():
        if not isinstance(item, dict):
            continue
        for method, operation in item.items():
            if method.lower() not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            op_id = operation.get("operationId")
            if op_id:
                tools.append(op_id)
            else:
                tools.append(f"{method.upper()} {path}")

    tools.sort()
    print(f"Loaded {len(tools)} tools from OpenAPI:")
    for tool in tools:
        print(f"- {tool}")


def get_auth_headers(oauth_token: str | None = None) -> dict:
    env_oauth_token = os.getenv("X_OAUTH_ACCESS_TOKEN", "").strip()
    bearer_token = os.getenv("X_BEARER_TOKEN", "").strip()
    token = oauth_token or env_oauth_token or bearer_token
    if not token:
        raise RuntimeError("Set X_BEARER_TOKEN or provide OAuth1 access token on startup.")
    return {"Authorization": f"Bearer {token}"}


def build_oauth1_client() -> OAuth1Client:
    consumer_key = os.getenv("X_OAUTH_CONSUMER_KEY")
    consumer_secret = os.getenv("X_OAUTH_CONSUMER_SECRET")
    if not consumer_key or not consumer_secret:
        raise RuntimeError(
            "Missing X_OAUTH_CONSUMER_KEY or X_OAUTH_CONSUMER_SECRET for OAuth1 signing."
        )
    access_token, access_secret = run_oauth1_flow()
    if is_truthy(os.getenv("X_OAUTH_PRINT_TOKENS", "0")):
        print("OAuth1 access token:", access_token)
        print("OAuth1 access token secret:", access_secret)
    return OAuth1Client(
        client_key=consumer_key,
        client_secret=consumer_secret,
        resource_owner_key=access_token,
        resource_owner_secret=access_secret,
        signature_type="AUTH_HEADER",
    )


def print_oauth1_header_probe(oauth1_client: OAuth1Client, base_url: str) -> None:
    probe_url = f"{base_url}/2/users/me"
    _, signed_headers, _ = oauth1_client.sign(
        probe_url,
        http_method="GET",
        headers={},
    )
    auth_header = signed_headers.get("Authorization")
    if auth_header:
        print("OAuth1 Authorization header (sample GET /2/users/me):", auth_header)
    else:
        print("OAuth1 Authorization header missing from signed probe request.")


# PATCHED (secure-agentic-engineering): Grok-mediated X search tool.
# Calls xAI's Responses API with the `x_search` tool — Grok searches X and returns a
# cited natural-language summary (vs the raw posts from the X-API tools).
XAI_RESPONSES_URL = "https://api.x.ai/v1/responses"
# >>> MODEL LEVER: set XAI_MODEL in .env to switch Grok models. <<<
# Default is non-reasoning (cheaper/faster); use a *-reasoning model for deeper synthesis.
DEFAULT_XAI_MODEL = "grok-4-1-fast"


# ---------------------------------------------------------------------------
# Out-of-band human-in-the-loop approval.
#
# claude.ai gives us no reliable in-chat gate: tool-approval is sticky (approve once
# and it's approved across every chat; the connector "needs approval" setting stops
# applying), and MCP elicitation dialogs don't render for custom connectors (tested).
# So a gated tool returns a clickable approval link to the chat and performs the
# action ONLY after the human opens the (capability-token) page and clicks Approve.
# The model can't click the link or forge the server-side "approved" state, so this
# is a real gate -- unlike a confirm-token the model can read and replay.
#
# Single uvicorn process => a plain in-memory dict is fine for pending approvals.
# ---------------------------------------------------------------------------
_PENDING_APPROVALS: dict[str, dict] = {}
_APPROVAL_TTL_SECONDS = 600  # approval links expire after 10 minutes


def _prune_approvals() -> None:
    now = time.time()
    stale = [t for t, r in _PENDING_APPROVALS.items() if now - r["created"] > _APPROVAL_TTL_SECONDS]
    for t in stale:
        _PENDING_APPROVALS.pop(t, None)


def _describe_call(tool_name: str, args: dict) -> str:
    """Short human-readable description of a tool call, for the approval prompt."""
    if not args:
        return f"{tool_name}()"
    shown = ", ".join(f"{k}={v!r}" for k, v in list(args.items())[:4])
    if len(args) > 4:
        shown += ", …"
    return f"{tool_name}({shown})"


def _call_key(tool_name: str, args: dict) -> str:
    """Stable key for a (tool, args) call so an approval can be matched on re-invoke."""
    blob = tool_name + "\x00" + json.dumps(args, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _find_by_call_key(call_key: str) -> tuple[str | None, dict | None]:
    _prune_approvals()
    for token, rec in _PENDING_APPROVALS.items():
        if rec.get("call_key") == call_key:
            return token, rec
    return None, None


async def require_approval_for_call(tool_name: str, args: dict) -> tuple[bool, str | None]:
    """Out-of-band approval gate for an arbitrary tool call. Returns (approved, message).

    When `approved` is False, the caller MUST NOT run the tool and should return
    `message` to the user. Approvals are keyed by (tool, args), so after the human
    approves the model just re-invokes the SAME tool with the SAME arguments — no
    token to thread through. The human clicking Approve (page or Slack) is the only
    thing that flips the stored state to "approved".
    """
    call_key = _call_key(tool_name, args)
    token, rec = _find_by_call_key(call_key)
    action = _describe_call(tool_name, args)

    if rec is not None:
        status = rec["status"]
        if status == "approved":
            _PENDING_APPROVALS.pop(token, None)  # one-time use
            return True, None
        if status == "denied":
            _PENDING_APPROVALS.pop(token, None)
            return False, f"❌ You denied this action ({action}), so I did not run it."
        return False, (
            f"⏳ Still waiting for your approval of `{action}` — open the link or tap "
            f"**Approve** in Slack, then ask me to continue."
        )

    # No record yet: register a pending request and surface the approval channels.
    token = secrets.token_urlsafe(24)
    _PENDING_APPROVALS[token] = {
        "action": action,
        "status": "pending",
        "created": time.time(),
        "call_key": call_key,
    }
    base = os.getenv("MCP_PUBLIC_URL", "").rstrip("/")
    link = f"{base}/approve/{token}"
    await _slack_post_approval(token, action)  # out-of-band push (best-effort)
    return False, (
        "APPROVAL REQUIRED — the action was NOT performed.\n\n"
        "INSTRUCTIONS FOR THE ASSISTANT: Show the user the full approval URL below "
        "exactly as written, on its own line, so it renders as a clickable link. Do "
        "NOT paraphrase it, shorten it, or say 'the link above' — the user cannot see "
        "your tool output, only what you write. Then stop and wait for the user.\n\n"
        f"Action awaiting approval: {action}\n\n"
        f"Approval URL: {link}\n\n"
        "After the user approves, call the SAME tool again with the SAME arguments to "
        "proceed. Until they approve, that call will report still-pending."
    )


def _approval_shell(title: str, body_html: str) -> str:
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>"
        "body{font-family:system-ui,-apple-system,sans-serif;max-width:34rem;"
        "margin:3rem auto;padding:0 1rem;background:#0b0b0c;color:#e8e8ea}"
        ".card{background:#161618;border:1px solid #2a2a2e;border-radius:14px;padding:1.5rem}"
        ".act{background:#0f1830;border:1px solid #24407a;border-radius:8px;padding:.75rem 1rem;"
        "margin:1rem 0;font-family:ui-monospace,monospace;word-break:break-word}"
        "button{font-size:1rem;padding:.7rem 1.4rem;border:0;border-radius:10px;cursor:pointer;margin:.25rem .5rem .25rem 0}"
        ".ok{background:#2563eb;color:#fff}.no{background:#3a1d1d;color:#f3b4b4}"
        "</style></head><body><div class=\"card\">"
        f"{body_html}</div></body></html>"
    )


def _approval_buttons_page(action: str) -> str:
    # Buttons POST the decision; a plain GET of the link has no side effect, so a
    # browser/chat link-prefetch can't silently approve.
    return _approval_shell(
        "Approve action",
        f"<h2>⏸ Approval requested</h2><p>An MCP tool wants to run:</p>"
        f'<div class="act">{html.escape(action)}</div>'
        '<form method="post">'
        '<button class="ok" name="decision" value="approve">✅ Approve</button>'
        '<button class="no" name="decision" value="deny">❌ Deny</button>'
        "</form>",
    )


# ---------------------------------------------------------------------------
# Slack as the out-of-band channel: push an interactive Approve/Deny message so the
# human can decide from their phone/desktop without opening the chat or a web page.
# All optional -- if SLACK_BOT_TOKEN / SLACK_APPROVAL_CHANNEL aren't set, posting is
# skipped and the in-chat approval link still works.
# ---------------------------------------------------------------------------
def _slack_enabled() -> bool:
    return bool(os.getenv("SLACK_BOT_TOKEN") and os.getenv("SLACK_APPROVAL_CHANNEL"))


async def _slack_post_approval(token: str, action: str) -> None:
    """Post an interactive Approve/Deny message to Slack. Best-effort (never raises)."""
    if not _slack_enabled():
        return
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"⏸ *Approval requested*\n>{action}"}},
        {
            "type": "actions",
            "block_id": f"approval:{token}",
            "elements": [
                {"type": "button", "style": "primary", "action_id": "approve",
                 "text": {"type": "plain_text", "text": "✅ Approve"}, "value": token},
                {"type": "button", "style": "danger", "action_id": "deny",
                 "text": {"type": "plain_text", "text": "❌ Deny"}, "value": token},
            ],
        },
    ]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {os.environ['SLACK_BOT_TOKEN']}"},
                json={
                    "channel": os.environ["SLACK_APPROVAL_CHANNEL"],
                    "text": f"Approval requested: {action}",  # notification fallback text
                    "blocks": blocks,
                },
            )
        data = resp.json()
        if not data.get("ok"):
            LOGGER.error("Slack chat.postMessage failed: %s", data.get("error"))
    except Exception:  # noqa: BLE001
        LOGGER.exception("Slack approval post failed")


def _verify_slack_signature(timestamp: str, body: bytes, signature: str) -> bool:
    """Verify Slack's request signature (HMAC-SHA256 over v0:timestamp:body)."""
    secret = os.getenv("SLACK_SIGNING_SECRET", "")
    if not (secret and timestamp and signature):
        return False
    try:
        if abs(time.time() - int(timestamp)) > 300:  # reject stale/replayed requests
            return False
    except ValueError:
        return False
    basestring = b"v0:" + timestamp.encode() + b":" + body
    expected = "v0=" + hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


# Tools exempt from the approval gate (e.g. trivial/free lookups). Comma-separated
# tool names in XMCP_APPROVAL_EXEMPT; empty by default => every tool is gated.
def _approval_exempt() -> set[str]:
    return parse_csv_env("XMCP_APPROVAL_EXEMPT")


class ApprovalMiddleware(Middleware):
    """Gate EVERY tool call behind out-of-band human approval (see require_approval_for_call).

    The first call to a (tool, args) combo short-circuits with an approval request and
    does NOT run the tool; once the human approves (page or Slack), re-calling the same
    tool with the same args runs it. The model can't forge the server-side approval, so
    this gates all tools — the OpenAPI X-API tools and grok_x_search alike — uniformly.
    """

    async def on_call_tool(self, context, call_next):  # type: ignore[no-untyped-def]
        tool_name = context.message.name
        if tool_name in _approval_exempt():
            return await call_next(context)
        args = dict(context.message.arguments or {})
        approved, note = await require_approval_for_call(tool_name, args)
        if not approved:
            return ToolResult(content=note)
        return await call_next(context)


async def grok_x_search(query: str) -> str:
    """Search X (Twitter) via xAI Grok's x_search and return a cited summary.

    Grok performs the search itself and returns a concise natural-language answer with
    citations — use for "what are people saying / summarize the discussion on X about
    ...". For raw post objects, use the X-API tools (searchPostsRecent, etc.) instead.

    Args:
        query: What to search X for, in natural language.
    """
    api_key = os.environ.get("XAI_API_KEY", "").strip()
    if not api_key:
        return "[grok_x_search: XAI_API_KEY not set in xMCP .env]"
    model = os.environ.get("XAI_MODEL", DEFAULT_XAI_MODEL)
    payload = {"model": model, "tools": [{"type": "x_search"}], "input": query}
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            resp = await client.post(
                XAI_RESPONSES_URL,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
            )
    except Exception as exc:  # noqa: BLE001
        return f"[grok_x_search: request to xAI failed: {exc}]"
    if resp.status_code >= 400:
        return f"[grok_x_search: xAI API error {resp.status_code}: {resp.text[:400]}]"
    data = resp.json()
    texts: list[str] = []
    citations: list[str] = []
    for item in data.get("output", []) or []:
        if item.get("type") == "message":
            for block in item.get("content", []) or []:
                if block.get("text"):
                    texts.append(block["text"])
                for ann in block.get("annotations", []) or []:
                    if ann.get("url"):
                        citations.append(ann["url"])
    out = "\n".join(texts).strip()
    if not out:
        import json as _json

        out = _json.dumps(data)[:1500]  # fallback so the response shape is visible
    if citations:
        out += "\n\nCitations:\n" + "\n".join(f"- {u}" for u in dict.fromkeys(citations))
    return out + f"\n\n(via xAI model: {model})"


def create_mcp() -> FastMCP:
    load_env()
    debug_enabled = setup_logging()
    parser_flag = os.getenv("FASTMCP_EXPERIMENTAL_ENABLE_NEW_OPENAPI_PARSER")
    if parser_flag is not None:
        os.environ["FASTMCP_EXPERIMENTAL_ENABLE_NEW_OPENAPI_PARSER"] = parser_flag

    base_url = os.getenv("X_API_BASE_URL", "https://api.x.com")
    timeout = float(os.getenv("X_API_TIMEOUT", "30"))

    # PATCHED (secure-agentic-engineering): headless app-only Bearer auth.
    # If OAuth1 consumer keys are absent, skip the interactive browser OAuth1 flow
    # and use X_BEARER_TOKEN as a static Bearer header (read-only, app-only) — so
    # this can run as an unattended service. Original OAuth1 path is preserved when
    # consumer keys are provided.
    use_oauth1 = bool(os.getenv("X_OAUTH_CONSUMER_KEY") and os.getenv("X_OAUTH_CONSUMER_SECRET"))
    oauth1_client = build_oauth1_client() if use_oauth1 else None
    print_oauth_header = is_truthy(os.getenv("X_OAUTH_PRINT_AUTH_HEADER", "0"))
    if print_oauth_header and oauth1_client is not None:
        print_oauth1_header_probe(oauth1_client, base_url)

    spec = load_openapi_spec()
    filtered_spec = filter_openapi_spec(spec)
    comma_params = collect_comma_params(filtered_spec)
    print_tool_list(filtered_spec)

    async def normalize_query_params(request: httpx.Request) -> None:
        if not comma_params:
            return
        params = list(request.url.params.multi_items())
        grouped: dict[str, list[str]] = {}
        ordered: list[str] = []
        normalized: list[tuple[str, str]] = []

        for key, value in params:
            if key in comma_params:
                if key not in grouped:
                    ordered.append(key)
                grouped.setdefault(key, []).append(value)
            else:
                normalized.append((key, value))

        if not grouped:
            return

        for key in ordered:
            values: list[str] = []
            for raw in grouped[key]:
                for part in raw.split(","):
                    part = part.strip()
                    if part and part not in values:
                        values.append(part)
            if values:
                normalized.append((key, ",".join(values)))

        request.url = request.url.copy_with(params=normalized)

    b3_flags = os.getenv("X_B3_FLAGS", "1")

    async def sign_oauth1_request(request: httpx.Request) -> None:
        request.headers["X-B3-Flags"] = b3_flags
        headers = dict(request.headers)
        content_type = headers.get("Content-Type", "")
        body: str | None = None
        if content_type.startswith("application/x-www-form-urlencoded"):
            body_bytes = request.content or b""
            body = body_bytes.decode("utf-8")
        signed_url, signed_headers, _ = oauth1_client.sign(
            str(request.url),
            http_method=request.method,
            body=body,
            headers=headers,
        )
        request.url = httpx.URL(signed_url)
        request.headers.update(signed_headers)
        if print_oauth_header:
            auth_header = signed_headers.get("Authorization")
            if auth_header:
                print("OAuth1 Authorization header:", auth_header)
            else:
                print("OAuth1 Authorization header missing from signed request.")

    async def log_request(request: httpx.Request) -> None:
        if not debug_enabled:
            return
        LOGGER.info("X API request %s %s", request.method, request.url)

    async def log_response(response: httpx.Response) -> None:
        if not debug_enabled:
            return
        LOGGER.info(
            "X API response %s %s -> %s",
            response.request.method,
            response.request.url,
            response.status_code,
        )
        if response.status_code >= 400:
            transaction_id = response.headers.get("x-transaction-id")
            if transaction_id:
                LOGGER.warning("X API x-transaction-id: %s", transaction_id)
            body = await response.aread()
            text = body.decode("utf-8", errors="replace")
            if len(text) > 1000:
                text = text[:1000] + "...<truncated>"
            LOGGER.warning("X API error body: %s", text)

    # PATCHED: bearer mode → static Authorization header, no OAuth1 signing hook.
    if use_oauth1:
        client_headers: dict = {}
        request_hooks = [normalize_query_params, sign_oauth1_request, log_request]
    else:
        client_headers = get_auth_headers()  # {"Authorization": "Bearer <X_BEARER_TOKEN>"}
        request_hooks = [normalize_query_params, log_request]

    client = httpx.AsyncClient(
        base_url=base_url,
        headers=client_headers,
        timeout=timeout,
        event_hooks={
            "request": request_hooks,
            "response": [log_response],
        },
    )
    # FastMCP 3.x derives an `outputSchema` for every OpenAPI tool. Claude's
    # connector mishandles tools that advertise outputSchema -- it drops the
    # per-tool approval toggle (and sometimes the tool) from connector settings
    # (anthropics/claude-code#25081). The pre-3.x FastMCP we used to run didn't
    # emit these, which is why toggles worked before the dependency moved to 3.x.
    # Strip output_schema at build time so the wire shape matches the old, working
    # one. Escape hatch: XMCP_KEEP_OUTPUT_SCHEMA=1.
    keep_output_schema = is_truthy(os.getenv("XMCP_KEEP_OUTPUT_SCHEMA", "0"))

    def _strip_output_schema(_route, component) -> None:
        if not keep_output_schema and getattr(component, "output_schema", None) is not None:
            component.output_schema = None

    mcp = FastMCP.from_openapi(
        openapi_spec=filtered_spec,
        client=client,
        name="X API MCP",
        mcp_component_fn=None if keep_output_schema else _strip_output_schema,
    )
    # PATCHED: register the Grok x_search tool alongside the raw X-API tools.
    grok_tool = mcp.add_tool(grok_x_search)
    # grok_x_search returns a plain str here, which FastMCP 3.x also wraps in an
    # outputSchema -- strip it too so no tool advertises one (see above).
    if not keep_output_schema and getattr(grok_tool, "output_schema", None) is not None:
        grok_tool.output_schema = None

    # Gate EVERY tool (X-API + grok) behind out-of-band human approval. One middleware
    # covers all of them, including the auto-generated OpenAPI tools that can't take an
    # approval param. Exempt specific tools via XMCP_APPROVAL_EXEMPT.
    # ORDER MATTERS: FastMCP wraps reversed(self.middleware), so the FIRST added is the
    # OUTERMOST. Approval must be outermost (it short-circuits BEFORE the tool runs, so a
    # pending-approval message is never screened); the guardrail sits INSIDE it and only
    # screens results of calls the human already approved.
    mcp.add_middleware(ApprovalMiddleware())
    # THREAT-MODEL L4 (detect): screen untrusted X content through the guardrail service
    # (:8071) before it reaches the model. Fails CLOSED if the service is down.
    mcp.add_middleware(GuardrailMiddleware(source="xmcp"))

    # Out-of-band human-in-the-loop approval page + Slack interactivity endpoint.
    @mcp.custom_route("/approve/{token}", methods=["GET", "POST"], include_in_schema=False)
    async def approve_route(request):  # type: ignore[no-untyped-def]
        from starlette.responses import HTMLResponse

        token = request.path_params["token"]
        _prune_approvals()
        rec = _PENDING_APPROVALS.get(token)
        if rec is None:
            return HTMLResponse(
                _approval_shell("Not found", "<h2>Link invalid or expired</h2>"
                                "<p>This approval link is no longer valid.</p>"),
                status_code=404,
            )
        if request.method == "POST":
            form = await request.form()
            decision = form.get("decision")
            if decision == "approve":
                rec["status"] = "approved"
                return HTMLResponse(_approval_shell(
                    "Approved", "<h2>✅ Approved</h2>"
                    f'<div class="act">{html.escape(rec["action"])}</div>'
                    "<p>Head back to Claude and tell it to continue.</p>"))
            if decision == "deny":
                rec["status"] = "denied"
                return HTMLResponse(_approval_shell(
                    "Denied", "<h2>❌ Denied</h2>"
                    f'<div class="act">{html.escape(rec["action"])}</div>'))
            return HTMLResponse(_approval_shell("Error", "<p>Unknown decision.</p>"), status_code=400)
        # GET: show buttons only (no side effect) so a link prefetch can't auto-approve.
        if rec["status"] != "pending":
            return HTMLResponse(_approval_shell(
                "Already decided", f"<h2>Already {html.escape(rec['status'])}</h2>"))
        return HTMLResponse(_approval_buttons_page(rec["action"]))

    @mcp.custom_route("/slack/interact", methods=["POST"], include_in_schema=False)
    async def slack_interact(request):  # type: ignore[no-untyped-def]
        from starlette.responses import PlainTextResponse, Response

        raw = await request.body()
        if not _verify_slack_signature(
            request.headers.get("X-Slack-Request-Timestamp", ""),
            raw,
            request.headers.get("X-Slack-Signature", ""),
        ):
            return PlainTextResponse("bad signature", status_code=403)

        import urllib.parse as _up

        form = _up.parse_qs(raw.decode())
        payload = json.loads((form.get("payload") or ["{}"])[0])
        actions = payload.get("actions") or []
        response_url = payload.get("response_url")
        if not actions:
            return Response(status_code=200)
        action_id = actions[0].get("action_id")
        token = actions[0].get("value")

        _prune_approvals()
        rec = _PENDING_APPROVALS.get(token)
        if rec is None:
            msg = "⚠️ This approval link has expired."
        elif action_id == "approve":
            rec["status"] = "approved"
            msg = f"✅ *Approved*\n>{rec['action']}\n\nReturn to Claude and tell it to continue."
        elif action_id == "deny":
            rec["status"] = "denied"
            msg = f"❌ *Denied*\n>{rec['action']}"
        else:
            msg = "Unknown action."

        # Replace the original message in place (removes the buttons).
        if response_url:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.post(response_url, json={"replace_original": True, "text": msg})
            except Exception:  # noqa: BLE001
                LOGGER.exception("Slack response_url update failed")
        return Response(status_code=200)

    # PATCHED (mcp-tools): attach MCP-native OAuth for public serving. When auth is
    # disabled (MCP_AUTH_ENABLED off) build_oauth_provider() returns None and the
    # server behaves exactly like the original loopback build (e.g. for a local
    # agent on 127.0.0.1). When enabled, FastMCP serves the OAuth discovery
    # metadata, DCR, and the WWW-Authenticate header that claude.ai web/mobile need.
    auth_provider = build_oauth_provider()
    if auth_provider is not None:
        mcp.auth = auth_provider

    return mcp


def main() -> None:
    host = os.getenv("MCP_HOST", "127.0.0.1")
    port = int(os.getenv("MCP_PORT", "8000"))
    mcp = create_mcp()
    mcp.run(transport="http", host=host, port=port)


if __name__ == "__main__":
    main()
