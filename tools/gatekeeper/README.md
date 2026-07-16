# gatekeeper (:8065)

The operator's control plane over every other tool: per-tool permission modes
(always_allow / needs_approval / blocked) via the in-chat permissions panel and
`set_gating`. Native code — no wrapped engine — and always on, like the
sidecars: "using" it is just attaching its connector in claude.ai.

The full design (mode authority, pins, the manage flow) lives in
[docs/GATEKEEPER.md](../../docs/GATEKEEPER.md).
