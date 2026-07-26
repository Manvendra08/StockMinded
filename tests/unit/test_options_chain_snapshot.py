from unittest.mock import patch
import pandas as pd
import pytest
from signals import options


@pytest.mark.unit
def test_chain_snapshot_resolves_underlying_price_keys() -> None:
    # Mock option_chain to return a Shoonya-style format where underlyingValue is absent from records,
    # but underlying_price is present at the root level.
    mock_raw_chain = {
        "records": {
            "data": [
                {
                    "strikePrice": 24000.0,
                    "expiryDate": "2026-07-28",
                    "CE": {"lastPrice": 150.0, "impliedVolatility": 15.0},
                    "PE": {"lastPrice": 150.0, "impliedVolatility": 15.0},
                },
                {
                    "strikePrice": 24500.0,
                    "expiryDate": "2026-07-28",
                    "CE": {"lastPrice": 0.0, "impliedVolatility": 15.0},  # 0.0 CE ltp
                    "PE": {"lastPrice": 200.0, "impliedVolatility": 15.0},
                }
            ]
        },
        "underlying_price": 24000.0,
    }

    with patch("signals.options.option_chain", return_value=mock_raw_chain):
        df = options.chain_snapshot("NIFTY", target_strikes=[24000.0, 24500.0])

        assert not df.empty
        # Verify 24500 CE LTP is filled with a non-zero synthetic premium
        ce_row_24500 = df[df["strike"] == 24500.0].iloc[0]
        assert ce_row_24500["ce_ltp"] > 0.0
        assert bool(ce_row_24500["ce_synthetic"]) is True
