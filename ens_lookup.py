"""
ENS reverse-resolution for top ETH funnel candidates.

Uses web3.py + a public Ethereum RPC (configured in .env via ETH_RPC_URLS).
No BigQuery token required.
"""
from __future__ import annotations
from pathlib import Path
import pandas as pd

import config

ROOT = Path(__file__).parent
OUT = ROOT / "outputs"
OUT.mkdir(exist_ok=True)

INPUT_CSV = ROOT / "usdt_funnel_candidates.csv"
if not INPUT_CSV.exists():
    raise SystemExit(f"{INPUT_CSV.name} not found - run the notebook first.")

candidates = pd.read_csv(INPUT_CSV)
top100 = candidates[candidates.chain == "ethereum"].nlargest(100, "in_usdt")["wallet"].tolist()
print(f"Resolving ENS for top {len(top100)} ETH candidates by inflow...")

try:
    from web3 import Web3
    from ens import ENS
except ImportError:
    raise SystemExit("web3.py not installed - run: pip install web3")

w3 = None
for url in config.ETH_RPC_URLS:
    try:
        cand = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 15}))
        if cand.is_connected():
            w3 = cand
            print(f"  Connected via {url}. Chain ID: {w3.eth.chain_id}")
            break
        print(f"  [skip] {url} did not connect")
    except Exception as e:
        print(f"  [skip] {url}: {type(e).__name__}: {e}")

if w3 is None:
    raise SystemExit("[ABORT] No public ETH RPC reachable")

ns = ENS.from_web3(w3)
found = []
for i, addr in enumerate(top100, 1):
    try:
        cs = Web3.to_checksum_address(addr)
        name = ns.name(cs)
        if name:
            found.append({"wallet": addr, "ens_name": name})
            print(f"  [{i:3d}/{len(top100)}] {addr} -> {name}")
        elif i % 10 == 0:
            print(f"  [{i:3d}/{len(top100)}] ...")
    except Exception as e:
        if i % 10 == 0:
            print(f"  [{i:3d}/{len(top100)}] (err: {type(e).__name__})")

ens_df = pd.DataFrame(found)
ens_df.to_csv(OUT / "ens_names.csv", index=False)
print(f"\nResolved {len(found)} ENS names out of {len(top100)} candidates.")
if len(ens_df):
    print(ens_df.to_string(index=False))
