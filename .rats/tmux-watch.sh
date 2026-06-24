#!/bin/bash
# TMUX operator console helper — creates all rats-* sessions

create_session() {
    local name="$1"
    local cmd="$2"
    tmux kill-session -t "$name" 2>/dev/null
    tmux new-session -d -s "$name" "bash -c '$cmd; exec bash'"
    if tmux has-session -t "$name" 2>/dev/null; then
        echo "  ✅ $name"
    else
        echo "  ❌ $name (failed)"
    fi
}

echo "Building TMUX Operator Console..."

# rats-pipeline — pipeline state + log
SESSION="rats-pipeline"
tmux kill-session -t "$SESSION" 2>/dev/null
tmux new-session -d -s "$SESSION" -n log "tail -f ~/logs/pipeline-cron.log 2>/dev/null || echo 'Waiting for pipeline log...'"
tmux new-window -t "$SESSION" -n state 'while true; do clear; echo "=== PIPELINE STATE ==="; cat ~/.rats/operating-mode 2>/dev/null && echo "" || echo "mode: unknown"; cat ~/pipeline-state.json 2>/dev/null | python3 -c "
import sys,json
try:
    d=json.load(sys.stdin)
    print(f\"Cycle: {d.get('cycle','?')} | Mode: {d.get('mode','?')} | Bankroll: \${d.get('bankroll','?')}\")
    print(f\"Last updated: {str(d.get('last_updated','?'))[:19]}\")
    ps=d.get('polyscan',{})or{}
    ww=d.get('whalewatch',{})or{}
    pb=d.get('polybrain',{})or{}
    pe=d.get('polyexec',{})or{}
    print(f\"PolyScan: {len(ps.get('markets',[]))} markets\")
    print(f\"WhaleWatch: {len(ww.get('signals',[]))} signals\")
    print(f\"PolyBrain: {len(pb.get('proposals',[]) if isinstance(pb,dict) else [])} proposals\")
    print(f\"PolyExec: {pe.get('status','?')}\")
except: print('Cannot read pipeline state')
"; sleep 30; done'
echo "  ✅ $SESSION"

# rats-gateway — gateway logs
create_session "rats-gateway" "journalctl --user -u openclaw-gateway.service -n 30 -f --no-pager 2>/dev/null | head -200"

# rats-risk — risk desk
SESSION="rats-risk"
tmux kill-session -t "$SESSION" 2>/dev/null
tmux new-session -d -s "$SESSION" -n risk 'while true; do clear; echo "=== RISK DESK ==="; echo "Mode: $(cat ~/.rats/operating-mode 2>/dev/null || echo unknown)"; echo ""; cat ~/trading-state.json 2>/dev/null | python3 -c "
import sys,json
try:
    d=json.load(sys.stdin)
    print(f\"Bankroll: \${d.get('bankroll','?')}\")
    print(f\"Capital deployed: \${d.get('capital_deployed','?')}\")
    print(f\"Open positions: {d.get('open_positions_count','?')}\")
    print(f\"Closed positions: {d.get('closed_positions_count','?')}\")
    print(f\"Realized PnL: \${d.get('realized_pnl','?')}\")
    print(f\"Unrealized PnL: \${d.get('unrealized_pnl','?')}\")
    print(f\"Equity: \${d.get('equity','?')}\")
except: print('Cannot read trading state')
"; echo ""; echo "--- Limits ---"; echo "Max drawdown: 10% (configurable)"; echo "Exposure cap: configurable"; echo "---"; echo "Press Ctrl+C to exit"; sleep 60; done'
echo "  ✅ $SESSION"

# rats-ledger — trade journal + trading state
SESSION="rats-ledger"
tmux kill-session -t "$SESSION" 2>/dev/null
tmux new-session -d -s "$SESSION" -n trades "tail -f ~/paper-trades.log 2>/dev/null || echo 'No paper-trades.log yet — waiting for trades...'"
tmux new-window -t "$SESSION" -n state 'while true; do clear; echo "=== TRADING STATE ==="; cat ~/trading-state.json 2>/dev/null | python3 -c "
import sys,json
try:
    d=json.load(sys.stdin)
    print(f\"Snapshot: {d.get('generated_at','?')[:19]}\")
    print(f\"Bankroll: \${d.get('bankroll','?')}\")
    print(f\"Deployed: \${d.get('capital_deployed','?')}\")
    print(f\"Equity: \${d.get('equity','?')}\")
    print(f\"Trades tracked: {d.get('total_positions_tracked',d.get('total_trades','?'))}\")
    print(f\"Wins: {d.get('wins','?')} | Losses: {d.get('losses','?')}\")
except: print('Cannot read trading state')
"; echo ""; echo "Waiting for updates..."; sleep 30; done'
echo "  ✅ $SESSION"

# rats-dashboard — overview dashboard
SESSION="rats-dashboard"
tmux kill-session -t "$SESSION" 2>/dev/null
tmux new-session -d -s "$SESSION" -n status 'while true; do clear; echo "========================================"; echo "  RATS ON WALLSTREET — OPERATOR DASHBOARD"; echo "========================================"; echo ""; echo "Operating Mode: $(cat ~/.rats/operating-mode 2>/dev/null || echo NO FILE)"; echo ""; echo "--- Pipeline ---"; cat ~/pipeline-state.json 2>/dev/null | python3 -c "
import sys,json
try:
    d=json.load(sys.stdin)
    print(f\"  Cycle #{d.get('cycle','?')} | Mode: {d.get('mode','?')}\")
    print(f\"  Bankroll: \${d.get('bankroll','?')}\")
    print(f\"  Last: {str(d.get('last_updated','?'))[:19]}\")
except: print('  No data')
"; echo ""; echo "--- Trading ---"; cat ~/trading-state.json 2>/dev/null | python3 -c "
import sys,json
try:
    d=json.load(sys.stdin)
    print(f\"  Capital: \${d.get('capital_deployed','?')} / \${d.get('bankroll','?')}\")
    print(f\"  Open: {d.get('open_positions_count','?')} | Closed: {d.get('closed_positions_count','?')}\")
    print(f\"  Realized PnL: \${d.get('realized_pnl','?')} | Unrealized: \${d.get('unrealized_pnl','?')}\")
except: print('  No data')
"; echo ""; echo "--- System ---"; echo "  Gateway: $(systemctl --user is-active openclaw-gateway.service 2>/dev/null || echo unknown)"; echo "  TMUX sessions: $(tmux ls 2>/dev/null | grep rats | wc -l)"; echo ""; echo "========================================"; sleep 30; done'
echo "  ✅ $SESSION"

echo ""
echo "=== TMUX Operator Console Ready ==="
tmux ls 2>/dev/null
