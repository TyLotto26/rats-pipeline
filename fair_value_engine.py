#!/home/tyreseN/polyenv/bin/python3
"""
fair_value_engine.py — External data-driven fair value estimation.
Computes independent probability estimates for Polymarket markets
using public APIs. No LLM reasoning — pure data and math.

Data sources:
  - Open-Meteo (weather: temperature, precipitation) — free, no key
  - Open-Meteo Geocoding (resolve any city name) — free, no key
  - FRED API (macro: CPI, GDP, unemployment, Fed funds) — free key required
  - yfinance (commodities: gold, silver, oil) — free, no key

Called by polyscan-fetch.py. Returns fair value per market.
"""

import json
import math
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

# ─── API Endpoints ───────────────────────────────────────────────────────────

OPEN_METEO_BASE = "https://api.open-meteo.com/v1/forecast"
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FRED_API_BASE = "https://api.stlouisfed.org/fred/series/observations"

# ─── City Coordinates (hardcoded cache — geocoding fallback for unknowns) ────

CITY_COORDS = {
    "shanghai": (31.23, 121.47),
    "new york": (40.71, -74.01), "nyc": (40.71, -74.01), "new york city": (40.71, -74.01),
    "paris": (48.86, 2.35),
    "london": (51.51, -0.13),
    "tokyo": (35.68, 139.69),
    "mumbai": (19.08, 72.88),
    "buenos aires": (-34.61, -58.38),
    "munich": (48.14, 11.58),
    "berlin": (52.52, 13.41),
    "sydney": (-33.87, 151.21),
    "dubai": (25.28, 55.30),
    "singapore": (1.35, 103.82),
    "seoul": (37.57, 126.98),
    "chicago": (41.88, -87.63),
    "los angeles": (34.05, -118.24),
    "miami": (25.76, -80.19),
    "dallas": (32.78, -96.80),
    "denver": (39.74, -104.99),
    "atlanta": (33.75, -84.39),
    "phoenix": (33.45, -112.07),
    "seattle": (47.61, -122.33),
    "boston": (42.36, -71.06),
    "san francisco": (37.77, -122.42),
    "houston": (29.76, -95.37),
    "washington": (38.91, -77.04), "dc": (38.91, -77.04),
}

# ─── FRED Series Map ─────────────────────────────────────────────────────────

FRED_API_KEY = os.environ.get("FRED_API_KEY", "")

FRED_SERIES = {
    "cpi": "CPIAUCSL",           # Consumer Price Index for All Urban Consumers
    "core cpi": "CPILFESL",      # CPI less food and energy
    "unemployment": "UNRATE",    # Unemployment Rate
    "unemployment rate": "UNRATE",
    "gdp": "GDP",                # Gross Domestic Product
    "gdp growth": "GDPC1",       # Real GDP
    "fed funds": "FEDFUNDS",     # Federal Funds Effective Rate
    "federal funds": "FEDFUNDS",
    "interest rate": "FEDFUNDS",
    "nonfarm": "PAYEMS",         # All Employees: Total Nonfarm
    "nonfarm payrolls": "PAYEMS",
    "jobs": "PAYEMS",
    "industrial production": "INDPRO",
    "retail sales": "RSXFS",     # Retail Sales
    "consumer confidence": "UMCSENT",
    "housing starts": "HOUST",
}

# ─── Commodity Futures Symbols ───────────────────────────────────────────────

