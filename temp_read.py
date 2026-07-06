with open("data/shoonya_fetcher.py", "r") as f:
    lines = f.readlines()
for i, line in enumerate(lines[689:750], 690):
    print(f"{i}: {line}", end="")
