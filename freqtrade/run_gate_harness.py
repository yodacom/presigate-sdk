"""
run_gate_harness.py — Standalone runner that exercises confirm_trade_entry()
directly against the live Presigate gate without needing a full Freqtrade loop.

This is the proof artifact: it shows that:
  1. The gate is called with Freqtrade-style trade context
  2. The live presigate.com/api/gate returns a verdict + decisionId
  3. SHADOW mode: all entries allowed regardless of verdict (the log shows what WOULD have blocked)
  4. ACTIVE mode: HOLD/ESCALATE verdicts return False from confirm_trade_entry()
  5. Forced-HOLD simulation: a second run blocks some entries deterministically

Usage:
    python3 run_gate_harness.py [--mode active] [--force-hold 0.5] [--count 5]

Output includes full gate log lines with decisionIds — quote these as evidence.
"""

from __future__ import annotations

import argparse
import datetime
import logging
import sys
import os
import time
import random

# Ensure local modules resolve
sys.path.insert(0, os.path.dirname(__file__))

from presigate_client import PresigateClient
from presigate_mixin import PresigateMixin

# Configure logging to show INFO and above with timestamps
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S,%f",
    stream=sys.stdout,
)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)

logger = logging.getLogger("harness")


# ---------------------------------------------------------------------------
# Minimal fake strategy that uses the mixin
# ---------------------------------------------------------------------------

class FakeStrategy(PresigateMixin):
    """
    Minimal stand-in for a Freqtrade IStrategy.
    Implements only what the mixin needs.
    """
    def __init__(self, shadow_mode: bool, force_hold_pct: float, api_key: str = ""):
        self.config = {
            "presigate": {
                "shadow_mode": shadow_mode,
                "fail_open": True,
                "gated_pairs": ["BTC/USDT"],
                "min_size_usd": 10.0,
                "force_hold_pct": force_hold_pct,
                "timeout_s": 6,
                "api_key": api_key,
            }
        }
        self._presigate_config = None
        self._presigate_client = None
        self._pending_gate_decisions = {}


def run_harness(mode: str, force_hold_pct: float, count: int, api_key: str = ""):
    shadow_mode = (mode == "shadow")
    strategy = FakeStrategy(shadow_mode=shadow_mode, force_hold_pct=force_hold_pct, api_key=api_key)

    mode_label = "SHADOW" if shadow_mode else "ACTIVE"
    key_label = f"{api_key[:12]}..." if api_key else "(none — direct/anon)"
    print(f"\n{'='*60}")
    print(f"Presigate Freqtrade Harness — mode={mode_label} | calls={count} | force_hold={force_hold_pct:.0%}")
    print(f"Gate URL: https://presigate.com/api/gate")
    print(f"API Key:  {key_label}")
    print(f"{'='*60}\n")

    results = []

    for i in range(count):
        # Simulate a Freqtrade entry signal context
        size_usd = random.uniform(80, 150)
        btc_price = 59000 + random.uniform(-2000, 2000)
        amount = size_usd / btc_price
        current_time = datetime.datetime.utcnow()

        print(f"--- Signal {i+1}/{count} | BTC/USDT | amount={amount:.6f} | rate={btc_price:.2f} | size_usd=${size_usd:.2f}")

        t0 = time.monotonic()
        allowed = strategy.confirm_trade_entry(
            pair="BTC/USDT",
            order_type="limit",
            amount=amount,
            rate=btc_price,
            time_in_force="GTC",
            current_time=current_time,
            entry_tag=None,
            side="long",
        )
        elapsed_ms = (time.monotonic() - t0) * 1000

        action = "ALLOWED" if allowed else "BLOCKED"
        print(f"    --> confirm_trade_entry() returned {allowed} ({action}) | elapsed={elapsed_ms:.0f}ms\n")
        results.append({"signal": i + 1, "allowed": allowed, "elapsed_ms": elapsed_ms})

        # Small delay to avoid hammering the gate
        if i < count - 1:
            time.sleep(1.5)

    # Summary
    allowed_count = sum(1 for r in results if r["allowed"])
    blocked_count = sum(1 for r in results if not r["allowed"])
    avg_latency = sum(r["elapsed_ms"] for r in results) / len(results)

    print(f"\n{'='*60}")
    print(f"Run complete: {count} gate calls | {allowed_count} ALLOWED | {blocked_count} BLOCKED")
    print(f"Average gate latency: {avg_latency:.0f}ms")
    print(f"Mode: {mode_label}")
    if force_hold_pct > 0:
        print(f"NOTE: force_hold_pct={force_hold_pct:.0%} — some blocks above are SIMULATED (forced), not live gate HOLD")
    print(f"{'='*60}\n")

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Presigate gate harness for Freqtrade confirm_trade_entry")
    parser.add_argument("--mode", choices=["shadow", "active"], default="shadow",
                        help="shadow=log only, active=enforce verdict (default: shadow)")
    parser.add_argument("--force-hold", type=float, default=0.0,
                        help="Fraction of entries to force-block (0.0–1.0) to demo block path (default: 0.0)")
    parser.add_argument("--count", type=int, default=5,
                        help="Number of entry signals to simulate (default: 5)")
    parser.add_argument("--api-key", type=str, default="",
                        help="Presigate API key for per-client attribution (Bearer token)")
    args = parser.parse_args()

    run_harness(mode=args.mode, force_hold_pct=args.force_hold, count=args.count, api_key=args.api_key)
