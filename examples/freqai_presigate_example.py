"""
freqai_presigate_example.py
===========================
A FreqAI strategy that integrates the Presigate gate as a mandatory pre-trade
filter alongside a machine-learning model.

HOW IT WORKS
------------
The strategy inherits from two classes:

    PresigateMixin  -- provides confirm_trade_entry() gating
    IFreqaiStrategy -- FreqAI base class (itself extends IStrategy)

Execution order for each entry signal:

  1. FreqAI model generates a prediction (custom entry score).
  2. populate_entry_trend() turns a strong prediction into an entry signal.
  3. Freqtrade calls confirm_trade_entry().
  4. PresigateMixin posts the intended trade to https://presigate.com/api/gate.
  5. If the gate returns HOLD or ESCALATE, the entry is blocked — regardless of
     what the ML model predicted.
  6. ACT (or gate error + fail_open=True) allows the trade to proceed.

WHY YOU CANNOT BACKFILL PRESIGATE SCORES INTO TRAINING DATA
-------------------------------------------------------------
Presigate scores are computed from live market data (order books, klines,
sentiment feeds) at the moment the gate is called. There is no historical
record of what the Presigate score WAS at a past timestamp, so the scores
cannot be reconstructed for past candles and cannot be added directly to a
training DataFrame.

The recommended integration:
  - Train the FreqAI model on standard OHLCV-derived features.
  - Apply Presigate as a hard gate at entry time (this file shows how).
  - Over time, accumulate outcome data via /api/gate/outcome (the mixin does
    this automatically). Presigate's flywheel builds a linkage between market
    condition snapshots and trade outcomes that can inform future features.

OPTIONAL: LIVE FEATURE ENRICHMENT
----------------------------------
If you want to use per-primitive scores (RXI, MMI, CSI, TVI) as features in
future training runs, you can:
  1. Log verbose gate results to a time-series store keyed by candle timestamp.
  2. After accumulating enough data, join those scores into your feature set
     as a new data source.
  3. Re-train the model with the enriched feature set.

This file includes a `_cache_gate_result()` helper and a `bot_loop_start()`
hook showing where to slot this in.

SETUP
-----
  pip install freqtrade requests

  Copy presigate_mixin.py and presigate_client.py (from ../freqtrade/) into the
  same directory as this file, or into your Freqtrade user_data/strategies/.

QUICKSTART
----------
  1. Copy this file and ../freqtrade/presigate_mixin.py and
     ../freqtrade/presigate_client.py to your Freqtrade user_data/strategies/.
  2. Add the "presigate" block to your Freqtrade config (see CONFIG EXAMPLE below).
  3. Start with shadow_mode: true to observe gate behavior without blocking trades.
  4. Run for 7–14 days. Review the gate hold rate in your logs.
  5. Flip shadow_mode to false to enforce verdicts.

CONFIG EXAMPLE
--------------
Add to your Freqtrade config.json:

    "freqai": {
        "enabled": true,
        "identifier": "presigate_demo",
        "feature_parameters": {
            "include_timeframes": ["5m", "15m", "1h"],
            "include_corr_pairlist": [],
            "label_period_candles": 24,
            "include_shifted_candles": 2,
            "DI_threshold": 0
        },
        "data_split_parameters": {
            "test_size": 0.25,
            "random_state": 42
        },
        "model_training_parameters": {
            "n_estimators": 200
        }
    },
    "presigate": {
        "shadow_mode": true,
        "fail_open": true,
        "timeout_s": 4,
        "gated_pairs": ["BTC/USDT"],
        "min_size_usd": 50.0,
        "api_key": "YOUR_PRESIGATE_API_KEY"
    }
"""

from __future__ import annotations

import logging
import sys
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any

import pandas as pd
import numpy as np

# Resolve sibling modules (presigate_mixin, presigate_client).
# When deployed to user_data/strategies/, all three files should be in the same directory.
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'freqtrade'))

from presigate_mixin import PresigateMixin
from presigate_client import PresigateClient

# FreqAI imports — available after `pip install freqtrade`
from freqtrade.strategy import IFreqaiStrategy, IntParameter, DecimalParameter

logger = logging.getLogger(__name__)


