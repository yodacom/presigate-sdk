# Presigate SDK

The Presigate gate API evaluates live market conditions before a trade executes and returns a single actionable verdict: **ACT**, **HOLD**, or **ESCALATE**. The gate combines regime, microstructure, sentiment, and data-trust signals into one pre-flight decision — so your bot stops trading into bad conditions automatically.

This repository contains the official client integration kit for connecting to the hosted Presigate API at `https://presigate.com/api/gate`. No scoring logic runs locally. The gate runs server-side; your code only sends trade context and reads back the verdict.

Full whitepaper and API documentation: [presigate.com](https://presigate.com)

---

## Getting an API Key

1. Go to [presigate.com](https://presigate.com) and request beta access.
2. Once approved, you will receive an API key.
3. Pass it as `api_key` in your client config (see below). Keys are optional during beta — calls without a key are accepted but are unattributed.

---

## Quickstart — Python

### Install dependencies

```bash
pip install requests
```

### Call the gate

```python
from freqtrade.presigate_client import PresigateClient

client = PresigateClient(api_key="your-api-key")  # api_key is optional during beta

result = client.check(side="buy", asset="BTCUSDT", size_usd=500.0)

if result["ok"]:
    verdict = result["verdict"]       # "ACT" | "HOLD" | "ESCALATE"
    decision_id = result["decision_id"]
    print(f"Gate verdict: {verdict} (decisionId: {decision_id})")
else:
    print(f"Gate unreachable: {result['error']}")
    # Implement fail-open or fail-closed depending on your risk tolerance
```

**Verdicts:**
- `ACT` — conditions are acceptable, proceed with the trade
- `HOLD` — conditions are unfavorable, wait and recheck
- `ESCALATE` — conditions are anomalous, halt and alert

---

## Freqtrade Integration

The `freqtrade/` directory contains a drop-in integration kit for [Freqtrade](https://www.freqtrade.io/).

### Files

| File | Purpose |
|---|---|
| `freqtrade/presigate_client.py` | Thin Python client for the gate API. Never raises. |
| `freqtrade/presigate_mixin.py` | Drop into any Freqtrade strategy via multiple inheritance. |
| `freqtrade/presigate_pilot_strategy.py` | Reference strategy (EMA crossover on BTC/USDT) showing the mixin in action. |
| `freqtrade/pilot_config.json` | Freqtrade dry-run config. Starts in shadow mode. |
| `freqtrade/run_gate_harness.py` | Standalone harness — test the integration without a full Freqtrade loop. |
| `freqtrade/test_smoke.py` | Smoke test suite (8 tests). Run before deploying. |

### Prerequisites

```bash
python3 -m venv venv
source venv/bin/activate
pip install freqtrade requests
```

### Run smoke tests first

```bash
cd freqtrade
python3 test_smoke.py
```

All 8 tests should pass. Test 1 makes a live call to the gate and prints the decisionId and verdict.

### Add the gate to your strategy

```python
from presigate_mixin import PresigateMixin
from freqtrade.strategy import IStrategy

class MyStrategy(PresigateMixin, IStrategy):
    # Your strategy logic is unchanged.
    # confirm_trade_entry() is handled by the mixin.
    pass
```

Configure in `pilot_config.json` under the `"presigate"` key:

```json
"presigate": {
  "shadow_mode": true,
  "fail_open": true,
  "timeout_s": 4,
  "gated_pairs": ["BTC/USDT"],
  "min_size_usd": 50.0,
  "api_key": "your-api-key"
}
```

**Start in shadow mode** (`shadow_mode: true`). The gate is called on every signal; verdicts are logged with decisionId; all trades proceed. After 7-14 days of observation, flip to `shadow_mode: false` to enforce verdicts.

### Run the harness (fastest proof)

```bash
python3 freqtrade/run_gate_harness.py --mode shadow --count 5
```

See `freqtrade/README.md` for the full integration walkthrough.

---

## API Reference

### POST /api/gate

```
POST https://presigate.com/api/gate
Content-Type: application/json
Authorization: Bearer <api-key>   (optional during beta)
```

Request body:
```json
{
  "action": {
    "side": "buy",
    "asset": "BTCUSDT",
    "sizeUsd": 500.0
  }
}
```

Response:
```json
{
  "verdict": "ACT",
  "confidence": 0.80,
  "reasons": ["All signals acceptable"],
  "meta": {
    "decisionId": "019efb19-9262-7d81-9655-886d900753f6",
    "btcMidprice": 59684.55
  }
}
```

### POST /api/gate/outcome

Report a trade outcome back to the Presigate flywheel for continuous improvement:

```json
{
  "decisionId": "019efb19-9262-7d81-9655-886d900753f6",
  "vendorTag": "freqtrade",
  "outcome": {
    "entryPrice": 59684.55,
    "exitPrice": 60210.00,
    "exitReason": "roi",
    "side": "buy"
  }
}
```

Outcome reporting is optional and best-effort. No errors are thrown if it fails.

---

## Support

- Documentation: [presigate.com](https://presigate.com)
- Beta access and API keys: [presigate.com](https://presigate.com)
- Issues: open a GitHub issue in this repository

---

## License

MIT — see [LICENSE](LICENSE).
