from __future__ import annotations

from typing import Tuple

import pandas as pd


def is_overextended_from_vwap(
    price: float, vwap: float, max_dist_pct: float
) -> tuple[bool, str]:
    """Return (overextended, reason) if price deviates from VWAP beyond threshold."""
    if vwap is None or vwap <= 0:
        return False, "VWAP unavailable"
    dist = abs(price - vwap) / float(vwap) * 100.0
    if dist > max_dist_pct:
        return (
            True,
            f"Overextended from VWAP by {dist:.2f}% (limit {max_dist_pct:.2f}%)",
        )
    return False, ""


def check_vwap_trigger(price: float, vwap: float) -> bool:
    """Return True if price is acceptable relative to VWAP as a trigger anchor."""
    if vwap is None or vwap <= 0:
        return True
    return price >= vwap


def compute_atr_from_df(df: pd.DataFrame, period: int = 14) -> float:
    """Compute 14-period ATR from OHLC dataframe."""
    if df is None or df.empty or len(df) < period:
        return 0.0
    h = df["high"]
    l = df["low"]
    c = df["close"].shift(1)
    tr = pd.concat([h - l, (h - c).abs(), (l - c).abs()], axis=1).max(axis=1)
    atr_val = tr.rolling(period).mean().iloc[-1]
    return float(atr_val) if pd.notna(atr_val) else 0.0


def compute_rsi_from_df(df: pd.DataFrame, period: int = 14) -> float:
    """Compute RSI(period) from OHLC dataframe."""
    if df is None or df.empty or len(df) < period + 1:
        return 50.0  # Default neutral RSI

    closes = df["close"]
    deltas = closes.diff()

    gains = deltas.where(deltas > 0, 0.0)
    losses = -deltas.where(deltas < 0, 0.0)

    avg_gain = gains.rolling(window=period, min_periods=1).mean()
    avg_loss = losses.rolling(window=period, min_periods=1).mean()

    rs = avg_gain / avg_loss.replace(0, 1e-10)
    rsi = 100 - (100 / (1 + rs))

    rsi_val = rsi.iloc[-1]
    return float(rsi_val) if pd.notna(rsi_val) else 50.0


def is_rsi_overextended(
    df_5m: pd.DataFrame, threshold_long: int, threshold_short: int, direction: str
) -> tuple[bool, str]:
    """
    Check if RSI(14) indicates overextension in current direction.

    Returns:
        (is_overextended: bool, reason: str)
    """
    if df_5m is None or df_5m.empty or len(df_5m) < 15:
        return False, "RSI unavailable (insufficient data); assume OK"

    rsi = compute_rsi_from_df(df_5m)

    if direction == "LONG":
        if rsi > threshold_long:
            return True, f"RSI({rsi:.1f}) > {threshold_long} (overbought)"
        return False, f"RSI({rsi:.1f}) within limits"
    elif direction == "SHORT":
        if rsi < threshold_short:
            return True, f"RSI({rsi:.1f}) < {threshold_short} (oversold)"
        return False, f"RSI({rsi:.1f}) within limits"

    return False, "Unknown direction"


def is_price_overextended(
    price: float,
    open_price: float,
    atr: float,
    max_atr_extension: float,
    direction: str,
) -> tuple[bool, str]:
    """
    Check if price has moved too far from day open (chasing indicator).

    Returns:
        (is_overextended: bool, reason: str)
    """
    if atr is None or atr <= 0:
        return False, "ATR unavailable; assume OK"

    distance_from_open_atr = abs(price - open_price) / atr

    if distance_from_open_atr > max_atr_extension:
        if direction == "LONG":
            return (
                True,
                f"LONG: price +{distance_from_open_atr:.2f} ATR from open (max {max_atr_extension})",
            )
        else:
            return (
                True,
                f"SHORT: price -{distance_from_open_atr:.2f} ATR from open (max {max_atr_extension})",
            )

    if direction == "LONG":
        return False, f"LONG: price +{distance_from_open_atr:.2f} ATR from open (OK)"
    else:
        return False, f"SHORT: price -{distance_from_open_atr:.2f} ATR from open (OK)"


