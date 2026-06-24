#!/home/tyreseN/polyenv/bin/python3
"""
Pipeline helper — CLI for state management that agents can call via exec.
Usage:
  pipeline-helper.py read                     → print pipeline state
  pipeline-helper.py read-polyscan            → print polyscan markets (count + slugs)
  pipeline-helper.py read-whalewatch          → print whalewatch signals summary
  pipeline-helper.py read-polybrain           → print polybrain proposals
  pipeline-helper.py run-polyscan             → run polyscan-fetch.py and write to state
  pipeline-helper.py run-quant                → run quant-metrics.py on polyscan data, write to state
  pipeline-helper.py run-polyexec             → run polyexec_execute.py
  pipeline-helper.py status                   → print pipeline health
  pipeline-helper.py cycle-summary            → print clean cycle summary
"""

import json
import os
import subprocess
import sys
import datetime
import fcntl
import time

HOME = os.path.expanduser("~")
PIPELINE_STATE = os.path.join(HOME, "pipeline-state.json")
PIPELINE_LOCK = PIPELINE_STATE + ".lock"
PYTHON = os.path.join(HOME, "polyenv", "bin", "python3")


def acquire_lock(timeout=30):
    """Acquire an exclusive lock on the pipeline state file with timeout."""
    lockfile = open(PIPELINE_LOCK, "w")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return lockfile
        except (IOError, OSError):
            time.sleep(0.1)
    # Fallback: blocking lock (will wait indefinitely)
    fcntl.flock(lockfile, fcntl.LOCK_EX)
    return lockfile


def release_lock(lockfile):
    """Release the lock."""
    fcntl.flock(lockfile, fcntl.LOCK_UN)
    lockfile.close()
    try:
        os.unlink(PIPELINE_LOCK)
    except (FileNotFoundError, OSError):
        pass


def load_state():
    lk = acquire_lock()
    try:
        with open(PIPELINE_STATE) as f:
            return json.load(f)
    finally:
        release_lock(lk)


def save_state(state):
    lk = acquire_lock()
    try:
        with open(PIPELINE_STATE, "w") as f:
            json.dump(state, f, indent=2)
    finally:
        release_lock(lk)


def cmd_read():
    state = load_state()
    ps = state.get("polyscan", {})
    ww = state.get("whalewatch", {})
    pb = state.get("polybrain", {})
    pe = state.get("polyexec", {})
    print(f"Pipeline State")
    print(f"==============")
    br = state.get('bankroll', '?')
    if isinstance(br, (int, float)):
        print(f"Bankroll: ${br:.2f}")
    else:
        print(f"Bankroll: ${br}")
    print(f"Mode: {state.get('mode', '?')}")
    print(f"Last updated: {state.get('last_updated', '?')}")
    print()
    print(f"PolyScan:   {ps.get('status', 'idle')} | {len(ps.get('markets', []))} markets")
    print(f"WhaleWatch: {ww.get('status', 'idle')} | {len(ww.get('signals', []))} signals")
    print(f"PolyBrain:  {pb.get('status', 'idle')} | {len(pb.get('proposals', []))} proposals")
    print(f"PolyExec:   {pe.get('status', 'idle')} | {pe.get('last_result', '')}")


def cmd_read_polyscan():
    state = load_state()
    markets = state.get("polyscan", {}).get("markets", [])
    print(f"MARKETS: {len(markets)}")
    for m in markets:
        fve = m.get("fair_value_estimate", 0.5)
        yp = m.get("yes_price", 0.5)
        edge = round((fve - yp) / yp * 100, 1) if yp > 0 else 0
        print(f"  {m.get('question','?')[:55]}")
        print(f"    yes=${yp:.4f} | fve=${fve:.4f} | edge={edge}% | vol=${m.get('volume_24h',0):.0f}")


def cmd_read_whalewatch():
    state = load_state()
    signals = state.get("whalewatch", {}).get("signals", [])
    print(f"SIGNALS: {len(signals)}")
    for s in signals:
        print(f"  {s.get('question','?')[:55]}")
        print(f"    dir={s.get('direction','?')} | ev={s.get('ev',0):.2f} | kelly=${s.get('kelly_size',0):.2f} | skip={s.get('skip')} | flags={' '.join(s.get('flags',[]))}")


def cmd_read_polybrain():
    state = load_state()
    proposals = state.get("polybrain", {}).get("proposals", [])
    print(f"PROPOSALS: {len(proposals)}")
    for p in proposals:
        print(f"  {p.get('market','?')[:55]}")
        print(f"    {p.get('direction','?')} | entry=${p.get('entry_price',0):.4f} | edge={p.get('edge',0)}% | size=${p.get('size_usd',0):.2f} | conf={p.get('confidence','?')}")