COMMODITY_MAP = {
    "silver": "SI=F", "si": "SI=F",
    "gold": "GC=F", "gc": "GC=F",
    "oil": "CL=F", "crude": "CL=F", "wti": "CL=F", "brent": "BZ=F",
    "copper": "HG=F", "hg": "HG=F",
    "platinum": "PL=F", "pl": "PL=F",
    "palladium": "PA=F", "pa": "PA=F",
    "natural gas": "NG=F", "ng": "NG=F",
    "gasoline": "RB=F", "rb": "RB=F",
    "corn": "ZC=F", "zc": "ZC=F",
    "wheat": "ZW=F", "zw": "ZW=F",
    "soybeans": "ZS=F", "zs": "ZS=F",
}


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def estimate_fair_value(market):
    """Estimate fair value for a market using external data.
    Returns (fair_value, direction, source) or (None, None, None) if no data available.
    """
    slug = market.get("slug", "").lower()
    question = market.get("question", "")
    q_lower = question.lower()
    yes_price = float(market.get("yes", market.get("yes_price", 0.5)))

    # 1. Weather: temperature markets
    temp_result = _try_temperature_market(slug, q_lower, question, yes_price)
    if temp_result:
        return temp_result

    # 2. Weather: precipitation markets
    precip_result = _try_precipitation_market(slug, q_lower, question, yes_price)
    if precip_result:
        return precip_result

    # 3. Stocks & Crypto: ticker-based price targets via yfinance
    stock_result = _try_stock_market(q_lower, question, yes_price)
    if stock_result:
        return stock_result

    # 4. Commodities: gold, silver, oil settlement
    commodity_result = _try_commodity_market(q_lower, question, yes_price)
    if commodity_result:
        return commodity_result

    # 5. Economic: FRED-based macro data
    econ_result = _try_economic_market(q_lower, question, yes_price)
    if econ_result:
        return econ_result

    # No matching data source — return identity mapping with UNKNOWN flag
    # so the pipeline knows this market has no edge rather than silent fallthrough
    return yes_price, "UNKNOWN", "no matching data source"


# ═══════════════════════════════════════════════════════════════════════════════
# 1. WEATHER — TEMPERATURE (Open-Meteo + Geocoding)
# ═══════════════════════════════════════════════════════════════════════════════

