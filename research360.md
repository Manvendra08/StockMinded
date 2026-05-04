# Research360.in API Endpoints — Complete Discovery Report

**Generated:** April 29, 2026  
**Methodology:** Live network capture via URLScan.io browser automation

---

## Architecture Overview

Research360.in uses a **hybrid architecture**:

- **New Next.js App** (`/market/*`) → REST JSON APIs at `/api/market/`
- **Old PHP App** (`/future-and-options/*`) → AJAX PHP handlers at `/ajax/`
- **Hidden Backend** → `https://research360api.motilaloswal.com/api/getdata` (proxied via Next.js, blocked externally)
- **Static Assets CDN** → `d1tymi9mhi46bx.cloudfront.net` (old) and `/assets/_next/static/` (new)

---

## 1. 📊 Market Dashboard

**URL:** `/market/dashboard`  
**Platform:** Next.js  
**Total Requests:** 193 HTTP transactions

### API Endpoints

| Method | Endpoint | Size | Purpose |
|--------|----------|------|---------|
| GET | `/api/market/main-indices` | 3 KB | NIFTY50, NIFTYBANK, SENSEX live data |
| GET | `/api/market/indian-indices` | 16 KB | All Indian indices list (called 2×) |
| GET | `/api/market/world-indices` | 2 KB | Global indices (DJIA, S&P 500, FTSE) |
| GET | `/api/market/advance-decline` | 932 B | Advance/decline ratio data |
| GET | `/api/market/fii-dii/carousel` | 3 KB | FII/DII buy-sell carousel |
| GET | `/api/market/corporate-actions` | 5 KB | Dividends, bonus, board meetings |
| GET | `/api/market/sentiments` | 644 B | Market sentiment (Bullish/Neutral/Bearish %) |
| GET | `/api/market/index-chart` | 8–38 KB | Intraday chart data (called 3×) |
| GET | `/api/market/indian-indices/intraday-chart` | 4–22 KB | Index intraday charts (called 3×) |
| GET | `/api/market/moplay` | 2 KB | MoPlay videos list |
| GET | `/api/market/moplay/categories` | 492 B | MoPlay category filter |
| GET | `/api/superstar-investor` | 20 KB | Ace investors portfolio data |
| GET | `/api/ipo/cmots` | 60–849 B | IPO data (called 7× with params) |
| GET | `/api/products/ready-portfolio-iap` | 187 KB | IAP investment advisory products |
| GET | `/api/blogs/blog-categories` | 546 B | Blog category list |
| GET | `/api/blogs/blog-summary` | 127 KB | Blog articles summary |
| POST | `/api/alpha-picks/baskets` | 3 KB | Investment baskets data |
| POST | `/api/alpha-picks/research-reports` | 156 KB | Research reports listing |
| POST | `/api/research360/currency-news` | 6 KB | Currency/commodity news |
| POST | `/api/research360/getdata` | 78B–3KB | **Generic multi-purpose proxy** (4×) |

> 💡 `/api/research360/getdata` is a Next.js proxy that calls `https://research360api.motilaloswal.com/api/getdata`

---

## 2. 📈 F&O Overview

**URL:** `/future-and-options/overview`  
**Platform:** Legacy PHP  
**Total Requests:** 64 HTTP transactions

### API Endpoints

| Method | Endpoint | Size | Purpose |
|--------|----------|------|---------|
| GET | `/ajax/fno/graph.php` | 8 KB | F&O index graph data |
| POST | `/ajax/optionChainApi.php` | 183 KB | Option chain data |

---

## 3. 🏭 Sector Analysis

**URL:** `/market/sector-analysis`  
**Platform:** Next.js  
**Total Requests:** 117 HTTP transactions

### API Endpoints

| Method | Endpoint | Size | Purpose |
|--------|----------|------|---------|
| GET | `/api/market/sector-analysis/list` | 5 KB | Sector performance (Adv/Dec, % change, top movers) |

---

## 4. 🔗 Option Chain (Nifty 50)

**URL:** `/future-and-options/option-chain`  
**Platform:** Legacy PHP  
**Total Requests:** 52 HTTP transactions

### API Endpoints

| Method | Endpoint | Size | Purpose |
|--------|----------|------|---------|
| POST | `/fno/option/ajax/optionChainApi.php` | 183 KB | Full option chain (strikes, OI, volume, CE/PE) |

> 📝 `optionChainApi.php` accessible from both `/ajax/` and `/fno/option/ajax/` paths

---

## 5. 🌡️ Heatmap

**URL:** `/future-and-options/heatmap`  
**Platform:** Legacy PHP  
**Total Requests:** 62 HTTP transactions

### API Endpoints

| Method | Endpoint | Size | Purpose |
|--------|----------|------|---------|
| POST | `/ajax/heatmapAPIHandler.php` | 225 KB | F&O heatmap (price change % for all F&O stocks) |

---

## Summary Table

| Section | Page URL | API Base | Key Endpoints |
|---------|----------|----------|--------------|
| **Market Dashboard** | `/market/dashboard` | `/api/market/` | main-indices, indian-indices, world-indices, advance-decline, fii-dii/carousel, sentiments, index-chart, corporate-actions |
| **F&O Overview** | `/future-and-options/overview` | `/ajax/fno/` | graph.php, optionChainApi.php |
| **Sector Analysis** | `/market/sector-analysis` | `/api/market/sector-analysis/` | list |
| **Option Chain** | `/future-and-options/option-chain` | `/fno/option/ajax/` | optionChainApi.php |
| **Heatmap** | `/future-and-options/heatmap` | `/ajax/` | heatmapAPIHandler.php |

---

## Important Notes

### Authentication
- Most `/api/*` endpoints require **session cookies or Bearer tokens** from Research360 login
- Direct access without auth returns errors or empty data

### Data Formats
- **Next.js endpoints** (`/api/*`): Return `application/json`
- **PHP endpoints** (`/ajax/*`): Return `text/html` (HTML-wrapped JSON or plain JSON)
- POST endpoints likely require form params: `symbol`, `expiry`, `exchange`

### Backend Infrastructure
- **Real backend URL:** `https://research360api.motilaloswal.com/api/getdata`
  - Proxied by Next.js app via `/api/research360/getdata`
  - Not directly accessible without MOFSL auth tokens
  - Serves as the actual data source behind the Next.js proxy layer

### Static Assets
- **Old site CDN:** `https://d1tymi9mhi46bx.cloudfront.net/Assets/uat/dist/`
- **New site assets:** `https://www.research360.in/assets/_next/static/media/`

---

## Base URLs