def cmd_run_polyscan():
    result = subprocess.run([PYTHON, os.path.join(HOME, "polyscan-fetch.py")],
                           capture_output=True, text=True, timeout=90)
    if result.returncode != 0:
        print(f"ERROR: {result.stderr[:200]}")
        return 1

    import re
    match = re.search(r'\[.*\]', result.stdout, re.DOTALL)
    if not match:
        print("ERROR: No market data in output")
        return 1

    markets = json.loads(match.group())
    state = load_state()
    state["polyscan"] = {
        "status": "complete",
        "last_run": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "markets": [{
            "question": m.get("question"),
            "slug": m.get("slug"),
            "token_id": m.get("token_id"),
            "yes_price": m.get("yes_price"),
            "no_price": m.get("no_price"),
            "fair_value_estimate": m.get("fair_value_estimate"),
            "fair_value_direction": m.get("fair_value_direction"),
            "volume_24h": m.get("volume_24h"),
            "liquidity": m.get("liquidity"),
        } for m in markets]
    }
    state["whalewatch"] = {"status": "idle", "signals": []}
    state["polybrain"] = {"status": "idle", "proposals": [], "notes": ""}
    state["polyexec"] = {"status": "idle", "last_result": ""}
    state["last_updated"] = datetime.datetime.now(datetime.timezone.utc).isoformat()

    save_state(state)

    print(f"✅ PolyScan: {len(markets)} markets written to state")
    for m in markets[:5]:
        fve = m.get("fair_value_estimate", 0.5)
        yp = m.get("yes_price", 0.5)
        edge = round((fve - yp) / yp * 100, 1) if yp > 0 else 0
        print(f"  {m.get('question','?')[:50]} | yes=${yp:.4f} | edge={edge}%")


