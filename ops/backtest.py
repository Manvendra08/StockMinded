"""Backtest harness for Phase 2 timing analysis: correlate entry_quality with PnL and suggest threshold adjustments."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd


@dataclass
class TradeEntry:
    """Single trade record from journal with timing metrics."""

    id: int
    symbol: str
    side: str
    entry: float
    exit_price: Optional[float]
    pnl_rupees: Optional[float]
    entry_quality: Optional[str]
    loss_root_cause: Optional[str]
    timing_snapshot: dict
    opened_at: str
    closed_at: Optional[str]
    regime: Optional[str]
    source_regime: Optional[str]


class TimingBacktester:
    """Analyze timing gate effectiveness and suggest threshold adjustments."""

    def __init__(self, journal_db: str, output_dir: str = "./data/backtest"):
        """Initialize backtest harness.

        Args:
            journal_db: Path to SQLite journal database (from config.paths.journal_db)
            output_dir: Directory for backtest reports
        """
        self.db_path = journal_db
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def load_trades_with_timing(self, since_date: Optional[str] = None) -> pd.DataFrame:
        """Load trades from journal with timing annotations.

        Args:
            since_date: ISO date string (e.g. '2024-06-01'); if None, load all

        Returns:
            DataFrame with columns: symbol, side, entry_quality, pnl_rupees,
                                   loss_root_cause, timing_snapshot, regime, etc.
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row

        query = """
        SELECT
            id, symbol, side, entry, exit_price, pnl_rupees,
            entry_quality, loss_root_cause, timing_snapshot,
            opened_at, closed_at, regime, source_regime,
            planned_risk, entry_rule
        FROM trades
        WHERE entry_quality IS NOT NULL
        """

        # BUG-08 FIX: Use parameterized query to prevent SQL injection.
        params: list = []
        if since_date:
            query += " AND opened_at >= ?"
            params.append(since_date)

        query += " ORDER BY opened_at DESC"

        df = pd.read_sql_query(query, conn, params=params)
        conn.close()

        # Parse timing_snapshot JSON
        if not df.empty and "timing_snapshot" in df.columns:
            df["timing_snapshot"] = df["timing_snapshot"].apply(
                lambda x: json.loads(x) if isinstance(x, str) else x or {}
            )
        else:
            df["timing_snapshot"] = [{}] * len(df)

        return df

    def analyze_entry_quality_performance(self) -> dict:
        """Correlate entry_quality with PnL outcomes.

        Returns:
            {
                "GOOD": {"win_rate": 0.72, "avg_pnl": 1.2, "count": 15, "median_pnl": 0.8},
                "LATE": {"win_rate": 0.45, "avg_pnl": -0.8, "count": 5, "median_pnl": -1.2},
                ...
                "correlation_r2": 0.68,
                "total_trades": 25,
                "total_win_rate": 0.64
            }
        """
        df = self.load_trades_with_timing()

        if df.empty:
            return {
                "error": "No trades with entry_quality found",
                "total_trades": 0,
            }

        results = {
            "total_trades": len(df),
            "total_win_rate": (df["pnl_rupees"] > 0).sum() / len(df),
        }

        # Group by entry_quality
        for quality in df["entry_quality"].unique():
            if pd.isna(quality):
                continue

            subset = df[df["entry_quality"] == quality]
            pnls = subset["pnl_rupees"].dropna()

            if len(pnls) > 0:
                results[quality] = {
                    "win_rate": round((pnls > 0).sum() / len(pnls), 3),
                    "avg_pnl": round(pnls.mean(), 2),
                    "median_pnl": round(pnls.median(), 2),
                    "std_pnl": round(pnls.std(), 2),
                    "min_pnl": round(pnls.min(), 2),
                    "max_pnl": round(pnls.max(), 2),
                    "count": len(pnls),
                }

        # Compute correlation between quality and PnL (simple: GOOD=1, LATE=0, MID=0.5)
        quality_map = {"GOOD": 1.0, "MID": 0.5, "LATE": 0.0, "EXHAUSTED": -1.0}
        df["quality_score"] = df["entry_quality"].map(quality_map)

        # BUG-30 FIX: Require minimum 10 data points before computing
        # correlation. Correlation with 3-9 points is statistically
        # meaningless and produces misleading r/r² values.
        valid_idx = df["quality_score"].notna() & df["pnl_rupees"].notna()
        if valid_idx.sum() >= 10:
            correlation = df.loc[valid_idx, "quality_score"].corr(
                df.loc[valid_idx, "pnl_rupees"]
            )
            results["correlation_r"] = round(correlation, 3)
            results["correlation_r2"] = round(correlation**2, 3)

        return results

    def suggest_threshold_adjustments(self) -> dict:
        """Suggest timing threshold adjustments based on performance.

        Analyzes which timing checks are correlating with losses and suggests
        tighter/looser thresholds.

        Returns:
            {
                "max_vwap_dist_pct": {
                    "current": 1.2,
                    "suggested": 0.9,
                    "reason": "LATE entries have 2.1% higher loss rate; tighten VWAP",
                    "confidence": 0.75,
                    "supporting_data": {
                        "late_loss_rate": 0.55,
                        "good_loss_rate": 0.28,
                        "delta": -0.27
                    }
                },
                ...
                "analysis_summary": "..."
            }
        """
        df = self.load_trades_with_timing()

        if df.empty or len(df) < 10:
            return {
                "error": "Insufficient trades for analysis",
                "min_required": 10,
                "available": len(df),
            }

        adjustments = {}

        # 1. If LATE entries show significantly worse win rate, tighten VWAP
        quality_perf = self.analyze_entry_quality_performance()
        late_perf = quality_perf.get("LATE", {})
        good_perf = quality_perf.get("GOOD", {})

        if late_perf and good_perf:
            late_loss_rate = 1.0 - late_perf.get("win_rate", 0.5)
            good_loss_rate = 1.0 - good_perf.get("win_rate", 0.5)
            loss_delta = late_loss_rate - good_loss_rate

            if loss_delta > 0.15:  # >15% worse loss rate
                adjustments["max_vwap_dist_pct"] = {
                    "current": 1.2,
                    "suggested": 0.85,
                    "reason": f"LATE entries lose {loss_delta:.1%} more often; tighten VWAP proximity",
                    "confidence": min(0.95, 0.6 + (loss_delta / 0.3)),
                    "supporting_data": {
                        "late_loss_rate": round(late_loss_rate, 3),
                        "good_loss_rate": round(good_loss_rate, 3),
                        "delta": round(loss_delta, 3),
                        "late_count": late_perf.get("count", 0),
                        "good_count": good_perf.get("count", 0),
                    },
                }

        # 2. If EXHAUSTED entries exist, flag threshold is working
        exhausted_perf = quality_perf.get("EXHAUSTED", {})
        if exhausted_perf and exhausted_perf.get("count", 0) > 3:
            adjustments["rsi_exhaustion_threshold"] = {
                "current": 70,
                "status": "working",
                "reason": f"RSI >70 filter identified {exhausted_perf['count']} exhausted entries",
                "confidence": 0.8,
                "supporting_data": {
                    "exhausted_count": exhausted_perf["count"],
                    "exhausted_win_rate": exhausted_perf.get("win_rate", 0.0),
                    "avg_pnl": exhausted_perf.get("avg_pnl", 0.0),
                },
            }

        # 3. Regime-based suggestions
        df["pnl_positive"] = df["pnl_rupees"] > 0
        for regime in df["source_regime"].unique():
            if pd.isna(regime):
                continue

            regime_trades = df[df["source_regime"] == regime]
            if len(regime_trades) > 5:
                regime_wr = regime_trades["pnl_positive"].mean()

                # If uptrend trades underperform, suggest tighter thresholds
                if "TREND_UP" in str(regime) and regime_wr < 0.55:
                    adjustments[f"regime_{regime}_adjustment"] = {
                        "current": "relaxed",
                        "suggested": "normal",
                        "reason": f"{regime} trades underperforming ({regime_wr:.1%} WR); revert threshold multipliers",
                        "confidence": 0.65,
                        "supporting_data": {
                            "regime_win_rate": round(regime_wr, 3),
                            "trade_count": len(regime_trades),
                        },
                    }

        summary_parts = []
        if adjustments:
            summary_parts.append(
                f"Identified {len(adjustments)} potential adjustments based on {len(df)} trades"
            )
        else:
            summary_parts.append(
                f"Current thresholds performing well ({len(df)} trades analyzed)"
            )

        # Add correlation insight
        if "correlation_r2" in quality_perf:
            r2 = quality_perf["correlation_r2"]
            summary_parts.append(f"Entry quality explains {r2:.1%} of PnL variance")

        adjustments["analysis_summary"] = "; ".join(summary_parts)
        adjustments["timestamp"] = datetime.now().isoformat()
        adjustments["trades_analyzed"] = len(df)

        return adjustments

    def export_report(self, report_type: str = "full") -> Path:
        """Export backtest report to JSON file.

        Args:
            report_type: 'full' (both analyses), 'performance', or 'suggestions'

        Returns:
            Path to generated report file
        """
        report = {}

        if report_type in ("full", "performance"):
            report["performance_analysis"] = self.analyze_entry_quality_performance()

        if report_type in ("full", "suggestions"):
            report["threshold_suggestions"] = self.suggest_threshold_adjustments()

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = self.output_dir / f"backtest_report_{timestamp}.json"

        with open(report_path, "w") as f:
            json.dump(report, f, indent=2)

        return report_path


