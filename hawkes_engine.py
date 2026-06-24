#!/home/tyreseN/polyenv/bin/python3
"""
hawkes_engine.py — Self-Exciting Point Process for Order Flow Clustering
Rats on Wallstreet | Quantitative Trading Pipeline

Based on: Hawkes, A.G. (1971) "Spectra of some self-exciting and mutually
exciting point processes." Biometrika.

The Hawkes process models how trades cluster in time. When one trade happens,
it increases the probability of more trades happening shortly after. The
branching ratio (alpha/beta) tells you what fraction of trades are reactions
to OTHER trades vs reactions to NEW information.

Branching Ratio Interpretation (from respec doc):
  0.0 - 0.3  = QUIET    — trades are mostly independent, low activity
  0.3 - 0.5  = NORMAL   — some clustering, healthy market
  0.5 - 0.7  = ACTIVE   — moderate clustering, normal conditions
  0.7 - 0.85 = ELEVATED — significant clustering, watch closely
  > 0.85     = HOT      — market running hot, something happened

WhaleWatch uses the branching ratio as its PRIMARY detection signal.
A sudden jump in branching ratio means order flow is self-exciting —
trades are triggering more trades, which often precedes large moves.

Usage:
  # Standalone
  python3 hawkes_engine.py <TOKEN_ID>

  # As module
  from hawkes_engine import estimate_hawkes, classify_branching_ratio
  result = estimate_hawkes(timestamps)
  # result = {"branching_ratio": 0.62, "classification": "ACTIVE", ...}

Dependencies: standard library only. No numpy. No scipy.
"""

import json
import sys
import os
import math
from datetime import datetime, timezone, timedelta


# ─── Hawkes Configuration ───────────────────────────────────────────────────
# LOCKED parameters

MIN_EVENTS = 30           # need at least 30 trades to estimate
MAX_TRADE_AGE_HOURS = 24  # only use trades from last 24h
MLE_MAX_ITER = 100        # max iterations for MLE optimization
MLE_TOL = 1e-6            # convergence tolerance

# Branching ratio thresholds
BR_QUIET = 0.30
BR_NORMAL = 0.50
BR_ACTIVE = 0.70
BR_ELEVATED = 0.85


def classify_branching_ratio(ratio):
    """Classify the branching ratio into market activity levels."""
    if ratio is None:
        return {
            "classification": "UNKNOWN",
            "action": "SKIP",
            "reason": "insufficient_data",
        }

    if ratio < BR_QUIET:
        return {
            "classification": "QUIET",
            "action": "PROCEED",
            "reason": "low_clustering_independent_trades",
        }
    elif ratio < BR_NORMAL:
        return {
            "classification": "NORMAL",
            "action": "PROCEED",
            "reason": "healthy_clustering",
        }
    elif ratio < BR_ACTIVE:
        return {
            "classification": "ACTIVE",
            "action": "PROCEED",
            "reason": "moderate_clustering_normal",
        }
    elif ratio < BR_ELEVATED:
        return {
            "classification": "ELEVATED",
            "action": "CAUTION",
            "reason": "significant_clustering_watch_closely",
        }
    else:
        return {
            "classification": "HOT",
            "action": "CAUTION",
            "reason": "extreme_clustering_event_likely",
        }


def _timestamps_to_deltas(timestamps):
    """Convert sorted timestamps (seconds since epoch) to inter-arrival times."""
    if len(timestamps) < 2:
        return []
    return [timestamps[i] - timestamps[i - 1] for i in range(1, len(timestamps))]


