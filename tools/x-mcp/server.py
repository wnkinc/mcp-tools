"""MCP server: X (Twitter) read-only search/lookup, built directly on FastMCP.

This is our own thin server on top of ``fastmcp`` (the OSS library) -- NOT a vendored
copy of any app. It fetches X's OpenAPI spec, filters it down to a code-enforced
read-only grant, and exposes the result as MCP tools via ``FastMCP.from_openapi``,
plus a custom ``grok_x_search`` tool. Auth is app-only Bearer (no act-as-account).

The cross-cutting security layers (OAuth, out-of-band approval, guardrail screening)
and the HTTP run are applied uniformly by ``security.serve.serve`` in :func:`main`.
"""

import copy
import logging
import os
import sys
from pathlib import Path

import httpx
from fastmcp import FastMCP

# Make the repo root importable regardless of the process CWD (systemd runs us from
# the tool dir), then pull in the shared serve() helper.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from security.serve import serve  # noqa: E402

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "trace"}

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
    """App-only Bearer header. Read-only; no OAuth1 user flow, no act-as-account."""
    token = os.getenv("X_BEARER_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Set X_BEARER_TOKEN (read-only app bearer) in the .env.")
    return {"Authorization": f"Bearer {token}"}


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
            tools.append(op_id if op_id else f"{method.upper()} {path}")

    tools.sort()
    print(f"Loaded {len(tools)} tools from OpenAPI:")
    for tool in tools:
        print(f"- {tool}")


# Grok-mediated X search tool: calls xAI's Responses API with the `x_search` tool —
# Grok searches X and returns a cited natural-language summary (vs the raw posts from
# the X-API tools). >>> MODEL LEVER: set XAI_MODEL in .env to switch Grok models. <<<
XAI_RESPONSES_URL = "https://api.x.ai/v1/responses"
DEFAULT_XAI_MODEL = "grok-4-1-fast"  # non-reasoning (cheaper/faster); use *-reasoning for depth


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

    client = httpx.AsyncClient(
        base_url=base_url,
        headers=get_auth_headers(),  # {"Authorization": "Bearer <X_BEARER_TOKEN>"}
        timeout=timeout,
        event_hooks={
            "request": [normalize_query_params, log_request],
            "response": [log_response],
        },
    )

    # x-mcp is guardrailed (main() calls serve(untrusted_output=True)): the guardrail nulls
    # each result's structuredContent, so any tool advertising an outputSchema then fails
    # the Claude connector's output validation (outputSchema with no structuredContent).
    # serve() strips the LocalProvider tools (grok), but these from_openapi tools live in a
    # dynamic OpenAPIProvider serve() can't reach -- so they must be stripped HERE, at build
    # time, via mcp_component_fn. Same MCP_KEEP_OUTPUT_SCHEMA escape hatch as serve().
    keep_output_schema = is_truthy(os.getenv("MCP_KEEP_OUTPUT_SCHEMA", "0"))

    def _strip_output_schema(_route, component) -> None:
        if getattr(component, "output_schema", None) is not None:
            component.output_schema = None

    mcp = FastMCP.from_openapi(
        openapi_spec=filtered_spec,
        client=client,
        name="X API MCP",
        mcp_component_fn=None if keep_output_schema else _strip_output_schema,
    )
    # The grok tool (add_tool -> LocalProvider) is stripped by serve()'s LocalProvider pass.
    mcp.add_tool(grok_x_search)
    return mcp


def main() -> None:
    port = int(os.getenv("MCP_PORT", "8000"))
    mcp = create_mcp()
    # x-mcp returns UNTRUSTED external X content, so screen output; and gate every call
    # behind out-of-band human approval. guardrail_source tags the wrapped content.
    serve(mcp, port=port, untrusted_output=True, require_approval=True, guardrail_source="xmcp")


if __name__ == "__main__":
    main()
