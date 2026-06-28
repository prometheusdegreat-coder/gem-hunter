import os
import time
import requests
from datetime import datetime, timezone

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

# ─────────────────────────────────────────────
# FILTERS — loosened so more tokens surface
# ─────────────────────────────────────────────
MIN_VOLUME_USD    = 500      # lowered from 1500
MIN_VOLUME_5M     = 50       # lowered from 300
MAX_AGE_MINUTES   = 120      # raised from 90
MIN_MCAP_USD      = 500      # lowered from 1000
MAX_MCAP_USD      = 1_000_000 # raised from 800k
MIN_BUY_PRESSURE  = 45       # lowered from 52
MIN_LIQUIDITY_USD = 100      # lowered from 500
SCAN_INTERVAL_SEC = 8
CHAINS            = ["bsc", "solana"]

alerted: set = set()

# ─────────────────────────────────────────────
# SCORING
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
        vol5m   = float(pair.get("volume", {}).get("m5", 0) or 0)
        vol1h   = float(pair.get("volume", {}).get("h1", 0) or 0)
        buys5m  = int(pair.get("txns", {}).get("m5", {}).get("buys",  0) or 0)
        sells5m = int(pair.get("txns", {}).get("m5", {}).get("sells", 0) or 0)
        total5m = buys5m + sells5m or 1
        buy_pct = 100.0 * buys5m / total5m
        impulse = min(vol5m / max(vol1h / 12, 1), 4.0)
        narr    = narrative_score(pair.get("baseToken", {}).get("symbol", ""))
        age_min = pair.get("_age_minutes", 999)
        freshness = (100 if age_min <= 15 else 80 if age_min <= 45 else
                      55 if age_min <= 90 else 30)
        return round(min(
            0.30 * min(buy_pct, 100)
          + 0.25 * impulse * 25
          + 0.20 * narr
          + 0.15 * freshness
          + 0.10 * min(buys5m * 10, 100),
        100.0), 1)
    except Exception:
        return 0.0

def risk_score(pair: dict) -> float:
    try:
        buys  = int(pair.get("txns", {}).get("h1", {}).get("buys",  0) or 0)
        sells = int(pair.get("txns", {}).get("h1", {}).get("sells", 0) or 0)
        total = buys + sells or 1
        buy_pct = 100.0 * buys / total
        liq   = float(pair.get("liquidity", {}).get("usd", 0) or 0)
        mcap  = float(pair.get("marketCap", 0) or 0)
        age   = pair.get("_age_minutes", 999)
        conc  = 80 if (mcap > 100_000 and liq < 5_000) else 20
        bp    = 90 if buy_pct < 40 else 50 if buy_pct < 55 else 20 if buy_pct < 70 else 5
        tr    = (30 if age <= 15 and buys < 5 else
                 15 if age <= 15 and buys < 15 else
                  5 if age <= 15 else
                 80 if buys < 5 else
                 45 if buys < 15 else
                 20 if buys < 40 else 5)
        return round(min(0.30 * bp + 0.40 * tr + 0.30 * conc, 100.0), 1)
    except Exception:
        return 99.0

# ─────────────────────────────────────────────
# DEXSCREENER
# ─────────────────────────────────────────────
def fetch_new_pairs(chain: str) -> list:
    try:
        r = requests.get(
            "https://api.dexscreener.com/token-profiles/latest/v1",
            timeout=10
        )
        if r.status_code != 200:
            return []
        data = r.json()
        return [p for p in (data if isinstance(data, list) else [])
                if (p.get("chainId") or "").lower() == chain.lower()]
    except Exception as e:
        print(f"[{chain}] fetch error: {e}")
        return []

def fetch_pair_detail(chain: str, token_address: str) -> list:
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{token_address}",
            timeout=10
        )
        if r.status_code != 200:
            return []
        pairs = r.json().get("pairs") or []
        return [p for p in pairs
                if (p.get("chainId") or "").lower() == chain.lower()]
    except Exception as e:
        print(f"[{chain}] detail error: {e}")
        return []

def age_minutes(pair: dict) -> float:
    try:
        created_at = pair.get("pairCreatedAt")
        if created_at:
            now_ms = datetime.now(timezone.utc).timestamp() * 1000
            return (now_ms - float(created_at)) / 60_000
    except Exception:
        pass
    return 9999

