#!/bin/bash
while true; do
    clear
    echo "========================================"
    echo "  RISK DESK"
    echo "========================================"
    echo "Mode: $(cat ~/.rats/operating-mode 2>/dev/null || echo 'NO FILE')"
    echo ""
    echo "--- Position Limits ---"
    cat ~/trading-state.json 2>/dev/null | python3 -c "
import sys,json
try:
    d=json.load(sys.stdin)
    bankroll = d.get('bankroll',0) or 0
    deployed = d.get('capital_deployed',0) or 0
    open_c = d.get('open_positions_count',0) or 0
    realized = d.get('realized_pnl',0) or 0
    unrealized = d.get('unrealized_pnl',0) or 0
    print(f'Bankroll:       \${bankroll:.2f}')
    print(f'Deployed:       \${deployed:.2f}  ({deployed/bankroll*100:.1f}% of bankroll)' if bankroll>0 else f'Deployed:       \${deployed:.2f}')
    print(f'Open positions: {open_c}')
    print(f'Realized PnL:   \${realized:.2f}')
    print(f'Unrealized PnL: \${unrealized:.2f}')
    dd = deployed/bankroll*100 if bankroll>0 else 0
    print(f'')
    if dd > 75: print('⚠️ WARNING: Over 75% capital deployed — high exposure')
    elif dd > 50: print('🟡 Advisory: Over 50% deployed — monitor closely')
    else: print('🟢 Capital deployment within limits')
except Exception as e:
    print(f'Cannot read trading state: {e}')
"
    echo ""
    echo "--- Drawdown Check ---"
    cat ~/trading-state.json 2>/dev/null | python3 -c "
import sys,json
try:
    d=json.load(sys.stdin)
    r = d.get('realized_pnl',0) or 0
    bankroll = d.get('bankroll',0) or 0
    if bankroll>0:
        dd = abs(r)/bankroll*100 if r<0 else 0
        print(f'Drawdown from realized losses: {dd:.1f}%')
        if dd > 10: print('🔴 BREACH: Drawdown exceeds 10% limit!')
        elif dd > 5: print('🟡 Warning: Approaching drawdown limit')
        else: print('🟢 Drawdown within limits')
except: print('Cannot calculate drawdown')
"
    echo ""
    echo "Last updated: $(date '+%Y-%m-%d %H:%M:%S UTC')"
    echo "========================================"
    sleep 30
done
