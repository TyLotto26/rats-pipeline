#!/usr/bin/env python3
"""
position_tracker.py — Track open positions and available bankroll from paper-trades.log.

Usage:
    python3 position_tracker.py                    -> print summary
    python3 position_tracker.py open               -> print open positions by token_id
    python3 position_tracker.py bankroll           -> print available bankroll
    python3 position_tracker.py is_open <token_id> -> print "true"/"false"
"""
import json
import os
import sys
from datetime import datetime, timezone

TRADES_LOG = os.path.expanduser("~/paper-trades.log")
INITIAL_BANKROLL = 48.85  # Starting bankroll — updates from pipeline-state when available


def load_trades():
    """Load all paper trades from log. Returns list of dicts."""
    trades = []
    if not os.path.exists(TRADES_LOG):
        return trades
    with open(TRADES_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            # Try JSON parse first (standard polyexec format)
            try:
                t = json.loads(line)
                trades.append(t)
                continue
            except json.JSONDecodeError:
                pass
            # Try pipe-delimited format (legacy cycle-runner format)
            try:
                # Format: "2026-06-13T22:13:37Z | PAPER | market=slug | dir=YES | ..."
                parts = [p.strip() for p in line.split("|")]
                t = {"timestamp": parts[0] if len(parts) > 0 else "",
                     "mode": "paper"}
                for part in parts:
                    if "market=" in part:
                        t["token_id"] = part.split("market=", 1)[1].strip()
                    if "dir=" in part:
                        t["side"] = part.split("dir=", 1)[1].strip()
                    if "size=" in part:
                        try:
                            t["usd_exposure"] = float(part.split("size=", 1)[1].strip().replace("$", ""))
                        except:
                            pass
                    if "entry=" in part:
                        try:
                            t["price_target"] = float(part.split("entry=", 1)[1].strip().replace("$", ""))
                        except:
                            pass
                t["status"] = "PAPER_LOGGED"
                trades.append(t)
            except Exception:
                pass
    return trades


def get_open_positions():
    """Return dict of token_id -> trade for open (unsettled) positions.
    
    A position is "open" if it has no exit_price and no resolved status.
    We consider any JSON trade with status=PAPER_LOGGED as open.
    """
    trades = load_trades()
    open_positions = {}
    
    for t in trades:
        token_id = t.get("token_id") or t.get("slug") or ""
        status = t.get("status", "")
        
        # Skip settled/resolved trades
        if status in ("SETTLED", "RESOLVED", "CANCELLED", "EXPIRED", "CLOSED"):
            continue
        
        # Has an exit_price -> closed
        if t.get("exit_price") is not None and t.get("exit_price", "") != "":
            continue
        
        if token_id and status == "PAPER_LOGGED":
            # Keep the most recent entry for a given token_id
            existing = open_positions.get(token_id)
            if existing is None:
                open_positions[token_id] = t
            else:
                # Prefer later timestamp
                ts_new = t.get("timestamp", "")
                ts_old = existing.get("timestamp", "")
                if ts_new > ts_old:
                    open_positions[token_id] = t
    
    return open_positions


def get_available_bankroll(initial=None):
    """Compute available bankroll = initial - deployed capital."""
    if initial is None:
        initial = INITIAL_BANKROLL
    
    open_positions = get_open_positions()
    deployed = sum(
        float(t.get("usd_exposure", 0) or 0)
        for t in open_positions.values()
    )
    
    available = max(0.0, initial - deployed)
    return {
        "initial": initial,
        "deployed": round(deployed, 2),
        "available": round(available, 2),
        "open_positions": len(open_positions),
    }


def get_open_token_ids():
    """Return set of token_ids with open positions."""
    return set(get_open_positions().keys())


def is_open(token_id):
    """Check if a token_id already has an open position."""
    open_ids = get_open_token_ids()
    # Check both raw token_id and possible slug versions
    if token_id in open_ids:
        return True
    # Also check if any open position has this as in their token_id or slug
    for t in get_open_positions().values():
        tid = t.get("token_id", "") or ""
        slug = t.get("slug", "") or ""
        q = t.get("question", "") or ""
        if token_id in (tid, slug, q):
            return True
    return False


def cmd_summary():
    bankroll = get_available_bankroll()
    open_positions = get_open_positions()
    print(f"📊 POSITION TRACKER")
    print(f"===================")
    print(f"Initial bankroll:  ${bankroll['initial']:.2f}")
    print(f"Deployed capital:  ${bankroll['deployed']:.2f}")
    print(f"Available bankroll: ${bankroll['available']:.2f}")
    print(f"Open positions:    {bankroll['open_positions']}")
    print()
    if open_positions:
        print("Open Positions:")
        for token_id, t in sorted(open_positions.items()):
            q = t.get("question", t.get("token_id", token_id))[:45]
            side = t.get("side", "?")
            exposure = t.get("usd_exposure", 0)
            entry = t.get("price_target", t.get("entry_price", "?"))
            print(f"  • {q}")
            print(f"    token_id={token_id[:16]}... | {side} @ ${entry} | ${exposure:.2f}")
    else:
        print("No open positions.")


def main():
    if len(sys.argv) < 2:
        cmd_summary()
        return
    
    cmd = sys.argv[1]
    
    if cmd == "open":
        pos = get_open_positions()
        for token_id, t in sorted(pos.items()):
            print(f"{token_id}")
    
    elif cmd == "bankroll":
        br = get_available_bankroll()
        print(br["available"])
    
    elif cmd == "is_open":
        if len(sys.argv) < 3:
            print("Usage: position_tracker.py is_open <token_id>")
            return
        print("true" if is_open(sys.argv[2]) else "false")
    
    elif cmd == "deployed":
        br = get_available_bankroll()
        print(br["deployed"])
    
    elif cmd == "count":
        print(len(get_open_positions()))
    
    elif cmd == "trades":
        trades = load_trades()
        print(f"Total trades in log: {len(trades)}")
        open_pos = get_open_positions()
        print(f"Open positions: {len(open_pos)}")
    
    else:
        cmd_summary()


if __name__ == "__main__":
    main()
