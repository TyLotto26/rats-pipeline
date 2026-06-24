#!/usr/bin/env python3
"""ledger-migrate.py — Split trading-state.json into ledger.json (append-only PnL)
+ trading-state.json (current positions only). Run once to bootstrap, then
cycle-runner appends to ledger.json for every trade lifecycle event.

Usage: python3 ledger-migrate.py
"""
import json
import os
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
TRADES_LOG = os.path.join(HOME, "paper-trades.log")
LEDGER = os.path.join(HOME, "ledger.json")
TRADING_STATE = os.path.join(HOME, "trading-state.json")
PIPELINE_STATE = os.path.join(HOME, "pipeline-state.json")

def load_paper_trades():
    trades = []
    if not os.path.exists(TRADES_LOG):
        return trades
    with open(TRADES_LOG) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    trades.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return trades

def build_ledger(trades):
    events = []
    for t in trades:
        ts = t.get("timestamp", "")
        q = t.get("question", "")[:60]
        slug = t.get("slug", "")
        token_id = t.get("token_id", "")
        side = t.get("side", "YES")
        direction = side[4:] if side.upper().startswith("BUY ") else side
        entry_price = t.get("price_target", 0)
        size = t.get("usd_exposure", 0)
        cycle = t.get("cycle", 0)
        edge = t.get("edge", 0)
        status = t.get("status", "PAPER_LOGGED")
        strategy = t.get("strategy_id", "general-quant")

        # Entry event
        events.append({
            "type": "ENTRY",
            "timestamp": ts,
            "cycle": cycle,
            "token_id": token_id,
            "slug": slug,
            "question": q,
            "side": direction,
            "entry_price": entry_price,
            "size_usd": size,
            "edge_pct": edge,
            "strategy_id": strategy,
            "status": status,
        })

        # Exit event for resolved/expired trades
        if status in ("EXPIRED", "SETTLED"):
            expired_at = t.get("expired_at") or t.get("settled_at") or ts
            events.append({
                "type": "EXIT",
                "timestamp": expired_at,
                "cycle": cycle,
                "token_id": token_id,
                "slug": slug,
                "question": q,
                "exit_reason": status,
                "exit_note": t.get("resolution_note", ""),
                "entry_price": entry_price,
                "size_usd": size,
            })
    return events

def build_positions(trades):
    """Build cleaned current-positions dict from non-expired trades."""
    positions = {}
    for t in trades:
        status = t.get("status", "")
        if status in ("EXPIRED", "SETTLED"):
            continue
        token_id = t.get("token_id", "")
        if not token_id:
            continue
        positions[token_id] = {
            "status": "open",
            "entry_price": t.get("price_target", 0),
            "size_usd": t.get("usd_exposure", 0),
            "side": t.get("side", "YES"),
            "entry_timestamp": t.get("timestamp", ""),
            "question": t.get("question", "")[:80],
            "slug": t.get("slug", ""),
            "cycle": t.get("cycle", 0),
            "strategy_id": t.get("strategy_id", "general-quant"),
        }
    return positions

def main():
    trades = load_paper_trades()
    print(f"📖 Loaded {len(trades)} trades from paper-trades.log")

    # Build ledger events
    events = build_ledger(trades)
    ledger = {
        "version": 1,
        "created": datetime.now(timezone.utc).isoformat(),
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "description": "Append-only trade lifecycle ledger",
        "total_entries": len(events),
        "events": events,
    }
    with open(LEDGER, "w") as f:
        json.dump(ledger, f, indent=2)
    print(f"✅ Ledger: {len(events)} events written to {LEDGER}")

    # Build cleaned positions
    positions = build_positions(trades)
    print(f"📊 Active positions: {len(positions)}")

    # Preserve bankroll from current trading-state
    bankroll = 48.85
    capital_deployed = 0.0
    if os.path.exists(TRADING_STATE):
        with open(TRADING_STATE) as f:
            try:
                ts = json.load(f)
                bankroll = ts.get("bankroll", 48.85)
                capital_deployed = ts.get("capital_deployed", 0)
            except json.JSONDecodeError:
                pass

    cleaned = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "bankroll": bankroll,
        "capital_deployed": capital_deployed,
        "mode": "paper",
        "open_positions_count": len(positions),
        "open_positions": positions,
    }
    with open(TRADING_STATE, "w") as f:
        json.dump(cleaned, f, indent=2)
    print(f"✅ trading-state.json cleaned: {len(positions)} active positions")

    print(f"\n🐀 Migration complete — ledger.json is append-only, trading-state.json has current positions only.")

if __name__ == "__main__":
    main()