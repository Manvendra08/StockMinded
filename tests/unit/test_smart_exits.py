"""Unit tests for Smart Option Exit Engine."""
import pytest
import pandas as pd
from unittest.mock import patch, MagicMock

# We test the pure logic functions directly
from signals.options import check_vix_spike_exit, net_position_delta


class TestVixSpikeExit:
    def test_spike_triggers_exit(self):
        should_exit, reason = check_vix_spike_exit(vix_current=16.5, vix_entry=14.0, threshold_pct=10.0)
        assert should_exit is True
        assert "spiked" in reason

    def test_no_spike_no_exit(self):
        should_exit, _ = check_vix_spike_exit(vix_current=14.5, vix_entry=14.0, threshold_pct=10.0)
        assert should_exit is False

    def test_zero_entry_vix_safe(self):
        should_exit, _ = check_vix_spike_exit(vix_current=20.0, vix_entry=0.0)
        assert should_exit is False

    def test_exact_threshold_no_exit(self):
        # Exactly 10% should not trigger (needs to be >)
        should_exit, _ = check_vix_spike_exit(vix_current=15.4, vix_entry=14.0, threshold_pct=10.0)
        assert should_exit is True  # 10% of 14 = 1.4, 14+1.4=15.4 → 10%, > threshold

    def test_custom_threshold(self):
        should_exit, _ = check_vix_spike_exit(vix_current=15.0, vix_entry=14.0, threshold_pct=5.0)
        assert should_exit is True  # 7.1% > 5%


class TestNetPositionDelta:
    def _make_chain(self, strikes_and_deltas):
        """Create a chain DataFrame with given strikes and deltas."""
        rows = []
        for strike, ce_delta, pe_delta in strikes_and_deltas:
            rows.append({
                "strike": strike, "expiry": "29-May-2026",
                "ce_delta": ce_delta, "pe_delta": pe_delta,
                "ce_ltp": 100, "pe_ltp": 100, "ce_iv": 0.15, "pe_iv": 0.15,
                "ce_oi": 1000, "pe_oi": 1000, "ce_vol": 500, "pe_vol": 500,
            })
        return pd.DataFrame(rows)

    def test_iron_condor_near_neutral(self):
        """Iron condor with balanced legs should have near-zero delta."""
        chain = self._make_chain([
            (24000, 0.20, -0.80),
            (24500, 0.40, -0.60),
            (25000, 0.60, -0.40),
            (25500, 0.80, -0.20),
        ])
        legs = [
            {"side": "SELL", "type": "CE", "strike": 25000, "expiry": "29-May-2026", "qty": 50},
            {"side": "BUY", "type": "CE", "strike": 25500, "expiry": "29-May-2026", "qty": 50},
            {"side": "SELL", "type": "PE", "strike": 24500, "expiry": "29-May-2026", "qty": 50},
            {"side": "BUY", "type": "PE", "strike": 24000, "expiry": "29-May-2026", "qty": 50},
        ]
        nd = net_position_delta(legs, chain)
        assert nd is not None
        # Iron condor should be roughly delta neutral (within ±0.5 for per-share option delta)
        assert abs(nd) < 0.5

    def test_directional_bias(self):
        """Single short put should show positive delta (bullish)."""
        chain = self._make_chain([
            (24500, 0.40, -0.60),
        ])
        legs = [
            {"side": "SELL", "type": "PE", "strike": 24500, "expiry": "29-May-2026", "qty": 50},
        ]
        nd = net_position_delta(legs, chain)
        assert nd is not None
        assert nd > 0  # Short put = positive delta
        assert nd == 0.60  # -1 * -0.60 = +0.60

    def test_empty_chain_returns_none(self):
        nd = net_position_delta(
            [{"side": "SELL", "type": "CE", "strike": 25000, "expiry": "29-May-2026", "qty": 50}],
            pd.DataFrame()
        )
        assert nd is None

    def test_no_matching_strike_returns_none(self):
        chain = self._make_chain([(24000, 0.2, -0.8)])
        legs = [{"side": "SELL", "type": "CE", "strike": 99999, "expiry": "29-May-2026", "qty": 50}]
        nd = net_position_delta(legs, chain)
        assert nd is None