def market_exhaustion_score(
    nifty_df: pd.DataFrame,
    advances: int,
    declines: int,
    breadth_drop_threshold_pct: float,
    vix_df: pd.DataFrame | None = None,
    vix_spike_threshold_pct: float = 5.0,
) -> tuple[float, str]:
    """
    Compute market exhaustion severity (0.0–1.0) based on breadth & VIX.

    Returns:
        (exhaustion_score: float, reason: str)
        - Score 0.0–1.0: 0=healthy, 1=severe exhaustion
    """
    if not nifty_df or nifty_df.empty or len(nifty_df) < 20:
        return 0.0, "Market data unavailable; assume healthy"

    exhaustion_score = 0.0
    reasons = []

    # Breadth check: compare current A/D ratio to 20-session average
    total_stocks = max(advances + declines, 1)
    current_ad_ratio = advances / total_stocks

    # Simple 20-session historical average from NIFTY closes
    # (In production, store breadth history; for now, estimate from close momentum)
    if len(nifty_df) >= 20:
        # Use momentum of last 20 closes as proxy for historical breadth health
        recent_closes = nifty_df["close"].tail(20)
        historical_momentum = (
            recent_closes.iloc[-1] - recent_closes.iloc[0]
        ) / recent_closes.iloc[0]

        # If current A/D ratio is weak (< 0.55 = more declines) and historical was stronger:
        if current_ad_ratio < 0.55:
            breadth_weakness = (0.55 - current_ad_ratio) / 0.55 * 100
            if breadth_weakness > breadth_drop_threshold_pct:
                exhaustion_score += 0.4
                reasons.append(
                    f"Breadth weak: A/D ratio {current_ad_ratio:.2%} (threshold {breadth_drop_threshold_pct}% drop)"
                )

    # VIX check: intraday spike
    if vix_df is not None and not vix_df.empty and len(vix_df) >= 2:
        vix_close_yesterday = vix_df["close"].iloc[-2]
        vix_close_today = vix_df["close"].iloc[-1]

        if vix_close_yesterday > 0:
            vix_spike_pct = (
                (vix_close_today - vix_close_yesterday) / vix_close_yesterday * 100
            )
            if vix_spike_pct > vix_spike_threshold_pct:
                exhaustion_score += 0.4
                reasons.append(
                    f"VIX spike: +{vix_spike_pct:.1f}% intraday (threshold {vix_spike_threshold_pct}%)"
                )

    # Cap at 1.0
    exhaustion_score = min(exhaustion_score, 1.0)

    if exhaustion_score > 0:
        reason_str = " + ".join(reasons)
    else:
        reason_str = "Market health: no exhaustion signals"

    return exhaustion_score, reason_str


