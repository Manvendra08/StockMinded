from unittest.mock import MagicMock
import pandas as pd
import pytest
from signals import option_strategy


@pytest.mark.unit
def test_resolve_structure_rejects_zero_premium_legs() -> None:
    # Create a mock setup
    setup = option_strategy.NiftyOptionSetup(
        symbol="NIFTY",
        mode="OPTION_SELL_DEFINED_RISK",
        strategy="IRON_CONDOR",
        suitable=True,
        skip_reason="",
        wing_width=100,
    )

    # Create a mock chain where strike 24100 CE has 0.0 premium
    # spot = 24000
    # strikes = [23800, 23900, 24000, 24100, 24200]
    chain = pd.DataFrame(
        [
            {"strike": 23800, "ce_ltp": 200.0, "pe_ltp": 5.0, "expiry": "2026-07-28"},
            {"strike": 23900, "ce_ltp": 100.0, "pe_ltp": 20.0, "expiry": "2026-07-28"},
            {"strike": 24000, "ce_ltp": 50.0, "pe_ltp": 50.0, "expiry": "2026-07-28"},
            {"strike": 24100, "ce_ltp": 0.0, "pe_ltp": 100.0, "expiry": "2026-07-28"},
            {"strike": 24200, "ce_ltp": 5.0, "pe_ltp": 200.0, "expiry": "2026-07-28"},
        ]
    )

    cfg = {
        "nifty_options": {
            "min_lots_per_leg": 1,
            "min_short_premium": 5.0,
            "max_short_premium": 250.0,
        }
    }

    # Resolve structure
    res = option_strategy._resolve_structure(
        setup, chain, spot=24000.0, lot_size=75, strike_step=100, cfg=cfg, symbol="NIFTY"
    )

    # The setup should be marked not suitable because of zero premium on a leg
    assert res.suitable is False
    assert "zero or corrupt premium" in res.skip_reason
