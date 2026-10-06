"""
tests/test_risk_manager.py

Covers bot/risk_manager.py's position sizing, with particular focus on two
real incidents:

  1. 2026-10-03: the bot attempted a $328,951.11 notional BTC/USD order
     against a $100,000 account (over 3x equity), which only Alpaca's own
     $200,000 max-notional-per-order cap prevented from being accepted. The
     ATR-based sizing formula alone (qty = risk_dollars / atr) has no limit
     on total notional when ATR is small relative to price, so
     size_position() must cap qty independently of the ATR math -- this is
     max_position_notional_pct_of_equity.

  2. 2026-10-06: with that cap fixed but given zero safety margin, every
     BTC/USD long entry since the 1Hour timeframe revert failed at the
     broker with "insufficient balance for USD" -- sizing capped notional
     at exactly 100% of equity, but BTC drifted ~2% between the signal
     price (the last closed bar's close) and the market order's actual
     fill, consistently pushing the real cost just over available cash.
     notional_safety_buffer_pct fixes this by capping at slightly less than
     100% of the configured fraction, leaving headroom to absorb that
     drift.
"""

import copy

import config
from bot.risk_manager import RiskManager


def make_risk_manager(**overrides):
    params = copy.deepcopy(config.RISK_PARAMS)
    params.update(overrides)
    return RiskManager(params)


class TestNotionalCap:
    def test_reproduces_and_fixes_real_incident(self):
        """Exact numbers (reconstructed) from the 2026-10-03 BTC/USD incident:
        a $100,000 account, BTC trading around $86,295 with an unusually
        tight ATR (~0.3% of price), previously produced a qty that implied
        $328,951.11 of notional -- over 3x equity and over Alpaca's own
        $200,000 per-order cap."""
        rm = make_risk_manager()
        equity = 100_000.0
        entry_price = 86_295.0696
        atr = 262.334  # ATR is only ~0.3% of price here

        result = rm.size_position(
            "BTC/USD", "long", entry_price, atr, equity, asset_class="crypto"
        )

        assert result.allowed
        notional = result.qty * entry_price
        # Must never exceed 100% of equity (the default cap) or Alpaca's
        # own $200,000 hard per-order limit.
        assert notional <= equity * 1.0001
        assert notional <= 200_000
        assert "NOTIONAL-CAPPED" in result.reason

    def test_normal_sizing_untouched_when_atr_is_proportionate(self):
        """When ATR is a normal fraction of price, the notional cap should
        never bind and dollar risk should still hit the 1% ATR target.

        Note: ATR here is 1.2% of price, not exactly 1% -- at exactly 1%
        ATR/price with a 1% risk target, notional lands exactly at equity
        (risk_dollars/atr * price == risk_dollars/0.01 == equity), which
        with the default 3% notional_safety_buffer_pct now legitimately
        triggers the cap (that's the fix working as intended -- see the
        module docstring's 2026-10-06 incident). 1.2% ATR keeps this test
        comfortably below the (now 97%-of-equity) cap, so it actually tests
        the "cap doesn't engage" path rather than sitting right on its new
        boundary.
        """
        rm = make_risk_manager()
        equity = 100_000.0
        entry_price = 500.0
        atr = 6.0  # 1.2% of price -- comfortably below the buffered cap

        result = rm.size_position(
            "SPY", "long", entry_price, atr, equity, asset_class="equity"
        )

        assert result.allowed
        assert "NOTIONAL-CAPPED" not in result.reason
        notional = result.qty * entry_price
        assert notional < equity * 0.97  # comfortably under the buffered cap
        # Dollar risk at 1 ATR should be ~1% of equity (within rounding).
        assert abs(result.dollar_risk - equity * 0.01) < 5.0

    def test_notional_cap_respects_custom_fraction(self):
        """A tighter configured cap (e.g. 0.5 = 50% of equity, for an
        account that wants less concentration per position) should bind
        earlier than the 100% default."""
        rm = make_risk_manager(max_position_notional_pct_of_equity=0.5)
        equity = 100_000.0
        entry_price = 86_295.0696
        atr = 262.334

        result = rm.size_position(
            "BTC/USD", "long", entry_price, atr, equity, asset_class="crypto"
        )

        assert result.allowed
        notional = result.qty * entry_price
        assert notional <= equity * 0.5 * 1.0001

    def test_missing_param_key_defaults_to_no_leverage(self):
        """A RiskManager constructed with a params dict that predates this
        field (e.g. an old strategy_params.json snapshot, or a test from
        before this fix) must not raise KeyError, and must default to no
        leverage (1.0) rather than silently allowing unlimited notional."""
        params = copy.deepcopy(config.RISK_PARAMS)
        del params["max_position_notional_pct_of_equity"]
        rm = RiskManager(params)

        equity = 100_000.0
        entry_price = 86_295.0696
        atr = 262.334
        result = rm.size_position(
            "BTC/USD", "long", entry_price, atr, equity, asset_class="crypto"
        )
        assert result.allowed
        assert result.qty * entry_price <= equity * 1.0001


