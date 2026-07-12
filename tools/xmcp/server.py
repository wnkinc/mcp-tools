"""MCP server: X (Twitter) API tools, closely following xdevplatform/xmcp.

This is upstream's ``server.py`` (https://github.com/xdevplatform/xmcp) with a
minimal delta, run through our shared security stack. It fetches X's OpenAPI spec,
filters it down to a code-enforced grant (read-only by default), and exposes the
result as MCP tools via ``FastMCP.from_openapi``.

X auth, in upstream's order of preference: OAuth1 user-context signing of EVERY
request (act-as-account; the four ``X_OAUTH_*`` values, minted in the developer
portal -- we skip upstream's interactive browser-consent flow, which can't run in
a headless container) with app-only ``X_BEARER_TOKEN`` as the read-only fallback.
Write operations require the OAuth1 path (a bearer token cannot act as an account).

Our delta from upstream, besides the auth-flow trim: the ``serve()`` wrapper
(Google OAuth, out-of-band approval, guardrail), the write-guard + allowlist
default (upstream exposes all ~140 ops unconditionally), MCP ``ToolAnnotations``
(``readOnlyHint`` drives Claude's read-only vs write/delete permission categories;
upstream sets none), and the guardrail outputSchema strip. A custom ``grok_x_search``
tool (xAI Grok's own X search; not part of upstream) lived here until 2026-07-04 --
recover it from git history if wanted; its XAI_* env keys remain parked in `.env`.
"""

import copy
import logging
import os
import sys
from pathlib import Path

import httpx
from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from oauthlib.oauth1 import Client as OAuth1Client

# Make the repo root importable regardless of the process CWD (we run from the tool
# dir), then pull in the shared serve() helper.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from security.serve import serve  # noqa: E402

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "trace"}

# Least privilege: code-enforced read-only default. The safe
# grant lives HERE, in version control -- not solely in a gitignored `.env`. An
# empty/missing `X_API_TOOL_ALLOWLIST` falls back to exactly these read ops (NOT
# "expose everything"), so a misconfigured deploy fails closed to read-only instead
# of silently exposing all 165 X operations (68 of them writes). `.env` may still
# set `X_API_TOOL_ALLOWLIST` to NARROW or customize this, or to the sentinel
# `all` (ALLOWLIST_ALL) to deliberately expose the whole spec -- still subject to
# the write-guard and the stream/webhook exclusion.
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

# X_API_TOOL_ALLOWLIST sentinel: expose every operation the spec filter otherwise
# permits. A deliberate one-word .env opt-out of the curated default above.
ALLOWLIST_ALL = "all"

LOGGER = logging.getLogger("xmcp.x_api")


def is_truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_csv_env(key: str) -> set[str]:
    raw = os.getenv(key, "")
    if not raw.strip():
        return set()
    return {item.strip() for item in raw.split(",") if item.strip()}


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


def get_auth_headers() -> dict:
    """App-only Bearer header: the read-only fallback when OAuth1 isn't configured."""
    token = os.getenv("X_BEARER_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "Set the four X_OAUTH_* values (user-context) or X_BEARER_TOKEN "
            "(read-only app bearer) in the .env."
        )
    return {"Authorization": f"Bearer {token}"}


def build_oauth1_client() -> OAuth1Client | None:
    """Upstream's OAuth1 signing client, minus its interactive consent flow.

    Upstream mints the access token/secret by opening a browser at startup; that
    can't happen in a headless container, so ours arrive pre-minted from the X
    developer portal ("Keys and tokens" -> Access Token and Secret, generated
    AFTER the app permission is read+write) via `.env`. All four values or the
    OAuth1 path is off (None -> app-only bearer fallback, read-only).
    """
    consumer_key = os.getenv("X_OAUTH_CONSUMER_KEY", "").strip()
    consumer_secret = os.getenv("X_OAUTH_CONSUMER_SECRET", "").strip()
    access_token = os.getenv("X_OAUTH_ACCESS_TOKEN", "").strip()
    access_secret = os.getenv("X_OAUTH_ACCESS_TOKEN_SECRET", "").strip()
    if not all((consumer_key, consumer_secret, access_token, access_secret)):
        return None
    return OAuth1Client(
        client_key=consumer_key,
        client_secret=consumer_secret,
        resource_owner_key=access_token,
        resource_owner_secret=access_secret,
        signature_type="AUTH_HEADER",
    )


def load_openapi_spec() -> dict:
    url = "https://api.x.com/2/openapi.json"
    LOGGER.info("Fetching OpenAPI spec from %s", url)
    resp = httpx.get(url, timeout=30)  # honors HTTPS_PROXY via trust_env
    resp.raise_for_status()
    return resp.json()


