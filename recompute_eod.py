import json
from pathlib import Path
from datetime import datetime

DATA_FILE = Path(__file__).parent / 'dashboard' / 'paper_trades.json'

def load_db():
    return json.loads(DATA_FILE.read_text(encoding='utf-8'))

def save_db(db):
    DATA_FILE.write_text(json.dumps(db, indent=2, default=str), encoding='utf-8')

def normalize_option_trade(t):
    exit_reason = t.get('exit_reason') or ('CLOSED' if t.get('status')=='CLOSED' else 'OPEN')
    pnl = t.get('pnl')
    # Skip invalid synthetic closures
    if exit_reason == 'INVALID_ZERO_PREMIUM' and (pnl is None or pnl == 0):
        return None
    return {
        'id': t.get('id'),
        'symbol': t.get('symbol'),
        'direction': 'LONG' if (t.get('net_premium') or 0) < 0 else 'SHORT',
        'entry_price': t.get('net_premium'),
        'exit_price': t.get('exit_premium'),
        'qty': sum((leg.get('qty') or 1) for leg in t.get('legs', [])),
        'pnl': t.get('pnl'),
        'status': t.get('status'),
        'exit_reason': exit_reason,
        'entry_date': t.get('entry_date'),
        'source': 'json_options'
    }

def normalize_trade(t):
    return {
        'id': t.get('id'),
        'symbol': t.get('symbol'),
        'direction': t.get('direction','LONG'),
        'entry_price': t.get('entry_price'),
        'exit_price': t.get('exit_price'),
        'qty': t.get('qty'),
        'pnl': t.get('pnl'),
        'status': t.get('status'),
        'exit_reason': t.get('exit_reason') or ('CLOSED' if t.get('status')=='CLOSED' else 'OPEN'),
        'entry_date': t.get('entry_date'),
        'source': 'json_trades'
    }

def calc_summary_for_day(all_trades, day):
    day_trades = [t for t in all_trades if t.get('entry_date') == day]
    closed_today = [t for t in day_trades if t.get('status') == 'CLOSED']
    winners = [t for t in closed_today if (t.get('pnl') or 0) > 0]
    losers = [t for t in closed_today if (t.get('pnl') or 0) < 0]
    total_pnl = sum((t.get('pnl') or 0) for t in closed_today)
    sl_hits = len([t for t in closed_today if t.get('exit_reason') == 'SL_HIT'])
    target_hits = len([t for t in closed_today if t.get('exit_reason') == 'TARGET_HIT'])
    eod_exits = len([t for t in closed_today if t.get('exit_reason') == 'EOD_CLOSE'])
    win_rate = round((len(winners) / max(len(closed_today), 1)) * 100, 1)
    return {
        'date': day,
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S IST'),
        'total_trades': len(day_trades),
        'closed': len(closed_today),
        'winners': len(winners),
        'losers': len(losers),
        'win_rate': win_rate,
        'sl_hits': sl_hits,
        'target_hits': target_hits,
        'eod_exits': eod_exits,
        'total_pnl': round(total_pnl,2),
        'trades': closed_today,
        'cumulative_pnl': 0.0,
        'analysis': {'what_went_right': [], 'what_went_wrong': [], 'patterns': []},
        'corrections': ['No trades to analyze. Consider lowering confidence threshold if signals were present but not taken.'] if not closed_today else []
    }

if __name__ == '__main__':
    db = load_db()
    all_trades = []
    for t in db.get('trades', []):
        all_trades.append(normalize_trade(t))
    for t in db.get('option_trades', []):
        nt = normalize_option_trade(t)
        if nt: all_trades.append(nt)

    dates = set(t.get('entry_date') for t in all_trades if t.get('entry_date'))
    # Ensure today is present
    from datetime import date
    dates.add(date.today().isoformat())

    # Recompute summaries for all dates in sorted order
    existing = {s['date']: s for s in db.get('daily_summaries', [])}
    new_summaries = []
    for d in sorted(dates):
        summary = calc_summary_for_day(all_trades, d)
        new_summaries.append(summary)
        print(f"Regenerated {d} -> total_trades: {summary['total_trades']} | closed: {summary['closed']} | winners: {summary['winners']} | total_pnl: {summary['total_pnl']}")

    # Sort and compute cumulative
    new_summaries.sort(key=lambda x: x['date'])
    running = 0.0
    for s in new_summaries:
        running += s['total_pnl']
        s['cumulative_pnl'] = round(running,2)

    db['daily_summaries'] = new_summaries
    db['cumulative_pnl'] = round(running,2)
    save_db(db)
    print('Recompute finished and written to', DATA_FILE)