def evaluate_timing_for_entry(
    symbol: str,
    direction: str,
    price: float,
    config: dict,
    df_5m: pd.DataFrame,
    df_1d: pd.DataFrame,
    vwap_5m: float | None,
    ai_sentiment_current: dict | None,
    market_breadth: dict | None,
    vix_df: pd.DataFrame | None = None,
) -> dict:
    """
    Unified entry point: evaluates all timing gates for a single symbol.

    Returns:
        dict {
            "timing_ok": bool,
            "checks": { ... },
            "reason": str,
            "event_risk_mode": bool,
            "size_multiplier": float
        }
    """
    if not config:
        return {
            "timing_ok": True,
            "checks": {},
            "reason": "No timing config provided",
            "event_risk_mode": False,
            "size_multiplier": 1.0,
        }

    if not config.get("enabled", True):
        return {
            "timing_ok": True,
            "checks": {},
            "reason": "Timing engine disabled",
            "event_risk_mode": False,
            "size_multiplier": 1.0,
        }

    checks = {}
    failed_checks = []

    # --- VWAP Trigger Check ---
    late_entry_cfg = config.get("late_entry_filter", {})
    if late_entry_cfg.get("enabled", True):
        is_over, reason_vwap = is_overextended_from_vwap(
            price, vwap_5m, late_entry_cfg.get("max_vwap_dist_pct", 1.0)
        )
        checks["vwap_overextended"] = (is_over, reason_vwap)
        if is_over:
            failed_checks.append(reason_vwap)

    # --- RSI Overextension Check ---
    if late_entry_cfg.get("enabled", True) and df_5m is not None and not df_5m.empty:
        is_rsi_over, reason_rsi = is_rsi_overextended(
            df_5m,
            late_entry_cfg.get("rsi_threshold_long", 70),
            late_entry_cfg.get("rsi_threshold_short", 30),
            direction,
        )
        checks["rsi_overextended"] = (is_rsi_over, reason_rsi)
        if is_rsi_over:
            failed_checks.append(reason_rsi)

    # --- Price Distance from Open Check ---
    if late_entry_cfg.get("enabled", True) and df_1d is not None and not df_1d.empty:
        open_price = df_1d["open"].iloc[-1]
        atr = compute_atr_from_df(df_1d)
        is_price_over, reason_dist = is_price_overextended(
            price,
            open_price,
            atr,
            late_entry_cfg.get("max_intraday_atr_extension", 1.0),
            direction,
        )
        checks["price_distance"] = (is_price_over, reason_dist)
        if is_price_over:
            failed_checks.append(reason_dist)

    # --- Market Exhaustion Check ---
    exhaustion_cfg = config.get("market_exhaustion", {})
    exhaustion_score = 0.0
    exhaustion_reason = ""
    if (
        exhaustion_cfg.get("enabled", True)
        and df_1d is not None
        and market_breadth is not None
    ):
        exhaustion_score, exhaustion_reason = market_exhaustion_score(
            df_1d,
            market_breadth.get("advances", 0),
            market_breadth.get("declines", 0),
            exhaustion_cfg.get("breadth_drop_threshold_pct", 8),
            vix_df,
            exhaustion_cfg.get("vix_intraday_spike_pct", 5),
        )
        checks["market_exhaustion"] = (exhaustion_score, exhaustion_reason)

        # If exhaustion high + direction is LONG: be stricter
        if exhaustion_score > 0.6 and direction == "LONG":
            failed_checks.append(
                f"Market exhaustion {exhaustion_score:.2f} (score > 0.6)"
            )

    # --- Consolidate Result ---
    timing_ok = len(failed_checks) == 0
    consolidated_reason = (
        "; ".join(failed_checks) if failed_checks else "All timing checks passed"
    )

    # --- Event Risk Mode ---
    event_risk_mode = False
    size_multiplier = 1.0
    event_risk_cfg = config.get("event_risk_mode", {})

    if event_risk_cfg.get("enabled", True):
        if exhaustion_score > 0.6:
            event_risk_mode = True
            size_multiplier = event_risk_cfg.get("size_multiplier", 0.5)

    return {
        "timing_ok": timing_ok,
        "checks": checks,
        "reason": consolidated_reason,
        "event_risk_mode": event_risk_mode,
        "size_multiplier": size_multiplier,
    }


