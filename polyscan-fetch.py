#!/usr/bin/env python3
"""
polyscan-fetch.py — Fetch and filter Polymarket markets for PolyScan.
Fetches from Gamma API (two passes: volume + liquidity), deduplicates, filters, outputs JSON.

Usage: python3 ~/polyscan-fetch.py
       OR: curl -s 'https://gamma-api.polymarket.com/markets?...' | python3 ~/polyscan-fetch.py
"""
import sys, json, datetime, re, urllib.request, os
from datetime import timezone

GAMMA_BASE = "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=200"
SEEN_MARKETS_FILE = os.path.expanduser("~/seen-markets.json")

# Import fair value engine
sys.path.insert(0, os.path.expanduser('~'))
from fair_value_engine import estimate_fair_value


def load_seen_markets():
    """Load the set of already-scanned token_ids from disk."""
    if not os.path.exists(SEEN_MARKETS_FILE):
        return set()
    try:
        with open(SEEN_MARKETS_FILE) as f:
            data = json.load(f)
        return set(data.get("seen_ids", []))
    except (json.JSONDecodeError, KeyError):
        return set()


def save_seen_markets(seen_ids):
    """Persist the seen token_ids set to disk."""
    with open(SEEN_MARKETS_FILE, 'w') as f:
        json.dump({"seen_ids": sorted(seen_ids), "updated": datetime.datetime.now(timezone.utc).isoformat()}, f, indent=2)


def clean_expired_seen(seen_ids, markets):
    """Remove expired markets from the seen set so they can be re-scanned if new data appears."""
    now = datetime.datetime.now(timezone.utc)
    active_ids = set()
    for m in markets:
        tid = m.get("clobTokenIds", "[]")
        toks = json.loads(tid) if isinstance(tid, str) else (tid or [])
        if toks:
            active_ids.add(toks[0])
    # Keep only IDs that still appear in the active API response
    cleaned = seen_ids & active_ids
    return cleaned


