import os
import time
import requests
import json
from datetime import datetime, timezone

# ─────────────────────────────────────────────
# CONFIG  (set these as environment variables)
# ─────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "")

# ─────────────────────────────────────────────
# GEM FILTERS  (tune these to your taste)
# ─────────────────────────────────────────────
MIN_VOLUME_USD       = 1_500    # minimum total volume since token created
MIN_VOLUME_5M        = 300      # minimum volume in last 5 minutes (momentum check)
MAX_AGE_MINUTES      = 90       # ignore tokens older than this
MIN_MCAP_USD         = 1_000    # minimum estimated market cap
MAX_MCAP_USD         = 800_000  # maximum estimated market cap
MIN_BUY_PRESSURE     = 52       # minimum buy % (buys vs sells)
MAX_TOP_WALLET_PCT   = 75       # reject if 1 wallet holds >X% of buys
MIN_LIQUIDITY_USD    = 500      # minimum pool liquidity
SCAN_INTERVAL_SEC    = 8        # how often to poll DEXScreener (seconds)
CHAINS               = ["bsc", "solana"]  # chains to scan

# ─────────────────────────────────────────────
# DEDUPLICATION  (don't re-alert same token)
# ─────────────────────────────────────────────
alerted: set = set()

# ─────────────────────────────────────────────
# SCORING  (mirrors your Dune gem_score logic)
# ─────────────────────────────────────────────
NARRATIVE_KEYWORDS = {
    "high": ["trump","maga","elon","musk","grok","ai","agent","gpt","llm","neural","agi","viral","trending","breaking"],
    "mid":  ["dragon","zodiac","lunar","diwali","festival","sakura","asia","china","japan","india","korea","thai","viet","panda","anime"],
    "low":  ["meme","pepe","doge","degen","alpha","gem","pump","moon","wagmi","bonk","wif","popcat","frog","ape"],
    "luck": ["888","777","lucky","fortune","gold","rich","wealth","lambo","jackpot","diamond"],
}

def narrative_score(symbol: str) -> float:
    s = (symbol or "").lower()
    score = 0.0
    for kw in NARRATIVE_KEYWORDS["high"]:
        if kw in s: score += 40; break
    for kw in NARRATIVE_KEYWORDS["mid"]:
        if kw in s: score += 30; break
    for kw in NARRATIVE_KEYWORDS["low"]:
        if kw in s: score += 20; break
    for kw in NARRATIVE_KEYWORDS["luck"]:
        if kw in s: score += 10; break
    return min(score, 100.0)

def gem_score(pair: dict) -> float:
    try:
        vol5m  = float(pair.get("volume", {}).get("m5", 0) or 0)
        vol1h  = float(pair.get("volume", {}).get("h1", 0) or 0)
        volAll = vol5m + vol1h
        buys5m  = int(pair.get("txns", {}).get("m5", {}).get("buys",  0) or 0)
        sells5m = int(pair.get("txns", {}).get("m5", {}).get("sells", 0) or 0)
        total5m = buys5m + sells5m or 1

        buy_pct      = 100.0 * buys5m / total5m
        impulse      = min(vol5m / max(vol1h / 12, 1), 4.0)   # vs avg 5m slice of last hour
        narr         = narrative_score(pair.get("baseToken", {}).get("symbol", ""))
        age_min      = pair.get("_age_minutes", 999)

        freshness = (
            100 if age_min <= 15 else
             80 if age_min <= 45 else
             55 if age_min <= 90 else 30
        )

        score = (
            0.30 * min(buy_pct, 100)
          + 0.25 * impulse * 25
          + 0.20 * narr
          + 0.15 * freshness
          + 0.10 * min(buys5m * 10, 100)
        )
        return round(min(score, 100.0), 1)
    except Exception:
        return 0.0

def risk_score(pair: dict) -> float:
    try:
        buys_total  = int(pair.get("txns", {}).get("h1", {}).get("buys",  0) or 0)
        sells_total = int(pair.get("txns", {}).get("h1", {}).get("sells", 0) or 0)
        total       = buys_total + sells_total or 1
        buy_pct     = 100.0 * buys_total / total

        liq     = float(pair.get("liquidity", {}).get("usd", 0) or 0)
        mcap    = float(pair.get("marketCap", 0) or 0)
        age_min = pair.get("_age_minutes", 999)

        # concentration proxy: low liq vs high mcap is suspicious
        conc_risk = 80 if (mcap > 100_000 and liq < 5_000) else 20

        bp_risk = (
            90 if buy_pct < 40 else
            50 if buy_pct < 55 else
            20 if buy_pct < 70 else 5
        )

        trader_risk = (
            30 if age_min <= 15 and buys_total <  5 else
            15 if age_min <= 15 and buys_total < 15 else
             5 if age_min <= 15 else
            80 if buys_total <  5 else
            45 if buys_total < 15 else
            20 if buys_total < 40 else 5
        )

        score = (
            0.30 * bp_risk
          + 0.40 * trader_risk
          + 0.30 * conc_risk
        )
        return round(min(score, 100.0), 1)
    except Exception:
        return 99.0

