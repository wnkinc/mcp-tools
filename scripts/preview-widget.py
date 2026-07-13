#!/usr/bin/env python3
"""Preview the gatekeeper widgets in a plain browser -- no claude.ai, no sidecar.

Serves security/approval/widgets/*.html at http://127.0.0.1:8123 with the
claude.ai host bridge stubbed (theme + tool-result delivery) and the sidecar
APIs replaced by in-memory stubs over canned data -- so Save is sandboxed and
nothing real changes. The page is RE-BAKED on every request: edit the HTML and
just reload the browser.

  /                the manage (permissions) widget      ?theme=dark for dark mode
  /?w=secrets      the secrets-staging form

  python3 scripts/preview-widget.py [port]
"""

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

WIDGETS = Path(__file__).resolve().parents[1] / "security" / "approval" / "widgets"

# Representative data: several connectors, both groups, plus a synthetic pin to
# exercise the pinned-row rendering (the real gatekeeper source never appears here:
# it's unmanageable and omitted from /manage sessions).
# read_only is a plain bool on the wire: the sidecar applies the MCP spec default
# (absent readOnlyHint = not read-only), so the widget never sees a null.
CATALOG = {
    "ok": True,
    "sources": {
        "telegram": {
            "pinned": ["send_message"],
            "last_seen": time.time() - 7200,  # "last used 2h ago"
            "registered": time.time() - 20,  # fresh beacon -> "deployed", no Forget
            "tools": {
                "get_me": {"description": "", "read_only": True, "mode": "always_allow"},
                "get_chats": {"description": "", "read_only": True, "mode": "always_allow"},
                "list_contacts": {"description": "", "read_only": True, "mode": "always_allow"},
                "search_messages": {"description": "", "read_only": True, "mode": "needs_approval"},
                "send_message": {"description": "", "read_only": False, "mode": "needs_approval"},
                "delete_message": {"description": "", "read_only": False, "mode": "blocked"},
                "create_group": {"description": "", "read_only": False, "mode": "always_allow"},
                "edit_message": {"description": "", "read_only": False, "mode": "always_allow"},
            },
        },
        "xmcp": {
            "pinned": [],
            "last_seen": None,  # "never used"
            "registered": time.time() - 86400 * 3,  # stale -> Forget appears
            "tools": {
                "searchPostsRecent": {"description": "", "read_only": True, "mode": "always_allow"},
                "getUserByUsername": {"description": "", "read_only": True, "mode": "always_allow"},
                "createPost": {"description": "", "read_only": False, "mode": "needs_approval"},
                "deleteTweetById": {"description": "", "read_only": False, "mode": "blocked"},
            },
        },
    },
}

# Canned secrets session for ?w=secrets (one staged field, one missing).
SECRETS = {
    "ok": True,
    "tool": "xmcp",
    "fields": [
        {
            "key": "X_OAUTH_CONSUMER_KEY",
            "label": "X app API key",
            "hint": "developer.x.com -> Keys and tokens",
            "staged": True,
        },
        {
            "key": "X_OAUTH_CONSUMER_SECRET",
            "label": "X app API key secret",
            "hint": "same card as the API key",
            "staged": False,
        },
    ],
}

# The pieces claude.ai normally provides: the ExtApps bridge (theme + tool result)
# and the network. The chat-surface background is painted here too, since the real
# widget is transparent on purpose.
STUB = """
globalThis.ExtApps = {
  applyDocumentTheme: (t) => {
    document.documentElement.dataset.theme = t;
    document.documentElement.style.colorScheme = t;
    document.body.style.background = t === "dark" ? "#262624" : "#faf9f5";
  },
  App: class {
    constructor() {}
    async connect() { Promise.resolve().then(() => this.ontoolresult?.(window.__TOOLRESULT)); }
    getHostContext() { return { theme: new URLSearchParams(location.search).get("theme") || "light" }; }
  },
};
const REAL_FETCH = window.fetch.bind(window);
window.fetch = async (url, opts = {}) => {
  if (String(url).includes("/secrets/")) {
    if ((opts.method || "GET") === "POST") {
      const vals = JSON.parse(opts.body).values;
      return new Response(JSON.stringify({ ok: true, staged: Object.keys(vals).sort() }), { status: 200 });
    }
    return new Response(JSON.stringify(window.__SECRETS), { status: 200 });
  }
  if (!String(url).includes("/manage/")) return REAL_FETCH(url, opts);
  if ((opts.method || "GET") === "POST") {
    const { changes, forget = [] } = JSON.parse(opts.body);
    const refused = {};
    const forgotten = [];
    let applied = 0;
    for (const [src, tools] of Object.entries(changes)) {
      const info = window.__CATALOG.sources[src];
      for (const [t, m] of Object.entries(tools)) {
        if (info.pinned.includes(t)) (refused[src] ??= []).push(t);
        else { info.tools[t].mode = m; applied += 1; }
      }
    }
    for (const src of forget) {
      if (window.__CATALOG.sources[src]) { delete window.__CATALOG.sources[src]; forgotten.push(src); applied += 1; }
    }
    return new Response(JSON.stringify({ ok: true, applied, forgotten, refused }), { status: 200 });
  }
  return new Response(JSON.stringify(window.__CATALOG), { status: 200 });
};
"""


def bake(widget: str = "manage") -> bytes:
    name, tag = ("secrets", "SECRETS") if widget == "secrets" else ("manage", "MANAGE")
    html = (WIDGETS / f"{name}.html").read_text()
    marker = json.dumps({"token": "preview", "tool": "xmcp"})
    boot = (
        f"{STUB}\nwindow.__CATALOG = {json.dumps(CATALOG)};\n"
        f"window.__SECRETS = {json.dumps(SECRETS)};\n"
        f"window.__TOOLRESULT = {{ content: [{{ text: `<!--{tag} {marker}-->` }}] }};"
    )
    return (
        html.replace("/*__EXT_APPS_BUNDLE__*/", boot)
        .replace("__APPROVAL_PUBLIC_BASE__", "")
        .encode()
    )


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server API
        path, _, query = self.path.partition("?")
        widget = "secrets" if "w=secrets" in query else "manage"
        body = bake(widget) if path == "/" else b""
        self.send_response(200 if body else 404)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # quiet
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8123
    print(
        f"widget preview: http://127.0.0.1:{port}/   (dark: ?theme=dark; edit manage.html, reload)"
    )
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
