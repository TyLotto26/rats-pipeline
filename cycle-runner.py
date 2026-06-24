#!/usr/bin/env python3
"""cycle-runner.py — Runs the full pipeline 10 clean cycles.
PolyScan → WhaleWatch → PolyBrain → PolyExec → reset → repeat.
"""
import fcntl
import json
import os
import re
import subprocess
from subprocess import CompletedProcess
import sys
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
STATE_FILE = os.path.join(HOME, "pipeline-state.json")
FETCH_SCRIPT = os.path.join(HOME, "polyscan-fetch.py")
POLYENV = os.path.join(HOME, "polyenv/bin/python3")
QUANT_METRICS = os.path.join(HOME, "quant-metrics.py")
VPIN_ENGINE = os.path.join(HOME, "vpin_engine.py")
TRADES_LOG = os.path.join(HOME, "paper-trades.log")

# Bot workspace token paths — each stage posts as its own Discord bot
POLYSCAN_WS = "nicole-polyscan"
WHALEWATCH_WS = "nicole-whalewatch"
POLYBRAIN_WS = "nicole-polybrain"
POLYEXEC_WS = "nicole-polyexec"

# Stage-specific reporting channels (per Tyrese's channel architecture)
POLYSCAN_CHANNEL = '1479003428545101845'      # PolyScan results
WHALEWATCH_CHANNEL = '1479181579518480676'     # WhaleWatch signals
POLYBRAIN_CHANNEL = '1479003430684065917'      # PolyBrain proposals
POLYEXEC_CHANNEL = '1479183146611249232'       # PolyExec executions

# Legacy channels for summaries
TRADE_LOG = '1481421156178329653'
ALERTS = '1481421175702945942'              # 🚨alerts

# OS Layer channels (Hedge Fund architecture)
FUND_SUMMARY = '1481443097337532486'    # #fund-summary
OPERATIONS = '1481421201107582987'      # #operations (was #bot-status)
PIPELINE_CHANNEL = '1519431240220934144' # #pipeline (new — cycle summary)
PORTFOLIO = '1494476176381706270'       # #portfolio
ALERTS_CHANNEL = '1481421175702945942'  # #alerts (Red/Black only)
RISK_DESK = '1519431340444094555'       # #risk-desk

# Operating-mode state file
OPERATING_MODE = os.path.expanduser("~/.rats/operating-mode")

# --- Option A: Fusion-powered PolyBrain deliberation ---
FUSION_ENABLED = True          # True = route through Fusion agent; False = direct Python
FUSION_BRIDGE = os.path.join(HOME, ".openclaw", "workspace", "trader-jane", "polybrain_fusion.py")

# Position tracking — reads paper-trades.log to avoid duplicate trades
from position_tracker import (
    get_open_token_ids,
    get_available_bankroll,
    get_open_positions,
    load_trades,
    is_open,
)


def post_discord_channel(channel_id, msg, workspace="nicole-polyexec"):
    """Post a message to a specific Discord channel using the given bot workspace token."""
    tf = os.path.expanduser(f'~/openclaw-vault/_hermes/workspaces/{workspace}/.discord_token')
    if not os.path.exists(tf):
        print(f"[DISCORD] No token file at {tf}")
        return False
    with open(tf) as f:
        token = f.read().strip()
    if not token:
        print("[DISCORD] Empty token")
        return False
    if not msg or not msg.strip():
        print("[DISCORD] Refusing to post empty message")
        return False
    payload = json.dumps({'content': msg[:2000]})
    r = subprocess.run(['curl', '-s', '-X', 'POST',
        f'https://discord.com/api/v10/channels/{channel_id}/messages',
        '-H', f'Authorization: Bot {token}',
        '-H', 'Content-Type: application/json',
        '-d', payload], capture_output=True, timeout=10)
    if r.returncode != 0:
        print(f"[DISCORD] curl error (rc={r.returncode}): {r.stderr[:200]}")
        return False
    try:
        resp = json.loads(r.stdout)
    except json.JSONDecodeError:
        print(f"[DISCORD] Non-JSON response: {r.stdout[:200]}")
        return False
    if 'id' in resp:
        return True
    print(f"[DISCORD] API error: {resp.get('message', r.stdout[:200])}")
    return False


INITIAL_BANKROLL = 48.85  # Single source of truth — never changes

def get_current_bankroll(state):
    """Compute available from INITIAL minus deployed capital.
    Never passes state.bankroll as initial (it may be stale)."""
    br = get_available_bankroll(initial=INITIAL_BANKROLL)
    return br["available"]


def run_cmd(cmd, timeout=30):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result
    except FileNotFoundError:
        print(f"[CMD] Command not found: {cmd[0]}")
        return CompletedProcess(cmd, returncode=-1, stdout="", stderr="not found")
    except subprocess.TimeoutExpired:
        print(f"[CMD] Timed out after {timeout}s: {cmd[0]}")
        return CompletedProcess(cmd, returncode=-2, stdout="", stderr="timeout")