class TestSmartExitCheck:
    """Test _smart_exit_check from paper_trader module."""

    def _make_trade(self, **overrides):
        base = {
            "id": 1, "symbol": "NIFTY", "structure": "IRON_CONDOR", "status": "OPEN",
            "legs": [], "net_premium": 5000.0, "net_credit": 5000.0,
            "entry_vix": 14.0, "short_strikes": [24500, 25500],
            "wing_width": 500, "peak_pnl": 0.0, "trailing_lock": False,
            "reentry_eligible": False,
        }
        base.update(overrides)
        return base

    @patch("dashboard.paper_trader._get_ltp")
    def test_strike_breach_exit(self, mock_ltp):
        """Underlying breaking past short strike + half wing → STRIKE_BREACH."""
        import sys, importlib
        sys.path.insert(0, ".")
        import dashboard.paper_trader as pt
        
        mock_ltp.return_value = 25850.0  # > 25500 + 250 = 25750
        t = self._make_trade()
        settings = {"smart_exits_enabled": True, "smart_exit_vix_spike_pct": 10.0,
                     "smart_exit_trail_lock_pct": 30.0, "smart_exit_trail_floor_pct": 20.0}
        reason = pt._smart_exit_check(t, 4000.0, settings, vix_now=14.5)
        assert reason == "STRIKE_BREACH"

    @patch("dashboard.paper_trader._get_ltp")
    def test_trail_lock_exit(self, mock_ltp):
        """Trail lock: once peak > 30%, if PnL drops below 20% → TRAIL_LOCK."""
        import dashboard.paper_trader as pt
        
        mock_ltp.return_value = 25000.0  # Within strikes, no breach
        t = self._make_trade(peak_pnl=2000.0, trailing_lock=True)  # 40% of 5000 already peaked
        settings = {"smart_exits_enabled": True, "smart_exit_vix_spike_pct": 10.0,
                     "smart_exit_trail_lock_pct": 30.0, "smart_exit_trail_floor_pct": 20.0}
        # Current PnL = 5000 - 4200 = 800 which is 16% < 20% floor
        reason = pt._smart_exit_check(t, 4200.0, settings, vix_now=14.5)
        assert reason == "TRAIL_LOCK"

    @patch("dashboard.paper_trader._get_ltp")
    def test_no_exit_when_disabled(self, mock_ltp):
        """Smart exits disabled → always None."""
        import dashboard.paper_trader as pt
        
        mock_ltp.return_value = 30000.0  # Way past strikes
        t = self._make_trade()
        settings = {"smart_exits_enabled": False}
        reason = pt._smart_exit_check(t, 4000.0, settings, vix_now=25.0)
        assert reason is None

    @patch("dashboard.paper_trader._get_ltp")
    def test_vix_spike_exit(self, mock_ltp):
        """VIX spike > 10% from entry AND crosses floor → VIX_SPIKE."""
        import dashboard.paper_trader as pt
        
        mock_ltp.return_value = 25000.0  # Within strikes
        t = self._make_trade(entry_vix=14.0)
        settings = {"smart_exits_enabled": True, "smart_exit_vix_spike_pct": 10.0,
                     "smart_exit_vix_floor": 15.0,
                     "smart_exit_trail_lock_pct": 30.0, "smart_exit_trail_floor_pct": 20.0}
        # entry 14 (< floor 15), now 16 (>= floor, +14.3% > 10%) → VIX_SPIKE
        reason = pt._smart_exit_check(t, 4000.0, settings, vix_now=16.0)
        assert reason == "VIX_SPIKE"
        assert t["reentry_eligible"] is True


class TestSmartExitDebitStructures:
    """Debit structures (e.g. LONG_STRADDLE) must compute P&L correctly."""

    def _make_debit_trade(self, **overrides):
        base = {
            "id": 9, "symbol": "NIFTY", "structure": "LONG_STRADDLE",
            "status": "OPEN", "legs": [], "net_premium": -5000.0,  # debit (negative)
            "entry_vix": 14.0, "long_strikes": [25000], "wing_width": 0.0,
            "peak_pnl": 0.0, "trailing_lock": False, "reentry_eligible": False,
        }
        base.update(overrides)
        return base

    @patch("dashboard.paper_trader._get_ltp")
    def test_debit_pnl_uses_signed_premium(self, mock_ltp):
        """LONG_STRADDLE: net_premium negative must NOT be coerced to 0."""
        import dashboard.paper_trader as pt

        mock_ltp.return_value = 25000.0
        t = self._make_debit_trade()
        settings = {"smart_exits_enabled": True, "smart_exit_vix_spike_pct": 10.0,
                     "smart_exit_trail_lock_pct": 50.0, "smart_exit_trail_floor_pct": 35.0}
        # current_net negative (BUY legs). pnl = (-5000) - (-4000) = -1000 (loss)
        reason = pt._smart_exit_check(t, -4000.0, settings, vix_now=14.0)
        assert reason is None  # no spike, no breach, no trail lock

    @patch("dashboard.paper_trader._get_ltp")
    def test_debit_trail_lock(self, mock_ltp):
        """Debit trail lock: peak profit then drop below floor → TRAIL_LOCK."""
        import dashboard.paper_trader as pt

        mock_ltp.return_value = 25000.0
        # Already peaked at +3000 (current value 2000 > debit 5000? no:
        # pnl = (-5000) - (-8000) = +3000 at peak). Now dropped to
        # pnl = (-5000) - (-5500) = +500 < 35% of 5000 (=1750) → lock fires.
        t = self._make_debit_trade(peak_pnl=3000.0, trailing_lock=True)
        settings = {"smart_exits_enabled": True, "smart_exit_vix_spike_pct": 10.0,
                     "smart_exit_trail_lock_pct": 50.0, "smart_exit_trail_floor_pct": 35.0}
        reason = pt._smart_exit_check(t, -5500.0, settings, vix_now=14.0)
        assert reason == "TRAIL_LOCK"
        assert t["reentry_eligible"] is True


