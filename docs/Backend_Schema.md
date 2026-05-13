# Backend Schema Documentation - StockMinded

## 1. Relational Schema (SQLite) - `journal.sqlite`

### 1.1 Table: `regime_snapshots`
| Column | Type | Description |
| :--- | :--- | :--- |
| `id` | TEXT (PK) | Unique ID (timestamp based) |
| `ts` | TEXT | ISO Timestamp (UTC) |
| `regime` | TEXT | Enum: TREND_UP, RANGE_LOW_VOL, etc. |
| `payload` | JSON | Full snapshot dictionary including VIX and Breadth. |

### 1.2 Table: `flow_snapshots`
| Column | Type | Description |
| :--- | :--- | :--- |
| `id` | TEXT (PK) | Unique ID |
| `ts` | TEXT | ISO Timestamp |
| `payload` | JSON | FII/DII data, PCR, Max Pain, and Smart Money Bias. |

### 1.3 Table: `trades` (Spot/Futures)
| Column | Type | Description |
| :--- | :--- | :--- |
| `id` | TEXT (PK) | Unique ID |
| `opened_at` | TEXT | Entry timestamp |
| `symbol` | TEXT | e.g., RELIANCE |
| `side` | TEXT | long / short |
| `qty` | INTEGER | Lot size adjusted quantity |
| `entry`, `stop`, `target` | REAL | Price levels |
| `pnl_rupees` | REAL | Final realized P&L |

## 2. Non-Relational Schema (JSON) - `paper_trades.json`

### 2.1 Option Trade Object
```json
{
  "option_trades": [
    {
      "id": "T_12345",
      "symbol": "NIFTY",
      "structure": "BULL_CALL_SPREAD",
      "status": "OPEN",
      "opened_at": "2024-05-13T09:25:00",
      "legs": [
        {
          "strike": 22500,
          "type": "CE",
          "side": "BUY",
          "qty": 75,
          "entry_price": 120.5,
          "current_price": 135.2
        }
      ],
      "entry_net_debit": 9037.5,
      "unrealized_pnl": 1102.5,
      "sl_hit": false,
      "exit_reason": null
    }
  ]
}
```

## 3. History Tracking (SQLite) - `iv_history.sqlite`
- **Table**: `iv_daily`
- **Columns**: `date`, `symbol`, `iv_atm`
- **Purpose**: Bootstrap IV Rank calculation (percentile ranking over 252 days).