class TestNotionalSafetyBuffer:
    """2026-10-06 incident: zero margin between the notional cap and actual
    available cash meant any price drift between the signal price and the
    market order's real fill price made the order unfillable. Every BTC/USD
    long entry failed this way ("insufficient balance for USD") for days."""

    def test_buffer_leaves_headroom_below_full_equity(self):
        """The capped notional must land strictly below 100% of equity (not
        just at-or-under it) once a position is big enough to hit the cap at
        all -- that headroom is the entire point of the fix."""
        rm = make_risk_manager()
        equity = 100_000.0
        entry_price = 86_295.0696
        atr = 262.334  # tiny ATR relative to price -- forces the cap to bind

        result = rm.size_position(
            "BTC/USD", "long", entry_price, atr, equity, asset_class="crypto"
        )

        assert result.allowed
        notional = result.qty * entry_price
        assert notional <= equity * 0.97 * 1.0001  # within the buffered cap...
        assert notional < equity * 0.99            # ...with real headroom below full equity

    def test_custom_buffer_value_is_respected(self):
        """A larger configured buffer (e.g. 10%, for an even more volatile
        instrument or a longer/staler signal timeframe) should cap notional
        even further below equity than the 3% default."""
        rm = make_risk_manager(notional_safety_buffer_pct=0.10)
        equity = 100_000.0
        entry_price = 86_295.0696
        atr = 262.334

        result = rm.size_position(
            "BTC/USD", "long", entry_price, atr, equity, asset_class="crypto"
        )

        assert result.allowed
        notional = result.qty * entry_price
        assert notional <= equity * 0.90 * 1.0001

    def test_missing_buffer_key_defaults_to_three_percent(self):
        """A params dict that predates this field (e.g. an old
        strategy_params.json snapshot) must not raise KeyError, and must
        default to the documented 3% buffer rather than 0 (no protection)."""
        params = copy.deepcopy(config.RISK_PARAMS)
        del params["notional_safety_buffer_pct"]
        rm = RiskManager(params)
        assert rm.notional_safety_buffer == 0.03

        equity = 100_000.0
        entry_price = 86_295.0696
        atr = 262.334
        result = rm.size_position(
            "BTC/USD", "long", entry_price, atr, equity, asset_class="crypto"
        )
        assert result.allowed
        notional = result.qty * entry_price
        assert notional <= equity * 0.97 * 1.0001


class TestExistingSizingBehaviorUnchanged:
    """Guards against the notional-cap fix regressing the pre-existing
    ATR-risk and hard-loss-cap behavior that was already correct."""

    def test_invalid_atr_rejected(self):
        rm = make_risk_manager()
        result = rm.size_position("SPY", "long", 500.0, 0.0, 100_000.0)
        assert not result.allowed

    def test_invalid_equity_rejected(self):
        rm = make_risk_manager()
        result = rm.size_position("SPY", "long", 500.0, 5.0, 0.0)
        assert not result.allowed

    def test_hard_stop_is_one_atr_from_entry(self):
        rm = make_risk_manager()
        result = rm.size_position("SPY", "long", 500.0, 5.0, 100_000.0)
        assert result.allowed
        assert abs(result.stop_price - (500.0 - 5.0)) < 1e-9

    def test_max_loss_cap_still_applies_when_tighter_than_risk_per_atr(self):
        params = copy.deepcopy(config.RISK_PARAMS)
        params["max_loss_per_trade_of_equity"] = 0.005  # tighter than 1% risk_per_atr
        rm = RiskManager(params)
        result = rm.size_position("SPY", "long", 500.0, 5.0, 100_000.0)
        assert result.allowed
        assert result.dollar_risk <= 100_000.0 * 0.005 * 1.0001