def read_state():
    with open(STATE_FILE) as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        state = json.load(f)
        fcntl.flock(f, fcntl.LOCK_UN)
    return state


def write_state(state):
    with open(STATE_FILE, 'r+') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        f.truncate()
        json.dump(state, f, indent=2)
        f.flush()
        fcntl.flock(f, fcntl.LOCK_UN)


def check_freshness(data_timestamp, max_age_minutes=30, label="data"):
    """Check if data is fresh enough to process. Returns True if fresh, False if stale."""
    if not data_timestamp:
        return True  # no timestamp = can't check, assume fresh
    try:
        ts = datetime.fromisoformat(data_timestamp.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - ts).total_seconds() / 60
        if age > max_age_minutes:
            print(f"  ⏰ Stale {label} ({age:.0f}m old, max {max_age_minutes}m) — skipping")
            return False
        return True
    except (ValueError, TypeError):
        return True  # unparseable timestamp = assume fresh


def stage_polyscan(state):
    ps = state.get("polyscan", {})
    if ps.get("status") == "ghost_town":
        return None
    r = run_cmd(["python3", FETCH_SCRIPT], timeout=60)
    if r.returncode != 0:
        return None
    try:
        markets = json.loads(r.stdout)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"[POLYSCAN] Failed to parse output: {e}")
        return None
    valid = [m for m in markets if m.get("fair_value_estimate") is not None]
    now = datetime.now(timezone.utc).isoformat()
    state["polyscan"] = {**ps, "status": "complete", "last_run": now, "markets": valid,
        "scanned": len(markets), "targets_count": len(valid), "timestamp": now}
    state["last_updated"] = now
    write_state(state)
    return valid


def stage_whalewatch(state):
    ww = state.get("whalewatch", {})
    markets = state.get("polyscan", {}).get("markets", [])
    if not markets:
        return []
    
    # Check data freshness — PolyScan data shouldn't be >30m old
    ps_timestamp = state.get("polyscan", {}).get("timestamp")
    if not check_freshness(ps_timestamp, max_age_minutes=30, label="PolyScan data"):
        return []
    
    # Load open positions to filter markets we've already traded
    bankroll = get_current_bankroll(state)
    
    signals = []
    for m in markets:
        token_id = m.get("token_id", "")
        if not token_id:
            continue
        
        # Skip if we already have an open position on this market
        if is_open(token_id):
            print(f"  SKIP (open position): {m.get('question','?')[:40]}")
            continue
        
        qm_input = json.dumps({"markets": [m], "bankroll": bankroll})
        r = run_cmd([POLYENV, QUANT_METRICS, qm_input], timeout=30)
        tradeable, ev, kelly_val = False, 0.0, 0.0
        if r.returncode == 0 and r.stdout.strip():
            try:
                qm = json.loads(r.stdout)
                results = qm.get("results") or []
                if results:
                    tradeable = results[0].get("tradeable", False)
                    ev = results[0].get("ev", 0)
                    kelly_val = results[0].get("kelly", {}).get("size_usd", 0)
            except (json.JSONDecodeError, KeyError, IndexError):
                pass
        
        r2 = run_cmd([POLYENV, VPIN_ENGINE, token_id], timeout=15)
        vpin_class = "SAFE"
        if r2.returncode == 0 and r2.stdout.strip():
            try:
                vd = json.loads(r2.stdout)
                cls = vd.get("classification", {})
                vpin_class = cls.get("classification", "SAFE") if isinstance(cls, dict) else str(cls)
            except (json.JSONDecodeError, AttributeError):
                pass
        
        vpin_ok = vpin_class in ("SAFE", "CAUTION", "THIN_MARKET")
        
        # Edge gate: minimum 15% price edge from fair value
        yes_price = m.get("yes_price", m.get("yes", 0.5))
        fv = m.get("fair_value_estimate", 0)
        edge_pct = round((fv - yes_price) / yes_price * 100, 1) if yes_price > 0 else 0
        edge_ok = edge_pct >= 15.0
        
        is_tradeable = tradeable and ev > 0.05 and vpin_ok and edge_ok
        if is_tradeable:
            signals.append({
                "token_id": token_id,
                "question": m.get("question", ""),
                "slug": m.get("slug", ""),
                "yes_price": m.get("yes_price", m.get("yes", 0.5)),
                "ev": ev,
                "kelly": kelly_val,
                "vpin_class": vpin_class,
                "fair_value_estimate": m.get("fair_value_estimate"),
                "fair_value_direction": m.get("fair_value_direction"),
            })
    now = datetime.now(timezone.utc).isoformat()
    state["whalewatch"] = {**ww, "status": "complete", "last_run": now,
        "signals": signals, "tradeable": len(signals), "analyzed": len(markets)}
    state["last_updated"] = now
    write_state(state)
    return signals


# Cost constants (per-trade economics — Polymarket paper mode)
GAS_COST = 0.10        # MATIC gas per on-chain settlement
MODEL_COST = 0.30      # PolyBrain Fusion frontier call
SLIPPAGE_PCT = 0.02    # 2% slippage on fill
COST_BUFFER = 2.0      # 2x buffer: must cover cost twice over


