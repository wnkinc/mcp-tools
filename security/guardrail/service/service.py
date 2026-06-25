"""Layer-4 guardrail service (THREAT-MODEL.md Layer 4, detect leg).

A thin loopback FastAPI wrapper around LlamaFirewall. Consumers (the x-search
wrapper, DeerFlow's guardrail middleware, future untrusted-content tools) POST
text to /scan and act on the decision. Heavy deps (torch/transformers + the
gated PromptGuard model) stay isolated in this service's own venv.

Scanners:
  - PROMPT_GUARD  — BERT injection classifier (HF-gated Meta model). Main detector.
  - HIDDEN_ASCII  — catches Unicode-smuggled / invisible-text injection. No model,
                    so the service still provides *some* protection (degraded mode)
                    before the gated PromptGuard model is available.

AlignmentCheck (AGENT_ALIGNMENT) is intentionally NOT wired here yet — deferred
pending the Together-vs-Claude vendor decision (see THREAT-MODEL Layer 4).
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

import anyio
from fastapi import FastAPI
from pydantic import BaseModel

from llamafirewall import (
    LlamaFirewall,
    Role,
    ScannerType,
    UserMessage,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("guardrail")

# Mutable service state, populated at startup.
STATE: dict = {"lf": None, "scanners": [], "prompt_guard_loaded": False, "degraded": True}


def _build_warm(scanners: list[ScannerType]) -> LlamaFirewall:
    """Build a firewall and force the lazy scanners to actually load by running a
    throwaway warmup scan — LlamaFirewall instantiates scanners on first scan, not
    at construction, so this is the only way to detect a broken/gated model early."""
    lf = LlamaFirewall(scanners={Role.USER: scanners})
    lf.scan(UserMessage(content="warmup"))
    return lf


def _build_firewall() -> None:
    """Prefer PromptGuard + HiddenASCII; degrade to HiddenASCII-only if the
    PromptGuard model can't load (gated/uncached/dep issue)."""
    try:
        STATE.update(
            lf=_build_warm([ScannerType.PROMPT_GUARD, ScannerType.HIDDEN_ASCII]),
            scanners=["prompt_guard", "hidden_ascii"], prompt_guard_loaded=True, degraded=False,
        )
        log.info("guardrail ready: PromptGuard + HiddenASCII")
    except Exception as exc:  # gated model missing / not logged in / dep mismatch
        log.warning("PromptGuard unavailable (%s) — DEGRADED to HiddenASCII-only", exc)
        STATE.update(
            lf=_build_warm([ScannerType.HIDDEN_ASCII]),
            scanners=["hidden_ascii"], prompt_guard_loaded=False, degraded=True,
        )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # LlamaFirewall.scan() spins its own event loop (asyncio.run), so it must NOT
    # run inside this async lifespan — warm up in a worker thread, exactly how the
    # sync /scan endpoint will execute it.
    await anyio.to_thread.run_sync(_build_firewall)
    yield


app = FastAPI(title="guardrail-service", version="0.1.0", lifespan=lifespan)


class ScanRequest(BaseModel):
    text: str
    role: str = "user"  # reserved; today everything is screened as untrusted user content


class ScanResponse(BaseModel):
    decision: str  # "allow" | "block" | "human_in_the_loop_required"
    score: float
    reason: str | None = None
    degraded: bool  # True => PromptGuard not loaded; only HiddenASCII ran


@app.get("/healthz")
def healthz() -> dict:
    return {
        "ready": STATE["lf"] is not None,
        "scanners": STATE["scanners"],
        "prompt_guard_loaded": STATE["prompt_guard_loaded"],
        "degraded": STATE["degraded"],
    }


@app.post("/scan", response_model=ScanResponse)
def scan(req: ScanRequest) -> ScanResponse:
    result = STATE["lf"].scan(UserMessage(content=req.text))
    return ScanResponse(
        decision=result.decision.value,
        score=float(getattr(result, "score", 0.0) or 0.0),
        reason=getattr(result, "reason", None),
        degraded=STATE["degraded"],
    )


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("GUARDRAIL_PORT", "8041")))


if __name__ == "__main__":
    main()
