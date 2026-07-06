with open("data/shoonya_fetcher.py", "r") as f:
    lines = f.readlines()
for i, line in enumerate(lines):
    if "def fetch_quote" in line:
        print(f"Line {i + 1}: {line.strip()}")
        break
