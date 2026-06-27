import os
import sys
from pathlib import Path

from fastmcp import FastMCP

# Make the repo root importable regardless of CWD, then load shared OAuth.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from security.auth import build_oauth_provider  # noqa: E402

import catalog  # noqa: E402

mcp = FastMCP(name="hamilton")


def load_env() -> None:
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(env_path, override=True)


@mcp.tool
def library_list(layer: str | None = None, family: str | None = None) -> list[dict]:
    """List catalogued pieces (indicators / features / alpha-signals).

    Optionally filter by tag: layer ("indicator", "feature", "alpha") and/or
    family ("momentum", "volatility", ...). Each item has name, return type, tags.
    """
    return catalog.list_pieces(layer=layer, family=family)


@mcp.tool
def library_lineage(name: str) -> dict:
    """Lineage for a piece: what it depends on (upstream) and what uses it (downstream)."""
    return catalog.lineage(name)


def main() -> None:
    load_env()
    host = os.getenv("MCP_HOST", "127.0.0.1")
    port = int(os.getenv("MCP_PORT", "8064"))
    auth = build_oauth_provider()
    if auth is not None:
        mcp.auth = auth
    mcp.run(transport="http", host=host, port=port)


if __name__ == "__main__":
    main()