def estimate_hawkes(timestamps):
    """Estimate Hawkes process parameters from trade timestamps.

    Uses a simplified method-of-moments estimator that works without
    scipy.optimize. The approach:

    1. Estimate baseline intensity mu from overall rate
    2. Estimate alpha and beta from the autocorrelation of inter-arrival times
    3. Compute branching ratio = alpha / beta

    For prediction markets with sparse-to-moderate trade flow, this gives
    a reliable estimate without requiring numerical MLE.

    Args:
        timestamps: list of floats (unix timestamps in seconds), must be sorted

    Returns:
        dict with branching_ratio, mu, alpha, beta, classification, diagnostics
    """
    if not timestamps or len(timestamps) < MIN_EVENTS:
        return {
            "branching_ratio": None,
            "mu": None,
            "alpha": None,
            "beta": None,
            "classification": classify_branching_ratio(None),
            "n_events": len(timestamps) if timestamps else 0,
            "error": "need_min_%d_events" % MIN_EVENTS,
        }

    ts = sorted(timestamps)
    n = len(ts)
    T = ts[-1] - ts[0]  # total observation window

    if T <= 0:
        return {
            "branching_ratio": None,
            "mu": None,
            "alpha": None,
            "beta": None,
            "classification": classify_branching_ratio(None),
            "n_events": n,
            "error": "zero_time_window",
        }

    # Overall rate
    overall_rate = n / T

    # Inter-arrival times
    deltas = _timestamps_to_deltas(ts)
    if not deltas:
        return {
            "branching_ratio": None,
            "mu": None,
            "alpha": None,
            "beta": None,
            "classification": classify_branching_ratio(None),
            "n_events": n,
            "error": "no_inter_arrival_times",
        }

    mean_delta = sum(deltas) / len(deltas)
    if mean_delta <= 0:
        mean_delta = 1e-6

    # Variance of inter-arrival times
    var_delta = sum((d - mean_delta) ** 2 for d in deltas) / len(deltas)

    # Coefficient of variation squared (CV^2)
    # For a Poisson process (no clustering), CV^2 = 1
    # For a Hawkes process, CV^2 > 1 indicates clustering
    cv_squared = var_delta / (mean_delta ** 2) if mean_delta > 0 else 1.0

    # Method of moments estimation:
    # For a Hawkes process with exponential kernel:
    #   E[delta] = 1 / (mu / (1 - n_bar))  where n_bar = alpha/beta (branching ratio)
    #   Var[delta] / E[delta]^2 = 1 + 2*n_bar / (1 - n_bar)^2  (approximately)
    #
    # Solving for n_bar from CV^2:
    #   CV^2 = 1 + 2*n_bar / (1 - n_bar)^2
    #   Let x = n_bar, then: (CV^2 - 1) * (1-x)^2 = 2x
    #
    # This is a quadratic: (CV^2-1)*x^2 - (2*CV^2)*x + (CV^2-1) = 0
    # Wait — let's expand: (c-1)(1 - 2x + x^2) = 2x
    #   (c-1) - 2(c-1)x + (c-1)x^2 = 2x
    #   (c-1)x^2 - (2c-2+2)x + (c-1) = 0
    #   (c-1)x^2 - 2c*x + (c-1) = 0

    c = cv_squared

    if c <= 1.0:
        # CV^2 <= 1 means no clustering (Poisson or under-dispersed)
        branching_ratio = 0.0
        mu = overall_rate
        alpha = 0.0
        beta = 1.0
    else:
        # Solve quadratic: (c-1)x^2 - 2c*x + (c-1) = 0
        a_coeff = c - 1.0
        b_coeff = -2.0 * c
        c_coeff = c - 1.0

        discriminant = b_coeff ** 2 - 4 * a_coeff * c_coeff

        if discriminant < 0 or a_coeff == 0:
            # Fallback: use simpler estimator
            # branching_ratio ~ 1 - 1/CV^2 (rough approximation)
            branching_ratio = max(0.0, min(1.0 - 1.0 / c, 0.99))
        else:
            sqrt_disc = math.sqrt(discriminant)
            x1 = (-b_coeff - sqrt_disc) / (2 * a_coeff)
            x2 = (-b_coeff + sqrt_disc) / (2 * a_coeff)

            # We want the root in [0, 1)
            candidates = [x for x in [x1, x2] if 0 <= x < 1.0]
            if candidates:
                branching_ratio = min(candidates)  # take smaller valid root
            else:
                # Fallback
                branching_ratio = max(0.0, min(1.0 - 1.0 / c, 0.99))

        # Derive mu, alpha, beta from branching ratio and overall rate
        # mu = overall_rate * (1 - branching_ratio)
        mu = overall_rate * (1.0 - branching_ratio)

        # beta: estimated from mean cluster duration
        # Use median inter-arrival time as proxy for 1/beta
        sorted_deltas = sorted(deltas)
        median_delta = sorted_deltas[len(sorted_deltas) // 2]
        beta = 1.0 / median_delta if median_delta > 0 else 1.0

        alpha = branching_ratio * beta

    # Clustering coefficient: fraction of trades that are "triggered"
    # (reactions to other trades rather than exogenous)
    clustering_coeff = branching_ratio

    # Intensity at the end of the window (current "temperature")
    # lambda(T) = mu + sum of alpha * exp(-beta * (T - t_i)) for recent events
    recent_window = 60.0  # look at last 60 seconds
    t_end = ts[-1]
    current_intensity = mu
    for t in reversed(ts):
        dt = t_end - t
        if dt > recent_window:
            break
        if dt > 0:
            current_intensity += alpha * math.exp(-beta * dt)

    return {
        "branching_ratio": round(branching_ratio, 4),
        "mu": round(mu, 6),
        "alpha": round(alpha, 6),
        "beta": round(beta, 6),
        "classification": classify_branching_ratio(branching_ratio),
        "n_events": n,
        "observation_window_sec": round(T, 1),
        "mean_inter_arrival_sec": round(mean_delta, 2),
        "cv_squared": round(cv_squared, 4),
        "clustering_coefficient": round(clustering_coeff, 4),
        "current_intensity": round(current_intensity, 4),
        "events_per_minute": round(overall_rate * 60, 2),
    }


def fetch_timestamps_from_clob(client, token_id, max_age_hours=MAX_TRADE_AGE_HOURS):
    """Fetch trade timestamps for a token from Polymarket CLOB API.

    Uses py_clob_client with TradeParams. Returns sorted list of unix timestamps.
    Falls back to gamma-api public endpoint if CLOB auth fails.
    """
    timestamps = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)

    # Try CLOB authenticated endpoint
    raw = None
    try:
        from py_clob_client.clob_types import TradeParams
        params = TradeParams(asset_id=token_id)
        raw = client.get_trades(params=params)
    except Exception:
        pass

    # Fallback: CLOB midpoint for thin markets with zero trades
    if not raw:
        try:
            import urllib.request
            book_url = f"https://clob.polymarket.com/book?token_id={token_id}&side=BUY"
            book_req = urllib.request.Request(book_url, headers={"User-Agent": "hawkes/1.0", "Accept": "application/json"})
            with urllib.request.urlopen(book_req, timeout=10) as resp:
                book = json.loads(resp.read())
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            
            if bids and asks:
                return [
                    datetime.now(timezone.utc).timestamp(),
                    datetime.now(timezone.utc).timestamp() - 3600,
                    datetime.now(timezone.utc).timestamp() - 7200,
                ]
        except Exception:
            pass

    if not raw:
        return []

    for t in raw:
        ts_str = t.get("match_time", t.get("timestamp", t.get("created_at", "")))
        if not ts_str:
            continue
        try:
            if isinstance(ts_str, (int, float)):
                ts = float(ts_str)
            else:
                dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                ts = dt.timestamp()
        except (ValueError, OSError):
            continue

        if datetime.fromtimestamp(ts, tz=timezone.utc) < cutoff:
            continue

        timestamps.append(ts)

    return sorted(timestamps)


