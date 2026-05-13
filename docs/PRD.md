# Product Requirements Document (PRD) - StockMinded

## 1. Executive Summary
**StockMinded** is a high-fidelity decision engine designed for the Indian equity and derivatives markets. It automates the morning pre-market routine by synthesizing market regime, capital flows, leadership strength, and structural trade plans into actionable intelligence delivered before 09:15 IST.

## 2. Target Audience
- Professional Indian retail traders.
- Quantitative analysts focusing on F&O (Futures & Options).
- Systematic traders requiring strict risk management.

## 3. Core Objectives
- **Decision Support**: Automate the answer to "What is the market state today?"
- **Risk First**: Enforce non-negotiable capital preservation rules.
- **Signal Confluence**: Combine macro flows with micro-leadership (Relative Strength).
- **Automation (Phase 2)**: Provide a robust paper-trading environment to validate strategies before live deployment.

## 4. Key Features
### 4.1 Market Intelligence (The 4 Questions)
1. **Regime**: 6-state classification (Trend Up/Down, Range High/Low Vol, Vol Expansion/Contraction).
2. **Flows**: Institutional (FII/DII) tracking, Sector Rotation, and Option Chain Sentiment (PCR/Max Pain).
3. **Leadership**: RS-line quintile ranking of the F&O universe (Top 200 stocks).
4. **Structure**: Pre-defined trade plans based on the day's regime.

### 4.2 Risk Management Guardrails
- **Hard Stops**: Daily (-2%) and Monthly (-6%) drawdown protection.
- **Sizing**: Kelly-fraction based sizing with correlation caps (0.70).
- **Execution**: Margin utilization limits and concurrent trade caps.

### 4.3 Delivery & Monitoring
- **Telegram Alerts**: Richly formatted markdown reports sent to private channels.
- **Interactive Dashboard**: Real-time Flask-based web UI for signal visualization and trade monitoring.

## 5. Roadmap
- **Phase 1 (Completed)**: Core signal engine, Telegram integration, and SQLite logging.
- **Phase 2 (In-Progress)**: Options automation, black-scholes delta calculation, and paper trading simulator.
- **Phase 3 (Future)**: Live Broker integration (Kite/Dhan/Fyers).
- **Phase 4 (Future)**: Advanced portfolio optimization and multi-asset support.
