"""
Feedback Loop - Learn from Trade Outcomes
==========================================

Closes the AGoT learning loop by analyzing journal outcomes and
updating the reasoning system:

1. Analyzes closed trades to identify winning/losing patterns
2. Updates evidence weights based on historical performance
3. Generates "correction rules" that adjust future reasoning
4. Tracks the accuracy of regime classifications over time

This module integrates with the existing intelligence/learner.py
but adds AGoT-specific learning:
- Regime classification accuracy tracking
- Signal ensemble performance analysis
- Confidence calibration (are 70% confidence trades actually winning 70%?)

Usage:
    from intelligence.feedback_loop import FeedbackLoop
    
    loop = FeedbackLoop(journal_db_path="./data/journal.sqlite")
    
    # Analyze recent outcomes
    analysis = loop.analyze_outcomes(lookback_days=30)
    
    # Get corrections to apply to future decisions
    corrections = loop.get_corrections()
    
    # Check regime classification accuracy
    regime_accuracy = loop.get_regime_accuracy(lookback_days=60)
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta, timezone
from typing import Any


@dataclass
class FeedbackAnalysis:
    """Results from feedback loop analysis."""
    total_trades: int
    win_rate: float
    avg_win: float
    avg_loss: float
    profit_factor: float
    regime_accuracy: dict[str, dict]       # regime -> {win_rate, count, avg_pnl}
    confidence_calibration: dict[str, float]  # confidence_bucket -> actual_win_rate
    signal_performance: dict[str, dict]    # signal -> {accuracy, contribution}
    corrections: list[dict]                # Suggested adjustments
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return {
            "total_trades": self.total_trades,
            "win_rate": round(self.win_rate, 3),
            "avg_win": round(self.avg_win, 2),
            "avg_loss": round(self.avg_loss, 2),
            "profit_factor": round(self.profit_factor, 2),
            "regime_accuracy": self.regime_accuracy,
            "confidence_calibration": self.confidence_calibration,
            "signal_performance": self.signal_performance,
            "corrections": self.corrections,
            "timestamp": self.timestamp,
        }


class FeedbackLoop:
    """
    Closes the AGoT learning loop by analyzing trade outcomes.
    """

    def __init__(self, journal_db_path: str = "./data/journal.sqlite"):
        self.db_path = journal_db_path
        self._cache: dict[str, Any] = {}

    def _get_connection(self) -> sqlite3.Connection:
        """Get database connection."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def get_closed_trades(self, lookback_days: int = 90) -> list[dict]:
        """Fetch closed trades from journal database."""
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()

            # Try to get closed trades (trades with exit_date and pnl)
            cursor.execute("""
                SELECT * FROM trades 
                WHERE exit_date IS NOT NULL 
                AND exit_date >= ?
                ORDER BY exit_date DESC
            """, (cutoff,))

            trades = [dict(row) for row in cursor.fetchall()]
            conn.close()
            return trades

        except Exception as e:
            print(f"[FeedbackLoop] Failed to fetch trades: {e}")
            return []

    def analyze_outcomes(self, lookback_days: int = 30) -> FeedbackAnalysis:
        """
        Comprehensive analysis of trade outcomes.
        
        Returns performance metrics, regime accuracy, confidence calibration,
        and suggested corrections.
        """
        trades = self.get_closed_trades(lookback_days)

        if not trades:
            return self._empty_analysis()

        # Basic metrics
        wins = [t for t in trades if (t.get("pnl") or 0) > 0]
        losses = [t for t in trades if (t.get("pnl") or 0) <= 0]
        win_rate = len(wins) / len(trades) if trades else 0

        avg_win = sum(t.get("pnl", 0) for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t.get("pnl", 0) for t in losses) / len(losses) if losses else 0
        total_wins = sum(t.get("pnl", 0) for t in wins) if wins else 0
        total_losses = abs(sum(t.get("pnl", 0) for t in losses)) if losses else 0
        profit_factor = (
            round(total_wins / total_losses, 2)
            if total_losses > 0
            else (0.0 if total_wins == 0 else None)
        )

        # Regime accuracy
        regime_accuracy = self._analyze_regime_accuracy(trades)

        # Confidence calibration
        confidence_calibration = self._calibrate_confidence(trades)

        # Signal performance
        signal_performance = self._analyze_signals(trades)

        # Generate corrections
        corrections = self._generate_corrections(
            win_rate, regime_accuracy, confidence_calibration, signal_performance
        )

        return FeedbackAnalysis(
            total_trades=len(trades),
            win_rate=win_rate,
            avg_win=avg_win,
            avg_loss=avg_loss,
            profit_factor=profit_factor,
            regime_accuracy=regime_accuracy,
            confidence_calibration=confidence_calibration,
            signal_performance=signal_performance,
            corrections=corrections,
        )

    def _analyze_regime_accuracy(self, trades: list[dict]) -> dict[str, dict]:
        """Analyze win rate by regime classification at entry."""
        regime_stats: dict[str, dict] = {}

        for trade in trades:
            regime = trade.get("source_regime") or trade.get("regime_at_entry") or "UNKNOWN"
            pnl = trade.get("pnl", 0) or 0

            if regime not in regime_stats:
                regime_stats[regime] = {
                    "count": 0,
                    "wins": 0,
                    "total_pnl": 0.0,
                    "pnls": [],
                }

            regime_stats[regime]["count"] += 1
            regime_stats[regime]["total_pnl"] += pnl
            regime_stats[regime]["pnls"].append(pnl)
            if pnl > 0:
                regime_stats[regime]["wins"] += 1

        # Calculate derived metrics
        result = {}
        for regime, stats in regime_stats.items():
            count = stats["count"]
            if count < 3:
                continue  # Skip regimes with too few trades

            win_rate = stats["wins"] / count
            avg_pnl = stats["total_pnl"] / count

            result[regime] = {
                "win_rate": round(win_rate, 3),
                "count": count,
                "avg_pnl": round(avg_pnl, 2),
                "total_pnl": round(stats["total_pnl"], 2),
                "verdict": self._verdict_from_win_rate(win_rate),
            }

        return result

    def _calibrate_confidence(self, trades: list[dict]) -> dict[str, float]:
        """
        Check if confidence scores are calibrated.
        
        If trades with 70% confidence actually win 70% of the time,
        the model is well-calibrated. If they only win 50%, confidence
        is overestimated.
        """
        buckets = {
            "low_30-50": {"count": 0, "wins": 0},
            "med_50-70": {"count": 0, "wins": 0},
            "high_70-90": {"count": 0, "wins": 0},
        }

        for trade in trades:
            conf = trade.get("confidence") or trade.get("agot_confidence")
            if conf is None:
                continue

            conf = float(conf)
            pnl = trade.get("pnl", 0) or 0

            if 0.3 <= conf < 0.5:
                bucket = "low_30-50"
            elif 0.5 <= conf < 0.7:
                bucket = "med_50-70"
            elif conf >= 0.7:
                bucket = "high_70-90"
            else:
                continue

            buckets[bucket]["count"] += 1
            if pnl > 0:
                buckets[bucket]["wins"] += 1

        result = {}
        for name, stats in buckets.items():
            if stats["count"] >= 5:
                actual_rate = stats["wins"] / stats["count"]
                result[name] = round(actual_rate, 3)

        return result

    def _analyze_signals(self, trades: list[dict]) -> dict[str, dict]:
        """Analyze which signal combinations perform best."""
        signal_stats: dict[str, dict] = {}

        for trade in trades:
            # Analyze by bias at entry
            bias = trade.get("bias_at_entry") or trade.get("flow_bias") or "UNKNOWN"
            pnl = trade.get("pnl", 0) or 0
            key = f"bias_{bias}"

            if key not in signal_stats:
                signal_stats[key] = {"count": 0, "wins": 0, "total_pnl": 0.0}

            signal_stats[key]["count"] += 1
            signal_stats[key]["total_pnl"] += pnl
            if pnl > 0:
                signal_stats[key]["wins"] += 1

        result = {}
        for key, stats in signal_stats.items():
            if stats["count"] < 5:
                continue
            win_rate = stats["wins"] / stats["count"]
            result[key] = {
                "win_rate": round(win_rate, 3),
                "count": stats["count"],
                "total_pnl": round(stats["total_pnl"], 2),
                "verdict": self._verdict_from_win_rate(win_rate),
            }

        return result

    def _generate_corrections(
        self,
        overall_win_rate: float,
        regime_accuracy: dict,
        confidence_calibration: dict,
        signal_performance: dict,
    ) -> list[dict]:
        """Generate actionable corrections based on analysis."""
        corrections = []

        # Regime-based corrections
        for regime, stats in regime_accuracy.items():
            if stats["count"] < 5:
                continue
            wr = stats["win_rate"]
            if wr < 0.40:
                corrections.append({
                    "type": "REGIME_AVOID",
                    "target": regime,
                    "action": "BLOCK or REDUCE_SIZE",
                    "reason": f"{regime} trades have {wr:.0%} win rate (n={stats['count']})",
                    "priority": "HIGH",
                })
            elif wr < 0.50:
                corrections.append({
                    "type": "REGIME_DOWNGRADE",
                    "target": regime,
                    "action": "REDUCE_SIZE",
                    "reason": f"{regime} trades have {wr:.0%} win rate (n={stats['count']})",
                    "priority": "MEDIUM",
                })

        # Confidence calibration corrections
        for bucket, actual_rate in confidence_calibration.items():
            expected = {
                "low_30-50": 0.40,
                "med_50-70": 0.60,
                "high_70-90": 0.80,
            }.get(bucket, 0.5)

            if actual_rate < expected - 0.10:
                corrections.append({
                    "type": "CONFIDENCE_OVERESTIMATE",
                    "target": bucket,
                    "action": "ADJUST_THRESHOLD",
                    "reason": f"{bucket} confidence trades win {actual_rate:.0%} vs expected {expected:.0%}",
                    "priority": "MEDIUM",
                })

        # Signal-based corrections
        for signal, stats in signal_performance.items():
            if stats["count"] < 5:
                continue
            wr = stats["win_rate"]
            if wr < 0.35:
                corrections.append({
                    "type": "SIGNAL_BLOCK",
                    "target": signal,
                    "action": "BLOCK",
                    "reason": f"{signal} has {wr:.0%} win rate (n={stats['count']})",
                    "priority": "HIGH",
                })

        return corrections

    def get_regime_accuracy(self, regime: str, lookback_days: int = 60) -> float | None:
        """Get win rate for a specific regime."""
        analysis = self.analyze_outcomes(lookback_days)
        regime_stats = analysis.regime_accuracy.get(regime)
        if regime_stats:
            return regime_stats["win_rate"]
        return None

    def get_corrections(self, lookback_days: int = 30) -> list[dict]:
        """Get current corrections to apply to future decisions."""
        analysis = self.analyze_outcomes(lookback_days)
        return analysis.corrections

    def get_active_block_rules(self) -> list[dict]:
        """
        Get active BLOCK rules from the legacy learner module.
        Integrates with intelligence.learner for backward compatibility.
        """
        try:
            from intelligence.learner import analyze_history, build_correction_strings
            trades = self.get_closed_trades(30)
            result = analyze_history(trades, lookback_days=30)
            return result.get("rules", [])
        except Exception:
            return []

    def _verdict_from_win_rate(self, win_rate: float) -> str:
        """Convert win rate to a verdict label."""
        if win_rate >= 0.60:
            return "STRONG"
        elif win_rate >= 0.50:
            return "OK"
        elif win_rate >= 0.40:
            return "WEAK"
        else:
            return "AVOID"

    def _empty_analysis(self) -> FeedbackAnalysis:
        """Return an empty analysis when no data is available."""
        return FeedbackAnalysis(
            total_trades=0,
            win_rate=0.0,
            avg_win=0.0,
            avg_loss=0.0,
            profit_factor=0.0,
            regime_accuracy={},
            confidence_calibration={},
            signal_performance={},
            corrections=[],
        )


# --- Convenience function for integration ---

def get_agot_corrections(journal_db_path: str = "./data/journal.sqlite") -> list[dict]:
    """
    Quick function to get current AGoT corrections.
    
    Usage in main.py or dashboard:
        corrections = get_agot_corrections()
        if any(c["action"] == "BLOCK" for c in corrections):
            # Skip trades in blocked regimes
    """
    loop = FeedbackLoop(journal_db_path)
    return loop.get_corrections()
