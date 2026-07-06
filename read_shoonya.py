with open(r"C:\Users\manve\Downloads\StockMinded\data\shoonya_fetcher.py", "r") as f:
    lines = f.readlines()

# Print first 80 lines
for i, line in enumerate(lines[:80]):
    print(f"{i + 1:4d}: {line.rstrip()}")

print(f"\n... Total lines: {len(lines)}")