# ─────────────────────────────────────────────
# DEXScreener API
# ─────────────────────────────────────────────
DEXSCREENER_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
NEW_PAIRS_URL   = "https://api.dexscreener.com/latest/dex/tokens/{address}"

def fetch_new_pairs(chain: str) -> list:
    """Poll DEXScreener for latest token profiles on a given chain."""
    try:
        url = f"https://api.dexscreener.com/token-profiles/latest/v1"
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return []
        data = r.json()
        # Filter to requested chain
        return [p for p in (data if isinstance(data, list) else [])
                if (p.get("chainId") or "").lower() == chain.lower()]
    except Exception as e:
        print(f"[{chain}] fetch error: {e}")
        return []

def fetch_pair_detail(chain: str, token_address: str) -> list:
    """Get full pair data for a token address."""
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return []
        data = r.json()
        pairs = data.get("pairs") or []
        return [p for p in pairs if (p.get("chainId") or "").lower() == chain.lower()]
    except Exception as e:
        print(f"[{chain}] pair detail error: {e}")
        return []

def age_minutes(pair: dict) -> float:
    """Minutes since pair creation."""
    try:
        created_at = pair.get("pairCreatedAt")  # Unix ms
        if created_at:
            now_ms = datetime.now(timezone.utc).timestamp() * 1000
            return (now_ms - float(created_at)) / 60_000
    except Exception:
        pass
    return 9999

# ─────────────────────────────────────────────
# FILTERING
# ─────────────────────────────────────────────
def passes_filters(pair: dict) -> tuple[bool, str]:
    """Returns (passes, reason_if_fails)."""
    age   = pair.get("_age_minutes", 9999)
    vol5m = float(pair.get("volume", {}).get("m5", 0) or 0)
    vol1h = float(pair.get("volume", {}).get("h1", 0) or 0)
    vol_total = vol5m + vol1h
    mcap  = float(pair.get("marketCap", 0) or 0)
    liq   = float(pair.get("liquidity", {}).get("usd", 0) or 0)
    buys5m  = int(pair.get("txns", {}).get("m5", {}).get("buys",  0) or 0)
    sells5m = int(pair.get("txns", {}).get("m5", {}).get("sells", 0) or 0)
    total5m = buys5m + sells5m or 1
    buy_pct = 100.0 * buys5m / total5m

    if age > MAX_AGE_MINUTES:
        return False, f"too old ({age:.0f}m)"
    if vol_total < MIN_VOLUME_USD:
        return False, f"low volume (${vol_total:,.0f})"
    if vol5m < MIN_VOLUME_5M:
        return False, f"no 5m momentum (${vol5m:,.0f})"
    if mcap and mcap < MIN_MCAP_USD:
        return False, f"mcap too low (${mcap:,.0f})"
    if mcap and mcap > MAX_MCAP_USD:
        return False, f"mcap too high (${mcap:,.0f})"
    if liq < MIN_LIQUIDITY_USD:
        return False, f"thin liquidity (${liq:,.0f})"
    if buy_pct < MIN_BUY_PRESSURE:
        return False, f"low buy pressure ({buy_pct:.0f}%)"
    return True, ""