def main():
    """CLI entry point: generate backtest report."""
    from config.loader import load_config

    cfg = load_config()
    db_path = cfg.get("paths", {}).get("journal_db", "data/trades.db")
    output_dir = (
        cfg.get("timing_engine", {})
        .get("backtest", {})
        .get("output_dir", "data/backtest")
    )

    backtest = TimingBacktester(db_path, output_dir)

    # Generate full report
    report_path = backtest.export_report("full")
    print(f"✓ Backtest report saved: {report_path}")

    # Print summary
    perf = backtest.analyze_entry_quality_performance()
    print(f"\n📊 Performance Summary:")
    print(f"  Total trades: {perf.get('total_trades')}")
    print(f"  Overall win rate: {perf.get('total_win_rate', 0):.1%}")

    if "correlation_r2" in perf:
        print(f"  Entry quality R²: {perf['correlation_r2']:.3f}")

    for quality in ["GOOD", "MID", "LATE", "EXHAUSTED"]:
        if quality in perf:
            stats = perf[quality]
            print(
                f"  {quality}: WR {stats['win_rate']:.1%}, avg PnL {stats['avg_pnl']:+.1f}, n={stats['count']}"
            )

    suggestions = backtest.suggest_threshold_adjustments()
    if suggestions.get("error"):
        print(f"\n⚠️  {suggestions['error']}")
    else:
        print(f"\n💡 Threshold Suggestions:")
        for key, val in suggestions.items():
            if key in ("timestamp", "analysis_summary", "trades_analyzed"):
                continue
            if isinstance(val, dict) and "suggested" in val:
                print(
                    f"  {key}: {val.get('current')} → {val.get('suggested')} ({val.get('reason', '')})"
                )

        print(f"\n📝 {suggestions.get('analysis_summary', '')}")


if __name__ == "__main__":
    main()
