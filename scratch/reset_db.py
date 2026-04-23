"""Reset paper_trades.json to only the 5 legitimate trades from today."""
import json
from pathlib import Path

DB = Path(r'c:\Users\manve\Downloads\StockMinded\dashboard\paper_trades.json')

db = json.loads(DB.read_text(encoding='utf-8'))

all_trades = db.get('trades', [])
print(f"Total trades before cleanup: {len(all_trades)}")

# Keep only trades with non-zero PNL (real trades that moved)
real_trades = [t for t in all_trades if t.get('pnl') not in (0, 0.0, None) or t.get('status') == 'OPEN']
print(f"Real trades kept: {len(real_trades)}")

# Show what's kept
for t in real_trades:
    print(f"  ID {t['id']:3d} | {t['symbol']:12} | {t['direction']:5} | PNL={t.get('pnl')} | {t['status']} | {t.get('entry_time','')}")

# Also clean daily summaries to reflect accurate counts
# Rebuild cumulative PNL from real trades only
total_pnl = sum(t.get('pnl', 0) or 0 for t in real_trades if t.get('status') == 'CLOSED')

db['trades'] = real_trades
db['cumulative_pnl'] = round(total_pnl, 2)

# Fix daily summary trade lists
for s in db.get('daily_summaries', []):
    date_str = s.get('date', '')
    day_trades = [t for t in real_trades if t.get('entry_date') == date_str]
    s['trades'] = day_trades
    s['total_pnl'] = round(sum(t.get('pnl', 0) or 0 for t in day_trades), 2)
    s['total_trades'] = len(day_trades)
    winners = [t for t in day_trades if (t.get('pnl') or 0) > 0]
    losers = [t for t in day_trades if (t.get('pnl') or 0) < 0]
    s['winners'] = len(winners)
    s['losers'] = len(losers)
    s['win_rate'] = round(len(winners) / len(day_trades) * 100, 1) if day_trades else 0

DB.write_text(json.dumps(db, indent=2, default=str), encoding='utf-8')
print(f"\nDB saved. Cumulative PNL: Rs {total_pnl:,.2f}")
print(f"Daily summaries fixed: {len(db.get('daily_summaries', []))}")