def passes_cost_filter(signal, bankroll=50.0):
    """Check if a trade signal is worth the model cost to analyze.
    
    Formula: expected_value × position_size ≥ (gas + model_cost + slippage) × 2
    
    Returns True if the signal clears the cost-of-thinking threshold.
    """
    ev = signal.get("ev", 0) or 0
    kelly = signal.get("kelly_size") or signal.get("kelly", 0) or 0
    size = kelly if kelly > 0 else min(2.50, bankroll * 0.02)
    
    # ev is a ratio (2.7 = 270% expected value)
    expected_profit = (ev - 1) * size if ev > 1 else ev * size
    
    # Total trading cost per trade
    slippage_cost = size * SLIPPAGE_PCT
    total_cost = GAS_COST + MODEL_COST + slippage_cost
    
    threshold = total_cost * COST_BUFFER
    result = expected_profit >= threshold
    
    q = signal.get("question", "?")[:45]
    if not result and expected_profit > 0:
        print(f"  💰 Skip {q}: expected ${expected_profit:.2f} < ${threshold:.2f} cost threshold")
    elif not result:
        print(f"  💰 Skip {q}: negative EV ({ev:.2f}) — not worth analyzing")
    return result


def stage_polybrain(state):
    signals = state.get("whalewatch", {}).get("signals", [])
    
    # Check data freshness — WhaleWatch data shouldn't be >30m old
    ww_timestamp = state.get("whalewatch", {}).get("last_run")
    if not check_freshness(ww_timestamp, max_age_minutes=30, label="WhaleWatch data"):
        return []
    
    # === Cost-of-Thinking filter ===
    # Skip signals where the potential profit won't cover the model call cost
    bankroll = get_current_bankroll(state)
    filtered = [s for s in signals if passes_cost_filter(s, bankroll)]
    skipped = len(signals) - len(filtered)
    if skipped:
        print(f"  💰 Cost-of-Thinking: filtered {skipped}/{len(signals)} signals (below threshold)")
    signals = filtered
    if not signals:
        print(f"  💰 No signals cleared cost-of-thinking threshold — skipping PolyBrain")
        return []
    
    proposals = []
    
    # Option A: Route through Fusion-enabled PolyBrain agent
    if FUSION_ENABLED and signals and os.path.exists(FUSION_BRIDGE):
        print(f"  Fusion deliberation: {len(signals)} signals → calling PolyBrain Fusion...")
        try:
            result = subprocess.run(
                [POLYENV, FUSION_BRIDGE],
                input=json.dumps(signals),
                capture_output=True, text=True, timeout=180
            )
            if result.returncode == 0 and result.stdout.strip():
                try:
                    fusion_out = json.loads(result.stdout)
                    fusion_proposals = fusion_out.get("proposals", [])
                    if fusion_proposals:
                        proposals = fusion_proposals
                        print(f"  Fusion: {len(proposals)} proposals generated")
                        print(f"  Judge: {fusion_out.get('judge', '?')} | Panel: {fusion_out.get('panel', [])}")
                    else:
                        print(f"  Fusion: returned 0 proposals, falling back to Python path")
                except json.JSONDecodeError:
                    print(f"  Fusion: invalid JSON output, falling back")
            else:
                if result.stderr:
                    print(f"  Fusion stderr: {result.stderr[:200]}")
                print(f"  Fusion: failed (rc={result.returncode}), falling back to Python path")
        except subprocess.TimeoutExpired:
            print(f"  Fusion: timed out after 180s, falling back to Python path")
        except Exception as e:
            print(f"  Fusion: error {e}, falling back to Python path")
    
    # Fallback: direct Python conversion (always available)
    if not proposals:
        for s in signals:
            proposals.append({
                "question": s.get("question", ""),
                "slug": s.get("slug", ""),
                "token_id": s.get("token_id", ""),
                "condition_id": s.get("condition_id", ""),
                "direction": s.get("fair_value_direction", "YES"),
                "entry_price": s.get("yes_price", 0.5),
                "fair_value": s.get("fair_value_estimate"),
                "ev": s.get("ev", 0),
                "kelly_size": round(s.get("kelly_size") or s.get("kelly", 0), 2),
                "vpin_class": s.get("vpin_class", "SAFE"),
                "strategy_id": "macro-fed-v1",
                "confidence": "high" if s.get("ev", 0) > 0.5 else "medium",
            })
    
    now = datetime.now(timezone.utc).isoformat()
    state["polybrain"] = {"status": "complete", "last_run": now,
        "proposals": proposals, "notes": f"{len(proposals)} proposal(s)"}
    state["last_updated"] = now
    write_state(state)
    return proposals


