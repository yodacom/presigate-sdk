# Presigate SDK

Presigate is a pre-trade decision gate: a market-condition signal API that an autonomous bot or agent calls **before** it acts. You POST the intended action (side, asset, size), and the gate returns a single actionable verdict — **ACT**, **HOLD**, or **ESCALATE** — derived from live regime, microstructure, sentiment, and data-trust signals evaluated server-side. No scoring logic runs in your bot. The gate absorbs market-condition risk so your strategy doesn't have to.

- Live demo: [presigate.com/demo](https://presigate.com/demo)
- Portal and API keys: [presigate.com](https://presigate.com)
- Issues: open a GitHub issue in this repository

---

## Quickstart — 5 minutes

### 1. Get an API key

Go to [presigate.com](https://presigate.com) and request beta access. Once approved, you receive an API key. Keys are **optional during beta** — calls without a key are accepted but are unattributed (tagged `direct`).

### 2. Make your first gate call

**curl:**

```bash
curl -s -X POST https://presigate.com/api/gate \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_PRESIGATE_API_KEY" \
  -d '{"action": {"side": "buy", "asset": "BTCUSDT", "sizeUsd": 500}}'
```

**Python (copy-paste):**

```bash
pip install requests
```

```python
import requests

response = requests.post(
    "https://presigate.com/api/gate",
    headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer YOUR_PRESIGATE_API_KEY",  # omit during beta if you don't have one yet
    },
    json={
        "action": {
            "side": "buy",       # "buy" or "sell"
            "asset": "BTCUSDT",  # only BTCUSDT supported in this version
            "sizeUsd": 500.0,
        }
    },
    timeout=8,
)
data = response.json()

verdict     = data["verdict"]                # "ACT" | "HOLD" | "ESCALATE"
confidence  = data["confidence"]             # 0.0 – 1.0
decision_id = data["meta"]["decisionId"]     # UUIDv7 — save this to report outcomes
reasons     = data["reasons"]                # list of human-readable reason strings

print(f"Verdict: {verdict}  ({confidence:.0%} confidence)")
print(f"Decision ID: {decision_id}")
print(f"Reasons: {reasons}")
```

### 3. Read the verdict

| Verdict | Meaning |
|---|---|
| `ACT` | Conditions are acceptable — proceed with the trade |
| `HOLD` | Conditions are unfavorable — wait and recheck before acting |
| `ESCALATE` | Conditions are anomalous — halt and alert; do not trade |

For any other response (network error, timeout), implement fail-open or fail-closed depending on your risk tolerance. The `PresigateClient` in this repo handles this automatically.

---

## The 5 Primitives

The gate evaluates five independent signal layers. The verdict is derived from their combined state. In lean mode (default), only the verdict, confidence, and reason text are returned. In verbose mode, per-primitive status and rationale are included.

| Primitive | What it signals |
|---|---|
| **RXI** — Regime Index | Whether the market is in a trending, ranging, or chaotic regime. Chaotic or undefined regime conditions trigger HOLD. |
| **MMI** — Market Microstructure Index | Real-time execution quality: spread, order book depth, and estimated slippage for your order size. Thin books or abnormal spreads trigger HOLD. |
| **CSI** — Composite Sentiment Index | Crowd sentiment and fear/greed positioning. Extreme sentiment extremes (peak fear or peak greed) signal elevated reversal risk. |
| **TVI** — Trust and Validity Index | Freshness, completeness, and cross-source consistency of the market data feeding the gate. Stale or inconsistent data triggers ESCALATE. |
| **Timing** | Time-of-day and calendar conditions known to correlate with liquidity gaps or abnormal volatility (e.g. low-liquidity windows). |

None of the internal scoring formulas, weights, or thresholds are exposed through this API. The gate is a black box by design — it returns verdicts, not math.

---

## Freqtrade Integration

The `freqtrade/` directory is a drop-in integration kit. It wires the gate into Freqtrade's `confirm_trade_entry()` callback so every entry signal is pre-screened before an order is placed.

### Files

| File | Purpose |
|---|---|
| `freqtrade/presigate_client.py` | Thin Python client for `/api/gate` and `/api/gate/outcome`. Never raises on gate errors. |
| `freqtrade/presigate_mixin.py` | `PresigateMixin` — drop into any Freqtrade strategy via multiple inheritance. Handles the full gate flow. |
| `freqtrade/presigate_pilot_strategy.py` | Reference strategy (EMA crossover, BTC/USDT) showing the mixin in action. |
| `freqtrade/pilot_config.json` | Freqtrade dry-run config. Starts in shadow mode. |
| `freqtrade/run_gate_harness.py` | Standalone harness — exercises `confirm_trade_entry()` directly without a full Freqtrade loop. Fastest proof. |
| `freqtrade/test_smoke.py` | Smoke test suite (8 tests). Run before deploying. |

### Install

```bash
python3 -m venv venv && source venv/bin/activate
pip install freqtrade requests
```

### Run smoke tests first

```bash
cd freqtrade
python3 test_smoke.py
```

All 8 tests should pass. Test 1 makes a live call to the gate and prints the `decisionId` and verdict.

### Wire the gate into your strategy

```python
from presigate_mixin import PresigateMixin
from freqtrade.strategy import IStrategy

class MyStrategy(PresigateMixin, IStrategy):
    # Your existing strategy logic is unchanged.
    # confirm_trade_entry() is handled by the mixin.
    pass
```

Configure under `"presigate"` in your Freqtrade config:

```json
"presigate": {
  "shadow_mode": true,
  "fail_open": true,
  "timeout_s": 4,
  "gated_pairs": ["BTC/USDT"],
  "min_size_usd": 50.0,
  "api_key": "YOUR_PRESIGATE_API_KEY"
}
```

**Start in shadow mode** (`shadow_mode: true`). The gate is called on every signal. Verdicts are logged with `decisionId`. All trades proceed. After 7–14 days of observation, flip `shadow_mode` to `false` to enforce verdicts.

### Run the harness (fastest proof, no Freqtrade loop needed)

```bash
python3 freqtrade/run_gate_harness.py --mode shadow --count 5
```

See [`freqtrade/README.md`](freqtrade/README.md) for the full integration walkthrough including shadow → active transition, A/B setup, and outcome reporting.

---

## FreqAI Integration

The `examples/` directory contains a ready-to-use FreqAI strategy example showing how to consume Presigate gate results alongside a machine-learning model.

See [`examples/freqai_presigate_example.py`](examples/freqai_presigate_example.py).

The integration uses two complementary patterns:

1. **Hard gate** (`confirm_trade_entry`): The `PresigateMixin` gates every entry regardless of what the ML model predicts. HOLD or ESCALATE blocks the trade. This is the primary integration and requires no changes to your model training pipeline.

2. **Live feature enrichment** (optional): In verbose mode, the gate returns per-primitive status and rationale for each signal layer. These can be cached and fed into future training runs so the model learns which market conditions are correlated with profitable signals over time.

---

## API Reference

### POST /api/gate

```
POST https://presigate.com/api/gate
Content-Type: application/json
Authorization: Bearer YOUR_PRESIGATE_API_KEY   (optional during beta)
```

**Request body:**

```json
{
  "action": {
    "side": "buy",
    "asset": "BTCUSDT",
    "sizeUsd": 500.0
  },
  "verbose": true
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `action.side` | `"buy"` \| `"sell"` | Yes | Intended trade direction |
| `action.asset` | string | No | Trading symbol. Default `"BTCUSDT"`. Only `BTCUSDT` is supported in this version. |
| `action.sizeUsd` | number | Yes | Intended order size in USD (positive, max 10,000,000) |
| `verbose` | boolean | No | If `true`, include per-primitive signals breakdown in response. Also accepted as `?verbose=1` query param. |

**Response — lean (default, `verbose` omitted or `false`):**

```json
{
  "verdict": "ACT",
  "confidence": 0.82,
  "reasons": ["All signals acceptable"],
  "action": {
    "side": "buy",
    "asset": "BTCUSDT",
    "sizeUsd": 500.0
  },
  "meta": {
    "decisionId": "019efb19-9262-7d81-9655-886d900753f6",
    "btcMidprice": 67240.50,
    "spreadBps": 0.42,
    "depthRatio": 8.3,
    "availableDepthUsd": 4150000,
    "estimatedImpact": 0.0021,
    "avoidedSlippageUsd": 0.00,
    "regimeLabel": "trending",
    "volatilityZScore": 0.31,
    "hurstExponent": 0.58,
    "trendStrength": 0.71,
    "fearGreedValue": 62,
    "fearGreedZScore": 0.45,
    "fearGreedLabel": "Greed",
    "csiReal": 0.67,
    "anomalyDetected": false,
    "fetchedAt": "2026-06-30T14:22:01.000Z"
  }
}
```

**Response — verbose (`verbose: true`):**

Same shape as lean, with `signals` added between `confidence` and `reasons`:

```json
{
  "verdict": "ACT",
  "confidence": 0.82,
  "signals": {
    "rxi": { "primitive": "RXI", "score": 0.78, "status": "green", "rationale": "Trend regime confirmed — Hurst 0.58, trend strength 0.71" },
    "mmi": { "primitive": "MMI", "score": 0.91, "status": "green", "rationale": "Spread 0.42bps, depth ratio 8.3x — execution conditions healthy" },
    "csi": { "primitive": "CSI", "score": 0.67, "status": "yellow", "rationale": "Fear & Greed 62 (Greed) — elevated but within normal range" },
    "tvi": { "primitive": "TVI", "score": 0.95, "status": "green", "rationale": "All data sources fresh and consistent" },
    "timing": { "blocked": false, "rationale": "No calendar or time-of-day exclusion active" }
  },
  "reasons": ["All signals acceptable"],
  "action": { "side": "buy", "asset": "BTCUSDT", "sizeUsd": 500.0 },
  "meta": { "..." : "..." }
}
```

**Meta field reference:**

| Field | Type | Description |
|---|---|---|
| `decisionId` | string (UUIDv7) | Unique ID for this gate call. Save this to link trade outcomes back via `/api/gate/outcome`. |
| `btcMidprice` | number | BTC/USDT mid-price at time of gate evaluation (USD) |
| `spreadBps` | number | Current bid-ask spread in basis points |
| `depthRatio` | number | Order book depth ratio — available liquidity relative to your order size |
| `availableDepthUsd` | number | Total available liquidity within normal spread range (USD) |
| `estimatedImpact` | number | Estimated market impact of your order as a fraction of mid-price |
| `avoidedSlippageUsd` | number | Estimated slippage saved by a HOLD verdict (for post-hoc analysis) |
| `regimeLabel` | string | Human-readable regime classification (`"trending"`, `"ranging"`, `"chaotic"`, `"undefined"`) |
| `volatilityZScore` | number | Current volatility relative to recent baseline (z-score) |
| `hurstExponent` | number | Hurst exponent of recent price series (>0.5 = trending, <0.5 = mean-reverting) |
| `trendStrength` | number | Directional trend strength, 0–1 |
| `fearGreedValue` | number | Raw Fear & Greed index value, 0–100 |
| `fearGreedZScore` | number | Fear & Greed relative to recent baseline (z-score) |
| `fearGreedLabel` | string | Fear & Greed label (`"Extreme Fear"`, `"Fear"`, `"Neutral"`, `"Greed"`, `"Extreme Greed"`) |
| `csiReal` | number | Composite sentiment score, 0–1 |
| `anomalyDetected` | boolean | Whether a cross-source data anomaly was flagged (triggers TVI degradation) |
| `fetchedAt` | string (ISO 8601) | Timestamp of live data fetch |

**Error responses:**

| Status | Shape | Cause |
|---|---|---|
| 400 | `{ "error": "Invalid JSON in request body" }` | Malformed JSON |
| 405 | `{ "error": "Use POST /api/gate with { action: { side, asset, sizeUsd } }" }` | Wrong HTTP method |
| 422 | `{ "error": "<description>" }` | Validation failure (missing field, unsupported asset, size out of range) |
| 503 | `{ "error": "Live data unavailable: <reason>" }` | Upstream data fetch failed |

---

### POST /api/gate/outcome

Report a trade outcome back to the Presigate flywheel. This closes the loop for continuous model improvement. Calling this is optional and best-effort — it never blocks a trade.

```
POST https://presigate.com/api/gate/outcome
Content-Type: application/json
Authorization: Bearer YOUR_PRESIGATE_API_KEY   (optional)
```

**Request body:**

```json
{
  "decisionId": "019efb19-9262-7d81-9655-886d900753f6",
  "vendorTag": "freqtrade",
  "outcome": {
    "side": "buy",
    "entryPrice": 67240.50,
    "exitPrice": 67890.00,
    "exitReason": "roi",
    "mode": "dry_run"
  }
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `decisionId` | string (UUID) | Yes | The `decisionId` from the original gate response |
| `vendorTag` | string | Yes | Adapter selector. Must be one of: `freqtrade`, `hummingbot`, `venice_x402`, `3commas`, `direct` |
| `outcome` | object | Yes | Vendor-specific outcome payload. Required fields depend on the adapter. For Freqtrade: `side`, `entryPrice`, `exitPrice`, `exitReason`. |

**Response 200 — recorded:**

```json
{ "recorded": true, "decisionId": "019efb19-9262-7d81-9655-886d900753f6" }
```

**Response 200 — not recorded (non-fatal; no retry needed):**

```json
{ "recorded": false, "reason": "decision_id not found" }
```

The server always returns 200 for outcome calls (even on soft failure) to prevent retry storms from callers.

---

## Support

- Documentation and portal: [presigate.com](https://presigate.com)
- Live demo: [presigate.com/demo](https://presigate.com/demo)
- GitHub issues: open an issue in this repository
- Beta access and API keys: [presigate.com](https://presigate.com)

---

## License

MIT — see [LICENSE](LICENSE).
