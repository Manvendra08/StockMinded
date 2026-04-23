"""Hard reset paper_trades.json directly."""
import json
from pathlib import Path

DB = Path(r'c:\Users\manve\Downloads\StockMinded\dashboard\paper_trades.json')

db = json.loads(DB.read_text(encoding='utf-8'))

# Keep ONLY the legitimately closed trades (ID 1-5 originally)
real_trades = [t for t in db.get('trades', []) if t.get('status') == 'CLOSED' and (t.get('pnl') or 0) != 0]

db['trades'] = real_trades
total_pnl = sum(t.get('pnl', 0) for t in real_trades)
db['cumulative_pnl'] = round(total_pnl, 2)

for s in db.get('daily_summaries', []):
    s['trades'] = [t for t in real_trades if t.get('entry_date') == s.get('date')]
    s['total_trades'] = len(s['trades'])
    s['total_pnl'] = round(sum(t.get('pnl', 0) for t in s['trades']), 2)

DB.write_text(json.dumps(db, indent=2, default=str), encoding='utf-8')
print("Deleted lingering open spam trades. DB is now 100% clean.")