# --- Comma-joined array query params (X API quirk) ---------------------------------
# X declares its field-selectors (tweet.fields, expansions, user.fields, ...) and
# id-lists (ids, usernames) as `explode: false` array query params, which MUST be
# serialized comma-joined (?ids=1,2,3), not repeated (?ids=1&ids=2). normalize_query_params
# (registered as an httpx request hook) collapses any such params to a single comma
# value. Idempotent: if they already arrive comma-joined it's a no-op.
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
    # never "expose everything". See DEFAULT_READ_ALLOWLIST. The explicit sentinel
    # `all` disables the operationId filter (empty allow_ops = no filter below);
    # the write-guard and stream/webhook exclusion still apply.
    raw_allow = parse_csv_env("X_API_TOOL_ALLOWLIST")
    expose_all = raw_allow == {ALLOWLIST_ALL}
    allow_ops = set() if expose_all else (raw_allow or set(DEFAULT_READ_ALLOWLIST))
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
        1 for item in new_paths.values() for method in item if method.lower() in HTTP_METHODS
    )
    if expose_all:
        allowlist_mode = "ALL(spec)"
    elif raw_allow:
        allowlist_mode = "env"
    else:
        allowlist_mode = "DEFAULT(read-only)"
    LOGGER.warning(
        "X grant: %d tools exposed, writes=%s, allowlist=%s (%d write ops blocked)",
        n_tools,
        "ON" if allow_writes else "OFF",
        allowlist_mode,
        n_write_blocked,
    )
    return filtered


def build_annotations(route) -> ToolAnnotations:  # type: ignore[no-untyped-def]
    """MCP annotations for an OpenAPI-derived tool, from its HTTP method.

    ``readOnlyHint`` is what Claude's connector UI groups the permission categories
    by (read-only vs write/delete, each with its own always-allow/approve/block
    policy) -- the same mechanism the telegram engine uses.
    """
    method = (route.method or "").upper()
    title = route.summary or route.operation_id
    if method == "GET":
        return ToolAnnotations(title=title, readOnlyHint=True, openWorldHint=True)
    return ToolAnnotations(
        title=title,
        readOnlyHint=False,
        destructiveHint=method == "DELETE",
        idempotentHint=method in {"PUT", "DELETE"},
        openWorldHint=True,
    )


def print_tool_list(spec: dict) -> None:
    tools: list[str] = []
    for path, item in spec.get("paths", {}).items():
        if not isinstance(item, dict):
            continue
        for method, operation in item.items():
            if method.lower() not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            op_id = operation.get("operationId")
            tools.append(op_id if op_id else f"{method.upper()} {path}")

    tools.sort()
    print(f"Loaded {len(tools)} tools from OpenAPI:")
    for tool in tools:
        print(f"- {tool}")


def create_mcp() -> FastMCP:
    load_env()
    debug_enabled = setup_logging()
    parser_flag = os.getenv("FASTMCP_EXPERIMENTAL_ENABLE_NEW_OPENAPI_PARSER")
    if parser_flag is not None:
        os.environ["FASTMCP_EXPERIMENTAL_ENABLE_NEW_OPENAPI_PARSER"] = parser_flag

    base_url = os.getenv("X_API_BASE_URL", "https://api.x.com")
    timeout = float(os.getenv("X_API_TIMEOUT", "30"))

    # Upstream's auth: OAuth1 user-context when configured (signs EVERY request,
    # reads and writes act as the account), else the app-only bearer (read-only).
    oauth1_client = build_oauth1_client()
    if oauth1_client is None:
        LOGGER.warning("X auth: app-only bearer (read-only; writes would fail at X)")
    else:
        LOGGER.warning("X auth: OAuth1 user-context (all requests signed, act-as-account)")

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

    # Upstream's signing hook, verbatim: runs AFTER normalize_query_params (the
    # signature must cover the final URL). Only form-encoded bodies are signed --
    # per the OAuth1 spec, JSON bodies are excluded from the signature base.
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

    async def log_request(request: httpx.Request) -> None:
        if debug_enabled:
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

    if oauth1_client is None:
        headers = get_auth_headers()  # {"Authorization": "Bearer <X_BEARER_TOKEN>"}
        request_hooks = [normalize_query_params, log_request]
    else:
        headers = {}  # the signing hook supplies the Authorization header per request
        request_hooks = [normalize_query_params, sign_oauth1_request, log_request]

    client = httpx.AsyncClient(
        base_url=base_url,
        headers=headers,
        timeout=timeout,
        event_hooks={
            "request": request_hooks,
            "response": [log_response],
        },
    )

    # mcp_component_fn customizes each from_openapi tool at build time (serve() can't
    # reach their dynamic OpenAPIProvider):
    # - outputSchema strip: x-mcp is guardrailed (main() calls serve(untrusted_output=
    #   True)) and the guardrail nulls each result's structuredContent, so any tool
    #   advertising an outputSchema then fails the Claude connector's output validation
    #   (outputSchema with no structuredContent). Same MCP_KEEP_OUTPUT_SCHEMA escape
    #   hatch as serve()'s LocalProvider pass.
    # - annotations: read/write hints + API-source title (see build_annotations).
    keep_output_schema = is_truthy(os.getenv("MCP_KEEP_OUTPUT_SCHEMA", "0"))

    def _customize_component(route, component) -> None:
        if not keep_output_schema and getattr(component, "output_schema", None) is not None:
            component.output_schema = None
        if hasattr(component, "annotations"):
            component.annotations = build_annotations(route)

    return FastMCP.from_openapi(
        openapi_spec=filtered_spec,
        client=client,
        name="X API MCP",
        mcp_component_fn=_customize_component,
    )


def main() -> None:
    port = int(os.getenv("MCP_PORT", "8000"))
    mcp = create_mcp()
    # x-mcp returns UNTRUSTED external X content, so screen output; and gate every call
    # behind out-of-band human approval. guardrail_source tags the wrapped content.
    serve(mcp, port=port, untrusted_output=True, require_approval=True, guardrail_source="xmcp")


if __name__ == "__main__":
    main()
