#!/home/tyreseN/polyenv/bin/python3
"""
quant-metrics.py — Deterministic math engine for Rats on Wallstreet
Callable via exec by pipeline agents.

Input (stdin or argv[1]): JSON with structure:
  {
    "markets": [
      {
        "slug": "market-slug",
        "question": "...",
        "yes_price": 0.35,
        "no_price": 0.65,
        "fair_value_estimate": 0.50,
        "direction": "YES",
        "token_id": "123..."       (optional — enables CLOB metrics)
      }
    ],
    "bankroll": 50.0,
    "trade_history": {
      "current_streak": 0,
      "streak_type": "loss"
    }
  }

Output (stdout): JSON with quant metrics per market.
NEVER prints credential values.
"""

import sys
import json
import os
import math
import socket
import re

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fair_value_engine import estimate_fair_value

# ─── Hard Gate Constants ────────────────────────────────────────────────────
OBI_TOXIC_THRESHOLD  = 0.70   # |OBI| > 0.70 → toxic order book
SPREAD_TOXIC_PCT     = 0.15   # spread/mid > 15% → toxic
DEPTH_RATIO_FLAG     = 5.0    # bid_depth / ask_depth > 5:1 or < 1:5 → flagged
MAX_TRADE_USD        = 2.50   # absolute hard cap
MAX_BANKROLL_PCT     = 0.02   # 2% of bankroll
MIN_TRADE_USD        = 0.10   # not worth gas below this
CLOB_HOST            = "https://clob.polymarket.com"
CHAIN_ID             = 137

# ─── Parse Input ────────────────────────────────────────────────────────────
try:
    if len(sys.argv) > 1:
        payload = json.loads(sys.argv[1])
    else:
        payload = json.load(sys.stdin)
except json.JSONDecodeError as e:
    print(json.dumps({"error": f"invalid_input: {e}"}))
    sys.exit(1)

markets      = payload.get("markets", [])
bankroll     = float(payload.get("bankroll", 50.0))
trade_hist   = payload.get("trade_history", {})
streak       = trade_hist.get("current_streak", 0) if trade_hist.get("streak_type") == "loss" else 0

# ─── Load Credentials (never printed) ──────────────────────────────────────
env_path = os.path.expanduser("~/.openclaw/.env")
creds_loaded = False
client = None

try:
    env_vars = {}
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env_vars[k.strip()] = v.strip().strip('"').strip("'")

    api_key        = env_vars.get("POLY_API_KEY", "")
    api_secret     = env_vars.get("POLY_SECRET", "")
    api_passphrase = env_vars.get("POLY_PASSPHRASE", "")
    private_key    = env_vars.get("POLY_PRIVATE_KEY", "")

    if api_key and api_secret and api_passphrase and private_key:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds

        creds = ApiCreds(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
        )
        client = ClobClient(
            host=CLOB_HOST,
            chain_id=CHAIN_ID,
            key=private_key,
            creds=creds,
            signature_type=2,
        )
        creds_loaded = True
except Exception:
    creds_loaded = False


# ─── Metrics Functions ──────────────────────────────────────────────────────

def _to_dicts(entries):
    """Normalise OrderSummary objects or raw dicts to {"price": str, "size": str}."""
    if not entries:
        return []
    if hasattr(entries[0], "price"):
        return [{"price": e.price, "size": e.size} for e in entries]
    return entries


def fetch_order_book(token_id):
    """Fetch CLOB order book via Kernel.sh stealth proxy.

    First tries Kernel.sh for CLOB data (our VPS IP is blocked from direct access),
    falls back to py_clob_client SDK if available.
    """
    # Try Kernel.sh proxy first (CLOB blocked from VPS)
    try:
        sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))
        from kernel_proxy import clob_snapshot
        raw = clob_snapshot(token_id)
        if raw and isinstance(raw, dict):
            bids = raw.get("bids", [])
            asks = raw.get("asks", [])
            if bids or asks:
                print(f"[KERNEL_PROXY] CLOB data for token {token_id[:16]}... | {len(bids)} bids, {len(asks)} asks", file=sys.stderr)
                return _SdkBook(bids, asks)
    except Exception as e:
        print(f"[KERNEL_PROXY] Failed: {e}", file=sys.stderr) if __debug__ else None
        pass

    # Fallback: py_clob_client SDK (may be blocked — add socket timeout)
    if not client or not token_id:
        return None
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(10)
    try:
        book = client.get_order_book(token_id)
        return book
    except Exception:
        return None
    finally:
        socket.setdefaulttimeout(old_timeout)


