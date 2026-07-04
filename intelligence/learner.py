import hashlib
import numpy as np
import pandas as pd
from datetime import datetime, date, timedelta, timezone

def _wilson_lower_bound(pos: int, n: int, confidence: float = 0.95) -> float:
    if n == 0:
        return 0.0
    z = 1.96  # approx for 95%
    phat = 1.0 * pos / n
    return (phat + z*z/(2*n) - z * np.sqrt((phat*(1-phat)+z*z/(4*n))/n))/(1+z*z/n)

def _get_entry_hour_bucket(hour: int) -> str:
    if hour <= 10:
        return "OPEN"
    elif hour <= 13:
        return "MID"
    else:
        return "CLOSE"

def _get_vix_bucket(vix: float) -> str:
    if vix < 14:
        return "LOW"
    elif vix <= 20:
        return "MED"
    else:
        return "HIGH"

def _get_adx_bucket(adx: float) -> str:
    if adx < 20:
        return "WEAK"
    elif adx <= 30:
        return "MID"
    else:
        return "STRONG"

def analyze_history(closed_trades: list[dict], lookback_days: int = 30) -> dict:
    now_utc = datetime.now(timezone.utc).isoformat()
    if not closed_trades:
        return {"rules": [], "segments": [], "generated_at": now_utc}
        
    cutoff_date = (date.today() - timedelta(days=lookback_days)).isoformat()
    recent = [t for t in closed_trades if t.get("entry_date", "") >= cutoff_date]
    if not recent:
        return {"rules": [], "segments": [], "generated_at": now_utc}
        
    data = []
    for t in recent:
        if t.get("pnl") is None:
            continue
        row = {
            "entry_date": t.get("entry_date"),
            "win": 1 if t.get("pnl", 0) > 0 else 0,
            "confidence": t.get("confidence"),
            "source_regime": t.get("source_regime"),
            "direction": t.get("direction"),
            "entry_hour_bucket": _get_entry_hour_bucket(t.get("entry_hour", 9)) if t.get("entry_hour") is not None else None,
            "sector": t.get("sector"),
            "bias_at_entry": t.get("bias_at_entry"),
            "vix_bucket": _get_vix_bucket(t.get("vix_at_entry")) if t.get("vix_at_entry") is not None else None,
            "adx_bucket": _get_adx_bucket(t.get("adx_at_entry")) if t.get("adx_at_entry") is not None else None,
        }
        data.append(row)
        
    df = pd.DataFrame(data)
    dims = ["confidence", "source_regime", "direction", "entry_hour_bucket", "sector", "bias_at_entry", "vix_bucket", "adx_bucket"]
    
    rules = []
    
    for dim in dims:
        if dim not in df.columns:
            continue
        
        grouped = df.groupby(dim)
        for val, group in grouped:
            if pd.isna(val):
                continue
            
            n = len(group)
            if n < 5:
                continue
                
            # BUG-16 FIX: dropna() before nunique() so None isn't counted as a distinct date.
            distinct_dates = group["entry_date"].dropna().nunique()
            if distinct_dates < 3:
                continue
                
            wins = group["win"].sum()
            win_rate = wins / n
            wilson = _wilson_lower_bound(wins, n)
            
            action = None
            if wilson < 0.40:
                action = "BLOCK"
            elif wilson < 0.50:
                action = "DOWNGRADE"
                
            if action:
                dim_str = str(dim)
                val_str = str(val)
                hash_input = f"{dim_str}:{val_str}"
                rule_id = hashlib.md5(hash_input.encode()).hexdigest()[:8]
                
                loss_pct = round((1 - win_rate) * 100)
                evidence = f"{val_str}-confidence trades lost {loss_pct}% (n={n}) over {lookback_days}d" if dim == "confidence" else f"{val_str} trades lost {loss_pct}% (n={n}) over {lookback_days}d"
                
                rules.append({
                    "id": rule_id,
                    "segment": {"dim": dim_str, "value": val_str},
                    "action": action,
                    "evidence": evidence,
                    "created_at": now_utc,
                    "expires_at": (datetime.now(timezone.utc) + timedelta(days=14)).isoformat(),
                    "sample_size": n,
                    "win_rate": round(win_rate, 3),
                    "wilson_low": round(wilson, 3)
                })
                
    return {
        "rules": rules,
        "segments": [],
        "generated_at": now_utc
    }

def build_correction_strings(rules: list[dict]) -> list[str]:
    res = []
    now = datetime.now(timezone.utc).isoformat()
    valid_rules = [r for r in rules if r.get("expires_at", "") >= now]
    
    if not valid_rules:
        return []
        
    for r in valid_rules:
        dim = r["segment"]["dim"]
        val = r["segment"]["value"]
        act = r["action"]
        ev = r["evidence"]
        res.append(f"{act} {dim}={val}: {ev}")
        
    return res

def apply_learned_filter(alert: dict, rules: list[dict]) -> tuple[str, str]:
    now = datetime.now(timezone.utc).isoformat()
    
    # Standardized IST hour lookup
    IST_TZ = timezone(timedelta(hours=5, minutes=30))
    current_hour = datetime.now(IST_TZ).hour
    
    # 1. Build Alert Dimensions
    alert_dims = {
        "confidence": alert.get("confidence"),
        "source_regime": alert.get("source_regime"),
        "direction": alert.get("direction"),
        "sector": alert.get("sector"),
        "bias_at_entry": alert.get("bias_at_entry") or alert.get("flow_bias"),
        "entry_hour_bucket": _get_entry_hour_bucket(current_hour),
    }
    
    if alert.get("vix_at_entry") is not None:
        alert_dims["vix_bucket"] = _get_vix_bucket(alert["vix_at_entry"])
    if alert.get("adx_at_entry") is not None:
        alert_dims["adx_bucket"] = _get_adx_bucket(alert["adx_at_entry"])
    
    # 2. O(R) to O(Dim) Pre-indexing
    # Only process valid rules. BLOCK overrides DOWNGRADE.
    active_rules = {}
    for r in rules:
        if r.get("expires_at", "") < now:
            continue
        dim = r["segment"]["dim"]
        val = str(r["segment"]["value"])
        key = (dim, val)
        
        # Priority: Keep BLOCK if multiple rules match different dims
        if key not in active_rules or r["action"] == "BLOCK":
            active_rules[key] = r

    # 3. Constant Time Lookups across alert attributes
    best_action = "PASS"
    best_evidence = ""
    
    for dim, alert_val in alert_dims.items():
        if alert_val is None:
            continue
        
        match = active_rules.get((dim, str(alert_val)))
        if match:
            if match["action"] == "BLOCK":
                return "BLOCK", match.get("evidence", "")
            best_action = "DOWNGRADE"
            best_evidence = match.get("evidence", "")
                
    return best_action, best_evidence