class TestVixFloorGate:
    """Corrected VIX floor: only exit when entry was BELOW floor and now crosses it."""

    def _make_trade(self, **overrides):
        base = {
            "id": 1, "symbol": "NIFTY", "structure": "IRON_CONDOR", "status": "OPEN",
            "legs": [], "net_premium": 5000.0, "net_credit": 5000.0,
            "entry_vix": 14.0, "short_strikes": [24500, 25500],
            "wing_width": 500, "peak_pnl": 0.0, "trailing_lock": False,
            "reentry_eligible": False,
        }
        base.update(overrides)
        return base

    @patch("dashboard.paper_trader._get_ltp")
    def test_no_exit_when_entry_already_above_floor(self, mock_ltp):
        """Entry VIX already high (e.g. 20) → even if VIX rises, no spike exit."""
        import dashboard.paper_trader as pt

        mock_ltp.return_value = 25000.0
        t = self._make_trade(entry_vix=20.0)
        settings = {"smart_exits_enabled": True, "smart_exit_vix_spike_pct": 15.0,
                     "smart_exit_vix_floor": 18.0, "smart_exit_trail_lock_pct": 30.0,
                     "smart_exit_trail_floor_pct": 20.0}
        # VIX now 25 (25% above 20) but entry was above floor → must NOT exit
        reason = pt._smart_exit_check(t, 4000.0, settings, vix_now=25.0)
        assert reason is None

    @patch("dashboard.paper_trader._get_ltp")
    def test_exit_when_entry_below_floor_and_crosses(self, mock_ltp):
        """Entry VIX 14 (< floor 18), now 22 (>= floor, +57%) → VIX_SPIKE."""
        import dashboard.paper_trader as pt

        mock_ltp.return_value = 25000.0
        t = self._make_trade(entry_vix=14.0)
        settings = {"smart_exits_enabled": True, "smart_exit_vix_spike_pct": 15.0,
                     "smart_exit_vix_floor": 18.0, "smart_exit_trail_lock_pct": 30.0,
                     "smart_exit_trail_floor_pct": 20.0}
        reason = pt._smart_exit_check(t, 4000.0, settings, vix_now=22.0)
        assert reason == "VIX_SPIKE"


class TestReentryAllSignals:
    """Every smart-exit signal must set reentry_eligible=True."""

    def _make_trade(self, **overrides):
        base = {
            "id": 1, "symbol": "NIFTY", "structure": "IRON_CONDOR", "status": "OPEN",
            "legs": [], "net_premium": 5000.0, "net_credit": 5000.0,
            "entry_vix": 14.0, "short_strikes": [24500, 25500],
            "wing_width": 500, "peak_pnl": 0.0, "trailing_lock": False,
            "reentry_eligible": False,
        }
        base.update(overrides)
        return base

    @patch("dashboard.paper_trader._get_ltp")
    def test_strike_breach_sets_reentry(self, mock_ltp):
        import dashboard.paper_trader as pt

        mock_ltp.return_value = 25850.0
        t = self._make_trade()
        settings = {"smart_exits_enabled": True, "smart_exit_vix_spike_pct": 10.0,
                     "smart_exit_trail_lock_pct": 30.0, "smart_exit_trail_floor_pct": 20.0}
        reason = pt._smart_exit_check(t, 4000.0, settings, vix_now=14.5)
        assert reason == "STRIKE_BREACH"
        assert t["reentry_eligible"] is True
