import pytest
from data.feed import _filter_atm_strikes

def test_filter_atm_strikes_normal():
    # 50 strikes from 10000 to 12450 (step 50)
    data = {
        "records": {
            "underlyingValue": 11025.0,
            "data": [
                {"strikePrice": float(s), "expiryDate": "18-Jun-2026", "CE": {}, "PE": {}}
                for s in range(10000, 12500, 50)
            ],
            "strikePrices": [float(s) for s in range(10000, 12500, 50)]
        },
        "filtered": {
            "data": [
                {"strikePrice": float(s), "expiryDate": "18-Jun-2026", "CE": {}, "PE": {}}
                for s in range(10000, 12500, 50)
            ],
            "strikePrices": [float(s) for s in range(10000, 12500, 50)]
        }
    }
    
    filtered_data = _filter_atm_strikes(data)
    
    # Underlying value is 11025, ATM strike should be 11000.
    # Sorted unique strikes are [10000, ..., 12450] (index range: [0, 49])
    # Index of 11000 is 20.
    # Sliced index range: [20 - 10, 20 + 10] = [10, 30] inclusive (size 21)
    # Sliced strikes: 10000 + 10*50 = 10500 to 10000 + 30*50 = 11500
    
    res_records = filtered_data["records"]
    res_filtered = filtered_data["filtered"]
    
    assert len(res_records["data"]) == 21
    assert len(res_filtered["data"]) == 21
    
    strikes = [r["strikePrice"] for r in res_records["data"]]
    assert min(strikes) == 10500.0
    assert max(strikes) == 11500.0
    assert 11000.0 in strikes
    
    # Check that strikePrices key was also updated
    assert res_records["strikePrices"] == strikes
    assert res_filtered["strikePrices"] == strikes

def test_filter_atm_strikes_boundary_low():
    # Test boundary where we don't have enough strikes below ATM
    data = {
        "records": {
            "underlyingValue": 10100.0,
            "data": [
                {"strikePrice": float(s), "expiryDate": "18-Jun-2026", "CE": {}, "PE": {}}
                for s in range(10000, 12500, 50)
            ]
        }
    }
    
    filtered_data = _filter_atm_strikes(data)
    res_records = filtered_data["records"]
    
    # Spot = 10100, ATM = 10100.
    # Unique strikes: 10000, 10050, 10100, 10150, ...
    # ATM index is 2.
    # Below ATM: 2 strikes (10000, 10050).
    # Above ATM: 10 strikes (10150 to 10600).
    # Total strikes = 2 + 1 + 10 = 13 strikes.
    assert len(res_records["data"]) == 13
    strikes = [r["strikePrice"] for r in res_records["data"]]
    assert strikes[0] == 10000.0
    assert strikes[-1] == 10600.0

def test_filter_atm_strikes_no_underlying():
    data = {
        "records": {
            "data": [
                {"strikePrice": 10000.0, "CE": {}, "PE": {}}
            ]
        }
    }
    # Should return unmodified
    assert _filter_atm_strikes(data) == data