def review_timing_with_llm(
    symbol: str,
    direction: str,
    price: float,
    timing_snapshot: dict,
    market_regime: str,
    ai_sentiment: dict | None,
    use_groq: bool = True,
    groq_config: dict | None = None,
) -> dict:
    """
    LLM review of entry timing quality (Phase 2).

    Validates entry timing against market context using a language model.
    Provides AI-driven confidence in addition to Phase 1 technical checks.

    Args:
        symbol: Stock ticker
        direction: LONG or SHORT
        price: Current LTP
        timing_snapshot: Dict from Phase 1 evaluate_timing_for_entry() checks
        market_regime: Detected regime (TREND_UP, RANGE_LOW_VOL, etc.)
        ai_sentiment: Current AI sentiment dict with overall/confidence/actionable_ideas
        use_groq: Try Groq first (fast); fallback to Gemini if fails
        groq_config: Config dict with api_key, model, timeout_sec

    Returns:
        {
            "ai_timing_ok": bool,  # LLM approves timing
            "confidence": float,   # 0.0–1.0 confidence in approval
            "reason": str,         # LLM explanation
            "sentiment_warning": bool,  # True if sentiment concern
            "model_used": str,     # "groq" | "gemini" | "fallback"
            "latency_ms": int      # API call duration
        }

    Fail-Safe: If LLM unavailable, returns ai_timing_ok=True (allow entry)
    """
    import logging
    import time

    start_time = time.time()
    logger = logging.getLogger(__name__)

    # Extract timing details
    vwap_status = timing_snapshot.get("vwap_overextended", (False, "N/A"))
    rsi_status = timing_snapshot.get("rsi_overextended", (False, "N/A"))
    exhaustion = timing_snapshot.get("market_exhaustion", (0.0, "N/A"))

    vwap_desc = (
        f"VWAP {vwap_status[1]}" if isinstance(vwap_status, tuple) else str(vwap_status)
    )
    rsi_desc = (
        f"RSI {rsi_status[1]}" if isinstance(rsi_status, tuple) else str(rsi_status)
    )
    exhaustion_score = exhaustion[0] if isinstance(exhaustion, tuple) else exhaustion
    exhaustion_desc = (
        exhaustion[1] if isinstance(exhaustion, tuple) else str(exhaustion)
    )

    # Build prompt
    sentiment_desc = "N/A"
    if ai_sentiment:
        sentiment_desc = ai_sentiment.get("overall", "NEUTRAL")
        confidence = ai_sentiment.get("confidence", "MEDIUM")
        sentiment_desc = f"{sentiment_desc} ({confidence})"

    prompt = f"""Entry Timing Review:
    Symbol: {symbol}
    Direction: {direction}
    Price: {price:.2f}
    Market: {market_regime}

    Technical Status:
    - {vwap_desc}
    - {rsi_desc}
    - Market Exhaustion: {exhaustion_desc}

    Market Sentiment: {sentiment_desc}

    Question: Is this {direction} entry timing sound? Consider market regime, technical status, and sentiment.
    Reply concisely: YES (good timing), MAYBE (marginal), or NO (poor timing).
    """

    try:
        # Try Groq first if configured
        if use_groq and groq_config:
            try:
                from groq import Groq

                client = Groq(
                    api_key=groq_config.get("api_key")
                    or __import__("os").getenv("GROQ_API_KEY")
                )
                timeout = groq_config.get("timeout_sec", 3)

                message = client.chat.completions.create(
                    model=groq_config.get("model", "mixtral-8x7b-32768"),
                    messages=[{"role": "user", "content": prompt}],
                    temperature=groq_config.get("temperature", 0.3),
                    max_tokens=100,
                    timeout=timeout,
                )

                response_text = message.choices[0].message.content.upper().strip()
                latency_ms = int((time.time() - start_time) * 1000)

                # Parse response
                ai_timing_ok = "YES" in response_text
                confidence = (
                    0.9
                    if "YES" in response_text
                    else (0.5 if "MAYBE" in response_text else 0.1)
                )
                sentiment_warning = (
                    "BEARISH" in sentiment_desc or "FLIP" in response_text
                )

                logger.debug(
                    f"[AI_REVIEW] {symbol} {direction}: Groq response ({latency_ms}ms) - {response_text[:50]}"
                )

                return {
                    "ai_timing_ok": ai_timing_ok,
                    "confidence": confidence,
                    "reason": response_text[:200],
                    "sentiment_warning": sentiment_warning,
                    "model_used": "groq",
                    "latency_ms": latency_ms,
                }

            except Exception as groq_err:
                logger.debug(
                    f"[AI_REVIEW] {symbol}: Groq failed ({groq_err}); trying Gemini"
                )
                # Fall through to Gemini

        # Fallback to Gemini
        try:
            import google.generativeai as genai

            api_key = groq_config.get("gemini_api_key") if groq_config else None
            if not api_key:
                api_key = __import__("os").getenv("GOOGLE_API_KEY")

            if api_key:
                genai.configure(api_key=api_key)
                model = genai.GenerativeModel("gemini-pro")
                response = model.generate_content(
                    prompt, generation_config={"max_output_tokens": 100}
                )

                response_text = response.text.upper().strip() if response.text else ""
                latency_ms = int((time.time() - start_time) * 1000)

                ai_timing_ok = "YES" in response_text
                confidence = (
                    0.85
                    if "YES" in response_text
                    else (0.45 if "MAYBE" in response_text else 0.15)
                )
                sentiment_warning = "BEARISH" in sentiment_desc

                logger.debug(
                    f"[AI_REVIEW] {symbol} {direction}: Gemini response ({latency_ms}ms) - {response_text[:50]}"
                )

                return {
                    "ai_timing_ok": ai_timing_ok,
                    "confidence": confidence,
                    "reason": response_text[:200],
                    "sentiment_warning": sentiment_warning,
                    "model_used": "gemini",
                    "latency_ms": latency_ms,
                }
        except Exception as gemini_err:
            logger.debug(f"[AI_REVIEW] {symbol}: Gemini failed ({gemini_err})")

    except Exception as e:
        logger.debug(f"[AI_REVIEW] {symbol}: LLM check error ({e})")

    # Fallback: fail-open (allow entry)
    latency_ms = int((time.time() - start_time) * 1000)
    logger.debug(f"[AI_REVIEW] {symbol}: Falling back to Phase 1; LLM unavailable")

    return {
        "ai_timing_ok": True,  # Fail-open
        "confidence": 0.0,
        "reason": "LLM unavailable; Phase 1 gates applied",
        "sentiment_warning": False,
        "model_used": "fallback",
        "latency_ms": latency_ms,
    }