def _resolve_city_coords(city_name):
    """Resolve city name to (lat, lon) — hardcoded cache first, then geocoding API."""
    city_lower = city_name.lower().strip()
    # Check hardcoded cache
    for key, coords in CITY_COORDS.items():
        if key in city_lower:
            return coords
    # Fallback: Open-Meteo Geocoding API
    try:
        url = f"{GEOCODING_URL}?name={urllib.parse.quote(city_lower)}&count=1&format=json"
        req = urllib.request.Request(url, headers={"User-Agent": "rats-quant/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        if data.get("results"):
            r = data["results"][0]
            return (r["latitude"], r["longitude"])
    except Exception:
        pass
    return None


def _try_temperature_market(slug, q_lower, question, yes_price):
    """Handle 'highest/lowest temperature in CITY on DATE' markets."""
    temp_match = re.search(
        r'(highest|lowest|maximum|minimum)\s+temperature\s+in\s+([a-z\s]+?)\s+'
        r'(?:be\s+(?:between\s+)?)?'
        r'(\d+)[\s°]*([cfCF])',
        q_lower
    )
    if not temp_match:
        return None

    temp_type = temp_match.group(1)
    city_name = temp_match.group(2).strip()
    target_temp = int(temp_match.group(3))
    unit = temp_match.group(4).lower()

    date_str = _extract_date(question)
    if not date_str:
        return None

    coords = _resolve_city_coords(city_name)
    if not coords:
        return None

    try:
        is_max = temp_type in ("highest", "maximum")
        forecast_temp = _fetch_temperature(coords, date_str, is_max, unit)
        if forecast_temp is None:
            return None

        range_match = re.search(r'between\s+(\d+)[\s°-]+(\d+)', q_lower)
        if range_match:
            low_bound = int(range_match.group(1))
            high_bound = int(range_match.group(2))
            fair_value = _temp_range_probability(forecast_temp, low_bound, high_bound)
        else:
            fair_value = _temp_threshold_probability(forecast_temp, target_temp)

        direction = "YES" if fair_value > yes_price else "NO"
        source = f"open-meteo forecast: {forecast_temp:.1f}{'°C' if unit == 'c' else '°F'} for {city_name} on {date_str}"

        return fair_value, direction, source

    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# 2. WEATHER — PRECIPITATION (Open-Meteo + Geocoding)
# ═══════════════════════════════════════════════════════════════════════════════

def _try_precipitation_market(slug, q_lower, question, yes_price):
    """Handle precipitation/rain markets."""
    precip_match = re.search(
        r'(\d+(?:\.\d+)?)\s*(?:and\s+(\d+(?:\.\d+)?)\s+)?inch(?:es)?\s+of\s+precipitation\s+in\s+([a-z\s]+)',
        q_lower
    )
    if not precip_match:
        return None

    low_inches = float(precip_match.group(1))
    high_inches = float(precip_match.group(2)) if precip_match.group(2) else low_inches + 1
    city_name = precip_match.group(3).strip()

    date_str = _extract_date(question)
    if not date_str:
        return None

    coords = _resolve_city_coords(city_name)
    if not coords:
        return None

    try:
        forecast_mm = _fetch_precipitation(coords, date_str)
        if forecast_mm is None:
            return None

        forecast_inches = forecast_mm / 25.4
        std_dev = max(forecast_inches * 0.3, 0.2)
        fair_value = _range_probability_gaussian(forecast_inches, std_dev, low_inches, high_inches)

        direction = "YES" if fair_value > yes_price else "NO"
        source = f"open-meteo precip forecast: {forecast_inches:.2f}in for {city_name}"

        return fair_value, direction, source

    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# 3. STOCKS & CRYPTO — yfinance (MU, TSLA, MSTR, BTC-USD, etc.)
# ═══════════════════════════════════════════════════════════════════════════════

STOCK_TICKER_MAP = {
    "mu": "MU", "micron": "MU", "micron technology": "MU",
    "tsla": "TSLA", "tesla": "TSLA",
    "mstr": "MSTR", "microstrategy": "MSTR", "micro strategy": "MSTR",
    "nvda": "NVDA", "nvidia": "NVDA",
    "aapl": "AAPL", "apple": "AAPL",
    "msft": "MSFT", "microsoft": "MSFT",
    "amzn": "AMZN", "amazon": "AMZN",
    "goog": "GOOGL", "google": "GOOGL", "alphabet": "GOOGL",
    "meta": "META", "facebook": "META",
    "spy": "SPY", "spx": "^GSPC", "s&p": "^GSPC", "s&p 500": "^GSPC",
    "btc": "BTC-USD", "bitcoin": "BTC-USD",
    "eth": "ETH-USD", "ethereum": "ETH-USD",
    "sol": "SOL-USD", "solana": "SOL-USD",
    "xrp": "XRP-USD", "ripple": "XRP-USD",
    "doge": "DOGE-USD", "dogecoin": "DOGE-USD",
}

def _try_stock_market(q_lower, question, yes_price):
    """Handle stock/crypto price target markets via yfinance.
    
    Detects ticker symbols in market questions, fetches current price + volatility,
    computes threshold probability using log-normal distribution (same as commodities).
    """
    try:
        import yfinance as yf
        import numpy as np
    except ImportError:
        return None  # yfinance not available

    # Detect ticker: match known stock/crypto names in question
    ticker = None
    for keyword, symbol in sorted(STOCK_TICKER_MAP.items(), key=lambda x: -len(x[0])):
        pattern = r'\b' + re.escape(keyword) + r'\b'
        if re.search(pattern, q_lower, re.I):
            ticker = symbol
            break

    if not ticker:
        return None

    # Extract price threshold: "hit $1350", "hit (HIGH) $1,350", "above $390", "close above $390"
    # Allow parenthetical text like (HIGH), (LOW) between verb and price
    m1 = re.search(
        r'(?:hit|reach|close|trade|be|above|below|over|under|at)\s*(?:\([^)]*\)\s*)?\$?(\d+(?:,\d{3})?(?:\.\d+)?)',
        q_lower
    )
    if not m1:
        # Try "exactly N" pattern
        m1 = re.search(r'exactly\s+\$?(\d+(?:,\d{3})?(?:\.\d+)?)', q_lower)
        if not m1:
            return None

    try:
        strike = float(m1.group(1).replace(",", ""))
    except ValueError:
        return None

    # Direction: "above/over/hit/reach" = over, "below/under" = under
    is_below = bool(re.search(r'below|under|less than|beneath', q_lower))
    
    # Check for range markets
    range_match = re.search(r'between\s+\$?(\d+(?:,\d{3})?(?:\.\d+)?)\s*(?:and|-|to)\s*\$?(\d+(?:,\d{3})?(?:\.\d+)?)', q_lower)
    is_range = bool(range_match)

    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="1mo")

        if hist.empty or len(hist) < 2:
            return None

        spot = hist["Close"].iloc[-1]
        returns = hist["Close"].pct_change().dropna()
        daily_vol = returns.std()

        if daily_vol == 0 or np.isnan(daily_vol):
            return None

        ann_vol = daily_vol * math.sqrt(252)

        # Days to expiry
        expiry_date = _extract_date(question)
        if expiry_date:
            try:
                exp_dt = datetime.strptime(expiry_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                days_left = max(1, (exp_dt - datetime.now(timezone.utc)).days)
            except ValueError:
                days_left = 30
        else:
            days_left = 30

        if is_range:
            high_strike = float(range_match.group(2).replace(",", ""))
            fair_value = _commodity_range_probability(spot, strike, high_strike, ann_vol, days_left)
        else:
            fair_value = _commodity_threshold_probability(spot, strike, ann_vol, days_left)

        if is_below:
            fair_value = 1.0 - fair_value

        direction = "YES" if fair_value > yes_price else "NO"
        source = f"yfinance {ticker} spot=${spot:.2f} vol={ann_vol*100:.1f}% {days_left}d to expiry"

        return fair_value, direction, source

    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# 4. COMMODITIES — yfinance (Gold, Silver, Oil, etc.)
# ═══════════════════════════════════════════════════════════════════════════════

def _try_commodity_market(q_lower, question, yes_price):
    """Handle commodity settlement price markets via yfinance."""
    # Import yfinance inside the function — gracefully handle missing dependency
    try:
        import yfinance as yf
        import numpy as np
    except ImportError:
        return None  # yfinance not available

    # Match commodity name + threshold
    # Pattern 1: "settle over/above/below/under $X"
    # Match commodity name + threshold
    # Pattern: "Silver settle over $65" or "Gold settle at $4,600"
    m1 = re.search(
        r'(\bsilver\b|\bgold\b|\boil\b|\bcrude\b|\bwti\b|\bbrent\b|\bcopper\b|\bplatinum\b|\bpalladium\b|\bnatural\s+gas\b|\bgasoline\b|\bcorn\b|\bwheat\b|\bsoybeans\b|\bsi\b|\bgc\b|\bcl\b|\bhg\b|\bpl\b|\bpa\b|\bng\b|\brb\b|\bzc\b|\bzw\b|\bzs\b)'
        r'.*?(?:settle|close|trade|hit|reach|be)\s+(?:at|over|above|below|under|between)?\s*'
        r'\$?(\d+(?:,\d{3})?(?:\.\d+)?)',
        q_lower, re.I
    )
    if not m1:
        return None

    commodity_name = m1.group(1).lower()
    strike_str = m1.group(2).replace(",", "")

    try:
        strike = float(strike_str)
    except ValueError:
        return None

    symbol = COMMODITY_MAP.get(commodity_name)
    if not symbol:
        return None

    # Check if it's a range market
    range_match = re.search(r'between\s+\$?(\d+(?:,\d{3})?(?:\.\d+)?)\s*(?:and|-|to)\s*\$?(\d+(?:,\d{3})?(?:\.\d+)?)', q_lower)
    is_range = bool(range_match)
    if is_range:
        high_strike = float(range_match.group(2).replace(",", ""))

    # Extract direction: "over/above" = YES, "below/under" = NO
    direction_hint = "YES"
    if re.search(r'(below|under|less than)\s', q_lower):
        direction_hint = "NO"

    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="1mo")

        if hist.empty or len(hist) < 2:
            return None

        spot = hist["Close"].iloc[-1]
        returns = hist["Close"].pct_change().dropna()
        daily_vol = returns.std()

        if daily_vol == 0 or np.isnan(daily_vol):
            return None

        # Annualized volatility
        ann_vol = daily_vol * math.sqrt(252)

        # Days to expiry — extract from question or default to 30
        expiry_date = _extract_date(question)
        if expiry_date:
            try:
                exp_dt = datetime.strptime(expiry_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                days_left = max(1, (exp_dt - datetime.now(timezone.utc)).days)
            except ValueError:
                days_left = 30
        else:
            days_left = 30

        if is_range:
            # Probability spot settles in [strike, high_strike] range
            fair_value = _commodity_range_probability(spot, strike, high_strike, ann_vol, days_left)
        else:
            # Probability spot settles above/below strike
            fair_value = _commodity_threshold_probability(spot, strike, ann_vol, days_left)

        # Apply direction hint
        if direction_hint == "NO":
            fair_value = 1.0 - fair_value

        direction = "YES" if fair_value > yes_price else "NO"
        source = f"yfinance {symbol} spot=${spot:.2f} vol={ann_vol*100:.1f}% {days_left}d to expiry"

        return fair_value, direction, source

    except Exception:
        return None


def _commodity_threshold_probability(spot, strike, ann_vol, days_left):
    """P(settle > strike) using log-normal distribution."""
    if spot <= 0 or strike <= 0:
        return 0.5
    t = days_left / 365.0
    if ann_vol <= 0 or t <= 0:
        return 0.5 if spot > strike else 0.5
    # d1 from Black-Scholes framework (no drift assumption)
    d1 = (math.log(spot / strike) + 0.5 * ann_vol * ann_vol * t) / (ann_vol * math.sqrt(t))
    return _normal_cdf(d1)


def _commodity_range_probability(spot, low_strike, high_strike, ann_vol, days_left):
    """P(low_strike < settle < high_strike) using log-normal."""
    p_above_low = _commodity_threshold_probability(spot, low_strike, ann_vol, days_left)
    p_above_high = _commodity_threshold_probability(spot, high_strike, ann_vol, days_left)
    return max(0.0, p_above_low - p_above_high)


# ═══════════════════════════════════════════════════════════════════════════════
# 4. ECONOMIC — FRED API (CPI, GDP, Unemployment, Fed Funds)
# ═══════════════════════════════════════════════════════════════════════════════

def _try_economic_market(q_lower, question, yes_price):
    """Handle economic data markets using real FRED data.
    
    Requires FRED_API_KEY environment variable. Returns None if no key set.
    
    Supports:
      - CPI threshold: "Will CPI exceed 3% in June 2026?"
      - Unemployment: "Will unemployment be above 4.5%?"
      - GDP range: "Will GDP growth be between 1.0% and 1.5%?"
      - Fed funds: "Will Fed funds rate be above 5%?"
      - Jobs: "Will nonfarm payrolls exceed 200K?"
    """
    if not FRED_API_KEY:
        return None  # No key configured — skip

    # Extract the indicator and threshold from the question
    indicator, threshold, is_above = _parse_economic_question(q_lower)
    if not indicator or threshold is None:
        return None

    series_id = FRED_SERIES.get(indicator)
    if not series_id:
        return None

    try:
        latest_value, unit = _fetch_fred_latest(series_id)
        if latest_value is None:
            return None

        # Compute fair value based on indicator type
        if indicator in ("fed funds", "federal funds", "interest rate"):
            # Fed funds rate — use current rate as fair value
            fair_value = _fed_funds_probability(latest_value, threshold, is_above)
        elif indicator in ("cpi", "core cpi"):
            # CPI YoY% — use current + trend
            fair_value = _cpi_threshold_probability(latest_value, threshold, is_above)
        elif indicator in ("unemployment", "unemployment rate"):
            fair_value = _unemployment_probability(latest_value, threshold, is_above)
        elif indicator in ("gdp", "gdp growth"):
            fair_value = _gdp_probability(latest_value, threshold, is_above)
        elif indicator in ("nonfarm", "nonfarm payrolls", "jobs"):
            fair_value = _jobs_probability(latest_value, threshold, is_above)
        else:
            # Generic: use normal distribution around latest value
            fair_value = _generic_threshold_probability(latest_value, threshold, is_above)

        direction = "YES" if fair_value > yes_price else "NO"
        unit_str = unit if unit else ""
        source = f"FRED {series_id}: {latest_value:.2f}{unit_str} — threshold {threshold}{unit_str}"

        return fair_value, direction, source

    except Exception:
        return None


def _parse_economic_question(q_lower):
    """Extract indicator name and threshold from question text.
    Returns (indicator, threshold, is_above) or (None, None, None).
    """
    # Match patterns like "CPI exceed 3%", "unemployment above 4.5%", "GDP between 1.0 and 1.5"
    threshold = None
    is_above = True
    indicator = None

    # Check for known indicators
    for keyword in sorted(FRED_SERIES.keys(), key=len, reverse=True):
        if keyword in q_lower:
            indicator = keyword
            break

    if not indicator:
        return None, None, None

    # Extract threshold value
    # Pattern: "above/below/exceed X%" or "between X and Y"
    range_match = re.search(r'between\s+(-?\d+\.?\d*)\s*%?\s*(?:and|to|-)\s*(-?\d+\.?\d*)', q_lower)
    if range_match:
        # Range market — use midpoint as threshold for now
        low = float(range_match.group(1))
        high = float(range_match.group(2))
        threshold = (low + high) / 2
        return indicator, threshold, is_above

    # Single threshold
    m = re.search(r'(?:above|over|exceed|exceeds|greater than|higher than|more than)\s+(-?\d+\.?\d*)', q_lower)
    if m:
        threshold = float(m.group(1))
        is_above = True
        return indicator, threshold, is_above

    m = re.search(r'(?:below|under|less than|lower than|beneath)\s+(-?\d+\.?\d*)', q_lower)
    if m:
        threshold = float(m.group(1))
        is_above = False
        return indicator, threshold, is_above

    # Bare number with % sign
    m = re.search(r'(-?\d+\.?\d*)\s*%', q_lower)
    if m:
        threshold = float(m.group(1))
        # Default: above if threshold > current typical value
        is_above = True
        return indicator, threshold, is_above

    return None, None, None


def _fetch_fred_latest(series_id):
    """Fetch latest observation from FRED API.
    Returns (value, unit_label) or (None, None).
    """
    url = (
        f"{FRED_API_BASE}?series_id={series_id}"
        f"&sort_order=desc&limit=3"
        f"&api_key={FRED_API_KEY}&file_type=json"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "rats-quant/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())

    observations = data.get("observations", [])
    if not observations:
        return None, None

    # Find the latest non-"." value
    for obs in observations:
        val = obs.get("value", ".").strip()
        if val and val != ".":
            try:
                return float(val), "%"
            except ValueError:
                pass

    return None, None


def _fed_funds_probability(current_rate, threshold, is_above):
    """Probability Fed funds rate is above/below threshold.
    Uses current rate with ~25bps std dev (typical FOMC step).
    """
    std_dev = 0.25  # 25 basis points
    if is_above:
        return 1.0 - _normal_cdf((threshold - current_rate) / std_dev)
    else:
        return _normal_cdf((threshold - current_rate) / std_dev)


def _cpi_threshold_probability(current_cpi, threshold, is_above):
    """Probability CPI YoY% is above/below threshold.
    Uses current CPI with ~0.3% std dev (typical monthly volatility).
    """
    std_dev = 0.30
    if is_above:
        return 1.0 - _normal_cdf((threshold - current_cpi) / std_dev)
    else:
        return _normal_cdf((threshold - current_cpi) / std_dev)


def _unemployment_probability(current_rate, threshold, is_above):
    """Probability unemployment rate is above/below threshold.
    Uses current rate with ~0.2% std dev.
    """
    std_dev = 0.20
    if is_above:
        return 1.0 - _normal_cdf((threshold - current_rate) / std_dev)
    else:
        return _normal_cdf((threshold - current_rate) / std_dev)


def _gdp_probability(current_gdp, threshold, is_above):
    """Probability GDP growth is above/below threshold.
    Uses current GDP with ~0.5% std dev (quarterly data is smoother).
    """
    std_dev = 0.50
    if is_above:
        return 1.0 - _normal_cdf((threshold - current_gdp) / std_dev)
    else:
        return _normal_cdf((threshold - current_gdp) / std_dev)


def _jobs_probability(current_jobs, threshold, is_above):
    """Probability nonfarm payrolls are above/below threshold (in thousands).
    Jobs numbers have ~100K std dev month-to-month.
    """
    std_dev = 100.0  # in thousands
    if is_above:
        return 1.0 - _normal_cdf((threshold - current_jobs) / std_dev)
    else:
        return _normal_cdf((threshold - current_jobs) / std_dev)


def _generic_threshold_probability(current_value, threshold, is_above):
    """Generic threshold probability for any indicator.
    Uses 5% of current value as std dev.
    """
    std_dev = max(abs(current_value) * 0.05, 0.01)
    if is_above:
        return 1.0 - _normal_cdf((threshold - current_value) / std_dev)
    else:
        return _normal_cdf((threshold - current_value) / std_dev)


# ═══════════════════════════════════════════════════════════════════════════════
# WEATHER API HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _fetch_temperature(coords, date_str, is_max, unit):
    """Fetch temperature forecast from Open-Meteo."""
    lat, lon = coords
    temp_var = "temperature_2m_max" if is_max else "temperature_2m_min"
    temp_unit = "fahrenheit" if unit == "f" else "celsius"

    url = (
        f"{OPEN_METEO_BASE}?latitude={lat}&longitude={lon}"
        f"&daily={temp_var}"
        f"&temperature_unit={temp_unit}"
        f"&start_date={date_str}&end_date={date_str}"
        f"&timezone=auto"
    )

    req = urllib.request.Request(url, headers={"User-Agent": "rats-quant/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())

    values = data.get("daily", {}).get(temp_var, [])
    if values and values[0] is not None:
        return float(values[0])
    return None


def _fetch_precipitation(coords, date_str):
    """Fetch precipitation sum forecast from Open-Meteo (in mm)."""
    lat, lon = coords
    url = (
        f"{OPEN_METEO_BASE}?latitude={lat}&longitude={lon}"
        f"&daily=precipitation_sum"
        f"&start_date={date_str}&end_date={date_str}"
        f"&timezone=auto"
    )

    req = urllib.request.Request(url, headers={"User-Agent": "rats-quant/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())

    values = data.get("daily", {}).get("precipitation_sum", [])
    if values and values[0] is not None:
        return float(values[0])
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# DATE PARSING
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_date(question):
    """Extract a date from a market question. Returns YYYY-MM-DD or None."""
    months = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
        "jan": 1, "feb": 2, "mar": 3, "apr": 4,
        "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }

    # "on April 23" or "on March 18, 2026"
    match = re.search(
        r'\bon\s+([a-z]+)\s+(\d{1,2})(?:\s*,?\s*(\d{4}))?',
        question, re.I
    )
    if match:
        month_str = match.group(1).lower()
        day = int(match.group(2))
        year = int(match.group(3)) if match.group(3) else datetime.now().year
        month = months.get(month_str)
        if month:
            return f"{year}-{month:02d}-{day:02d}"

    # ISO date in slug: 2026-04-23
    iso_match = re.search(r'(\d{4}-\d{2}-\d{2})', question)
    if iso_match:
        return iso_match.group(1)

    # "final/last trading day of MONTH YEAR" — use last calendar day of month
    final_day = re.search(
        r'(?:final|last)\s+(?:trading\s+)?day\s+of\s+([a-z]+)\s+(\d{4})',
        question, re.I
    )
    if final_day:
        month = months.get(final_day.group(1).lower())
        year = int(final_day.group(2))
        if month:
            if month == 12:
                return f"{year}-12-31"
            next_month = datetime(year, month + 1, 1)
            last_day = (next_month - timedelta(days=1)).day
            return f"{year}-{month:02d}-{last_day:02d}"

    # "by June 30" or "by June 30, 2026"
    by_match = re.search(r'\bby\s+([a-z]+)\s+(\d{1,2})(?:\s*,?\s*(\d{4}))?', question, re.I)
    if by_match:
        month_str = by_match.group(1).lower()
        day = int(by_match.group(2))
        year = int(by_match.group(3)) if by_match.group(3) else datetime.now().year
        month = months.get(month_str)
        if month:
            return f"{year}-{month:02d}-{day:02d}"

    # "in June 2026" — use last day of month (for settlement contexts)
    in_month = re.search(r'\bin\s+([a-z]+)\s+(\d{4})', question, re.I)
    if in_month:
        month = months.get(in_month.group(1).lower())
        year = int(in_month.group(2))
        if month:
            if month == 12:
                return f"{year}-12-31"
            next_month = datetime(year, month + 1, 1)
            last_day = (next_month - timedelta(days=1)).day
            return f"{year}-{month:02d}-{last_day:02d}"

    return None


# ═══════════════════════════════════════════════════════════════════════════════
# PROBABILITY FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def _temp_threshold_probability(forecast, target):
    """Probability that actual temp hits exactly target (±0.5°C).
    Uses gaussian with std_dev of ~2°C for forecast uncertainty.
    """
    std_dev = 2.0
    z_low = (target - 0.5 - forecast) / std_dev
    z_high = (target + 0.5 - forecast) / std_dev
    return _normal_cdf(z_high) - _normal_cdf(z_low)


def _temp_range_probability(forecast, low, high):
    """Probability that actual temp falls in [low, high] range."""
    std_dev = 2.0
    z_low = (low - forecast) / std_dev
    z_high = (high - forecast) / std_dev
    return _normal_cdf(z_high) - _normal_cdf(z_low)


def _range_probability_gaussian(forecast, std_dev, low, high):
    """Generic range probability using gaussian."""
    z_low = (low - forecast) / std_dev
    z_high = (high - forecast) / std_dev
    return _normal_cdf(z_high) - _normal_cdf(z_low)


def _normal_cdf(x):
    """Approximation of the standard normal CDF."""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


# ═══════════════════════════════════════════════════════════════════════════════
# STANDALONE TEST
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    test_markets = [
        # Weather — known city (should work)
        {"slug": "highest-temperature-in-shanghai-on-june-17-2026-28c",
         "question": "Will the highest temperature in Shanghai be 28°C on June 17?",
         "yes": 0.355, "no": 0.645},
        # Weather — unknown city (geocoding test)
        {"slug": "highest-temperature-in-wellington-on-june-17-2026-14c",
         "question": "Will the highest temperature in Wellington be 14°C on June 17?",
         "yes": 0.26, "no": 0.74},
        # Commodity — silver
        {"slug": "si-above-65-jun-2026",
         "question": "Will Silver (SI) settle over $65 on the final trading day of June 2026?",
         "yes": 0.785, "no": 0.215},
        # Commodity — gold
        {"slug": "gc-settle-4600-5000-jun-2026",
         "question": "Will Gold (GC) settle at $4,600-$5,000 in June?",
         "yes": 0.059, "no": 0.941},
        # Economic — CPI (requires FRED key)
        {"slug": "will-cpi-exceed-3-percent-june-2026",
         "question": "Will CPI exceed 3% in June 2026?",
         "yes": 0.42, "no": 0.58},
        # Economic — unemployment
        {"slug": "will-unemployment-be-above-4-percent",
         "question": "Will the unemployment rate be above 4%?",
         "yes": 0.35, "no": 0.65},
        # Economic — Fed funds
        {"slug": "fed-funds-rate-above-5-percent",
         "question": "Will the Fed funds rate be above 5%?",
         "yes": 0.50, "no": 0.50},
    ]

    print("=" * 70)
    print("FAIR VALUE ENGINE — MODULE TEST")
    print("=" * 70)

    for m in test_markets:
        fv, direction, source = estimate_fair_value(m)
        print(f"\n{m['question'][:65]}")
        print(f"  Market: YES={m['yes']} NO={m['no']}")
        if fv is not None:
            edge_pct = round((fv - m['yes']) / m['yes'] * 100, 1) if m['yes'] > 0 else 0
            print(f"  Fair value: {fv:.4f} → {direction} (edge: {edge_pct}%)")
            print(f"  Source: {source}")
        else:
            print(f"  No external data available")