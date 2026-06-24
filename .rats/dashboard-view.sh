#!/bin/bash
while true; do
    clear
    echo "=========================================="
    echo "  RATS ON WALLSTREET — OPERATOR DASHBOARD"
    echo "=========================================="
    echo ""
    echo "Operating Mode: $(cat ~/.rats/operating-mode 2>/dev/null || echo 'NO FILE')"
    echo ""
    echo "--- Pipeline ---"
    cat ~/pipeline-state.json 2>/dev/null | python3 -c "
import sys,json
try:
    d=json.load(sys.stdin)
    print(f'  Cycle #      {d.get(chr(99)+chr(121)+chr(99)+chr(108)+chr(101),chr(63))}')
    print(f'  Mode         {d.get(chr(109)+chr(111)+chr(100)+chr(101),chr(63))}')
    print(f'  Bankroll     \${d.get(chr(98)+chr(97)+chr(110)+chr(107)+chr(114)+chr(111)+chr(108)+chr(108),chr(63))}')
    print(f'  Last update  {str(d.get(chr(108)+chr(97)+chr(115)+chr(116)+chr(95)+chr(117)+chr(112)+chr(100)+chr(97)+chr(116)+chr(101)+chr(100),chr(63)))[:19]}')
except: print('  Cannot read pipeline state')
"
    echo ""
    echo "--- Trading ---"
    cat ~/trading-state.json 2>/dev/null | python3 -c "
import sys,json
try:
    d=json.load(sys.stdin)
    b = d.get(chr(98)+chr(97)+chr(110)+chr(107)+chr(114)+chr(111)+chr(108)+chr(108),0) or 0
    c = d.get(chr(99)+chr(97)+chr(112)+chr(105)+chr(116)+chr(97)+chr(108)+chr(95)+chr(100)+chr(101)+chr(112)+chr(108)+chr(111)+chr(121)+chr(101)+chr(100),0) or 0
    print(f'  Capital:  \${c:.2f} / \${b:.2f}')
    print(f'  Open pos:  {d.get(chr(111)+chr(112)+chr(101)+chr(110)+chr(95)+chr(112)+chr(111)+chr(115)+chr(105)+chr(116)+chr(105)+chr(111)+chr(110)+chr(115)+chr(95)+chr(99)+chr(111)+chr(117)+chr(110)+chr(116),chr(63))}')
    print(f'  Real PnL:  \${d.get(chr(114)+chr(101)+chr(97)+chr(108)+chr(105)+chr(122)+chr(101)+chr(100)+chr(95)+chr(112)+chr(110)+chr(108),0):.2f}')
    print(f'  Unreal:    \${d.get(chr(117)+chr(110)+chr(114)+chr(101)+chr(97)+chr(108)+chr(105)+chr(122)+chr(101)+chr(100)+chr(95)+chr(112)+chr(110)+chr(108),0):.2f}')
except: print('  Cannot read trading state')
"
    echo ""
    echo "--- System ---"
    gw=$(systemctl --user is-active openclaw-gateway.service 2>/dev/null || echo "unknown")
    if [ "$gw" = "active" ]; then echo "  Gateway:  ✅ active"; else echo "  Gateway:  ❌ $gw"; fi
    tn=$(tmux ls 2>/dev/null | grep -c rats || echo 0)
    echo "  TMUX ops: $tn sessions"
    echo "  Host:     $(hostname)"
    df -h / | tail -1 | awk '{print "  Disk:     " $3 " used / " $2 " (" $5 ")"}'
    free -h | grep Mem | awk '{print "  Memory:   " $3 " used / " $2}'
    echo ""
    echo "TMUX sessions: $(tmux ls 2>/dev/null | grep rats | awk '{print $1}' | tr '\n' ' ')"
    echo ""
    echo "Last refresh: $(date '+%Y-%m-%d %H:%M:%S UTC')"
    echo "=========================================="
    sleep 30
done