def get_regime_adjusted_thresholds(
    market_regime: str, base_config: dict, dynamic_rules: dict
) -> dict:
    """
    Adjust timing thresholds based on detected market regime (Phase 2).

    Args:
        market_regime: e.g., TREND_UP, TREND_DOWN, RANGE_LOW_VOL, VOL_CONTRACTION
        base_config: Base timing_engine.late_entry_filter config
        dynamic_rules: timing_engine.dynamic_thresholds.adjustment_rules

    Returns:
        dict with adjusted thresholds + applied_regime + multiplier
        {
            "max_vwap_dist_pct": float,
            "rsi_threshold_long": int,
            "rsi_threshold_short": int,
            "max_intraday_atr_extension": float,
            "breadth_drop_threshold_pct": float,
            "applied_regime": str,
            "multiplier": dict,  # How much each was adjusted
        }
    """
    if not dynamic_rules or market_regime not in dynamic_rules:
        # No override; return base config
        return {
            "max_vwap_dist_pct": base_config.get("max_vwap_dist_pct", 1.0),
            "rsi_threshold_long": base_config.get("rsi_threshold_long", 70),
            "rsi_threshold_short": base_config.get("rsi_threshold_short", 30),
            "max_intraday_atr_extension": base_config.get(
                "max_intraday_atr_extension", 1.0
            ),
            "breadth_drop_threshold_pct": base_config.get(
                "breadth_drop_threshold_pct", 8
            ),
            "applied_regime": market_regime,
            "multiplier": {},
        }

    # Apply regime-specific overrides
    regime_rules = dynamic_rules[market_regime]
    adjusted = {}
    multiplier = {}

    # VWAP adjustment
    base_vwap = base_config.get("max_vwap_dist_pct", 1.0)
    adjusted_vwap = regime_rules.get("max_vwap_dist_pct", base_vwap)
    adjusted["max_vwap_dist_pct"] = adjusted_vwap
    multiplier["vwap"] = adjusted_vwap / base_vwap if base_vwap > 0 else 1.0

    # RSI thresholds
    base_rsi_long = base_config.get("rsi_threshold_long", 70)
    adjusted_rsi_long = regime_rules.get("rsi_threshold_long", base_rsi_long)
    adjusted["rsi_threshold_long"] = adjusted_rsi_long
    multiplier["rsi_long"] = (
        adjusted_rsi_long / base_rsi_long if base_rsi_long > 0 else 1.0
    )

    base_rsi_short = base_config.get("rsi_threshold_short", 30)
    adjusted_rsi_short = regime_rules.get("rsi_threshold_short", base_rsi_short)
    adjusted["rsi_threshold_short"] = adjusted_rsi_short
    multiplier["rsi_short"] = (
        adjusted_rsi_short / base_rsi_short if base_rsi_short > 0 else 1.0
    )

    # ATR extension
    base_atr = base_config.get("max_intraday_atr_extension", 1.0)
    adjusted_atr = regime_rules.get("max_intraday_atr_extension", base_atr)
    adjusted["max_intraday_atr_extension"] = adjusted_atr
    multiplier["atr"] = adjusted_atr / base_atr if base_atr > 0 else 1.0

    # Breadth threshold
    base_breadth = base_config.get("breadth_drop_threshold_pct", 8)
    adjusted_breadth = regime_rules.get("breadth_drop_threshold_pct", base_breadth)
    adjusted["breadth_drop_threshold_pct"] = adjusted_breadth
    multiplier["breadth"] = adjusted_breadth / base_breadth if base_breadth > 0 else 1.0

    adjusted["applied_regime"] = market_regime
    adjusted["multiplier"] = multiplier

    return adjusted


