"""
test_smoke.py — Smoke tests for the Presigate gate client + mixin logic.

Run with:
    python3 test_smoke.py

Tests:
  1. Gate client: lean call (default) — signals NOT in response
  2. Gate client: verbose call — signals ARE in response
  3. Gate client: timeout/error path returns ok=False with error string (no raise)
  4. Gate client: asset normalisation (BTC/USDT → BTCUSDT)
  5. Mixin logic: shadow_mode=True always returns True regardless of verdict
  6. Mixin logic: active_mode HOLD → False, ACT → True, ESCALATE → False
  7. Mixin logic: gate error + fail_open=True → True; fail_open=False → False
  8. Outcome reporter: no decision_id → no-op (no raise)
"""

from __future__ import annotations

import sys
import traceback
from unittest.mock import MagicMock, patch

# -- Ensure local modules resolve --
import os
sys.path.insert(0, os.path.dirname(__file__))

from presigate_client import PresigateClient
from presigate_mixin import PresigateMixin

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"

results: list[tuple[str, bool, str]] = []


def run(name: str, fn):
    try:
        fn()
        results.append((name, True, ""))
        print(f"  {PASS}  {name}")
    except AssertionError as e:
        results.append((name, False, str(e)))
        print(f"  {FAIL}  {name}: {e}")
    except Exception as e:
        results.append((name, False, traceback.format_exc()))
        print(f"  {FAIL}  {name}: {e}")


# ---------------------------------------------------------------------------
# Helper: build a fake PresigateMixin subclass without a full Freqtrade stack
# ---------------------------------------------------------------------------

def make_mixin(shadow_mode: bool = True, fail_open: bool = True) -> PresigateMixin:
    class FakeMixin(PresigateMixin):
        config = {
            "presigate": {
                "shadow_mode": shadow_mode,
                "fail_open": fail_open,
                "gated_pairs": ["BTC/USDT"],
                "min_size_usd": 10.0,
                "force_hold_pct": 0.0,
                "timeout_s": 4,
            }
        }
        # Reset class-level cache between instances
        _presigate_config = None
        _presigate_client = None
        _pending_gate_decisions: dict = {}

    return FakeMixin()


FAKE_ENTRY_KWARGS = dict(
    pair="BTC/USDT",
    order_type="limit",
    amount=0.01,
    rate=60000.0,
    time_in_force="GTC",
    current_time=__import__("datetime").datetime.utcnow(),
    entry_tag=None,
    side="long",
)


# ---------------------------------------------------------------------------
# Test 1 — Lean call (default): signals must NOT be in response
# ---------------------------------------------------------------------------

def test_lean_call():
    client = PresigateClient(timeout_s=8)
    result = client.check(side="buy", asset="BTCUSDT", size_usd=500.0)

    assert result["ok"], f"Lean gate call failed: {result['error']}"
    assert result["verdict"] in ("ACT", "HOLD", "ESCALATE"), f"Unknown verdict: {result['verdict']}"
    assert result["decision_id"], "decisionId missing from lean response"
    assert 0.0 <= result["confidence"] <= 1.0, f"Confidence out of range: {result['confidence']}"
    assert isinstance(result["reasons"], list), "reasons must be a list"
    # Lean mode: signals dict must be empty (not populated from API response)
    assert result["signals"] == {}, f"Lean response must NOT return signals; got: {result['signals']}"

    meta = result["meta"]
    assert "btcMidprice" in meta, "meta.btcMidprice missing"
    assert float(meta["btcMidprice"]) > 1000, f"btcMidprice implausible: {meta['btcMidprice']}"

    print(
        f"       [lean] verdict={result['verdict']} confidence={result['confidence']:.2f} "
        f"decisionId={result['decision_id'][:12]}... "
        f"btcMid={meta.get('btcMidprice', '?'):.2f}"
    )


# ---------------------------------------------------------------------------
# Test 2 — Verbose call: signals MUST be in response
# ---------------------------------------------------------------------------

def test_verbose_call():
    client = PresigateClient(timeout_s=8)
    result = client.check(side="buy", asset="BTCUSDT", size_usd=500.0, verbose=True)

    assert result["ok"], f"Verbose gate call failed: {result['error']}"
    assert result["verdict"] in ("ACT", "HOLD", "ESCALATE"), f"Unknown verdict: {result['verdict']}"
    assert isinstance(result["signals"], dict) and len(result["signals"]) > 0, (
        f"Verbose response must include signals breakdown; got: {result['signals']}"
    )
    assert "rxi" in result["signals"] or "mmi" in result["signals"], (
        f"signals missing expected primitives: {result['signals'].keys()}"
    )

    print(
        f"       [verbose] verdict={result['verdict']} signals keys={list(result['signals'].keys())}"
    )


# ---------------------------------------------------------------------------
# Test 3 — Timeout / error → ok=False, no raise
# ---------------------------------------------------------------------------

def test_gate_error_no_raise():
    client = PresigateClient(timeout_s=0.001)  # guaranteed timeout
    result = client.check(side="buy", asset="BTCUSDT", size_usd=500.0)
    assert not result["ok"], "Expected ok=False on timeout"
    assert result["error"], "Expected non-empty error string"
    assert result["verdict"] == "", "Verdict should be empty on error"


