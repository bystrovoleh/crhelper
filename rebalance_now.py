"""
Run rebalance_orders() standalone — same logic as the end-of-scan step in /analyze_all.
Uses the same ASSETS list from config/settings.py.

Usage:
    python rebalance_now.py           # live
    python rebalance_now.py --dry-run # preview only, no orders touched
"""

import sys
from config.settings import ASSETS
from trading.order_manager import rebalance_orders

dry_run = "--dry-run" in sys.argv

print(f"{'[DRY RUN] ' if dry_run else ''}Rebalancing {len(ASSETS)} assets...\n")

result = rebalance_orders(list(ASSETS), dry_run=dry_run)

print()
print(f"target_margin : {result['target_margin']:.2f} USDT per symbol")
print(f"rebalanced    : {len(result['rebalanced'])}")
print(f"skipped       : {len(result['skipped'])}")
print(f"cancelled     : {len(result['cancelled'])}")
print(f"errors        : {len(result['errors'])}")

if result["rebalanced"]:
    print("\nRebalanced:")
    for r in result["rebalanced"]:
        dry = " [dry]" if r.get("dry_run") else ""
        print(f"  {r['symbol']:12s} @ {r['price']}  vol {r['old_vol']} → {r['new_vol']}{dry}")

if result["cancelled"]:
    print("\nCancelled (vol too small):")
    for c in result["cancelled"]:
        print(f"  {c['symbol']:12s}  order {c.get('order_id')}  reason: {c['reason']}")

if result["errors"]:
    print("\nErrors:")
    for e in result["errors"]:
        print(f"  ❌ {e}")
