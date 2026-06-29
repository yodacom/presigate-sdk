"""
presigate_pilot_strategy.py — BTC/USDT pilot strategy for Freqtrade dry-run.

This is the minimal strategy used to prove the Presigate gate integration.
It uses a simple EMA crossover to generate entry signals on BTC/USDT,
then gates every entry through the live Presigate API before allowing execution.

The strategy is intentionally simple — the point is to exercise the gate,
not to be profitable.  It will generate real entry signals on public
BTC/USDT 5m data that exercise the confirm_trade_entry() hook.

SHADOW MODE (default):  the gate is called on every signal; verdicts are
logged with decisionId; trades always proceed.  Zero risk, full observability.

ACTIVE MODE (shadow_mode: false in config):  HOLD/ESCALATE verdicts block entry.

Forced HOLD test:
    Set FORCE_HOLD_PERCENT (0.0–1.0) in config["presigate"]["force_hold_pct"]
    to deterministically block that fraction of entries regardless of the live
    verdict.  Used during the pilot to prove the block path works even when
    live conditions yield ACT.  Label clearly in your notes.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime

import pandas as pd
from freqtrade.strategy import IStrategy, IntParameter

# Import the Presigate mixin from the same directory.
# When deploying to Freqtrade's user_data/strategies/, copy both files.
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from presigate_mixin import PresigateMixin

logger = logging.getLogger(__name__)


class PresigateGatedBTCStrategy(PresigateMixin, IStrategy):
    """
    Minimal BTC/USDT grid-style strategy with Presigate gate integration.

    Entry:  EMA-fast crosses above EMA-slow (bullish momentum signal)
    Exit:   Fixed ROI table or stop-loss
    Gate:   Every entry is pre-screened by Presigate confirm_trade_entry()

    Inherits confirm_trade_entry() from PresigateMixin.
    All gate verdicts are logged with decisionId to the Freqtrade log.
    """

    INTERFACE_VERSION = 3

    # Strategy metadata
    timeframe = "1m"
    can_short = False

    # Minimal ROI: exit with any profit after 1 hour, flat after 4 hours
    minimal_roi = {
        "0": 0.03,     # 3% take profit
        "60": 0.01,    # 1% after 1h
        "240": 0.0,    # exit at breakeven after 4h
    }

    # Stop loss
    stoploss = -0.02   # 2% stop

    # Trailing stop (optional — helps reduce losses in fast-moving markets)
    trailing_stop = False

    # Process only new candles (saves CPU in dry-run)
    process_only_new_candles = True

    # Allow multiple open trades per pair
    max_open_trades = 1

    # EMA periods — tunable via hyperopt but fixed here for simplicity
    ema_fast = IntParameter(5, 20, default=9, space="buy", optimize=False)
    ema_slow = IntParameter(20, 60, default=26, space="buy", optimize=False)

    def populate_indicators(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        """Compute EMAs used for entry signal."""
        for period in set([self.ema_fast.value, self.ema_slow.value, 9, 26]):
            dataframe[f"ema_{period}"] = dataframe["close"].ewm(span=period).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        """
        BUY signal: EMA-fast crosses above EMA-slow on a healthy candle.
        This is intentionally loose to generate signals for the gate demo.
        """
        fast = self.ema_fast.value
        slow = self.ema_slow.value

        dataframe.loc[
            (
                (dataframe[f"ema_{fast}"] > dataframe[f"ema_{slow}"])
                & (dataframe[f"ema_{fast}"].shift(1) <= dataframe[f"ema_{slow}"].shift(1))
                & (dataframe["volume"] > 0)
            ),
            "enter_long",
        ] = 1

        return dataframe

    def populate_exit_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        """EMA-fast crosses below EMA-slow → exit signal."""
        fast = self.ema_fast.value
        slow = self.ema_slow.value

        dataframe.loc[
            (
                (dataframe[f"ema_{fast}"] < dataframe[f"ema_{slow}"])
                & (dataframe[f"ema_{fast}"].shift(1) >= dataframe[f"ema_{slow}"].shift(1))
                & (dataframe["volume"] > 0)
            ),
            "exit_long",
        ] = 1

        return dataframe

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
        **kwargs,
    ) -> bool:
        """
        Gate every entry through Presigate.

        Delegates to PresigateMixin.confirm_trade_entry() which handles:
          - Gate call with structured logging
          - Shadow vs active mode enforcement
          - Fail-open on gate error
          - decisionId capture for outcome reporting

        FORCED HOLD TEST:
        If config["presigate"]["force_hold_pct"] is set (0.0–1.0), randomly
        block that fraction of entries to demonstrate the block path during
        the pilot.  This is a LOCAL override — the gate is still called live;
        this just overrides the result for a subset.  Clearly labeled in logs.
        """
        # Call the real gate via the mixin
        gate_allowed = super().confirm_trade_entry(
            pair=pair,
            order_type=order_type,
            amount=amount,
            rate=rate,
            time_in_force=time_in_force,
            current_time=current_time,
            entry_tag=entry_tag,
            side=side,
            **kwargs,
        )

        # Forced HOLD simulation (pilot demo only — remove in production)
        force_hold_pct = float(self._pg_config().get("force_hold_pct", 0.0))
        if force_hold_pct > 0 and gate_allowed:
            if random.random() < force_hold_pct:
                logger.info(
                    "Presigate [FORCED-HOLD-DEMO] | pair=%s | amount=%.6f | rate=%.2f | "
                    "BLOCKING entry to demonstrate block path (force_hold_pct=%.0f%%) — "
                    "LABEL: SIMULATED, not a live gate HOLD",
                    pair, amount, rate, force_hold_pct * 100,
                )
                return False

        return gate_allowed

    def custom_exit(
        self,
        pair: str,
        trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs,
    ):
        """Report outcome to Presigate flywheel on every exit."""
        exit_reason = kwargs.get("exit_reason", "unknown")
        self.report_trade_outcome(
            trade=trade,
            exit_price=current_rate,
            exit_reason=exit_reason,
        )
        return None  # use ROI/stop-loss table, not a custom exit signal