def cmd_run_quant():
    state = load_state()
    markets = state.get("polyscan", {}).get("markets", [])
    if not markets:
        print("SKIP: No markets to analyze")
        return 0

    quant_input = json.dumps({
        "markets": [{
            "slug": m.get("slug", ""),
            "question": m.get("question", ""),
            "yes_price": m.get("yes_price", 0.5),
            "no_price": 1.0 - float(m.get("yes_price", 0.5)),
            "fair_value_estimate": m.get("fair_value_estimate", 0.5),
            "direction": m.get("fair_value_direction", "YES"),
            "token_id": m.get("token_id", ""),
        } for m in markets],
        "bankroll": state.get("bankroll", 45.47),
        "trade_history": {"current_streak": 0, "streak_type": "loss"},
    })

    result = subprocess.run(
        [PYTHON, os.path.join(HOME, "quant-metrics.py")],
        input=quant_input, capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        print(f"ERROR: quant-metrics.py failed: {result.stderr[:200]}")
        return 1

    quant = json.loads(result.stdout)
    tradeable = [r for r in quant.get("results", []) if r.get("tradeable")]

    signals = []
    proposals = []
    for r in tradeable:
        signals.append({
            "question": r["question"],
            "slug": r["slug"],
            "yes_price": r["yes_price"],
            "fair_value": r["fair_value"],
            "direction": r["direction"],
            "ev": r["ev"],
            "kelly_size": r["kelly"]["size_usd"],
            "kelly_fraction": r["kelly"]["fraction"],
            "skip": r["kelly"]["skip"],
            "flags": r.get("flags", []),
        })
        if not r["kelly"]["skip"]:
            edge_pct = round((r["fair_value"] - r["yes_price"]) / r["yes_price"] * 100, 1)
            if edge_pct < 15.0:
                continue
            proposals.append({
                "market": r["question"],
                "slug": r["slug"],
                "token_id": r.get("token_id", ""),
                "direction": f"BUY {r['direction']}",
                "entry_price": r["yes_price"],
                "edge": edge_pct,
                "size_usd": r["kelly"]["size_usd"],
                "est_prob": round(r["fair_value"] * 100, 1),
                "strategy_id": "general-quant-v1",
                "confidence": "HIGH" if r["ev"] > 1.0 else "MED",
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            })

    state["whalewatch"] = {
        "status": "complete",
        "last_run": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "signals": signals,
    }
    state["polybrain"] = {
        "status": "complete",
        "last_run": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "markets_analyzed": quant.get("markets_analyzed"),
        "signals_available": len(tradeable),
        "decision": "PROPOSAL_POSTED",
        "proposals": proposals,
    }
    state["last_updated"] = datetime.datetime.now(datetime.timezone.utc).isoformat()

    save_state(state)

    print(f"✅ WhaleWatch: {len(signals)} signals, {len(proposals)} proposals written")
    for p in proposals[:5]:
        print(f"  {p['market'][:50]} | {p['direction']} | edge={p['edge']}% | ${p['size_usd']:.2f}")
    return 0


def cmd_run_polyexec():
    result = subprocess.run(
        [PYTHON, os.path.join(HOME, ".openclaw", "workspace", "polyexec", "polyexec_execute.py")],
        capture_output=True, text=True, timeout=120
    )
    if result.returncode != 0:
        for line in result.stdout.split("\n"):
            print(line)
        for line in result.stderr.split("\n"):
            if line.strip():
                print(f"STDERR: {line}")

    try:
        with open(os.path.join(HOME, "paper-trades.log")) as f:
            lines = f.readlines()
        recent = lines[-10:] if len(lines) > 10 else lines
        print(f"✅ PolyExec complete. Recent paper trades:")
        for line in recent:
            try:
                t = json.loads(line.strip())
                status = t.get("status", "?")
                q = t.get("question", "?")[:40]
                exposure = t.get("usd_exposure", 0)
                if status != "PAPER_LOGGED":
                    reason = t.get("failure_reason", t.get("reason", ""))
                    print(f"  {q} | FAIL: {reason[:50]}")
                else:
                    print(f"  {q} | ${exposure:.2f} | {status}")
            except:
                pass
    except FileNotFoundError:
        print("No paper-trades.log found")
    return 0


def cmd_status():
    state = load_state()
    ps = state.get("polyscan", {})
    ww = state.get("whalewatch", {})
    pb = state.get("polybrain", {})
    pe = state.get("polyexec", {})
    print(f"📊 PIPELINE STATUS")
    print(f"=================")
    br = state.get('bankroll', '?')
    if isinstance(br, (int, float)):
        print(f"💰 Bankroll: ${br:.2f}")
    else:
        print(f"💰 Bankroll: ${br}")
    print(f"📋 Mode: {state.get('mode', '?')}")
    print(f"🔄 Last full cycle: {state.get('last_updated', '?')}")
    print()
    print(f"  PolyScan  : {ps.get('status', 'idle'):>8} | {len(ps.get('markets', []))} markets")
    print(f"  WhaleWatch: {ww.get('status', 'idle'):>8} | {len(ww.get('signals', []))} signals")
    print(f"  PolyBrain : {pb.get('status', 'idle'):>8} | {len(pb.get('proposals', []))} proposals")
    print(f"  PolyExec  : {pe.get('status', 'idle'):>8} | {pe.get('last_result', '')[:30]}")
    try:
        with open(os.path.join(HOME, "paper-trades.log")) as f:
            count = sum(1 for _ in f)
        print(f"  📝 Paper trades logged: {count}")
    except:
        pass


def cmd_cycle_summary():
    state = load_state()
    ps = state.get("polyscan", {})
    pb = state.get("polybrain", {})
    pe = state.get("polyexec", {})

    markets = len(ps.get("markets", []))
    proposals = pb.get("proposals", [])
    tradeable = len([p for p in proposals if p.get("edge", 0) >= 15.0])

    print(f"📋 CYCLE SUMMARY")
    print(f"================")
    print(f"Time: {state.get('last_updated', '?')[:19]} UTC")
    br = state.get('bankroll', '?')
    if isinstance(br, (int, float)):
        print(f"Bankroll: ${br:.2f}")
    else:
        print(f"Bankroll: ${br}")
    print(f"Mode: {state.get('mode', 'paper')}")
    print()

    if markets > 0:
        print(f"🔍 PolyScan: {markets} markets scanned")
        for m in ps.get("markets", [])[:3]:
            fve = m.get("fair_value_estimate", 0.5)
            yp = m.get("yes_price", 0.5)
            edge = round((fve - yp) / yp * 100, 1) if yp > 0 else 0
            print(f"  • {m.get('question','?')[:45]} — edge {edge}%")
    else:
        print(f"🔍 PolyScan: No markets found")

    print()
    if tradeable > 0:
        print(f"🧠 PolyBrain: {len(proposals)} tradeable proposals")
        for p in proposals[:3]:
            print(f"  • {p.get('market','?')[:45]} — {p.get('direction','?')} @ ${p.get('entry_price',0):.4f} — edge {p.get('edge',0)}%")
    else:
        print(f"🧠 PolyBrain: No proposals")

    print()
    print(f"⚡ PolyExec: {pe.get('last_result', 'No recent execution')[:60]}")

    try:
        with open(os.path.join(HOME, "paper-trades.log")) as f:
            count = sum(1 for _ in f)
        print(f"📝 Total paper trades logged: {count}")
    except:
        pass


if __name__ == "__main__":
    cmds = {
        "read": cmd_read,
        "read-polyscan": cmd_read_polyscan,
        "read-whalewatch": cmd_read_whalewatch,
        "read-polybrain": cmd_read_polybrain,
        "run-polyscan": cmd_run_polyscan,
        "run-quant": cmd_run_quant,
        "run-polyexec": cmd_run_polyexec,
        "status": cmd_status,
        "cycle-summary": cmd_cycle_summary,
    }

    if len(sys.argv) < 2 or sys.argv[1] not in cmds:
        print(__doc__.strip())
        sys.exit(1)

    sys.exit(cmds[sys.argv[1]]())