class _SdkBook:
    """Adapter so Kernel.sh order book data matches py_clob_client's Book object shape."""
    def __init__(self, bids, asks):
        self.bids = [_SdkOrder(b) for b in bids]
        self.asks = [_SdkOrder(a) for a in asks]


class _SdkOrder:
    def __init__(self, order):
        self.price = order.get("price", "0")
        self.size = order.get("size", "0")


def fetch_amm_prices(token_id):
    """Fetch AMM outcome prices from gamma-api as secondary spread source.

    Polymarket has two venues: CLOB (thin on prediction markets) and
    neg-risk AMM (where most retail trades). This returns the AMM price.
    """
    try:
        import urllib.request
        url = f"https://gamma-api.polymarket.com/markets?limit=1&clob_token_ids={token_id}"
        req = urllib.request.Request(url, headers={"User-Agent": "quant/1.0", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        if data and len(data) > 0:
            m = data[0]
            outcomes = m.get("outcomePrices", [])
            last_trade = m.get("lastTradePrice", None)
            volume = float(m.get("volumeClob", 0) or 0)
            if outcomes:
                try:
                    parsed = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
                    if len(parsed) >= 2:
                        yes_price = float(parsed[0])
                        no_price = float(parsed[1])
                        amm_spread = abs(no_price + yes_price - 1.0)
                        return {
                            "yes_price": yes_price,
                            "no_price": no_price,
                            "last_trade_price": float(last_trade) if last_trade else None,
                            "volume": volume,
                            "amm_divergence": round(amm_spread, 4),
                        }
                except Exception:
                    pass
    except Exception:
        pass
    return None


def compute_obi(bids, asks):
    """Order Book Imbalance: (bid_vol - ask_vol) / (bid_vol + ask_vol)
    Positive → more buying pressure. Negative → more selling pressure.
    |OBI| > 0.70 is toxic (one-sided book)."""
    bid_vol = sum(float(b["size"]) for b in bids)
    ask_vol = sum(float(a["size"]) for a in asks)
    total = bid_vol + ask_vol
    if total == 0:
        return 0.0
    return (bid_vol - ask_vol) / total


def compute_spread(bids, asks):
    """Bid-ask spread as fraction of mid-price.
    > 15% → toxic (wide enough to indicate illiquid or adversely selected book)."""
    if not bids or not asks:
        return 1.0
    best_bid = max(float(b["price"]) for b in bids)
    best_ask = min(float(a["price"]) for a in asks)
    mid = (best_bid + best_ask) / 2.0
    if mid == 0:
        return 1.0
    return (best_ask - best_bid) / mid


def compute_depth_ratio(bids, asks, levels=5):
    """Dollar-weighted depth ratio across top N levels.
    bid_depth / ask_depth. > 5:1 or < 1:5 → flagged."""
    sorted_bids = sorted(bids, key=lambda x: float(x["price"]), reverse=True)[:levels]
    sorted_asks = sorted(asks, key=lambda x: float(x["price"]))[:levels]
    bid_depth = sum(float(b["size"]) * float(b["price"]) for b in sorted_bids)
    ask_depth = sum(float(a["size"]) * float(a["price"]) for a in sorted_asks)
    if ask_depth == 0:
        return 999.0
    return bid_depth / ask_depth


def compute_kyle_lambda(bids, asks):
    """Kyle's lambda: price impact per unit of order flow.
    Estimated as the slope of (price deviation from mid) vs (cumulative volume)
    on the ask side. Higher lambda → more toxic / less liquid."""
    if len(asks) < 2:
        return 0.0
    best_bid = max(float(b["price"]) for b in bids) if bids else 0.0
    best_ask = min(float(a["price"]) for a in asks)
    mid = (best_bid + best_ask) / 2.0
    if mid == 0:
        return 0.0

    sorted_asks = sorted(asks, key=lambda x: float(x["price"]))[:10]
    points = []
    cum_vol = 0.0
    for a in sorted_asks:
        price  = float(a["price"])
        size   = float(a["size"])
        cum_vol += size
        points.append((cum_vol, price - mid))

    n = len(points)
    if n < 2:
        return 0.0

    sum_x  = sum(p[0] for p in points)
    sum_y  = sum(p[1] for p in points)
    sum_xy = sum(p[0] * p[1] for p in points)
    sum_x2 = sum(p[0] ** 2 for p in points)
    denom  = n * sum_x2 - sum_x ** 2
    if denom == 0:
        return 0.0
    return (n * sum_xy - sum_x * sum_y) / denom


def compute_ev(entry_price, fair_value, direction):
    """Expected value per dollar risked.
    EV = p_win * b - p_lose  where b = payout odds."""
    try:
        if direction == "YES":
            p_win = fair_value
            b     = (1.0 - entry_price) / entry_price
        else:
            no_entry = 1.0 - entry_price
            p_win    = 1.0 - fair_value
            b        = (1.0 - no_entry) / no_entry

        if b <= 0:
            return 0.0
        ev = p_win * b - (1.0 - p_win)
        return round(ev, 4)
    except (ZeroDivisionError, ValueError):
        return 0.0


def compute_kelly(entry_price, fair_value, direction, bankroll, loss_streak):
    """Half-Kelly (quarter-Kelly after 3+ losses) with hard caps.

    Returns dict with:
      kelly        — raw Kelly fraction
      kelly_mode   — "half" or "quarter"
      fraction     — fraction of bankroll to wager (post-cap)
      size_usd     — dollar amount to wager (post-cap)
      skip         — True if trade should not be taken
      reason       — why skip (if applicable)
    """
    try:
        if direction == "YES":
            p = fair_value
            b = (1.0 - entry_price) / entry_price
        else:
            no_entry = 1.0 - entry_price
            p = 1.0 - fair_value
            b = (1.0 - no_entry) / no_entry
    except ZeroDivisionError:
        return {"kelly": 0.0, "fraction": 0.0, "size_usd": 0.0,
                "skip": True, "reason": "zero_division"}

    if b <= 0:
        return {"kelly": 0.0, "fraction": 0.0, "size_usd": 0.0,
                "skip": True, "reason": "negative_odds"}

    q     = 1.0 - p
    kelly = (b * p - q) / b

    if kelly <= 0:
        return {"kelly": round(kelly, 4), "fraction": 0.0, "size_usd": 0.0,
                "skip": True, "reason": "no_edge"}

    if loss_streak >= 3:
        fraction    = kelly / 4.0
        kelly_mode  = "quarter"
    else:
        fraction    = kelly / 2.0
        kelly_mode  = "half"

    fraction = min(fraction, MAX_BANKROLL_PCT)
    size_usd = min(bankroll * fraction, MAX_TRADE_USD)

    if size_usd < MIN_TRADE_USD:
        return {"kelly": round(kelly, 4), "kelly_mode": kelly_mode,
                "fraction": round(fraction, 6), "size_usd": round(size_usd, 4),
                "skip": True, "reason": "below_min_size"}

    return {
        "kelly":      round(kelly, 4),
        "kelly_mode": kelly_mode,
        "fraction":   round(fraction, 6),
        "size_usd":   round(size_usd, 4),
        "skip":       False,
    }


# ─── Main Loop ──────────────────────────────────────────────────────────────
results = []

for market in markets:
    token_id   = market.get("token_id") or market.get("yes_token_id")
    slug       = market.get("slug", "")
    question   = market.get("question", "")
    yes_price  = float(market.get("yes", market.get("yes_price", 0.5)))
    no_price   = float(market.get("no",  market.get("no_price",  0.5)))
    fair_value = float(market.get("fair_value_estimate", yes_price))
    direction  = market.get("direction", "YES")

    result = {
        "slug":       slug,
        "token_id":   token_id,
        "question":   question,
        "yes_price":  yes_price,
        "no_price":   no_price,
        "fair_value": fair_value,
        "direction":  direction,
        "metrics":    {},
        "flags":      [],
        "kelly":      {},
        "ev":         0.0,
        "tradeable":  False,
    }

    # ── VENUE-FIRST ANALYSIS ─────────────────────────────────────────────
    # Polymarket has dual venues: CLOB (thin order books) + neg-risk AMM (real volume).
    # Most prediction markets trade on the neg-risk AMM. CLOB books are naturally 
    # thin with wide spreads. We check AMM FIRST — if a market has real AMM volume,
    # we use AMM prices as the source of truth and skip CLOB toxic gates.
    
    amm = fetch_amm_prices(token_id) if token_id else None
    venue = "UNKNOWN"
    amm_volume = 0.0
    
    if amm and amm["volume"] >= 500.0:
        # ── AMM-FIRST PATH ────────────────────────────────────────────
        # Market trades primarily on the neg-risk AMM with real volume.
        # Use AMM prices directly — CLOB toxicity is irrelevant.
        venue = "AMM"
        amm_volume = amm["volume"]
        amm_yes_price = amm["yes_price"]
        amm_no_price = amm["no_price"]
        amm_spread = abs(amm_yes_price + amm_no_price - 1.0)
        
        result["metrics"] = {
            "venue": venue,
            "amm_volume": amm_volume,
            "amm_yes_price": amm_yes_price,
            "amm_no_price": amm_no_price,
            "amm_spread": round(amm_spread, 4),
            "amm_divergence": amm["amm_divergence"],
        }
        
        # Override yes_price with AMM price for EV computation
        # AMM prices are the actual execution price — more accurate than CLOB
        yes_price = amm_yes_price
        
        # Flag for tracking but do NOT block
        if amm_spread > 0.05:
            result["flags"].append(f"AMM_SPREAD:{amm_spread*100:.1f}%")
        if amm_volume < 2000.0:
            result["flags"].append(f"MODERATE_AMM_VOL:{amm_volume:.0f}")
        else:
            result["flags"].append(f"HEALTHY_AMM_VOL:{amm_volume:.0f}")
        
        # Also fetch CLOB for reference but don't gate on it
        book = fetch_order_book(token_id) if token_id else None
        if book is not None:
            bids = _to_dicts(book.bids or [])
            asks = _to_dicts(book.asks or [])
            clob_obi = compute_obi(bids, asks)
            clob_spread_pct = compute_spread(bids, asks)
            result["metrics"]["clob_obi"] = round(clob_obi, 4)
            result["metrics"]["clob_spread"] = round(clob_spread_pct, 4)
            # Large CLOB/AMM divergence = genuine market inefficiency
            if len(bids) > 0 and len(asks) > 0:
                clob_mid = (max(float(b["price"]) for b in bids) + min(float(a["price"]) for a in asks)) / 2.0
                amm_divergence_pct = abs(amm_yes_price - clob_mid) / clob_mid if clob_mid > 0 else 0
                if amm_divergence_pct > 0.10:
                    result["flags"].append(f"CLOB_AMM_DIVERGE:{amm_divergence_pct:.1%}")
    
    elif amm and amm["volume"] >= 100.0:
        # ── LOW-VOLUME AMM PATH ───────────────────────────────────────
        # Low but non-trivial AMM volume — still use AMM prices with CAUTION
        venue = "AMM_LOW"
        amm_volume = amm["volume"]
        yes_price = amm.get("yes_price", yes_price)  # align EV with AMM
        result["metrics"] = {
            "venue": venue,
            "amm_volume": amm_volume,
            "amm_yes_price": amm["yes_price"],
            "amm_no_price": amm["no_price"],
            "amm_spread": round(abs(amm["yes_price"] + amm["no_price"] - 1.0), 4),
        }
        result["flags"].append(f"LOW_AMM_VOLUME:{amm_volume:.0f}")
        
    else:
        # ── CLOB FALLBACK PATH ────────────────────────────────────────
        # No meaningful AMM data — fall back to CLOB order book (current behavior)
        book = fetch_order_book(token_id) if token_id else None
        if book is not None:
            venue = "CLOB"
            bids = _to_dicts(book.bids or [])
            asks = _to_dicts(book.asks or [])
            
            obi          = compute_obi(bids, asks)
            spread_pct   = compute_spread(bids, asks)
            depth_ratio  = compute_depth_ratio(bids, asks)
            kyle_lambda  = compute_kyle_lambda(bids, asks)

            result["metrics"] = {
                "venue": venue,
                "obi":          round(obi, 4),
                "spread_pct":   round(spread_pct, 4),
                "depth_ratio":  round(depth_ratio, 4),
                "kyle_lambda":  round(kyle_lambda, 6),
                "best_bid":     max((float(b["price"]) for b in bids), default=None),
                "best_ask":     min((float(a["price"]) for a in asks), default=None),
            }

            # Thin market detection
            is_thin_market = spread_pct > 0.50 and len(bids) <= 3 and len(asks) <= 3
            result["metrics"]["is_thin_market"] = is_thin_market

            if abs(obi) > OBI_TOXIC_THRESHOLD and not is_thin_market:
                result["flags"].append(f"OBI_TOXIC:{obi:.3f}")
            elif abs(obi) > OBI_TOXIC_THRESHOLD and is_thin_market:
                result["flags"].append(f"THIN_MARKET_OBI:{obi:.3f}")
            if spread_pct > SPREAD_TOXIC_PCT and not is_thin_market:
                result["flags"].append(f"SPREAD_TOXIC:{spread_pct:.1%}")
            elif spread_pct > SPREAD_TOXIC_PCT and is_thin_market:
                result["flags"].append(f"THIN_MARKET_SPREAD:{spread_pct:.1%}")
            if depth_ratio > DEPTH_RATIO_FLAG or depth_ratio < (1.0 / DEPTH_RATIO_FLAG):
                flag_type = "THIN_MARKET_DEPTH" if is_thin_market else "DEPTH_IMBALANCE"
                result["flags"].append(f"{flag_type}:{depth_ratio:.2f}:1")
        elif token_id:
            venue = "NO_DATA"
            result["flags"].append("ORDERBOOK_UNAVAILABLE")
        else:
            venue = "NO_TOKEN"
            result["flags"].append("NO_TOKEN_ID")
    
    result["metrics"]["venue"] = venue
    result["metrics"]["amm_volume"] = amm_volume
    
    # ── EV & Kelly ────────────────────────────────────────────────────────
    result["ev"]    = round(compute_ev(yes_price, fair_value, direction), 4)
    result["kelly"] = compute_kelly(yes_price, fair_value, direction, bankroll, streak)
    
    # ── Tradeable Gate ───────────────────────────────────────────────────
    # For AMM venues: only block on explicit AMM flags, never CLOB toxicity
    # For CLOB venues: current behavior (block on TOXIC flags)
    if venue in ("AMM", "AMM_LOW"):
        # AMM path: only blocked by Kelly (no edge, no money) or explicit AMM flags
        # CLOB flags (THIN_MARKET_OBI, THIN_MARKET_SPREAD) do NOT block AMM trades
        amm_blockers = [f for f in result["flags"] if f.startswith("AMM_") and "SPREAD" in f 
                        and float(f.split(":")[1].replace("%","")) > 15.0]
        result["tradeable"] = (not result["kelly"].get("skip", True)) and len(amm_blockers) == 0
    else:
        # CLOB fallback path: current behavior
        toxic = [f for f in result["flags"] if re.search(r"TOXIC", f) 
                 and not f.startswith("AMM_") and not f.startswith("THIN_MARKET_")]
        result["tradeable"] = (not result["kelly"].get("skip", True)) and len(toxic) == 0

    results.append(result)


# ─── Output ─────────────────────────────────────────────────────────────────
output = {
    "bankroll":           bankroll,
    "loss_streak":        streak,
    "kelly_mode":         "quarter" if streak >= 3 else "half",
    "clob_connected":     creds_loaded,
    "markets_analyzed":   len(results),
    "tradeable_count":    sum(1 for r in results if r["tradeable"]),
    "results":            results,
}

print(json.dumps(output, indent=2))