# ─────────────────────────────────────────────
# FILTERING
# ─────────────────────────────────────────────
def passes_filters(pair: dict) -> tuple:
    age     = pair.get("_age_minutes", 9999)
    vol5m   = float(pair.get("volume", {}).get("m5", 0) or 0)
    vol1h   = float(pair.get("volume", {}).get("h1", 0) or 0)
    vol_tot = vol5m + vol1h
    mcap    = float(pair.get("marketCap", 0) or 0)
    liq     = float(pair.get("liquidity", {}).get("usd", 0) or 0)
    buys5m  = int(pair.get("txns", {}).get("m5", {}).get("buys",  0) or 0)
    sells5m = int(pair.get("txns", {}).get("m5", {}).get("sells", 0) or 0)
    total5m = buys5m + sells5m or 1
    buy_pct = 100.0 * buys5m / total5m

    if age > MAX_AGE_MINUTES:        return False, f"too old ({age:.0f}m)"
    if vol_tot < MIN_VOLUME_USD:     return False, f"low volume (${vol_tot:,.0f})"
    if vol5m < MIN_VOLUME_5M:        return False, f"no 5m momentum (${vol5m:,.0f})"
    if mcap and mcap < MIN_MCAP_USD: return False, f"mcap too low (${mcap:,.0f})"
    if mcap and mcap > MAX_MCAP_USD: return False, f"mcap too high (${mcap:,.0f})"
    if liq < MIN_LIQUIDITY_USD:      return False, f"thin liquidity (${liq:,.0f})"
    if buy_pct < MIN_BUY_PRESSURE:   return False, f"low buys ({buy_pct:.0f}%)"
    return True, ""