# ─────────────────────────────────────────────
# ALERT FORMATTING
# ─────────────────────────────────────────────
def format_alert(pair: dict, g_score: float, r_score: float) -> str:
    chain     = (pair.get("chainId") or "").upper()
    symbol    = pair.get("baseToken", {}).get("symbol", "???")
    name      = pair.get("baseToken", {}).get("name", "")
    address   = pair.get("baseToken", {}).get("address", "")
    age       = pair.get("_age_minutes", 0)
    price     = float(pair.get("priceUsd") or 0)
    mcap      = float(pair.get("marketCap") or 0)
    liq       = float(pair.get("liquidity", {}).get("usd") or 0)
    vol5m     = float(pair.get("volume", {}).get("m5") or 0)
    vol1h     = float(pair.get("volume", {}).get("h1") or 0)
    buys5m    = int(pair.get("txns", {}).get("m5", {}).get("buys",  0) or 0)
    sells5m   = int(pair.get("txns", {}).get("m5", {}).get("sells", 0) or 0)
    pair_url  = pair.get("url", "")
    dex       = pair.get("dexId", "")

    total5m   = buys5m + sells5m or 1
    buy_pct   = 100.0 * buys5m / total5m

    # Grade
    if g_score >= 70 and r_score <= 35:
        grade, action = "A+", "🚀 ACT NOW"
    elif g_score >= 60 and r_score <= 45:
        grade, action = "A",  "⚡ STRONG SIGNAL"
    elif g_score >= 50 and r_score <= 55:
        grade, action = "B",  "👀 ENTER"
    elif g_score >= 40 and r_score <= 65:
        grade, action = "C",  "📊 WATCH"
    else:
        grade, action = "D",  "📋 MONITOR"

    narr = narrative_score(symbol)
    narr_bar = "█" * int(narr / 20) + "░" * (5 - int(narr / 20))

    dex_link = f"https://dexscreener.com/{(pair.get('chainId') or '').lower()}/{address}"

    msg = (
        f"{'='*32}\n"
        f"{action}  |  Grade: {grade}  |  {chain}\n"
        f"{'='*32}\n"
        f"🪙 *{symbol}* — {name}\n"
        f"⏱ Age: {age:.0f}m  |  DEX: {dex}\n\n"
        f"📊 *Scores*\n"
        f"  Gem:  {g_score:.0f}/100\n"
        f"  Risk: {r_score:.0f}/100\n"
        f"  Narr: {narr_bar} {narr:.0f}/100\n\n"
        f"💰 *Market*\n"
        f"  Price:  ${price:.8f}\n"
        f"  MCap:   ${mcap:,.0f}\n"
        f"  Liq:    ${liq:,.0f}\n\n"
        f"📈 *Volume*\n"
        f"  5m:  ${vol5m:,.0f}\n"
        f"  1h:  ${vol1h:,.0f}\n\n"
        f"🔄 *Trades (5m)*\n"
        f"  Buys: {buys5m}  Sells: {sells5m}  Buy%: {buy_pct:.0f}%\n\n"
        f"🔗 [View on DEXScreener]({dex_link})\n"
        f"`{address}`\n"
    )
    return msg

# ─────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────
def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[TELEGRAM] Not configured — printing alert to console:\n")
        print(message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code != 200:
            print(f"[TELEGRAM] Error {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[TELEGRAM] Send error: {e}")

# ─────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────
def scan_chain(chain: str):
    profiles = fetch_new_pairs(chain)
    found = 0
    for profile in profiles:
        token_address = profile.get("tokenAddress") or profile.get("address")
        if not token_address:
            continue

        # Get full pair data with volumes + txns
        pairs = fetch_pair_detail(chain, token_address)
        if not pairs:
            continue

        # Pick the pair with highest liquidity
        pairs.sort(key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0), reverse=True)
        pair = pairs[0]

        # Attach age
        pair["_age_minutes"] = age_minutes(pair)

        # Dedup key
        key = f"{chain}:{token_address}"
        if key in alerted:
            continue

        passes, reason = passes_filters(pair)
        if not passes:
            continue

        g = gem_score(pair)
        r = risk_score(pair)

        # Only alert if gem score decent and risk not too high
        if g < 35 or r > 70:
            continue

        alerted.add(key)
        found += 1

        symbol = pair.get("baseToken", {}).get("symbol", "???")
        print(f"[{chain.upper()}] 🔥 {symbol} | gem={g} risk={r} age={pair['_age_minutes']:.0f}m")
        msg = format_alert(pair, g, r)
        send_telegram(msg)

    return found

def main():
    print("="*40)
    print("  GEM HUNTER BOT — DEXScreener Edition")
    print(f"  Chains: {', '.join(c.upper() for c in CHAINS)}")
    print(f"  Interval: {SCAN_INTERVAL_SEC}s")
    print(f"  Max age: {MAX_AGE_MINUTES}m")
    print(f"  Min volume: ${MIN_VOLUME_USD:,}")
    print("="*40)

    if not TELEGRAM_BOT_TOKEN:
        print("\n⚠️  TELEGRAM_BOT_TOKEN not set — alerts will print to console only\n")

    scan_count = 0
    while True:
        scan_count += 1
        ts = datetime.now().strftime("%H:%M:%S")
        total = 0
        for chain in CHAINS:
            total += scan_chain(chain)
            time.sleep(1)   # small gap between chains to avoid rate limits

        if scan_count % 30 == 0:   # print heartbeat every ~4 min
            print(f"[{ts}] ❤️  Still running — scan #{scan_count} | alerted so far: {len(alerted)}")

        time.sleep(SCAN_INTERVAL_SEC)

if __name__ == "__main__":
    main()
