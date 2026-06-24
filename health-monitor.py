#!/usr/bin/env python3
"""health-monitor.py — no_agent, pure python. Cron-friendly. Posts to #operations."""
import json, os, subprocess, urllib.request
from datetime import datetime

CHANNEL = '1481421201107582987'  # 💻operations
TOKEN_FILE = os.path.expanduser('~/openclaw-vault/_hermes/workspaces/nicole-discord/.discord_token')
STATE = os.path.expanduser('~/pipeline-state.json')
MODE_FILE = os.path.expanduser('~/.rats/operating-mode')

def http_code(url):
    try:
        r = subprocess.run(['curl', '-s', '-o', '/dev/null', '-w', '%{http_code}', '--max-time', '3', url],
                         capture_output=True, text=True, timeout=5)
        return r.stdout.strip()
    except:
        return '000'

def main():
    d4 = http_code('http://127.0.0.1:4000/')
    d9 = http_code('http://127.0.0.1:9100/')
    d4s = '\U0001f7e2' if d4 == '200' else '\U0001f534'
    d9s = '\U0001f7e2' if d9 == '200' else '\U0001f534'

    try:
        with open(STATE) as f:
            d = json.load(f)
        cycle = d.get('cycle', '?')
        ts_raw = d.get('last_updated', '')
        bankroll = d.get('bankroll', '?')
        dry_streak = d.get('dry_streak', 0)
        pb = d.get('polybrain', {}) or {}
        pr_raw = pb.get('proposals', []) if isinstance(pb, dict) else []
        pc = len(pr_raw) if isinstance(pr_raw, list) else (pr_raw if isinstance(pr_raw, (int, float)) else 0)
        ww = d.get('whalewatch', {}) or {}
        sigs = len(ww.get('signals', [])) if isinstance(ww, dict) else 0
        pe = d.get('polyexec', {}) or {}
        exec_st = pe.get('status', pe.get('last_result', 'idle')) if isinstance(pe, dict) else '?'
    except:
        cycle, bankroll, dry_streak, pc, sigs, exec_st, ts_raw = '?', '?', '?', '?', '?', '?', ''

    age_mins = 999
    if ts_raw:
        try:
            updated = datetime.fromisoformat(ts_raw).timestamp()
            age_mins = int((time.time() - updated) / 60)
        except:
            pass

    if age_mins > 40:
        age_msg = f'\u23f0 **STALE** - {age_mins}m idle'
    else:
        age_msg = f'\U0001f7e2 {age_mins}m since last cycle'

    if pc == 0 or pc == '?':
        prop_msg = f'\u26a0\ufe0f **0 proposals** ({sigs} signals, no edge)'
    else:
        prop_msg = f'\U0001f7e2 {pc} proposals ({sigs} signals)'

    # Read operating mode
    try:
        with open(MODE_FILE) as f:
            op_mode = f.read().strip().upper()
    except:
        op_mode = 'LIVE'
    
    mode_symbol = {'LIVE': '\U0001f7e2', 'HOLD': '\U0001f7e1', 'PAUSE': '\U0001f534', 'DEAD': '\u26d4'}.get(op_mode, '\u2753')
    
    msg = (
        f'\U0001f9a0 **Ops Desk**'
        f' | Mode: {mode_symbol} {op_mode}'
        f' | Cycle: {cycle}'
        f' | Bankroll: ${bankroll}'
        f' | {age_msg}\n'
        f'Dashboards: Trade {d4s} OS {d9s}'
        f' | Pipeline: {prop_msg}'
        f' | Exec: {exec_st}'
    )

    with open(TOKEN_FILE) as f:
        token = f.read().strip()

    url = f'https://discord.com/api/v10/channels/{CHANNEL}/messages'
    payload = json.dumps({'content': msg}).encode('utf-8')
    req = urllib.request.Request(url, data=payload, method='POST')
    req.add_header('Authorization', f'Bot {token}')
    req.add_header('Content-Type', 'application/json; charset=utf-8')
    req.add_header('User-Agent', 'rats-health-monitor/1.0')
    resp = urllib.request.urlopen(req)
    print(f'Posted ({resp.status})')
    print(msg)

if __name__ == '__main__':
    main()