def detect_sentiment_flip(
    current_sentiment: dict | None,
    previous_sentiment: dict | None,
    window_trades: list[dict] | None,
) -> dict:
    """
    Detect recent sentiment reversals (BULLISH → BEARISH, etc.) (Phase 2).

    Args:
        current_sentiment: Latest AI sentiment from flows_mod.snapshot()
        previous_sentiment: Last recorded sentiment (from journal)
        window_trades: Last N trades (for context and loss analysis)

    Returns:
        {
            "flip_detected": bool,
            "flip_type": "BULLISH_TO_BEARISH" | "BEARISH_TO_BULLISH" | "NEUTRAL_SHIFT",
            "flip_timestamp": datetime,
            "flip_confidence": float (0.0-1.0),
            "trading_blocked_until": datetime,  # 30 min freeze
            "reason": str
        }
    """
    from datetime import datetime, timedelta

    now = datetime.now()
    flip_detected = False
    flip_type = "NEUTRAL_SHIFT"
    flip_confidence = 0.0
    reason = ""

    # If no previous sentiment: no flip
    if not previous_sentiment or not current_sentiment:
        return {
            "flip_detected": False,
            "flip_type": "NO_HISTORY",
            "flip_timestamp": None,
            "flip_confidence": 0.0,
            "trading_blocked_until": None,
            "reason": "Insufficient sentiment history",
        }

    # Extract sentiments
    prev_overall = (previous_sentiment.get("overall") or "NEUTRAL").upper()
    curr_overall = (current_sentiment.get("overall") or "NEUTRAL").upper()

    # Detect direction flip
    if prev_overall != curr_overall:
        if prev_overall == "BULLISH" and curr_overall == "BEARISH":
            flip_type = "BULLISH_TO_BEARISH"
            flip_detected = True
            flip_confidence = 0.8
        elif prev_overall == "BEARISH" and curr_overall == "BULLISH":
            flip_type = "BEARISH_TO_BULLISH"
            flip_detected = True
            flip_confidence = 0.8
        else:
            flip_type = "NEUTRAL_SHIFT"
            flip_detected = True
            flip_confidence = 0.5

        reason = f"Sentiment changed from {prev_overall} to {curr_overall}"

    # Confidence drop also signals flip (market doubt)
    prev_conf = previous_sentiment.get("confidence", "MEDIUM")
    curr_conf = current_sentiment.get("confidence", "MEDIUM")

    confidence_map = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
    if confidence_map.get(curr_conf, 2) < confidence_map.get(prev_conf, 2) - 1:
        flip_detected = True
        flip_confidence = max(flip_confidence, 0.6)
        reason += f"; confidence dropped from {prev_conf} to {curr_conf}"

    # Check recent losses in window (suggests flip was real)
    if flip_detected and window_trades:
        recent_losses = sum(1 for t in window_trades[-5:] if t.get("pnl_rupees", 0) < 0)
        if recent_losses >= 3:
            flip_confidence = min(flip_confidence + 0.2, 1.0)
            reason += f"; {recent_losses}/5 recent trades losing"

    trading_blocked_until = now + timedelta(minutes=30) if flip_detected else None

    return {
        "flip_detected": flip_detected,
        "flip_type": flip_type,
        "flip_timestamp": now if flip_detected else None,
        "flip_confidence": flip_confidence,
        "trading_blocked_until": trading_blocked_until,
        "reason": reason,
    }