# ---------------------------------------------------------------------------
# Test 4 — Asset normalisation
# ---------------------------------------------------------------------------

def test_asset_normalisation():
    """BTC/USDT → BTCUSDT should be handled by the client."""
    client = PresigateClient(timeout_s=8)
    # The API only supports BTCUSDT; if normalisation is wrong it returns 422
    result = client.check(side="buy", asset="BTC/USDT", size_usd=500.0)
    assert result["ok"], f"Normalisation failed: {result['error']}"


# ---------------------------------------------------------------------------
# Test 5 — Shadow mode always returns True
# ---------------------------------------------------------------------------

def test_shadow_mode_always_true():
    mixin = make_mixin(shadow_mode=True)

    for verdict in ("ACT", "HOLD", "ESCALATE"):
        fake_result = {
            "ok": True, "verdict": verdict, "confidence": 0.7,
            "decision_id": "test-id-" + verdict, "reasons": [f"test {verdict}"],
            "signals": {}, "meta": {"btcMidprice": 60000.0, "spreadBps": 1.0, "depthRatio": 5.0},
            "error": "",
        }
        with patch.object(mixin, "_pg_client") as mock_client:
            mock_instance = MagicMock()
            mock_instance.check.return_value = fake_result
            mock_client.return_value = mock_instance

            allowed = mixin.confirm_trade_entry(**FAKE_ENTRY_KWARGS)
            assert allowed, f"Shadow mode should allow trade for verdict={verdict}"


# ---------------------------------------------------------------------------
# Test 6 — Active mode enforces verdict
# ---------------------------------------------------------------------------

def test_active_mode_enforces_verdict():
    mixin = make_mixin(shadow_mode=False)

    cases = [
        ("ACT", True),
        ("HOLD", False),
        ("ESCALATE", False),
    ]

    for verdict, expected in cases:
        fake_result = {
            "ok": True, "verdict": verdict, "confidence": 0.7,
            "decision_id": "test-id", "reasons": [f"test {verdict}"],
            "signals": {}, "meta": {"btcMidprice": 60000.0, "spreadBps": 1.0, "depthRatio": 5.0},
            "error": "",
        }
        with patch.object(mixin, "_pg_client") as mock_client:
            mock_instance = MagicMock()
            mock_instance.check.return_value = fake_result
            mock_client.return_value = mock_instance

            allowed = mixin.confirm_trade_entry(**FAKE_ENTRY_KWARGS)
            assert allowed == expected, (
                f"Active mode: verdict={verdict} expected={expected} got={allowed}"
            )


# ---------------------------------------------------------------------------
# Test 7 — Gate error + fail_open
# ---------------------------------------------------------------------------

def test_fail_open():
    mixin_open = make_mixin(shadow_mode=False, fail_open=True)
    mixin_closed = make_mixin(shadow_mode=False, fail_open=False)

    error_result = {
        "ok": False, "verdict": "", "confidence": 0.0,
        "decision_id": "", "reasons": [], "signals": {}, "meta": {},
        "error": "Connection refused",
    }

    for mixin, expected_allow in [(mixin_open, True), (mixin_closed, False)]:
        with patch.object(mixin, "_pg_client") as mock_client:
            mock_instance = MagicMock()
            mock_instance.check.return_value = error_result
            mock_client.return_value = mock_instance

            allowed = mixin.confirm_trade_entry(**FAKE_ENTRY_KWARGS)
            assert allowed == expected_allow, (
                f"fail_open={mixin._fail_open()} expected={expected_allow} got={allowed}"
            )


# ---------------------------------------------------------------------------
# Test 8 — Outcome report no-op on missing decisionId
# ---------------------------------------------------------------------------

def test_outcome_no_op():
    mixin = make_mixin()
    fake_trade = MagicMock()
    fake_trade.pair = "BTC/USDT"
    # No pending decisions stored → should no-op without error
    mixin.report_trade_outcome(trade=fake_trade, exit_price=61000.0)
    # If we reach here without exception, the test passes


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("\nPresgate integration smoke tests")
    print("=" * 50)

    run("1. Lean call — signals omitted (default)", test_lean_call)
    run("2. Verbose call — signals present", test_verbose_call)
    run("3. Timeout → ok=False, no raise", test_gate_error_no_raise)
    run("4. Asset normalisation BTC/USDT → BTCUSDT", test_asset_normalisation)
    run("5. Shadow mode always returns True", test_shadow_mode_always_true)
    run("6. Active mode enforces HOLD/ESCALATE/ACT", test_active_mode_enforces_verdict)
    run("7. Gate error + fail_open=True/False", test_fail_open)
    run("8. Outcome no-op on missing decisionId", test_outcome_no_op)

    print("=" * 50)
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    status = PASS if passed == total else FAIL
    print(f"\nResult: {status}  {passed}/{total} tests passed\n")
    if passed < total:
        for name, ok, err in results:
            if not ok:
                print(f"  FAILED: {name}")
                if err:
                    print(f"    {err[:300]}")

    sys.exit(0 if passed == total else 1)