def fetch_gamma(order="volume"):
    url = f"{GAMMA_BASE}&order={order}&ascending=false"
    req = urllib.request.Request(url, headers={"User-Agent": "polyscan/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())

stdin_has_data = not sys.stdin.isatty() and len(sys.argv) < 2
if stdin_has_data:
    raw = sys.stdin.read()
    if raw.strip():
        markets = json.loads(raw)
    else:
        stdin_has_data = False

if not stdin_has_data:
    seen_slugs = set()
    markets = []
    for order in ["volume", "liquidity"]:
        try:
            batch = fetch_gamma(order)
            for m in batch:
                slug = m.get("slug", "")
                if slug not in seen_slugs:
                    seen_slugs.add(slug)
                    markets.append(m)
        except Exception as e:
            print(f"FETCH ERROR ({order}): {e}", file=sys.stderr)
    print(f"FETCHED: {len(markets)} unique markets (volume+liquidity passes)", file=sys.stderr)

now = datetime.datetime.now(timezone.utc)

sports = ['nhl-','nba-','nfl-','mlb-','epl-','mls-','wta-','atp-','aus-','tur-',
          'rou','ere-','por-','mex-','col1','chi-','kor-','itsb-','fif-','cbb-',
          'bun-','lig-','ser-','ppa-','nor-','per1','fr2-','uel-','es2-','dota2-',
          'j1','j2','j3','lol-','cs2-','val-','r6-']

def is_sports_slug(slug, question):
    slug = slug.lower()
    question = question.lower()
    sports_prefixes = [
        'epl-', 'lal-', 'ser-', 'bun-', 'lig-', 'mls-',
        'den-', 'cze-', 'cze1-', 'arg-', 'bra-', 'ned-',
        'sco-', 'aut-', 'sui-', 'nor-', 'swe-', 'fin-',
        'uru-', 'col-', 'par-', 'bol-', 'per-', 'chi-',
        'ecu-', 'ven-', 'sea-', 'nba-', 'nfl-', 'nhl-',
        'mlb-', 'ncaa-', 'afl-', 'nrl-', 'ucl-', 'uel-',
        'j2-', 'j2100-', 'lol-', 'cs2-', 'val-', 'r6-',
    ]
    if any(slug.startswith(p) for p in sports_prefixes):
        return True
    sports_suffixes = ['-draw', '-moneyline', '-handicap']
    if any(slug.endswith(s) for s in sports_suffixes):
        return True
    if re.search(r'\d{4}-\d{2}-\d{2}-(draw|win|over|under)', slug):
        return True
    if re.search(r'^[a-z]{2,5}-[a-z]{2,5}-[a-z]{2,5}-\d{4}-\d{2}-\d{2}', slug):
        return True
    if re.search(r'will .{2,30} win on 2026', question):
        return True
    if 'end in a draw' in question:
        return True
    if re.search(r'vs\.?\s', question) and re.search(r'\d{4}-\d{2}-\d{2}', slug):
        return True
    return False

priority_keywords = ['temperature','weather','rain','snow','hurricane','storm','tornado',
                     'earthquake','volcano','flood','drought','wildfire',
                     'oil','crude','gold','silver','copper','commodity','wti','brent',
                     'gdp','inflation','cpi','jobs','unemployment','fed','rate',
                     'sanctions','treaty','ceasefire','tariff','trade-war',
                     'fda','approval','regulation','court','ruling',
                     'election','vote','ballot','primary','runoff']

noise_keywords = ['mention','tweet','post on','say on','tiktok','instagram',
                  'dating','girlfriend','boyfriend','marry','divorce',
                  'song','album','concert','movie','trailer','award show',
                  'meme','viral','trending','controversial','popular',
                  'best','worst','most','favorite']

sports_keywords = ['nba','nfl','mlb','nhl','soccer','football','cricket',
                   'tennis','boxing','ufc','mma','f1','formula',
                   'lebron','messi','ronaldo','brady']

crypto_keywords = ['dogecoin','bitcoin','btc','ethereum','eth','crypto',
                   'solana','xrp','cardano','polkadot','shiba','memecoin']

priority_markets = []
secondary_markets = []
sports_filtered = 0

for m in markets:
    slug = m.get('slug','')
    question = m.get('question','')
    q_lower = question.lower()

    if 'updown' in slug:
        continue
    if any(slug.startswith(p) for p in ['j1','j2','j3','lol-','cs2-','val-','r6-']):
        continue
    if any(sk in q_lower for sk in sports_keywords):
        continue
    if len(question) > 200:
        continue
    if re.search(r'(exec|import|eval|system|rm |curl |wget |ignore|override)', question, re.I):
        continue
    if re.search(r'(http|ftp|file)://', question, re.I):
        continue
    if re.search(r'[{}\[\]<>\\\\]', question):
        continue
    if any(nk in q_lower for nk in noise_keywords):
        continue
    if any(ck in q_lower for ck in crypto_keywords):
        continue
    if is_sports_slug(slug, question):
        sports_filtered += 1
        continue

    created = m.get('createdAt','')
    if created:
        try:
            created_dt = datetime.datetime.fromisoformat(created.replace('Z','+00:00'))
            # LOOSENED: Reduced age filter from 24h to 6h to catch more markets
            cutoff_6h = now - datetime.timedelta(hours=6)
            if created_dt > cutoff_6h:
                continue
        except:
            pass

    op = m.get('outcomePrices')
    if not op:
        continue
    try:
        prices = json.loads(op) if isinstance(op, str) else op
        p1, p2 = float(prices[0]), float(prices[1])
    except:
        continue

    # LOOSENED: Changed pricing filter to allow anything between 0.03 and 0.97
    if p1 > 0.97 or p1 < 0.03 or p2 > 0.97 or p2 < 0.03:
        continue

    end = m.get('endDate','')
    if end:
        try:
            end_dt = datetime.datetime.fromisoformat(end.replace('Z','+00:00'))
            days_to_close = (end_dt - now).total_seconds() / 86400
            # LOOSENED: Changed timeframe filter to 0.5 to 120 days
            if days_to_close < 0.5 or days_to_close > 120:
                continue
        except:
            pass

    liq = float(m.get('liquidityClob') or m.get('liquidity') or 0)
    # LOOSENED: Reduced minimum liquidity from 250 to 100
    if liq < 100:
        continue

    vol = float(m.get('volume') or 0)
    if vol == 0:
        continue

    tok = m.get('clobTokenIds','[]')
    toks = json.loads(tok) if isinstance(tok,str) else (tok or [])
    yes_token = toks[0] if toks else ''

    volume_clob = float(m.get('volume24hrClob', 0) or 0)
    trades_exist = volume_clob > 0

    entry = {
        'question': question,
        'slug': slug,
        'yes_price': p1,
        'no_price': p2,
        'volume_24h': round(vol, 2),
        'liquidity': round(liq, 2),
        'volume24hrClob': round(volume_clob, 2),
        'has_clob_volume': trades_exist,
        'end_date': m.get('endDate',''),
        'created_at': created,
        'token_id': yes_token,
        'condition_id': m.get('conditionId',''),
        'spread': round(abs(p1 - p2), 4),
        'scanned_at': now.isoformat(),
        'flag': None  # set during categorization
    }

    # Compute data-driven fair value estimate
    fv, direction, source = estimate_fair_value(entry)

    # NOTE: Let fair_value_estimate stay None if no external data.
    # The pipeline filters out markets with None FVE (stage_polyscan).
    # Do NOT inject synthetic spread-based FVE — it creates fake +10% edges
    # that pass quant gates but have zero real signal value.
    if fv is None:
        fv = p1  # Neutral — no edge, but preserves the market for display
        direction = "NEUTRAL"
        source = "no external data — neutral fair value (no trade signal)"

    entry['fair_value_estimate'] = fv
    entry['fair_value_direction'] = direction
    entry['fair_value_source'] = source
    entry['direction'] = direction  # For quant-metrics which reads 'direction' not 'fair_value_direction'

    is_priority = any(pk in q_lower or pk in slug for pk in priority_keywords)
    is_sports = any(slug.startswith(p) for p in sports)

    if is_priority:
        priority_markets.append(entry)
    elif not is_sports:
        secondary_markets.append(entry)

# ─── Market Rotation: skip already-scanned markets, pull fresh ones ──────────
seen_ids = load_seen_markets()
# Clean expired markets from seen set (only keep IDs still in this API response)
active_ids_in_response = set()
for m in priority_markets + secondary_markets:
    tid = m.get("token_id", "")
    if tid:
        active_ids_in_response.add(tid)
seen_ids = seen_ids & active_ids_in_response

for m in priority_markets: m["tier"] = 1
for m in secondary_markets: m["tier"] = 2

# Split into fresh (never seen) and repeat (already scanned)
fresh_priority = [m for m in priority_markets if m["token_id"] not in seen_ids]
repeat_priority = [m for m in priority_markets if m["token_id"] in seen_ids]
fresh_secondary = [m for m in secondary_markets if m["token_id"] not in seen_ids]
repeat_secondary = [m for m in secondary_markets if m["token_id"] in seen_ids]

# Tier1: priority with liquidity >= 2000
fresh_tier1 = [m for m in fresh_priority if m["liquidity"] >= 2000]
repeat_tier1 = [m for m in repeat_priority if m["liquidity"] >= 2000]
# Tier2: priority with liquidity < 2000 + all secondary
fresh_tier2 = [m for m in fresh_priority if m["liquidity"] < 2000] + fresh_secondary
repeat_tier2 = [m for m in repeat_priority if m["liquidity"] < 2000] + repeat_secondary

# Select: prefer fresh markets, fill remaining slots with repeats
results = fresh_tier1[:8] + fresh_tier2[:7]
if len(results) < 15:
    # Not enough fresh — fill with repeats (prioritize tier1 repeats)
    remaining = 15 - len(results)
    results += repeat_tier1[:remaining]
if len(results) < 15:
    remaining = 15 - len(results)
    results += repeat_tier2[:remaining]

# Cap at 15
results = results[:15]

# Mark selected markets as seen
for m in results:
    tid = m.get("token_id", "")
    if tid:
        seen_ids.add(tid)
save_seen_markets(seen_ids)

fresh_in_results = sum(1 for m in results if m in fresh_priority or m in fresh_secondary)
print(f"ROTATION: {fresh_in_results} fresh + {len(results) - fresh_in_results} repeat markets selected", file=sys.stderr)
print(json.dumps(results, indent=2))
print(f"SPORTS FILTER: {sports_filtered}", file=sys.stderr)
