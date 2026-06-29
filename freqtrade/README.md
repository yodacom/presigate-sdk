# Presigate x Freqtrade — Integration Kit

Scope: **BTC/USDT only** (multi-asset requires per-asset MMI baselines — tracked as follow-up).

This kit integrates the live [Presigate gate API](https://presigate.com/api/gate) into a Freqtrade strategy via the `confirm_trade_entry()` callback. Before every entry order, the gate evaluates live execution conditions (regime, microstructure, sentiment, data trust) and returns `ACT`, `HOLD`, or `ESCALATE`. The strategy enforces the verdict or logs it (shadow mode).

---

## Files

| File | Purpose |
|---|---|
| `presigate_client.py` | Thin Python client for `/api/gate` and `/api/gate/outcome`. Never raises. |
| `presigate_mixin.py` | `PresigateMixin` — drop into any Freqtrade strategy. Handles the full gate flow. |
| `presigate_pilot_strategy.py` | Reference strategy (EMA crossover on BTC/USDT) showing the mixin in action. |
| `pilot_config.json` | Freqtrade dry-run config for the pilot. Starts in shadow mode. |
| `run_gate_harness.py` | Standalone runner. Exercises `confirm_trade_entry()` directly — no Freqtrade loop needed. Useful for testing gate behavior before running the full bot. |
| `test_smoke.py` | 7-test smoke suite: live gate call, error handling, shadow/active logic, fail-open. |

---

## Prerequisites

- Python 3.12 (arm64 on Apple Silicon)
- Freqtrade 2026.x

```bash
arch -arm64 python3 -m venv ft_venv
source ft_venv/bin/activate
pip install freqtrade requests
```

For partners not on Apple Silicon, use plain `python3 -m venv ft_venv`.

---

## Step 0 — Run the smoke tests

Verify the gate is reachable and the integration logic is sound before starting Freqtrade:

```bash
python3 test_smoke.py
```

All 7 tests should pass. Test 1 makes a live call to `https://presigate.com/api/gate` and prints the decisionId and verdict.

---

## Step 1 — Run the harness (fastest proof)

The harness exercises `confirm_trade_entry()` directly without needing the full Freqtrade bot loop. Good for validating the integration before deploying.

**Shadow mode (default — zero risk):**

```bash
python3 run_gate_harness.py --mode shadow --count 5
```

Gate is called live. Verdicts are logged. All entries proceed.

**Active mode (enforce verdicts):**

```bash
python3 run_gate_harness.py --mode active --count 5
```

HOLD/ESCALATE verdicts block entry. ACT allows.

**Demo the block path (for when live conditions are healthy/ACT):**

```bash
python3 run_gate_harness.py --mode active --force-hold 0.5 --count 5
```

`force_hold` randomly overrides 50% of ACT results to False, demonstrating that the block path in `confirm_trade_entry()` works end-to-end. Label these as SIMULATED in your records — the gate still makes a live call; only the local enforcement is overridden.

---

## Step 2 — Run Freqtrade dry-run (full loop)

```bash
freqtrade create-userdir --userdir user_data

freqtrade trade \
  --config pilot_config.json \
  --strategy PresigateGatedBTCStrategy \
  --userdir user_data
```

The bot starts in **shadow mode** (`"shadow_mode": true` in config). Gate is called on every EMA crossover signal; verdicts are logged; all trades proceed.

**What you'll see in the log:**

```
Presigate [SHADOW] | pair=BTC/USDT | side=buy | size_usd=92.94 |
  verdict=ACT | confidence=0.80 |
  decisionId=019efb19-9262-7d81-9655-886d900753f6 |
  elapsed=713ms |
  reason=All signals acceptable — composite score 0.80 exceeds ACT threshold 0.6 |
  btcMid=59684.55 | spread=0.0168bps | depth=59899.6x
```

If the gate would have blocked: `Presigate [SHADOW] would have BLOCKED | pair=BTC/USDT | verdict=HOLD | decisionId=...`

---

## Step 3 — Switch from shadow to active

Edit `pilot_config.json`:

```json
"presigate": {
  "shadow_mode": false,
  "force_hold_pct": 0.0
}
```

Restart Freqtrade. Subsequent HOLD or ESCALATE verdicts will cause `confirm_trade_entry()` to return `False` and Freqtrade will not place the entry order.

**Active mode block log line:**

```
Presigate [ACTIVE] BLOCKED ENTRY | pair=BTC/USDT | verdict=HOLD |
  decisionId=019efb19-bcec-7c21-bf5c-27f286da90ab |
  reason=MMI: depth insufficient ...
```

---

## Step 4 — Gated vs ungated A/B

To run a controlled A/B, split by pair:

```json
"presigate": {
  "shadow_mode": false,
  "gated_pairs": ["BTC/USDT"]
}
```

Add more pairs to the whitelist. Only pairs in `gated_pairs` will be gated; others proceed ungated. Compare P&L and slippage between gated and ungated pairs over 30 days.

**Key metrics to capture per trade:**

| Metric | How |
|---|---|
| Gate hold rate | HOLD+ESCALATE / total signals |
| Execution slippage | (fill_price - mid_price) / mid_price at fill |
| P&L on held trades | Price move in 30 min after a HOLD |
| Net P&L delta | Gated group vs ungated group, same capital |

---

## Configuration reference

All Presigate options live under `"presigate"` in `pilot_config.json`:

| Key | Default | Description |
|---|---|---|
| `shadow_mode` | `true` | Log verdicts only; never block. Start here. |
| `fail_open` | `true` | Allow trade if gate is unreachable. Flip to `false` for strict risk budgets. |
| `timeout_s` | `4` | HTTP timeout for gate call. Keep short to avoid stalling Freqtrade's loop. |
| `gated_pairs` | `["BTC/USDT"]` | Pairs to gate. `null` or omit = gate all pairs. |
| `min_size_usd` | `50.0` | Skip gate for orders below this USD value. |
| `force_hold_pct` | `0.0` | Fraction of entries to force-block (demo only, remove in production). |

---

## Gate API contract

### Lean (default — public/partner-facing)

```
POST https://presigate.com/api/gate
Content-Type: application/json

{
  "action": {
    "side": "buy",
    "asset": "BTCUSDT",
    "sizeUsd": 100.0
  }
}
```

Response — `signals` is **omitted**:
```json
{
  "verdict": "ACT",
  "confidence": 0.80,
  "reasons": ["All signals acceptable ..."],
  "meta": {
    "decisionId": "019efb19-9262-7d81-9655-886d900753f6",
    "btcMidprice": 59684.55,
    ...
  }
}
```

In active mode, the verdict is all the mixin needs to block or allow. Lean is the right default — it protects the per-primitive signal decomposition (trade secret).

### Verbose (opt-in — shadow mode / dev transparency)

Add `"verbose": true` to the request body (or `?verbose=1` as a query param):

```json
{
  "action": { "side": "buy", "asset": "BTCUSDT", "sizeUsd": 100.0 },
  "verbose": true
}
```

Response — `signals` **included**:
```json
{
  "verdict": "ACT",
  "confidence": 0.80,
  "signals": {
    "rxi": { "primitive": "RXI", "score": 0.78, "status": "green", "rationale": "..." },
    "mmi": { "primitive": "MMI", "score": 0.91, "status": "green", "rationale": "..." },
    "csi": { "primitive": "CSI", "score": 0.62, "status": "yellow", "rationale": "..." },
    "tvi": { "primitive": "TVI", "score": 0.95, "status": "green", "rationale": "..." },
    "timing": { "blocked": false, "rationale": "..." }
  },
  "reasons": ["All signals acceptable ..."],
  "meta": { "decisionId": "...", "btcMidprice": 59684.55, ... }
}
```

The mixin automatically uses **verbose in shadow mode** (so devs can see which primitive caused a HOLD) and **lean in active mode** (verdict only — no unnecessary data transfer). This behavior is configured in `presigate_mixin.py`; no changes needed in `pilot_config.json`.

The flywheel always stores full per-primitive scores server-side regardless of response mode.

Verdicts: `ACT` (proceed), `HOLD` (wait, recheck), `ESCALATE` (halt, alert).

---

## Outcome reporting

When a trade closes, call from your strategy's `custom_exit()`:

```python
def custom_exit(self, pair, trade, current_time, current_rate, current_profit, **kwargs):
    self.report_trade_outcome(trade, current_rate, exit_reason=kwargs.get('exit_reason', ''))
    return None
```

This posts the outcome to `/api/gate/outcome` keyed by `decisionId`. No-ops gracefully if Supabase is not connected (flywheel is best-effort at this stage).

---

## Scope note

The gate currently computes on live BTC/USDT data (Kraken order book + 5m klines + Fear & Greed). Multi-asset support requires per-asset spread baselines for accurate MMI scoring and is tracked as a follow-up. The pilot is intentionally scoped to BTC/USDT.

**Exchange note:** Binance is geo-blocked at the API level from US IPs (HTTP 451). Freqtrade's market data fetching uses Kraken for this pilot. The Presigate gate itself uses Binance data server-side (deployed on Cloudflare, EU edge) — no geo-restriction applies.

---

## Latency

Gate call p50: 200-400ms. p99: under 2s. Acceptable for 1m/5m candle strategies. For sub-second execution, call the gate on signal generation, not at order submission.