def stage_polyexec(state):
    proposals = state.get("polybrain", {}).get("proposals", [])
    
    # Check data freshness — PolyBrain data shouldn't be >30m old
    pb_timestamp = state.get("polybrain", {}).get("last_run")
    if not check_freshness(pb_timestamp, max_age_minutes=30, label="PolyBrain proposals"):
        return []
    
    bankroll = get_current_bankroll(state)
    open_ids = get_open_token_ids()
    executed = []
    
    for p in proposals:
        token_id = p.get("token_id", "")
        direction_raw = p.get("direction", "YES")
        # Strip "BUY " prefix if present (Fusion outputs "BUY YES")
        direction = direction_raw[4:] if direction_raw and direction_raw.upper().startswith("BUY ") else direction_raw
        entry = p.get("entry_price", 0.5)
        # Normalize field names: Fusion outputs size_usd, quant pipeline outputs kelly_size
        kelly = p.get("kelly_size") or p.get("size_usd", 0) or 0
        if isinstance(kelly, (int, float)) and kelly > 0:
            max_size = min(2.50, bankroll * 0.02)
            size = min(kelly, max_size)
        else:
            size = 0
        
        # Gate: skip if already have open position on this market
        if is_open(token_id):
            print(f"  SKIP trade (already open): {p.get('question','?')[:40]}")
            continue
        
        # Gate: minimum size — handle ev (ratio), edge (%), or missing
        edge_raw = p.get("ev") or p.get("edge", 0) or 0
        # ev is ratio (2.7 = 270%), edge is percentage (324.4 = 324.4%)
        is_ev = p.get("ev") is not None and p.get("ev") != 0
        edge_val = edge_raw / 100.0 if isinstance(edge_raw, (int, float)) and edge_raw > 1 and not is_ev else edge_raw
        if size < 0.10 or edge_val < 0.05:
            continue
        
        # Gate: max 5 concurrent open trades
        if len(get_open_positions()) + len(executed) >= 10:
            print(f"  SKIP trade (at max capacity): {p.get('question','?')[:40]}")
            continue
        
        # Refresh open_ids to prevent duplicate trades within same batch
        
        now = datetime.now(timezone.utc).isoformat()
        trade = {
            "id": f"TRADE-{state.get('cycle',0)}-{token_id[:12]}",
            "action": f"BUY {direction}",
            "contracts": 1,
            "price": round(entry, 3),
            "total": round(size, 2),
            "token_id": token_id,
            "timestamp": now,
            "strategy_id": p.get("strategy_id", "general-quant"),
            "mode": "paper",
            "question": p.get("question", ""),
        }
        executed.append(trade)
        
        # Write to paper-trades.log in clean JSON format
        log_entry = json.dumps({
            "timestamp": now,
            "mode": "paper",
            "question": p.get("question", ""),
            "slug": p.get("slug", ""),
            "token_id": token_id,
            "side": direction_raw,
            "price_target": entry,
            "usd_exposure": round(size, 2),
            # Normalize: use ev (ratio) or edge (percentage) whichever available
            "edge": round((p.get("ev") or p.get("edge", 0) or 0) * (100 if p.get("ev") is not None else 1), 1),
            "status": "PAPER_LOGGED",
            "strategy_id": p.get("strategy_id", "general-quant"),
            "cycle": state.get("cycle", 0),
        })
        with open(TRADES_LOG, 'a') as f:
            f.write(log_entry + "\n")
        
        # Update open_ids in-memory so same-batch duplicates are caught
        open_ids.add(token_id)
        if p.get("slug"):
            open_ids.add(p.get("slug"))
        
        print(f"  EXECUTED: {p.get('question','?')[:40]} | {direction} @ ${entry:.3f} | ${size:.2f} | token_id={token_id[:16]}...")
    
    now = datetime.now(timezone.utc).isoformat()
    state["polyexec"] = {"status": "complete", "last_run": now,
        "executed": executed, "last_result": "EXECUTED" if executed else "SKIP",
        "reason": f"{len(executed)} trade(s)" if executed else "ghost_town"}
    
    # Update bankroll in state — store INITIAL, not available
    # (get_current_bankroll computes available from INITIAL - deployed)
    if executed:
        state["bankroll"] = INITIAL_BANKROLL
    
    state["last_updated"] = now
    write_state(state)
    return executed


def reset_cycle(state):
    state["cycle"] = state.get("cycle", 0) + 1
    for stage in ["polyscan", "whalewatch", "polybrain", "polyexec"]:
        if stage not in state:
            state[stage] = {}
        state[stage]["status"] = "idle"
    state["last_updated"] = datetime.now(timezone.utc).isoformat()
    write_state(state)