# ─────────────────────────────────────────────
# DISCORD ALERT — full token info
# ─────────────────────────────────────────────
def send_discord(pair: dict, g: float, r: float):
    base     = pair.get("baseToken", {})
    quote    = pair.get("quoteToken", {})
    symbol   = base.get("symbol", "???")
    name     = base.get("name", "Unknown")
    address  = base.get("address", "")
    chain    = (pair.get("chainId") or "").upper()
    dex      = pair.get("dexId", "unknown").upper()
    age      = pair.get("_age_minutes", 0)

    # Price
    price_usd  = float(pair.get("priceUsd") or 0)
    price_native = pair.get("priceNative", "?")

    # Market
    mcap    = float(pair.get("marketCap") or pair.get("fdv") or 0)
    fdv     = float(pair.get("fdv") or 0)
    liq     = float(pair.get("liquidity", {}).get("usd") or 0)

    # Volume
    vol5m   = float(pair.get("volume", {}).get("m5")  or 0)
    vol1h   = float(pair.get("volume", {}).get("h1")  or 0)
    vol6h   = float(pair.get("volume", {}).get("h6")  or 0)
    vol24h  = float(pair.get("volume", {}).get("h24") or 0)

    # Price change %
    pc5m    = pair.get("priceChange", {}).get("m5",  "?")
    pc1h    = pair.get("priceChange", {}).get("h1",  "?")
    pc6h    = pair.get("priceChange", {}).get("h6",  "?")
    pc24h   = pair.get("priceChange", {}).get("h24", "?")

    # Trades
    buys5m   = int(pair.get("txns", {}).get("m5",  {}).get("buys",  0) or 0)
    sells5m  = int(pair.get("txns", {}).get("m5",  {}).get("sells", 0) or 0)
    buys1h   = int(pair.get("txns", {}).get("h1",  {}).get("buys",  0) or 0)
    sells1h  = int(pair.get("txns", {}).get("h1",  {}).get("sells", 0) or 0)
    buys24h  = int(pair.get("txns", {}).get("h24", {}).get("buys",  0) or 0)
    sells24h = int(pair.get("txns", {}).get("h24", {}).get("sells", 0) or 0)
    total5m  = buys5m + sells5m or 1
    buy_pct  = 100.0 * buys5m / total5m

    narr     = narrative_score(symbol)
    dex_link = f"https://dexscreener.com/{(pair.get('chainId') or '').lower()}/{address}"

    # Grade + color
    if   g >= 70 and r <= 35: grade, action, color = "A+", "🚀 ACT NOW",       0x00C853
    elif g >= 60 and r <= 45: grade, action, color = "A",  "⚡ STRONG SIGNAL",  0x64DD17
    elif g >= 50 and r <= 55: grade, action, color = "B",  "👀 ENTER",          0xFFD600
    elif g >= 40 and r <= 65: grade, action, color = "C",  "📊 WATCH",          0xFF6D00
    else:                      grade, action, color = "D",  "📋 MONITOR",        0x9E9E9E

    def fmt_pct(v):
        try:
            f = float(v)
            return f"+{f:.1f}%" if f >= 0 else f"{f:.1f}%"
        except Exception:
            return str(v)

    embed = {
        "title": f"{action}  |  {symbol} / {quote.get('symbol','?')}  |  Grade {grade}  |  {chain}",
        "url":   dex_link,
        "color": color,
        "description": f"**{name}**  •  {dex}  •  Age: `{age:.0f}m`",
        "fields": [
            {
                "name": "💰 Price",
                "value": (
                    f"USD: `${price_usd:.8f}`\n"
                    f"Native: `{price_native}`"
                ),
                "inline": True
            },
            {
                "name": "📊 Market Cap & FDV",
                "value": (
                    f"MCap: `${mcap:,.0f}`\n"
                    f"FDV:  `${fdv:,.0f}`\n"
                    f"Liq:  `${liq:,.0f}`"
                ),
                "inline": True
            },
            {
                "name": "📈 Price Change",
                "value": (
                    f"5m:  `{fmt_pct(pc5m)}`\n"
                    f"1h:  `{fmt_pct(pc1h)}`\n"
                    f"6h:  `{fmt_pct(pc6h)}`\n"
                    f"24h: `{fmt_pct(pc24h)}`"
                ),
                "inline": True
            },
            {
                "name": "💵 Volume",
                "value": (
                    f"5m:  `${vol5m:,.0f}`\n"
                    f"1h:  `${vol1h:,.0f}`\n"
                    f"6h:  `${vol6h:,.0f}`\n"
                    f"24h: `${vol24h:,.0f}`"
                ),
                "inline": True
            },
            {
                "name": "🔄 Trades",
                "value": (
                    f"5m  → B:`{buys5m}` S:`{sells5m}` ({buy_pct:.0f}% buys)\n"
                    f"1h  → B:`{buys1h}` S:`{sells1h}`\n"
                    f"24h → B:`{buys24h}` S:`{sells24h}`"
                ),
                "inline": True
            },
            {
                "name": "🏆 Scores",
                "value": (
                    f"Gem:  `{g:.0f}/100`\n"
                    f"Risk: `{r:.0f}/100`\n"
                    f"Narr: `{narr:.0f}/100`"
                ),
                "inline": True
            },
            {
                "name": "🔗 Contract",
                "value": f"`{address}`",
                "inline": False
            },
        ],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": f"Gem Hunter Bot • DEXScreener • {chain}"}
    }

    payload = {"embeds": [embed]}

    if not DISCORD_WEBHOOK_URL:
        print(f"\n--- ALERT ---\n{symbol} | gem={g} risk={r} age={age:.0f}m\n{dex_link}\n")
        return

    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        if resp.status_code not in (200, 204):
            print(f"[DISCORD] Error {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"[DISCORD] Send error: {e}")

# ─────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────
def scan_chain(chain: str) -> int:
    profiles = fetch_new_pairs(chain)
    found = 0
    for profile in profiles:
        token_address = profile.get("tokenAddress") or profile.get("address")
        if not token_address:
            continue
        pairs = fetch_pair_detail(chain, token_address)
        if not pairs:
            continue
        pairs.sort(
            key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0),
            reverse=True
        )
        pair = pairs[0]
        pair["_age_minutes"] = age_minutes(pair)

        key = f"{chain}:{token_address}"
        if key in alerted:
            continue

        passes, reason = passes_filters(pair)
        if not passes:
            continue

        g = gem_score(pair)
        r = risk_score(pair)
        if g < 25 or r > 80:
            continue

        alerted.add(key)
        found += 1
        symbol = pair.get("baseToken", {}).get("symbol", "???")
        print(f"[{chain.upper()}] 🔥 {symbol} | gem={g} risk={r} age={pair['_age_minutes']:.0f}m mcap=${float(pair.get('marketCap') or 0):,.0f}")
        send_discord(pair, g, r)

    return found

def main():
    print("=" * 42)
    print("  GEM HUNTER BOT — Discord Edition v2")
    print(f"  Chains: {', '.join(c.upper() for c in CHAINS)}")
    print(f"  Scan every {SCAN_INTERVAL_SEC}s | Max age {MAX_AGE_MINUTES}m")
    print("=" * 42)

    if not DISCORD_WEBHOOK_URL:
        print("\n⚠️  DISCORD_WEBHOOK_URL not set\n")

    scan_count = 0
    while True:
        scan_count += 1
        for chain in CHAINS:
            scan_chain(chain)
            time.sleep(1)
        if scan_count % 30 == 0:
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"[{ts}] ❤️  Scan #{scan_count} | alerted: {len(alerted)}")
        time.sleep(SCAN_INTERVAL_SEC)

if __name__ == "__main__":
    main()
