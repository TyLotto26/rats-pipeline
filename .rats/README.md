# 🐀 Rats on Wallstreet — TMUX Operator Console

## Quick Start
```bash
# See all running sessions
tmux ls

# Attach to a session
tmux attach -t rats-pipeline
tmux attach -t rats-gateway
tmux attach -t rats-risk
tmux attach -t rats-ledger
tmux attach -t rats-dashboard

# Kill a session
tmux kill-session -t rats-pipeline

# Rebuild all sessions (if killed)
bash ~/.rats/tmux-watch.sh
```

## Session Map

| Session | What it shows | Windows |
|---------|--------------|---------|
| `rats-pipeline` | Pipeline cron log + pipeline state | `log`, `stages` |
| `rats-gateway` | OpenClaw gateway journal | `journal` |
| `rats-risk` | Risk desk — bankroll, PnL, limits | `risk` |
| `rats-ledger` | Trade journal + trading state | `trades`, `state` |
| `rats-dashboard` | Full hedge fund overview | `status` |

## TMUX Cheats
- `Ctrl+B 1` → switch to window 1
- `Ctrl+B d` → detach (leave session running)
- `Ctrl+B [` → scroll mode (q to quit)
- `Ctrl+B s` → interactive session picker
- `Ctrl+B ,` → rename current window

## Operating Mode
Current mode: `live`
File location: `~/.rats/operating-mode`

| Mode | Effect |
|------|--------|
| `live` | Full trading — pipeline runs normally |
| `hold` | No new signals, drain existing positions |
| `pause` | Freeze everything, gateway stays alive |
| `dead` | Emergency stop, gateways can terminate |

To change mode: `echo "hold" > ~/.rats/operating-mode`