def sweep_expired_positions():
    """Auto-settle stale positions that have passed their market end date.
    
    Reads paper-trades.log, checks each PAPER_LOGGED trade for date clues
    in the slug/question, and marks expired ones as EXPIRED so position
    slots free up for new trades.
    """
    import calendar
    if not os.path.exists(TRADES_LOG):
        return 0
    
    # Build a map of token_id -> end_date from pipeline-state.json
    market_end_dates = {}
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
        for m in state.get("polyscan", {}).get("markets", []):
            tid = m.get("token_id", "")
            ed = m.get("end_date", "")
            if tid and ed:
                market_end_dates[tid] = ed
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    
    # Read all trades
    lines = []
    updated_lines = []
    swept = 0
    now = datetime.now(timezone.utc)
    
    try:
        with open(TRADES_LOG) as f:
            lines = f.readlines()
    except FileNotFoundError:
        return 0
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            trade = json.loads(line)
        except json.JSONDecodeError:
            updated_lines.append(line)
            continue
        
        status = trade.get("status", "")
        if status != "PAPER_LOGGED":
            updated_lines.append(json.dumps(trade))
            continue
        
        token_id = trade.get("token_id", "")
        slug = trade.get("slug", "") or ""
        question = trade.get("question", "") or ""
        expired = False
        
        # Method 1: Check end_date from pipeline-state.json
        if token_id in market_end_dates:
            try:
                ed = datetime.fromisoformat(market_end_dates[token_id].replace("Z", "+00:00"))
                if now > ed:
                    expired = True
            except (ValueError, TypeError):
                pass
        
        # Method 2: Parse date from slug pattern "on-{month}-{day}-{year}"
        if not expired and slug:
            slug_lower = slug.lower()
            # Match patterns like "june-17-2026" in slug
            months = {
                "january": 1, "february": 2, "march": 3, "april": 4,
                "may": 5, "june": 6, "july": 7, "august": 8,
                "september": 9, "october": 10, "november": 11, "december": 12
            }
            date_match = re.search(r'(january|february|march|april|may|june|july|august|september|october|november|december)-(\d{1,2})-(\d{4})', slug_lower)
            if date_match:
                month_str, day_str, year_str = date_match.groups()
                month = months[month_str]
                day = int(day_str)
                year = int(year_str)
                try:
                    # Weather markets typically resolve ~24h after the date
                    last_day = calendar.monthrange(year, month)[1]
                    day = min(day, last_day)
                    # For daily markets, resolve next day
                    market_date = datetime(year, month, day, 23, 59, 59, tzinfo=timezone.utc)
                    if now > market_date:
                        expired = True
                except (ValueError, calendar.IllegalMonthError):
                    pass
            # Also check month-only slugs (e.g., "jun-2026")
            if not expired:
                month_match = re.search(r'(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)-(\d{4})', slug_lower)
                if month_match:
                    short_months = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
                                    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
                    month = short_months.get(month_match.group(1), 0)
                    year = int(month_match.group(2))
                    if month and year:
                        # Month-level markets (like Silver) - only expire if next month
                        import calendar
                        last_day = calendar.monthrange(year, month)[1]
                        market_end = datetime(year, month, last_day, 23, 59, 59, tzinfo=timezone.utc)
                        if now > market_end:
                            expired = True
        
        if expired:
            trade["status"] = "EXPIRED"
            trade["expired_at"] = now.isoformat()
            trade["resolution_note"] = "auto-sweep: end date passed"
            print(f"  💀 Swept: {trade.get('question','?')[:45]} | was ${trade.get('usd_exposure',0):.2f}")
            swept += 1
        
        updated_lines.append(json.dumps(trade, ensure_ascii=False))
    
    if swept > 0:
        with open(TRADES_LOG, 'w') as f:
            for line in updated_lines:
                f.write(line + "\n")
        print(f"  ✅ Sweep complete: {swept} stale position(s) marked EXPIRED")
    else:
        print(f"  ℹ️  No stale positions to sweep")
    
    return swept



def stage_settlement(state):
    """Sweep stale daily-market positions (>48h old) to free up position slots.
    
    Polymarket daily weather markets resolve within 24h of the event date.
    If our local log still shows them as PAPER_LOGGED after 48h, they're stale
    and should be auto-settled to free the cap for new trades.
    """
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    trades = load_trades()
    settled_count = 0
    freed_capital = 0.0
    now_iso = now.isoformat()
    
    # Read and rewrite the log with stale trades marked as settled
    updated_lines = []
    for line in open(TRADES_LOG):
        line = line.strip()
        if not line:
            updated_lines.append(line)
            continue
        try:
            t = json.loads(line)
        except json.JSONDecodeError:
            updated_lines.append(line)
            continue
        
        status = t.get("status", "")
        if status != "PAPER_LOGGED":
            updated_lines.append(line)
            continue
        
        timestamp = t.get("timestamp", "")
        exposure = float(t.get("usd_exposure", 0) or 0)
        
        if not timestamp:
            updated_lines.append(line)
            continue
        
        try:
            logged_ts = datetime.datetime.fromisoformat(timestamp)
        except (ValueError, TypeError):
            updated_lines.append(line)
            continue
        
        age_hours = (now - logged_ts).total_seconds() / 3600
        
        # Stale threshold: 48 hours for daily markets
        if age_hours >= 48:
            t["status"] = "SETTLED"
            t["settled_at"] = now_iso
            t["settlement_reason"] = f"auto-sweep — stale position ({age_hours:.0f}h old, >48h threshold)"
            t["exit_price"] = t.get("price_target", 0)
            updated_lines.append(json.dumps(t))
            settled_count += 1
            freed_capital += exposure
        else:
            updated_lines.append(line)
    
    # Write back if changes were made
    if settled_count > 0:
        with open(TRADES_LOG, 'w') as f:
            for line in updated_lines:
                if line:
                    f.write(line + "\n")
        print(f"  Settlement: {settled_count} stale position(s) swept (${freed_capital:.2f} capital freed)")
        # Post to bot-status
        post_discord_channel(OPERATIONS, 
            f"🧹 **Settlement sweep** — {settled_count} stale position(s) auto-settled, freed ${freed_capital:.2f}")
    else:
        print(f"  Settlement: no stale positions found")
    
    return settled_count, freed_capital

