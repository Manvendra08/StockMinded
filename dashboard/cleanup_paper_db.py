r"""Database Cleanup Script for StockMinded.

Removes legacy 'strategy_notes' and 'learned_filters' keys from paper_trades.json.
Run this from the project root.
"""
import json
import os
from pathlib import Path

DB_PATH = Path("dashboard/paper_trades.json")

def run_migration():
    if not DB_PATH.exists():
        print(f"Error: Paper trades database not found at {DB_PATH}")
        return

    print(f"Analyzing database: {DB_PATH}...")
    
    try:
        with open(DB_PATH, "r", encoding="utf-8") as f:
            db = json.load(f)
    except Exception as e:
        print(f"Failed to load JSON: {e}")
        return

    legacy_keys = ["strategy_notes", "learned_filters"]
    found_keys = [k for k in legacy_keys if k in db]

    if not found_keys:
        print("Database is already clean. No legacy keys found.")
        return

    print(f"Found legacy keys: {found_keys}")
    
    # Create safety backup
    backup_path = DB_PATH.with_suffix(".json.mig_bak")
    with open(backup_path, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2)
    print(f"Backup created at {backup_path}")

    for k in found_keys:
        del db[k]

    with open(DB_PATH, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2)
    
    print("Success: Legacy keys removed from paper_trades.json.")

if __name__ == "__main__":
    run_migration()