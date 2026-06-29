"""
presigate_client.py — Thin Python client for the Presigate gate API.

Calls POST https://presigate.com/api/gate and returns a structured result.
Does NOT raise on gate errors — callers get a Result dict they can inspect.

Usage:
    from presigate_client import PresigateClient, GateResult

    client = PresigateClient()
    result = client.check(side="buy", asset="BTCUSDT", size_usd=1000.0)

    if result["ok"]:
        verdict = result["verdict"]           # "ACT" | "HOLD" | "ESCALATE"
        decision_id = result["decision_id"]   # UUIDv7 for outcome linkage
        reasons = result["reasons"]
    else:
        # Gate was unreachable or returned a non-200 — caller decides fail-open/closed
        print(result["error"])

Response modes
--------------
LEAN (default):
    Returns { verdict, confidence, reasons, meta }.
    The per-primitive signals breakdown (rxi/mmi/csi/tvi/timing) is OMITTED.
    Use this in active/production mode — verdict is all that is needed to block.

VERBOSE (verbose=True):
    Also returns { signals: { rxi, mmi, csi, tvi, timing } }.
    Use this in shadow mode so developers can see WHY the gate held.
    Builds trust during the observation period before going active.
"""

from __future__ import annotations

import logging
import time
from typing import TypedDict

import requests

logger = logging.getLogger(__name__)

GATE_URL = "https://presigate.com/api/gate"
OUTCOME_URL = "https://presigate.com/api/gate/outcome"

# Timeout for the gate call.  Keep short — fail-open if gate is slow.
DEFAULT_TIMEOUT_S = 4


class GateResult(TypedDict):
    """Structured result from a gate call.  Always present; inspect 'ok' first."""
    ok: bool
    # -- populated when ok=True --
    verdict: str           # "ACT" | "HOLD" | "ESCALATE"
    confidence: float
    decision_id: str       # UUIDv7 — use this to report outcomes
    reasons: list[str]
    signals: dict          # per-primitive breakdown — only present when verbose=True
    meta: dict             # btcMidprice, spreadBps, depthRatio, etc.
    # -- populated when ok=False --
    error: str             # human-readable error description


class PresigateClient:
    """
    Minimal synchronous client for the Presigate gate API.

    Thread-safe (uses a requests.Session but only for connection pooling;
    no shared mutable state).

    api_key — optional Presigate API key for per-client attribution.
        When supplied it is sent as ``Authorization: Bearer <key>`` on every
        request.  The server looks it up in api_key_registry and records the
        resolved vendor_tag + client_id on the flywheel_record row, making
        this client's calls filterable separately from other callers.

        Obtain a key by inserting a row into api_key_registry (see the
        Presigate README for the exact SQL).  Without a key, calls are tagged
        vendor_tag='direct' as before.
    """

    def __init__(self, timeout_s: float = DEFAULT_TIMEOUT_S, api_key: str = "") -> None:
        self.timeout_s = timeout_s
        self._api_key = api_key.strip()
        self._session = requests.Session()
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        self._session.headers.update(headers)

    def check(
        self,
        *,
        side: str,               # "buy" or "sell"
        asset: str = "BTCUSDT",
        size_usd: float,
        verbose: bool = False,   # True → include per-primitive signals breakdown
    ) -> GateResult:
        """
        Call the gate and return a structured result.

        Never raises.  On any error (network, timeout, 4xx, 5xx) returns
        ok=False with an error description.  Callers implement fail-open
        or fail-closed based on their risk tolerance.

        verbose=False (default / LEAN): response contains verdict, confidence,
            reasons, meta.  The signals breakdown is omitted.  Use in active mode.

        verbose=True (VERBOSE): response also contains signals (rxi/mmi/csi/tvi/
            timing scores).  Use in shadow mode so devs can inspect why the gate held.
        """
        payload: dict = {
            "action": {
                "side": side,
                "asset": asset.upper().replace("/", ""),
                "sizeUsd": size_usd,
            }
        }
        if verbose:
            payload["verbose"] = True

        t0 = time.monotonic()
        try:
            resp = self._session.post(GATE_URL, json=payload, timeout=self.timeout_s)
        except requests.exceptions.Timeout:
            elapsed = time.monotonic() - t0
            return self._err(f"Gate timeout after {elapsed:.1f}s (url={GATE_URL})")
        except requests.exceptions.ConnectionError as exc:
            return self._err(f"Gate connection error: {exc}")
        except Exception as exc:
            return self._err(f"Gate unexpected error: {exc}")

        elapsed = time.monotonic() - t0

        # Non-200 → error
        if not resp.ok:
            body_preview = resp.text[:200] if resp.text else "(empty)"
            return self._err(
                f"Gate HTTP {resp.status_code} after {elapsed:.2f}s: {body_preview}"
            )

        try:
            data = resp.json()
        except Exception as exc:
            return self._err(f"Gate returned non-JSON: {exc}")

        verdict = data.get("verdict", "")
        if verdict not in ("ACT", "HOLD", "ESCALATE"):
            return self._err(f"Gate returned unknown verdict: {verdict!r}")

        meta = data.get("meta", {})
        decision_id = meta.get("decisionId", "")

        logger.debug(
            "Presigate gate OK in %.2fs | verdict=%s | decisionId=%s",
            elapsed, verdict, decision_id,
        )

        return GateResult(
            ok=True,
            verdict=verdict,
            confidence=float(data.get("confidence", 0.0)),
            decision_id=decision_id,
            reasons=list(data.get("reasons", [])),
            signals=dict(data.get("signals", {})),
            meta=meta,
            error="",
        )

    def report_outcome(
        self,
        *,
        decision_id: str,
        vendor_tag: str = "freqtrade",
        outcome: dict,
    ) -> dict:
        """
        POST outcome data to /api/gate/outcome keyed by decisionId.

        Returns {"recorded": bool, ...}.  Never raises — callers should treat
        failure as non-fatal (flywheel data is best-effort at this stage).
        """
        if not decision_id:
            return {"recorded": False, "reason": "no decision_id provided"}

        payload = {
            "decisionId": decision_id,
            "vendorTag": vendor_tag,
            "outcome": outcome,
        }
        try:
            resp = self._session.post(OUTCOME_URL, json=payload, timeout=self.timeout_s)
            return resp.json()
        except Exception as exc:
            logger.warning("Presigate outcome report failed (non-fatal): %s", exc)
            return {"recorded": False, "reason": str(exc)}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _err(msg: str) -> GateResult:
        return GateResult(
            ok=False,
            verdict="",
            confidence=0.0,
            decision_id="",
            reasons=[],
            signals={},
            meta={},
            error=msg,
        )