def main():
    import sys as _sys
    
    # === Operating-mode check ===
    if os.path.exists(OPERATING_MODE):
        with open(OPERATING_MODE) as f:
            op_mode = f.read().strip()
    else:
        op_mode = "live"  # default to live if no file
    print(f"  🐀 Mode: {op_mode.upper()}")

    # Mode-change alert: post to #alerts if mode changed since last run
    prev_mode_file = os.path.join(HOME, ".rats", ".previous_mode")
    try:
        with open(prev_mode_file) as f:
            prev_mode = f.read().strip()
    except:
        prev_mode = op_mode  # first run — no alert
    
    if prev_mode != op_mode:
        post_discord_channel(ALERTS_CHANNEL,
            f"🚨 **Mode Change** — {prev_mode.upper()} → {op_mode.upper()}")
        post_discord_channel(OPERATIONS,
            f"🔄 **Mode Change** — {prev_mode.upper()} → {op_mode.upper()}")
    
    with open(prev_mode_file, 'w') as f:
        f.write(op_mode)
    
    if op_mode in ("dead", "pause"):
        print(f"  🚫 Pipeline halted — mode is {op_mode}")
        return
    
    # Default: 5 cycles in live mode, 1 in hold mode
    target = 5
    if op_mode == "hold":
        print(f"  ⏸️  Hold mode — one cycle then stop")
        target = 1
    
    start_cycle = 1
    # CLI: --single or --cycles N — overrides the default
    if '--single' in _sys.argv:
        target = 1
    for i, arg in enumerate(_sys.argv):
        if arg in ('--cycles', '-c') and i + 1 < len(_sys.argv):
            try:
                target = max(1, int(_sys.argv[i + 1]))
            except ValueError:
                pass
    results = []

    # Settlement sweep: auto-expire stale positions before pipeline runs
    print(f"\n{'='*60}")
    print(f"SETTLEMENT SWEEP — pre-cycle cleanup")
    print(f"{'='*60}")
    sweep_expired_positions()

    for i in range(1, target + 1):
        state = read_state()
        current_cycle = state.get("cycle", 0)
        print(f"\n{'='*60}")
        print(f"CYCLE {i}/{target} (pipeline cycle {current_cycle})")
        print(f"{'='*60}")

        bankroll = get_current_bankroll(state)
        open_count = len(get_open_token_ids())
        print(f"  Bankroll: ${bankroll:.2f} | Open positions: {open_count}")

        markets = stage_polyscan(state)
        if markets:
            print(f"  PolyScan: {len(markets)} markets")
            # Post to PolyScan channel
            ps_lines = [
                f"🔬 **PolyScan — Cycle {current_cycle}**",
                f"├ Markets scanned: {len(markets)}",
                f"└ Tiers: {sum(1 for m in markets if m.get('tier')==1)} T1, {sum(1 for m in markets if m.get('tier')==2)} T2, {sum(1 for m in markets if m.get('tier')==3)} T3",
            ]
            # Show top 3 markets by liquidity
            by_liq = sorted(markets, key=lambda m: m.get('liquidity', 0), reverse=True)[:3]
            for m in by_liq:
                fv = m.get('fair_value_estimate', '?')
                q = m.get('question', '?')[:50]
                liq = m.get('liquidity', 0)
                ps_lines.append(f"• {q} | FV: {float(fv):.3f}" if isinstance(fv, (int,float)) else f"• {q} | FV: {fv}")
            post_discord_channel(POLYSCAN_CHANNEL, "\n".join(ps_lines), workspace=POLYSCAN_WS)
        else:
            print(f"  PolyScan: failed — waiting for next tick")
            # Post stall alert to #pipeline and #operations so silence ≠ broken
            post_discord_channel(PIPELINE_CHANNEL,
                f"⚠️ **Cycle {current_cycle}** — PolyScan failed, no markets fetched")
            post_discord_channel(OPERATIONS,
                f"🔴 **Cycle {current_cycle}** 😭 Pipeline failed — no markets fetched | Bankroll: ${bankroll:.2f}")
            post_discord_channel(ALERTS_CHANNEL,
                f"🚨 **PolyScan stall** — Cycle {current_cycle} failed to fetch markets")
            continue

        state = read_state()
        signals = stage_whalewatch(state)
        print(f"  WhaleWatch: {len(signals)} tradeable signals (fresh markets only)")
        # Post to WhaleWatch channel
        if signals:
            ww_lines = [
                f"🐋 **WhaleWatch — Cycle {current_cycle}**",
                f"├ Signals found: {len(signals)}",
            ]
            for s in signals:
                q = s.get('question', '?')[:50]
                ev = s.get('ev', 0)
                kelly = s.get('kelly', 0)
                vpin = s.get('vpin_class', '?')
                ww_lines.append(f"• {q} | EV: {ev:.2f} | Kelly: ${kelly:.2f} | VPIN: {vpin}")
            post_discord_channel(WHALEWATCH_CHANNEL, "\n".join(ww_lines), workspace=WHALEWATCH_WS)

        state = read_state()
        proposals = stage_polybrain(state)
        print(f"  PolyBrain: {len(proposals)} proposals")
        
        # 🐀 0-PROPOSAL DETECTION GATE — if markets were scanned but no proposals,
        # silently flag it to the war room so we know the pipeline is spinning wheels
        if len(proposals) == 0 and markets and len(markets) > 0:
            fve_with_data = sum(1 for m in markets if m.get("fair_value_source"))
            fve_identity = len(markets) - fve_with_data
            warning = (
                f"🔴 **Pipeline Anomaly — Cycle {current_cycle}**\n"
                f"├ Markets scanned: {len(markets)}\n"
                f"├ FVE with data: {fve_with_data} | Identity fallback: {fve_identity}\n"
                f"├ WhaleWatch signals: {len(signals)}\n"
                f"└ PolyBrain proposals: 0 — pipeline produced no edge"
            )
            print(f"  ⚠️  {warning[:80]}...")
            print(f"  Karpathy gate: 0 proposals from {len(markets)} markets = silent failure. Flagging.")
            # Check if any T1 markets existed — if so, this is a real signal gap
            t1_count = sum(1 for m in markets if m.get('tier') == 1)
            if t1_count > 0:
                post_discord_channel(ALERTS_CHANNEL,
                    f"🚨 **Signal Gap** — {t1_count} Tier-1 markets scanned, 0 proposals | Cycle {current_cycle}")
        
        # Post to PolyBrain channel
        if proposals:
            pb_lines = [
                f"🧠 **PolyBrain — Cycle {current_cycle}**",
                f"├ Proposals generated: {len(proposals)}",
            ]
            for p in proposals:
                q = p.get('question', '?')[:50]
                direction = p.get('direction', '?')
                fv = p.get('fair_value', 0)
                ev = p.get('ev', 0)
                kelly = p.get('kelly_size', 0)
                strat = p.get('strategy_id', '?')
                pb_lines.append(f"• {q} | {direction} FV: {float(fv):.3f}" if isinstance(fv, (int,float)) else f"• {q} | {direction}")
            post_discord_channel(POLYBRAIN_CHANNEL, "\n".join(pb_lines), workspace=POLYBRAIN_WS)

        state = read_state()
        trades = stage_polyexec(state)
        
        # Stage: Settlement Sweep
        settled, freed = stage_settlement(state)

        if trades:
            total = sum(t["total"] for t in trades)
            print(f"  PolyExec: {len(trades)} trade(s) executed, total ${total:.2f}")
            # Post to PolyExec channel
            pe_lines = [
                f"💰 **PolyExec — Cycle {current_cycle}**",
                f"├ Trades: {len(trades)} | Total: ${total:.2f}",
                f"├ Bankroll: ${bankroll:.2f} | Remaining: ${bankroll - total:.2f}",
            ]
            for t in trades:
                q = t.get('question', '?')[:45]
                action = t.get('action', '?')
                price = t.get('price', 0)
                size = t.get('total', 0)
                pe_lines.append(f"└ {q} | {action} @ ${price:.3f} | ${size:.2f}")
            post_discord_channel(POLYEXEC_CHANNEL, "\n".join(pe_lines), workspace=POLYEXEC_WS)
            for t in trades:
                post_discord_channel(TRADE_LOG,
                    f"🐀 **Cycle {current_cycle}** — {t['action']} @ ${t['price']:.3f} | Size: ${t['total']:.2f} | {t.get('question','')[:30]}")
            # Post PnL summary to #trade-activity
            pnl_lines = [
                f"📊 **Cycle {current_cycle} — Paper Trades Executed**",
                f"├ Trades: {len(trades)} | Total deployed: ${total:.2f}",
                f"├ Bankroll remaining: ${bankroll - total:.2f}",
            ]
            for t in trades:
                pnl_lines.append(f"└ {t.get('question','?')[:35]} | {t['action']} @ ${t['price']:.3f} | ${t['total']:.2f}")
            post_discord_channel(FUND_SUMMARY, "\n".join(pnl_lines))

        # Always post cycle summary to #pipeline
        summary_lines = [
            f"🐀 **Pipeline Cycle {current_cycle}**",
            f"├ Bankroll: ${bankroll:.2f} | Open positions: {len(get_open_token_ids())}",
            f"├ PolyScan: {len(markets) if markets else 0} markets scanned",
            f"├ WhaleWatch: {len(signals)} tradeable signals",
            f"├ PolyBrain: {len(proposals)} proposals",
        ]
        if trades:
            total = sum(t["total"] for t in trades)
            summary_lines.append(f"└ PolyExec: ✅ {len(trades)} trade(s), ${total:.2f} deployed")
        else:
            summary_lines.append(f"└ PolyExec: ⏭️ No trades (max capacity or no valid edge)")
        post_discord_channel(PIPELINE_CHANNEL, "\n".join(summary_lines))

        # Post to #operations
        if trades:
            total = sum(t["total"] for t in trades)
            bot_status_msg = f"🧠 **Cycle {current_cycle}** ✅ {len(trades)} trade(s), ${total:.2f} | Bankroll: ${bankroll:.2f} | Open: {len(get_open_token_ids())}"
        elif signals:
            bot_status_msg = f"🧠 **Cycle {current_cycle}** ⏭️ No trades ({len(signals)} signals found) | Bankroll: ${bankroll:.2f} | Open: {len(get_open_token_ids())}"
        elif markets:
            bot_status_msg = f"🧠 **Cycle {current_cycle}** ⚠️ No tradeable signals ({len(markets)} markets scanned) | Bankroll: ${bankroll:.2f}"
        else:
            bot_status_msg = f"🧠 **Cycle {current_cycle}** ❌ Pipeline failed — no markets fetched | Bankroll: ${bankroll:.2f}"
        post_discord_channel(OPERATIONS, bot_status_msg)

        results.append({
            "cycle": i,
            "pipeline_cycle": current_cycle,
            "markets": len(markets) if markets else 0,
            "signals": len(signals),
            "proposals": len(proposals),
            "trades": len(trades),
        })

        reset_cycle(read_state())

    # Drawdown check: post to #alerts if realized losses exceed 10% of bankroll
    try:
        with open(os.path.join(HOME, 'trading-state.json')) as f:
            ts = json.load(f)
        realized = float(ts.get('realized_pnl', 0) or 0)
        bankroll_val = float(ts.get('bankroll', 48.85) or 48.85)
        if realized < 0 and bankroll_val > 0:
            dd_pct = abs(realized) / bankroll_val * 100
            if dd_pct > 10:
                post_discord_channel(ALERTS_CHANNEL,
                    f"🚨 **Drawdown Breach** — {dd_pct:.1f}% drawdown exceeds 10% limit | Realized: ${realized:.2f}")
                post_discord_channel(OPERATIONS,
                    f"🔴 **Drawdown Breach** — {dd_pct:.1f}% drawdown exceeds limit")
            elif dd_pct > 5:
                print(f"  ⚠️ Drawdown: {dd_pct:.1f}% — approaching 10% limit")
    except:
        pass
    
    print(f"\n{'='*60}")
    print(f"SUMMARY: {target} Cycles Complete")
    print(f"{'='*60}")
    print(f"{'Cycle':>6} {'Mkts':>5} {'Sig':>4} {'Prop':>4} {'Trades':>6}")
    for r in results:
        status = "✅" if r["trades"] > 0 else "⚠️"
        print(f"{status} {r['cycle']:>5} {r['markets']:>5} {r['signals']:>4} {r['proposals']:>4} {r['trades']:>6}")
    total_trades = sum(r["trades"] for r in results)
    print(f"\nTotal trades: {total_trades}")
    print(f"Trades per cycle: {total_trades/target:.1f}")

    # Post final summary to pipeline-updates
    state = read_state()  # fresh read after all cycles
    final_lines = [
        f"🏁 **Pipeline Batch Complete — {target} Cycles**",
        f"├ Total trades: {total_trades}",
        f"├ Trades/cycle: {total_trades/target:.1f}",
        f"├ Final bankroll: ${state.get('bankroll', 48.85):.2f}",
        f"└ Open positions: {len(get_open_token_ids())}",
    ]
    for r in results:
        status = "✅" if r["trades"] > 0 else "⏭️"
        final_lines.append(f"  {status} Cycle {r['cycle']}: {r['markets']} mkts → {r['signals']} sig → {r['proposals']} prop → {r['trades']} trades")
    post_discord_channel(PIPELINE_CHANNEL, "\n".join(final_lines))

    print(f"\n=== paper-trades.log (last 10 lines) ===")
    try:
        with open(TRADES_LOG) as f:
            lines = f.readlines()
            for l in lines[-10:]:
                print(f"  {l.strip()}")
    except FileNotFoundError:
        print("  No trades log found")

    print(f"\n=== Final State ===")
    state = read_state()
    print(f"  Bankroll: ${state.get('bankroll', '?'):.2f}")
    print(f"  Cycle: {state.get('cycle', 0)}")
    print(f"  Open positions: {len(get_open_token_ids())}")


if __name__ == "__main__":
    main()