class PresigateFreqAIStrategy(PresigateMixin, IFreqaiStrategy):
    """
    FreqAI strategy with Presigate hard-gate integration.

    The ML model generates entry predictions based on OHLCV-derived features.
    Before each entry, PresigateMixin.confirm_trade_entry() calls the Presigate
    gate API. HOLD or ESCALATE verdicts block the trade regardless of the model's
    prediction.

    MRO resolution: PresigateMixin.confirm_trade_entry() takes precedence over
    IFreqaiStrategy's default (which returns True). If you override
    confirm_trade_entry() in this class, call super() to preserve the gate.
    """

    INTERFACE_VERSION = 3

    # ------------------------------------------------------------------
    # Strategy parameters
    # ------------------------------------------------------------------

    timeframe = "5m"
    can_short = False

    # Minimal ROI: exit at 2% after 30 min, at breakeven after 4h
    minimal_roi = {
        "0":   0.02,
        "30":  0.01,
        "240": 0.0,
    }

    stoploss = -0.02          # 2% stop
    trailing_stop = False
    process_only_new_candles = True
    max_open_trades = 1

    # Entry score threshold: the FreqAI model prediction must exceed this to
    # trigger an entry signal. Tune after reviewing model performance.
    entry_score_threshold = DecimalParameter(
        0.5, 0.9, default=0.6, space="buy", optimize=True
    )

    # ------------------------------------------------------------------
    # Live gate result cache (populated by _cache_gate_result)
    # ------------------------------------------------------------------
    # Stores the most recent verbose gate response for optional feature use.
    # Thread-safe via a lock since bot_loop_start() runs in a background thread
    # while FreqAI's populate_indicators() may run concurrently.

    _gate_cache: dict = {}
    _gate_cache_lock = threading.Lock()
    _gate_cache_ts: float = 0.0
    _gate_cache_ttl_s: float = 30.0   # refresh no more than once per 30s

    # ------------------------------------------------------------------
    # FreqAI: Feature engineering
    # ------------------------------------------------------------------

    def feature_engineering_expand_all(
        self, dataframe: pd.DataFrame, period: int, metadata: dict, **kwargs
    ) -> pd.DataFrame:
        """
        Add features that FreqAI will expand across all configured timeframes.
        These are standard OHLCV-derived features — no external API calls here.
        FreqAI calls this method on historical candle DataFrames for training.
        """
        dataframe[f"%-rsi-period_{period}"] = self._rsi(dataframe["close"], period)
        dataframe[f"%-mfi-period_{period}"] = self._mfi(dataframe, period)
        dataframe[f"%-adx-period_{period}"] = self._adx(dataframe, period)
        dataframe[f"%-close-pct-change-period_{period}"] = (
            dataframe["close"].pct_change(period)
        )
        dataframe[f"%-volume-zscore-period_{period}"] = self._zscore(
            dataframe["volume"], period
        )
        return dataframe

    def feature_engineering_standard(
        self, dataframe: pd.DataFrame, metadata: dict, **kwargs
    ) -> pd.DataFrame:
        """
        Features computed once per timeframe (not expanded across periods).
        Add spread, day-of-week, and hour-of-day as contextual signals.
        """
        dataframe["%-hour-of-day"]  = pd.to_datetime(dataframe["date"]).dt.hour
        dataframe["%-day-of-week"]  = pd.to_datetime(dataframe["date"]).dt.dayofweek
        dataframe["%-high-low-pct"] = (dataframe["high"] - dataframe["low"]) / dataframe["close"]

        # Optional: if a recent verbose gate result is cached (from bot_loop_start),
        # inject per-primitive scores as features on the last candle.
        # During training these columns will be NaN and are handled by FreqAI's
        # DI threshold / NaN fill — they only contribute at live inference time.
        self._inject_cached_gate_features(dataframe)

        return dataframe

    def set_freqai_targets(
        self, dataframe: pd.DataFrame, metadata: dict, **kwargs
    ) -> pd.DataFrame:
        """
        Define the prediction target for the ML model.

        Target: price change over the next `label_period_candles` candles,
        expressed as a z-score of recent returns. Positive values indicate
        upward price movement; the model learns to predict this.
        """
        label_period = self.freqai_info.get("feature_parameters", {}).get(
            "label_period_candles", 24
        )
        future_return = dataframe["close"].pct_change(label_period).shift(-label_period)
        rolling_std   = future_return.rolling(window=label_period * 4).std()
        rolling_mean  = future_return.rolling(window=label_period * 4).mean()

        # Avoid division by zero in flat markets
        dataframe["&-s_close"] = np.where(
            rolling_std > 0,
            (future_return - rolling_mean) / rolling_std,
            0.0,
        )
        return dataframe

    # ------------------------------------------------------------------
    # FreqAI: Indicator population (called by FreqAI on every candle)
    # ------------------------------------------------------------------

    def populate_indicators(
        self, dataframe: pd.DataFrame, metadata: dict
    ) -> pd.DataFrame:
        """
        Delegates to FreqAI to build features and run the model.
        Do not add indicators directly here — use feature_engineering_* methods.
        """
        dataframe = self.freqai.start(dataframe, metadata, self)
        return dataframe

    # ------------------------------------------------------------------
    # Entry / exit signals
    # ------------------------------------------------------------------

    def populate_entry_trend(
        self, dataframe: pd.DataFrame, metadata: dict
    ) -> pd.DataFrame:
        """
        Entry signal fires when the FreqAI model's prediction score exceeds
        the threshold and the model is confident (do_predict == 1).

        `do_predict` is set by FreqAI:
          1  = model is confident (inside training distribution)
          0  = model is uncertain (outside DI threshold — skip)
         -1  = predicted target is below entry threshold (no signal)
        """
        threshold = self.entry_score_threshold.value

        dataframe.loc[
            (dataframe["do_predict"] == 1)
            & (dataframe["&-s_close"] > threshold)
            & (dataframe["volume"] > 0),
            "enter_long",
        ] = 1

        return dataframe

    def populate_exit_trend(
        self, dataframe: pd.DataFrame, metadata: dict
    ) -> pd.DataFrame:
        """
        Exit when the model predicts a negative z-score (downward momentum).
        ROI and stop-loss tables handle most exits — this is a signal-based supplement.
        """
        dataframe.loc[
            (dataframe["do_predict"] == 1)
            & (dataframe["&-s_close"] < 0)
            & (dataframe["volume"] > 0),
            "exit_long",
        ] = 1

        return dataframe

    # ------------------------------------------------------------------
    # confirm_trade_entry — gate runs here
    # ------------------------------------------------------------------
    # PresigateMixin.confirm_trade_entry() is inherited via MRO and runs
    # automatically. You do NOT need to override this method unless you want
    # additional logic (e.g. a secondary model check) layered on top.
    #
    # If you do override it, always call super():
    #
    #   def confirm_trade_entry(self, pair, order_type, amount, rate,
    #                           time_in_force, current_time, entry_tag,
    #                           side, **kwargs) -> bool:
    #       if not super().confirm_trade_entry(...):
    #           return False          # Presigate said HOLD or ESCALATE
    #       # your additional checks here
    #       return True

    # ------------------------------------------------------------------
    # Outcome reporting — wired via custom_exit
    # ------------------------------------------------------------------

    def custom_exit(
        self,
        pair: str,
        trade: Any,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs,
    ):
        """
        Report the trade outcome to the Presigate flywheel after every exit.
        Keyed by the decisionId captured at entry. No-ops if decisionId not found.
        """
        exit_reason = kwargs.get("exit_reason", "unknown")
        self.report_trade_outcome(
            trade=trade,
            exit_price=current_rate,
            exit_reason=exit_reason,
        )
        return None  # Use ROI/stop-loss table for exit timing, not a custom signal

    # ------------------------------------------------------------------
    # Optional: live gate cache for feature enrichment
    # ------------------------------------------------------------------

    def bot_loop_start(self, current_time: datetime, **kwargs) -> None:
        """
        Called by Freqtrade at the start of each bot loop iteration.

        Fetch a verbose gate result and cache it for optional injection into
        the feature DataFrame via _inject_cached_gate_features().

        This is intentionally non-blocking: if the gate is slow or unreachable,
        the cache simply remains stale and the feature columns stay NaN.
        """
        now = time.monotonic()
        with self._gate_cache_lock:
            if now - self._gate_cache_ts < self._gate_cache_ttl_s:
                return  # cache is still fresh

        # Call in verbose mode to receive per-primitive scores
        try:
            result = self._pg_client().check(
                side="buy",          # direction is irrelevant for feature collection
                asset="BTCUSDT",
                size_usd=1000.0,     # nominal size — only affects MMI depth check
                verbose=True,
            )
        except Exception as exc:
            logger.debug("Presigate feature cache refresh failed (non-fatal): %s", exc)
            return

        if not result["ok"]:
            logger.debug("Presigate feature cache: gate error — %s", result["error"])
            return

        # Extract per-primitive scores from verbose signals
        signals = result.get("signals", {})
        cache_entry = {
            "verdict":    result["verdict"],
            "confidence": result["confidence"],
            "rxi_score":  signals.get("rxi", {}).get("score"),
            "mmi_score":  signals.get("mmi", {}).get("score"),
            "csi_score":  signals.get("csi", {}).get("score"),
            "tvi_score":  signals.get("tvi", {}).get("score"),
            "timing_ok":  not signals.get("timing", {}).get("blocked", False),
            "ts":         time.monotonic(),
        }

        with self._gate_cache_lock:
            self._gate_cache = cache_entry
            self._gate_cache_ts = time.monotonic()

        logger.debug(
            "Presigate feature cache refreshed | verdict=%s | confidence=%.2f | "
            "rxi=%.2f | mmi=%.2f | csi=%.2f | tvi=%.2f",
            cache_entry["verdict"],
            cache_entry["confidence"],
            cache_entry.get("rxi_score") or 0,
            cache_entry.get("mmi_score") or 0,
            cache_entry.get("csi_score") or 0,
            cache_entry.get("tvi_score") or 0,
        )

    def _inject_cached_gate_features(self, dataframe: pd.DataFrame) -> None:
        """
        Inject the most recent cached gate scores into the last row of the
        DataFrame as informative features.

        Only the last (current) candle receives these values. All historical
        candles have NaN, which FreqAI handles gracefully. Over enough live
        inference cycles, the model accumulates signal-linked outcome data in
        the flywheel that can inform future training sets.

        Column naming uses the `%` prefix convention so FreqAI includes them
        in the feature set automatically.
        """
        with self._gate_cache_lock:
            cache = dict(self._gate_cache)

        if not cache:
            return  # no gate data yet — columns stay absent (NaN after FreqAI fill)

        # Encode verdict as a numeric feature (ACT=1, HOLD=0, ESCALATE=-1)
        verdict_map = {"ACT": 1.0, "HOLD": 0.0, "ESCALATE": -1.0}
        verdict_num = verdict_map.get(cache.get("verdict", ""), 0.0)

        # Write to last row only
        idx = dataframe.index[-1]
        dataframe.at[idx, "%-presigate-verdict"]    = verdict_num
        dataframe.at[idx, "%-presigate-confidence"] = float(cache.get("confidence") or 0)
        dataframe.at[idx, "%-presigate-rxi"]        = float(cache.get("rxi_score") or 0)
        dataframe.at[idx, "%-presigate-mmi"]        = float(cache.get("mmi_score") or 0)
        dataframe.at[idx, "%-presigate-csi"]        = float(cache.get("csi_score") or 0)
        dataframe.at[idx, "%-presigate-tvi"]        = float(cache.get("tvi_score") or 0)
        dataframe.at[idx, "%-presigate-timing-ok"]  = float(cache.get("timing_ok", True))

    # ------------------------------------------------------------------
    # Feature computation helpers (pure OHLCV — no external calls)
    # ------------------------------------------------------------------

    @staticmethod
    def _rsi(series: pd.Series, period: int) -> pd.Series:
        delta = series.diff()
        gain  = delta.clip(lower=0).rolling(period).mean()
        loss  = (-delta.clip(upper=0)).rolling(period).mean()
        rs    = gain / loss.replace(0, float("nan"))
        return 100 - (100 / (1 + rs))

    @staticmethod
    def _mfi(dataframe: pd.DataFrame, period: int) -> pd.Series:
        tp  = (dataframe["high"] + dataframe["low"] + dataframe["close"]) / 3
        rmf = tp * dataframe["volume"]
        pos = rmf.where(tp > tp.shift(1), 0).rolling(period).sum()
        neg = rmf.where(tp < tp.shift(1), 0).rolling(period).sum()
        mfr = pos / neg.replace(0, float("nan"))
        return 100 - (100 / (1 + mfr))

    @staticmethod
    def _adx(dataframe: pd.DataFrame, period: int) -> pd.Series:
        high, low, close = dataframe["high"], dataframe["low"], dataframe["close"]
        tr = pd.concat([
            high - low,
            (high - close.shift(1)).abs(),
            (low  - close.shift(1)).abs(),
        ], axis=1).max(axis=1)
        atr = tr.rolling(period).mean()
        dm_pos = (high - high.shift(1)).clip(lower=0)
        dm_neg = (low.shift(1) - low).clip(lower=0)
        di_pos = 100 * dm_pos.rolling(period).mean() / atr.replace(0, float("nan"))
        di_neg = 100 * dm_neg.rolling(period).mean() / atr.replace(0, float("nan"))
        dx = 100 * (di_pos - di_neg).abs() / (di_pos + di_neg).replace(0, float("nan"))
        return dx.rolling(period).mean()

    @staticmethod
    def _zscore(series: pd.Series, period: int) -> pd.Series:
        m = series.rolling(period).mean()
        s = series.rolling(period).std()
        return (series - m) / s.replace(0, float("nan"))
