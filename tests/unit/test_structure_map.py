"""Tests for signals/structure_map.py."""
import pytest
from signals.regime import Regime
from signals.structure_map import plan_for, StructurePlan, MAP


class TestPlanFor:
    @pytest.mark.parametrize("regime", list(Regime))
    def test_all_regimes_return_plan(self, regime):
        plan = plan_for(regime)
        assert isinstance(plan, StructurePlan)
        assert plan.primary
        assert plan.secondary
        assert plan.notes

    def test_trend_up_contains_long(self):
        plan = plan_for(Regime.TREND_UP)
        assert "Long" in plan.primary or "long" in plan.primary.lower()

    def test_trend_down_contains_bear(self):
        plan = plan_for(Regime.TREND_DOWN)
        assert "Bear" in plan.primary or "short" in plan.primary.lower() or "Put" in plan.primary

    def test_range_low_vol_is_iron_condor(self):
        plan = plan_for(Regime.RANGE_LOW_VOL)
        assert "Iron Condor" in plan.primary

    def test_vol_expansion_is_straddle(self):
        plan = plan_for(Regime.VOL_EXPANSION)
        assert "Straddle" in plan.primary or "Strangle" in plan.primary

    def test_vol_contraction_is_short_premium(self):
        plan = plan_for(Regime.VOL_CONTRACTION)
        assert "Short premium" in plan.primary or "credit" in plan.primary.lower()

    def test_map_covers_all_regimes(self):
        assert set(MAP.keys()) == set(Regime)
