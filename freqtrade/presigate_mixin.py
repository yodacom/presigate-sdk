"""
presigate_mixin.py — Freqtrade strategy mixin that gates every entry via Presigate.

Drop this into any Freqtrade strategy using multiple inheritance:

    from presigate_mixin import PresigateMixin

    class MyStrategy(PresigateMixin, IStrategy):
        # No changes to your existing strategy logic.
        # confirm_trade_entry() is handled here.
        pass

Configuration keys (set in config.json under "presigate"):
    {
      "presigate": {
        "shadow_mode": true,      // log verdicts but never block (default: true — SAFE start)
        "fail_open": true,        // allow trade if gate is unreachable (default: true)
        "timeout_s": 4,           // gate request timeout in seconds
        "gated_pairs": ["BTC/USDT"],  // pairs to gate; null/absent = gate all
        "min_size_usd": 50.0,     // skip gate for tiny orders below this threshold
        "api_key": ""             // optional: Presigate API key for per-client attribution
                                  // obtain via INSERT into api_key_registry (see README)
      }
    }

SHADOW MODE (default):
    The gate is called on every eligible entry.  The verdict is logged with
    decisionId, confidence, and reasons.  The trade ALWAYS proceeds.
    Use this for the first 7-14 days to observe gate behavior without
    affecting strategy outcomes.

ACTIVE MODE (shadow_mode = false):
    HOLD → return False (trade blocked)
    ESCALATE → return False (trade blocked, warning logged)
    ACT → return True (trade proceeds)
    Gate error → fail_open=true: trade proceeds | fail_open=false: trade blocked

OUTCOME REPORTING:
    When a trade closes, call self.report_trade_outcome(trade, exit_price) from
    your custom_exit() callback.  This wires the outcome back to Presigate for
    flywheel learning (no-ops gracefully if Supabase not connected).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

from presigate_client import PresigateClient

logger = logging.getLogger(__name__)


class PresigateMixin:
    """
    Mixin for Freqtrade IStrategy subclasses.

    Python MRO ensures confirm_trade_entry() here takes precedence over IStrategy's
    default (which returns True).  If the subclass defines its own
    confirm_trade_entry(), it MUST call super().confirm_trade_entry(...) or the
    gate won't run.
    """

    # Populated lazily on first call — avoids issues during Freqtrade's
    # class construction phase where config may not yet be available.
    _presigate_client: PresigateClient | None = None
    _presigate_config: dict | None = None

    # In-memory store for pending outcomes: {decision_id: {pair, size_usd, verdict, ts}}
    # Used to match trade.open_order_id → decisionId for outcome reporting.
    _pending_gate_decisions: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    def _pg_config(self) -> dict:
        """Lazily read presigate config block from Freqtrade config."""
        if self._presigate_config is None:
            # self.config is the Freqtrade config dict, available after __init__
            cfg = getattr(self, "config", {})
            self._presigate_config = cfg.get("presigate", {})
        return self._presigate_config

    def _pg_client(self) -> PresigateClient:
        if self._presigate_client is None:
            cfg = self._pg_config()
            timeout = float(cfg.get("timeout_s", 4))
            api_key = str(cfg.get("api_key", "") or "")
            self._presigate_client = PresigateClient(timeout_s=timeout, api_key=api_key)
        return self._presigate_client

    def _shadow_mode(self) -> bool:
        return bool(self._pg_config().get("shadow_mode", True))

    def _fail_open(self) -> bool:
        return bool(self._pg_config().get("fail_open", True))

    def _gated_pairs(self) -> set[str] | None:
        """Returns a set of gated pairs, or None to mean 'gate all'."""
        pairs = self._pg_config().get("gated_pairs")
        if pairs is None:
            return None  # gate all
        return set(pairs)

    def _min_size_usd(self) -> float:
        return float(self._pg_config().get("min_size_usd", 50.0))

    # ------------------------------------------------------------------
    # confirm_trade_entry — the Freqtrade hook
    # ------------------------------------------------------------------

    def confirm_trade_entry(
        self,
        pair: str,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        current_time: datetime,
        entry_tag: str | None,
        side: str,
        **kwargs: Any,
    ) -> bool:
        """
        Called by Freqtrade before every entry order.

        Returns True to allow the trade, False to block it.
        In shadow mode, always returns True regardless of verdict.
        """
        mode_label = "[SHADOW]" if self._shadow_mode() else "[ACTIVE]"

        # -- Eligibility checks --

        # Check pair filter
        gated = self._gated_pairs()
        if gated is not None and pair not in gated:
            logger.debug("Presigate %s pair %s not in gated_pairs — skipping gate", mode_label, pair)
            return True

        size_usd = amount * rate

        if size_usd < self._min_size_usd():
            logger.debug(
                "Presigate %s pair=%s size_usd=%.2f below min_size_usd=%.2f — skipping",
                mode_label, pair, size_usd, self._min_size_usd()
            )
            return True

        # Normalize side: Freqtrade uses "long"/"short" in some paths
        gate_side = "sell" if side in ("short", "sell") else "buy"

        # -- Call the gate --
        # Shadow mode requests verbose so devs can see WHY the gate held.
        # Active mode uses lean (verdict only) — signals are not needed to block.
        t0 = time.monotonic()
        result = self._pg_client().check(
            side=gate_side,
            asset=pair.replace("/", ""),   # "BTC/USDT" → "BTCUSDT"
            size_usd=size_usd,
            verbose=self._shadow_mode(),
        )
        elapsed_ms = (time.monotonic() - t0) * 1000

        # -- Handle gate error (network, timeout, unexpected response) --
        if not result["ok"]:
            if self._fail_open():
                logger.warning(
                    "Presigate %s GATE_ERROR (fail-open) | pair=%s | size_usd=%.2f | error=%s | elapsed=%.0fms — ALLOWING trade",
                    mode_label, pair, size_usd, result["error"], elapsed_ms,
                )
                return True
            else:
                logger.warning(
                    "Presigate %s GATE_ERROR (fail-closed) | pair=%s | size_usd=%.2f | error=%s | elapsed=%.0fms — BLOCKING trade",
                    mode_label, pair, size_usd, result["error"], elapsed_ms,
                )
                return False

        verdict = result["verdict"]
        decision_id = result["decision_id"]
        confidence = result["confidence"]
        primary_reason = result["reasons"][0] if result["reasons"] else "(no reason)"
        meta = result.get("meta", {})

        # -- Structured log line (machine-parseable) --
        logger.info(
            "Presigate %s | pair=%s | side=%s | size_usd=%.2f | verdict=%s | confidence=%.2f | decisionId=%s | elapsed=%.0fms | reason=%s | btcMid=%.2f | spread=%.4fbps | depth=%.1fx",
            mode_label,
            pair,
            gate_side,
            size_usd,
            verdict,
            confidence,
            decision_id,
            elapsed_ms,
            primary_reason,
            float(meta.get("btcMidprice", 0)),
            float(meta.get("spreadBps", 0)),
            float(meta.get("depthRatio", 0)),
        )

        # Store decision for outcome reporting
        if decision_id:
            self._pending_gate_decisions[decision_id] = {
                "pair": pair,
                "side": gate_side,
                "size_usd": size_usd,
                "verdict": verdict,
                "ts": current_time.isoformat() if hasattr(current_time, "isoformat") else str(current_time),
                "mid_at_decision": float(meta.get("btcMidprice", 0)),
            }

        # -- Shadow mode: always proceed, regardless of verdict --
        if self._shadow_mode():
            if verdict != "ACT":
                logger.info(
                    "Presigate [SHADOW] would have BLOCKED | pair=%s | verdict=%s | decisionId=%s",
                    pair, verdict, decision_id,
                )
            return True

        # -- Active mode: enforce the verdict --
        if verdict == "ACT":
            return True

        if verdict == "HOLD":
            logger.info(
                "Presigate [ACTIVE] BLOCKED ENTRY | pair=%s | verdict=HOLD | decisionId=%s | reason=%s",
                pair, decision_id, primary_reason,
            )
            return False

        if verdict == "ESCALATE":
            logger.warning(
                "Presigate [ACTIVE] BLOCKED ENTRY (ESCALATE) | pair=%s | decisionId=%s | reason=%s",
                pair, decision_id, primary_reason,
            )
            return False

        # Unknown verdict → fail-open (defensive)
        logger.warning(
            "Presigate [ACTIVE] unknown verdict %r — failing open | pair=%s | decisionId=%s",
            verdict, pair, decision_id,
        )
        return True

    # ------------------------------------------------------------------
    # Outcome reporting — call from custom_exit()
    # ------------------------------------------------------------------

    def report_trade_outcome(
        self,
        trade: Any,
        exit_price: float,
        exit_reason: str = "unknown",
        lookback_seconds: int = 1800,
    ) -> None:
        """
        Report the trade outcome to Presigate's flywheel endpoint.

        Call this from your strategy's custom_exit() or from a bot_loop_start()
        periodic check on closed trades.  No-ops if decisionId is not found.

        Example:
            def custom_exit(self, pair, trade, current_time, current_rate, current_profit, **kwargs):
                self.report_trade_outcome(trade, current_rate)
                return None
        """
        # Look up by pair — match most recent pending decision for this pair
        decision_id = None
        pending_entry = None
        for did, entry in list(self._pending_gate_decisions.items()):
            if entry["pair"] == getattr(trade, "pair", ""):
                decision_id = did
                pending_entry = entry
                break

        if not decision_id or not pending_entry:
            return

        # Remove from pending
        del self._pending_gate_decisions[decision_id]

        mid_at_decision = pending_entry["mid_at_decision"] or getattr(trade, "open_rate", exit_price)
        entry_price = getattr(trade, "open_rate", mid_at_decision)

        outcome_payload = {
            "mode": "dry_run",
            "side": pending_entry["side"],
            "entryPrice": float(entry_price),
            "midAtDecision": float(mid_at_decision),
            "exitPrice": float(exit_price),
            "exitReason": exit_reason,
            "lookbackSeconds": lookback_seconds,
            "observedAt": datetime.utcnow().isoformat() + "Z",
        }

        result = self._pg_client().report_outcome(
            decision_id=decision_id,
            vendor_tag="freqtrade",
            outcome=outcome_payload,
        )
        logger.info(
            "Presigate outcome reported | decisionId=%s | recorded=%s | pair=%s | exit_reason=%s",
            decision_id,
            result.get("recorded"),
            pending_entry["pair"],
            exit_reason,
        )