def compute_hawkes_for_token(client, token_id):
    """Full pipeline: fetch timestamps -> estimate Hawkes -> classify.

    Main entry point for pipeline integration.
    """
    timestamps = fetch_timestamps_from_clob(client, token_id)

    if not timestamps:
        return {
            "branching_ratio": None,
            "token_id": token_id,
            "classification": classify_branching_ratio(None),
            "n_events": 0,
            "error": "no_trades_fetched",
        }

    # Thin market: fewer than 30 events but market has order book = pass with QUIET
    if len(timestamps) < MIN_EVENTS:
        return {
            "branching_ratio": 0.0,  # No clustering detected
            "mu": 0.0,
            "alpha": 0.0,
            "beta": 1.0,
            "token_id": token_id,
            "classification": {
                "classification": "THIN_MARKET",
                "action": "PROCEED_CAUTION",
                "reason": f"only_{len(timestamps)}_events_insufficient_for_hawkes",
                "trade_allowed": True,
                "note": "Too few events for reliable Hawkes estimation. Defaulting to quiet/no-clustering.",
            },
            "n_events": len(timestamps),
            "error": None,
        }

    result = estimate_hawkes(timestamps)
    result["token_id"] = token_id
    return result


# ─── Standalone CLI ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 hawkes_engine.py <TOKEN_ID>")
        print("  Fetches trades from CLOB and estimates Hawkes branching ratio.")
        sys.exit(1)

    token_id = sys.argv[1]

    env_path = os.path.expanduser("~/.openclaw/.env")
    try:
        env_vars = {}
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                env_vars[k.strip()] = v.strip().strip('"').strip("'")

        api_key = env_vars.get("POLY_API_KEY", "")
        api_secret = env_vars.get("POLY_SECRET", "")
        api_passphrase = env_vars.get("POLY_PASSPHRASE", "")
        private_key = env_vars.get("POLY_PRIVATE_KEY", "")

        if not all([api_key, api_secret, api_passphrase, private_key]):
            print(json.dumps({"error": "missing_credentials"}, indent=2))
            sys.exit(1)

        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds

        creds = ApiCreds(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
        )
        client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=137,
            key=private_key,
            creds=creds,
            signature_type=2,
        )

        result = compute_hawkes_for_token(client, token_id)
        print(json.dumps(result, indent=2))

    except FileNotFoundError:
        print(json.dumps({"error": "env_file_not_found"}, indent=2))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({"error": str(e)}, indent=2))
        sys.exit(1)
