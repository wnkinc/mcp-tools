# Guardrail eval

Red-teams the guardrail so detection is **measured, not assumed** — the
same "test that it blocks" discipline that validated `srt`.

## Fast gate (always runnable)

`eval_guardrail.py` fires a labelled payload set (direct injection, jailbreak,
indirect/tool-hijack, hidden-ASCII, plus benign + a tricky benign) at the
guardrail service and reports detection rate (TPR) vs false-positive rate (FPR).

```bash
cd security/eval/garak
uv sync
uv run python eval_guardrail.py     # exits non-zero if the gate fails
```

- With **PromptGuard loaded**: gate = TPR ≥ 80% and FPR ≤ 20% (tune in the script).
- In **degraded mode** (HiddenASCII-only): only the hidden-ascii payload must be
  caught; the rest is informational until the gated model is loaded.

## Deep corpus (garak proper)

`garak` carries a much larger attack corpus. Point it at a REST endpoint that
exercises the *full* agent path (not just the scanner) once such an endpoint
exists:

```bash
uv run garak --model_type rest -G <agent-rest-config>.json \
  --probes promptinject,latentinjection,encoding,dan
```

Relevant probes: `promptinject`, `latentinjection`, `agent_breaker`, `encoding`,
`dan`, `sysprompt_extraction`.

## Where this fits

The guardrail is detect → isolate → gate. This harness verifies the **detect** step
(the guardrail service). Isolation + HITL gating are architectural and verified
separately.
