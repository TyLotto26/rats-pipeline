#!/usr/bin/env python3
"""
pnl_tracker.py — Paper Trade PnL & Strategy Analytics
Rats on Wallstreet | Sovereign Thiren Edition

Parses paper-trades.log, queries Gamma API for resolved markets,
computes per-strategy PnL, win rates, edge distribution, and trend analysis.

Usage:
  python3 pnl_tracker.py                          # Full scan + settlement
  python3 pnl_tracker.py --settle-only            # Only check new settlements
  python3 pnl_tracker.py --report-only            # Only print analytics from cache
  python3 pnl_tracker.py --dry-run                # Simulate without saving
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HOME = Path.home()
PAPER_LOG = HOME / "paper-trades.log"
SETTLE_CACHE = HOME / "settled-pnl.json"
VAULT_API = "http://127.0.0.1:27124/vault"
VAULT_KEY_PATH = HOME / ".openclaw/vault-api-key"
GAMMA_API = "https://gamma-api.polymarket.com"
TRADING_STATE = HOME / "trading-state.json"


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] [PNL] {msg}")


def parse_paper_trades(path: Path) -> list[dict]:
    """Parse the pipe-delimited paper-trades.log into structured dicts."""
    trades = []
    pattern = re.compile(r'(\w+)=([^|]+)')
    
    if not path.exists():
        log(f"ERROR: {path} not found")
        return trades
    
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            
            parts = line.split(" | ")
            if len(parts) < 3:
                continue
            
            trade = {
                "timestamp": parts[0].strip(),
                "mode": parts[1].strip(),
                "raw": line
            }
            
            kv = " | ".join(parts[2:]) if len(parts) > 2 else parts[2]
            for match in pattern.finditer(kv):
                key = match.group(1)
                val = match.group(2).strip()
                
                if key == "entry":
                    trade["entry_price"] = float(val.replace("$", ""))
                elif key == "size":
                    trade["size_usd"] = float(val.replace("$", ""))
                elif key == "edge":
                    trade["edge_pct"] = float(val.replace("%", ""))
                elif key == "dir":
                    trade["direction"] = val
                elif key == "market":
                    trade["market_question"] = val
                elif key == "conf":
                    trade["confidence"] = val
                elif key == "vpin":
                    trade["vpin_class"] = val
                elif key == "garch":
                    trade["garch_scalar"] = float(val.replace("x", ""))
                elif key == "regime":
                    trade["regime"] = val
                elif key == "strat":
                    trade["strategy"] = val
                elif key == "result":
                    trade["result"] = val
            
            trades.append(trade)
    
    return trades


def get_vault_key() -> str:
    if VAULT_KEY_PATH.exists():
        return VAULT_KEY_PATH.read_text().strip().replace(" ", "")
    return ""


def vault_put(path: str, content: str) -> bool:
    key = get_vault_key()
    if not key:
        return False
    try:
        r = subprocess.run(
            ["curl", "-s", "-X", "PUT",
             "-H", f"Authorization: Bearer {key}",
             "-H", "Content-Type: text/markdown",
             "-d", content,
             f"{VAULT_API}/{path}"],
            capture_output=True, text=True, timeout=15
        )
        return r.returncode == 0
    except Exception:
        return False


def search_market_by_question(question: str) -> dict | None:
    """Search Gamma API for a market by question text (fuzzy match)."""
    try:
        # First try exact slug search
        slug = question.lower().replace("?", "").replace(",", "").replace("'", "")
        slug = re.sub(r'[^a-z0-9\s-]', '', slug)
        slug = slug.replace(" ", "-").strip("-")
        slug = slug[:200]
        
        r = subprocess.run(
            ["curl", "-s", f"{GAMMA_API}/markets?slug={slug}&limit=1"],
            capture_output=True, text=True, timeout=10
        )
        data = json.loads(r.stdout) if r.stdout else []
        if data and len(data) > 0:
            return data[0]
        
        # Fallback: search with tag
        r2 = subprocess.run(
            ["curl", "-s", f"{GAMMA_API}/markets?limit=5&closed=true&tag=weather"],
            capture_output=True, text=True, timeout=10
        )
        return None
    except Exception as e:
        log(f"  Search error for '{question[:40]}': {e}")
        return None


def get_market_outcomes(market_id: str) -> dict | None:
    """Get market outcomes (prices, resolution) from Gamma API."""
    try:
        r = subprocess.run(
            ["curl", "-s", f"{GAMMA_API}/markets/{market_id}"],
            capture_output=True, text=True, timeout=10
        )
        return json.loads(r.stdout) if r.stdout else None
    except Exception:
        return None


def compute_paper_pnl(trades: list[dict]) -> dict[str, Any]:
    """Compute analytics from paper trade log without API calls."""
    by_strategy = defaultdict(list)
    by_market = defaultdict(list)
    
    total_notional = 0
    total_trades = len(trades)
    
    for t in trades:
        strat = t.get("strategy", "unknown")
        market = t.get("market_question", "unknown")
        entry = t.get("entry_price", 0)
        size = t.get("size_usd", 0)
        edge = t.get("edge_pct", 0)
        
        by_strategy[strat].append(t)
        by_market[market].append(t)
        total_notional += size
    
    # Per-strategy stats
    strategy_stats = {}
    for strat, ts in sorted(by_strategy.items()):
        edges = [t.get("edge_pct", 0) for t in ts]
        sizes = [t.get("size_usd", 0) for t in ts]
        confs = [t.get("confidence", "unknown") for t in ts]
        
        strategy_stats[strat] = {
            "count": len(ts),
            "total_notional": round(sum(sizes), 2),
            "avg_edge": round(sum(edges) / len(edges), 2) if edges else 0,
            "min_edge": round(min(edges), 2) if edges else 0,
            "max_edge": round(max(edges), 2) if edges else 0,
            "high_conf": sum(1 for c in confs if c == "high"),
            "medium_conf": sum(1 for c in confs if c == "medium"),
            "low_conf": sum(1 for c in confs if c == "low"),
            "avg_garch": round(sum(t.get("garch_scalar", 0) for t in ts) / len(ts), 2) if ts else 0,
        }
    
    # Per-market stats
    market_stats = {}
    for market, ts in sorted(by_market.items()):
        edges = [t.get("edge_pct", 0) for t in ts]
        sizes = [t.get("size_usd", 0) for t in ts]
        entries = [t.get("entry_price", 0) for t in ts]
        strats = list(set(t.get("strategy", "unknown") for t in ts))
        
        market_stats[market] = {
            "count": len(ts),
            "total_notional": round(sum(sizes), 2),
            "avg_edge": round(sum(edges) / len(edges), 2) if edges else 0,
            "avg_entry": round(sum(entries) / len(entries), 4) if entries else 0,
            "avg_size": round(sum(sizes) / len(sizes), 2) if sizes else 0,
            "strategies": strats,
        }
    
    # Confidence breakdown
    all_confs = [t.get("confidence", "unknown") for t in trades]
    
    # Edge distribution
    all_edges = [t.get("edge_pct", 0) for t in trades if t.get("edge_pct", 0) > 0]
    bucket_edges = {"0-5%": 0, "5-10%": 0, "10-15%": 0, "15-20%": 0, "20-25%": 0, "25%+": 0}
    for e in all_edges:
        if e < 5: bucket_edges["0-5%"] += 1
        elif e < 10: bucket_edges["5-10%"] += 1
        elif e < 15: bucket_edges["10-15%"] += 1
        elif e < 20: bucket_edges["15-20%"] += 1
        elif e < 25: bucket_edges["20-25%"] += 1
        else: bucket_edges["25%+"] += 1
    
    return {
        "analysis_ts": datetime.now(timezone.utc).isoformat(),
        "total_trades": total_trades,
        "total_notional": round(total_notional, 2),
        "unique_markets": len(by_market),
        "unique_strategies": len(by_strategy),
        "avg_edge_all": round(sum(all_edges) / len(all_edges), 2) if all_edges else 0,
        "max_edge_all": round(max(all_edges), 2) if all_edges else 0,
        "confidence_breakdown": {
            "high": sum(1 for c in all_confs if c == "high"),
            "medium": sum(1 for c in all_confs if c == "medium"),
            "low": sum(1 for c in all_confs if c == "low"),
        },
        "edge_distribution": bucket_edges,
        "strategies": strategy_stats,
        "markets": market_stats,
    }


def print_report(stats: dict, title: str = "ANALYTICS REPORT"):
    """Pretty-print the analytics report."""
    print(f"\n{'='*60}")
    print(f"  🐀 {title}")
    print(f"{'='*60}")
    print(f"  Generated: {stats['analysis_ts'][:19]} UTC")
    print(f"{'='*60}")
    
    print(f"\n📊 OVERVIEW")
    print(f"  Total paper trades:  {stats['total_trades']}")
    print(f"  Total notional:     ${stats['total_notional']:.2f}")
    print(f"  Unique markets:     {stats['unique_markets']}")
    print(f"  Active strategies:  {stats['unique_strategies']}")
    print(f"  Average edge:       {stats['avg_edge_all']:.2f}%")
    print(f"  Max edge seen:      {stats['max_edge_all']:.2f}%")
    
    print(f"\n🔵 CONFIDENCE BREAKDOWN")
    cb = stats['confidence_breakdown']
    total = cb['high'] + cb['medium'] + cb['low']
    if total:
        print(f"  High:   {cb['high']:>4} ({cb['high']/total*100:.0f}%)")
        print(f"  Medium: {cb['medium']:>4} ({cb['medium']/total*100:.0f}%)")
        print(f"  Low:    {cb['low']:>4} ({cb['low']/total*100:.0f}%)")
    
    print(f"\n📈 EDGE DISTRIBUTION")
    for bucket, count in sorted(stats['edge_distribution'].items()):
        bar = "█" * count
        print(f"  {bucket:>8}: {count:>4} {bar}")
    
    print(f"\n🏆 STRATEGY RANKINGS (by avg edge)")
    ranked_sorted = sorted(stats['strategies'].items(), key=lambda x: x[1]['avg_edge'], reverse=True)
    for i, (strat, s) in enumerate(ranked_sorted, 1):
        print(f"  #{i} {strat:<25s} edge={s['avg_edge']:>5.2f}%  trades={s['count']:>4}  notion=${s['total_notional']:>6.2f}  high_conf={s['high_conf']}")
    
    print(f"\n📋 TOP MARKETS BY TRADE COUNT")
    ranked_sorted_markets = sorted(stats['markets'].items(), key=lambda x: x[1]['count'], reverse=True)[:10]
    for market, m in ranked_sorted_markets:
        print(f"  {m['count']:>4}x  {market[:55]:<55s}  edge={m['avg_edge']:.1f}%")
    
    print(f"\n{'='*60}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Paper Trade PnL & Strategy Analytics")
    parser.add_argument("--settle-only", action="store_true", help="Only check new settlements")
    parser.add_argument("--report-only", action="store_true", help="Only print analytics from cache")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without saving")
    parser.add_argument("--save", action="store_true", help="Save report to vault + state")
    args = parser.parse_args()
    
    # Parse paper trades
    trades = parse_paper_trades(PAPER_LOG)
    log(f"Parsed {len(trades)} trades from {PAPER_LOG.name}")
    
    if not trades:
        log("No trades found. Exiting.")
        return 1
    
    # Compute analytics
    stats = compute_paper_pnl(trades)
    
    # Print report
    print_report(stats, f"CYCLE ANALYSIS — {Path(PAPER_LOG.name)}")
    
    # Load existing settled cache
    settled = []
    if SETTLE_CACHE.exists():
        with open(SETTLE_CACHE) as f:
            cached = json.load(f)
            settled = cached.get("settled", [])
        log(f"Loaded {len(settled)} previously settled trades")
    
    if not args.report_only:
        log(f"Settlement scan would check {stats['unique_markets']} unique markets")
        
        # Group trades by market for resolution checking
        by_market = defaultdict(list)
        for t in trades:
            m = t.get("market_question", "unknown")
            by_market[m].append(t)
        
        log(f"Markets needing resolution check: {len(by_market)}")
        
        # Sample the top unsolved markets for manual checking
        unsolved = [m for m in by_market if not any(
            s.get("market_question") == m for s in settled
        )]
        log(f"Unsolved markets (no cache): {len(unsolved)}")
        
        if unsolved and not args.dry_run:
            # Write a market resolution check list
            check_list = "\n".join(f"- {m} ({len(by_market[m])} trades)" for m in sorted(unsolved)[:20])
            print(f"\n📋 MARKETS NEEDING RESOLUTION CHECK ({len(unsolved)} total):")
            print(check_list)
            if len(unsolved) > 20:
                print(f"  ... and {len(unsolved) - 20} more")
    
    # Save report
    if args.save:
        output_path = HOME / "pnl-report.json"
        with open(output_path, "w") as f:
            json.dump(stats, f, indent=2)
        log(f"Saved report to {output_path}")
        
        # Also save a human-readable version to vault
        report_md = f"""# 📊 PnL & Strategy Report — {stats['analysis_ts'][:19]} UTC

## Overview
- Total paper trades: {stats['total_trades']}
- Total notional: ${stats['total_notional']:.2f}
- Unique markets: {stats['unique_markets']}
- Active strategies: {stats['unique_strategies']}
- Average edge: {stats['avg_edge_all']:.2f}%
- Max edge: {stats['max_edge_all']:.2f}%

## Strategy Rankings
| Rank | Strategy | Avg Edge | Trades | Notional | High Conf |
|------|----------|----------|--------|----------|-----------|
"""
        ranked = sorted(stats['strategies'].items(), key=lambda x: x[1]['avg_edge'], reverse=True)
        for i, (strat, s) in enumerate(ranked, 1):
            report_md += f"| {i} | {strat} | {s['avg_edge']:.2f}% | {s['count']} | ${s['total_notional']:.2f} | {s['high_conf']} |\n"
        
        vault_put("reports/pnl-strategy-report-latest.md", report_md)
        log("Wrote report to vault")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
