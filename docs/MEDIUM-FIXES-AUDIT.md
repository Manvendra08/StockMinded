# StockMinded — MEDIUM Severity Bug Fixes Audit

**Date:** 2026-07-01  
**Scope:** All 8 MEDIUM-severity bugs (M1–M8) from the codebase audit  
**Status:** ✅ ALL VERIFIED APPLIED

---

## Summary

| ID | Bug | File | Status |
|----|-----|------|--------|
| M1 | `generate_eod_summary()` double-counts trades | `paper_trader.py` | ✅ Fixed |
| M2 | PnL sanity bound allows 10% corruption (1.1×) | `paper_trader.py` | ✅ Fixed |
| M3 | Generic `enter_option_structure()` hardcodes `wing_width: 0.0` | `paper_trader.py` | ✅ Fixed |
| M4 | `_nearest()` always biases higher on equidistant strikes | `option_strategy.py` | ✅ Fixed |
| M5 | `peak_price` misleading name for SHORT trades | `paper_trader.py` | ✅ Fixed |
| M6 | `check_naked_legs()` no strike ordering validation | `options.py` | ✅ Fixed |
| M7 | `is_within_exit_window()` uses NIFTY config for BANKNIFTY | `options.py` | ✅ Fixed |
| M8 | `option_structure_size()` no margin check | `risk/sizing.py` | ✅ Fixed |

---

## M1: `cleanup_db()` Double-Counting in Daily Summaries

### Problem
When trades exist in both JSON (`db["trades"]`) and SQLite journal stores after sync,
the daily summary recalculation counted each trade twice, inflating `total_pnl`,
`total_trades`, and `win_rate`.

### Fix Applied
Added deduplication by trade ID when building `all_day_trades`:

```python
seen_ids: set = set()
all_day_trades: list[dict] = []
for t in s_options + s_trades:
    tid = t.get("id") or t.get("trade_id")
    if tid is not None and tid in seen_ids:
        continue
    if tid is not None:
        seen_ids.add(tid)
    all_day_trades.append(t)
```

Option trades are preferred over stock trades when IDs overlap (iterated first).

### Verification
- ✅ Code reads correctly at line ~3191
- ✅ Dedup logic handles missing IDs gracefully (None → always included)
- ✅ No breaking change to existing callers

---

## M2: PnL Sanity Bound Changed from 1.1× to Strict 1.0×

### Problem
The old code used `max_loss_rupees * 1.1` as the PnL clamp boundary, silently
tolerating 10% data corruption for defined-risk structures where PnL should
NEVER exceed max_loss by definition.

### Fix Applied
All 4 sites in `paper_trader.py` now use strict `1.0×` boundary with an
`is_defined_risk` guard:

```python
is_defined_risk = t.get("is_defined_risk", True)
if (
    is_defined_risk
    and max_loss_rupees
    and abs(pnl) > max_loss_rupees
):
    # Log CORRUPT DATA error and clamp
    pnl = -max_loss_rupees if pnl < 0 else max_loss_rupees
```

Naked/undefined-risk structures are NOT clamped (their loss can exceed any estimate).

### Verification
- ✅ Lines ~1418-1438: First exit path (check_option_exits)
- ✅ Lines ~1459-1477: Final PnL re-application
- ✅ Lines ~1592-1612: Second exit path (_check_option_exits)
- ✅ Lines ~1633-1651: Final PnL re-application (second path)
- ✅ All 4 sites use identical logic with `is_defined_risk` guard

---

## M3: Generic `enter_option_structure()` Missing `wing_width`

### Problem
The generic entry function hardcoded `"wing_width": 0.0`, breaking STRIKE_BREACH
smart exits which check `spot vs short_strikes ± wing_width`. It also lacked
`max_loss_rupees`, `is_defined_risk`, and `structure_type` fields.

### Fix Applied
Full computation added (~lines 730-810):

1. **Wing width**: Computed from resolved leg strikes
   - Call spread width = |short CE − long CE|
   - Put spread width = |short PE − long PE|
   - Final = max(call_spread_width, put_spread_width)

2. **Structure type detection**: iron_condor / bear_call_spread / bull_put_spread / naked_short

3. **Max loss**: Delegates to `calc_structure_max_loss()` with computed parameters

4. **Trade dict**: Now includes `max_loss_rupees`, `is_defined_risk`, `structure_type`, `wing_width`

### Verification
- ✅ Wing width computed before trade creation
- ✅ Structure type inferred from leg composition
- ✅ Max loss calculated via shared utility
- ✅ All smart-exit-required fields present in trade dict

---

## M4: `_nearest()` Equidistant Strike Bias

### Problem
Original code used `<=` comparison: `abs(upper - target) <= abs(lower - target)`,
which ALWAYS returned the upper strike when equidistant. This created systematic
placement bias in Iron Condors and spreads.

### Fix Applied
Changed to strict `<` comparison with explicit tie-break:

```python
dist_lower = abs(lower - target)
dist_upper = abs(upper - target)
if dist_upper < dist_lower:
    return upper
if dist_lower < dist_upper:
    return lower
# Equidistant: defer to prefer_higher parameter
return upper if prefer_higher else lower
```

### Verification
- ✅ Line ~970: `dist_upper < dist_lower` (strict)
- ✅ Tie-break explicitly uses `prefer_higher` parameter
- ✅ No behavioral change for non-equidistant cases

---

## M5: `peak_price` Misleading Name for SHORT Trades

### Problem
For SHORT trades, `peak_price` tracked the LOWEST price (trough), but the variable
name suggested "highest price reached." Future developers could misinterpret it.

### Fix Applied
Introduced local variable `best_price` with direction-aware comments:

```python
# M5 FIX: Trailing stop logic with direction-aware variable naming.
# peak_price semantics depend on direction:
#   - LONG: highest price (true "peak") — trailing stop ratchets UP
#   - SHORT: lowest price ("trough") — trailing stop ratchets DOWN
# We use local variable `best_price` to avoid the misleading "peak" name
# for SHORT trades. The stored field name is kept for backward compatibility.
best_price = trade.get("peak_price", entry_price)
```

The persisted field name `peak_price` is preserved for backward compatibility
with existing trade data.

### Verification
- ✅ Lines ~2001-2027: Full trailing stop block uses `best_price`
- ✅ Field write-back still uses `trade["peak_price"]` for storage compat
- ✅ Comments clearly explain direction-dependent semantics

---

## M6: `check_naked_legs()` Strike Ordering Validation

### Problem
The function verified that protective legs existed (count-wise) but never checked
that they were on the CORRECT side of the short leg. A long CE below the short CE
does NOT cap upside risk — it creates a debit spread inside the credit spread.

### Fix Applied
Added per-leg strike ordering validation:

**Bear Call Spread:** For each short CE, verifies at least one long CE exists
with `strike > short_strike`.

**Bull Put Spread:** For each short PE, verifies at least one long PE exists
with `strike < short_strike`.

Returns descriptive error messages like:
```
"Call spread strike ordering invalid: short CE at 25000 has no protective long CE above it"
```

### Verification
- ✅ Lines ~710-730: Call spread strike validation loop
- ✅ Lines ~735-755: Put spread strike validation loop
- ✅ Handles None strikes gracefully (skips)
- ✅ Works with both dict and object leg types via `_get()` helper

---

## M7: `is_within_exit_window()` Symbol-Aware Config

### Problem
Always read from `cfg["nifty_options"]` regardless of symbol, causing BANKNIFTY
positional exits to follow NIFTY's timing rules.

### Fix Applied
Added `symbol` parameter with config dispatch:

```python
def is_within_exit_window(
    cfg: dict = None,
    now: datetime = None,
    mode: str = "positional",
    symbol: str = "NIFTY",  # NEW parameter
) -> Tuple[bool, str]:
    cfg_key = "banknifty_options" if symbol == "BANKNIFTY" else "nifty_options"
    sym_cfg = cfg.get(cfg_key, {})
```

Also uses `is_symbol_expiry_today(symbol)` for symbol-specific expiry detection.

### Verification
- ✅ Function signature includes `symbol: str = "NIFTY"` (backward compatible default)
- ✅ Config key dispatch at line ~553
- ✅ Expiry check uses symbol-aware function
- ✅ Existing callers without `symbol` arg default to NIFTY (no breakage)

---

## M8: `option_structure_size()` Margin Guard

### Problem
Sized positions purely by `risk_budget / max_loss_per_lot`. Structures with low
max_loss but high margin requirements could exceed the account's margin capacity,
causing broker rejection or margin calls.

### Fix Applied
Added `margin_per_lot` keyword argument:

```python
def option_structure_size(
    capital: float,
    per_trade_pct: float,
    max_loss_per_lot: float,
    lot_size: int,
    *,
    margin_per_lot: float = 0.0,  # NEW
    max_notional: float = 0.0,
) -> SizeResult:
    lots_by_risk = math.floor(risk_budget / max_loss_per_lot)
    lots_by_margin = lots_by_risk
    if margin_per_lot > 0:
        lots_by_margin = math.floor(capital / margin_per_lot)
    lots = min(lots_by_risk, lots_by_margin)
```

Notes field reports which constraint was binding: `"3 lots of structure | capped by margin | margin/lot=₹150,000"`.

### Verification
- ✅ Keyword-only parameter (after `*`) prevents accidental positional usage
- ✅ Default `0.0` preserves existing behavior for callers that don't pass it
- ✅ Conservative: always takes `min(risk, margin)`
- ✅ Notes field provides audit trail for sizing decisions

---

## Cumulative Audit Status

| Severity | Total | Fixed | Remaining |
|----------|-------|-------|-----------|
| 🔴 CRITICAL | 5 | 5 | 0 |
| 🔴 HIGH | 8 | 8 | 0 |
| 🟡 MEDIUM | 8 | 8 | 0 |
| **TOTAL** | **21** | **21** | **0** |

All identified trading logic flaws and technical bugs have been resolved.
