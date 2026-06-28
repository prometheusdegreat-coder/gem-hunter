import os
import time
import requests
from datetime import datetime, timezone

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
DISCORD_WEBHOOK_URL       = os.getenv("DISCORD_WEBHOOK_URL", "")
DISCORD_WEBHOOK_SMART     = os.getenv("DISCORD_WEBHOOK_SMART", "")   # optional separate channel for smart money
DISCORD_WEBHOOK_NARRATIVE = os.getenv("DISCORD_WEBHOOK_NARRATIVE", "") # optional separate channel for narrative gems

# ─────────────────────────────────────────────
# FILTERS
# ─────────────────────────────────────────────
MIN_VOLUME_USD    = 500
MIN_VOLUME_5M     = 50
MAX_AGE_MINUTES   = 120
MIN_MCAP_USD      = 500
MAX_MCAP_USD      = 1_000_000
MIN_BUY_PRESSURE  = 45
MIN_LIQUIDITY_USD = 100
SCAN_INTERVAL_SEC = 8
CHAINS            = ["bsc", "solana"]

alerted: set = set()

# ─────────────────────────────────────────────
# KNOWN SMART MONEY / KOL WALLETS
# Add any known alpha wallets you want to track
# ─────────────────────────────────────────────
SMART_MONEY_WALLETS = set([
    # BNB smart money — add known wallets here
    # e.g. "0xabc123...",
])

KNOWN_SNIPERS = set([
    # Known sniper bots — add addresses here
])

# ─────────────────────────────────────────────
# NARRATIVE ENGINE
# ─────────────────────────────────────────────
NARRATIVES = {
    "🤖 AI / Tech":       ["ai","agent","gpt","llm","neural","agi","robot","cyber","tech","compute"],
    "🇺🇸 USA / Politics": ["trump","maga","potus","elon","musk","election","fed","biden","kamala","america"],
    "🐸 Meme Classic":    ["pepe","wojak","chad","degen","wagmi","ngmi","frog","ape","meme","based"],
    "🐕 Animal":          ["dog","cat","shiba","panda","bear","rabbit","monkey","whale","shark","bonk","wif","popcat","floki","doge"],
    "🌙 Moon / Space":    ["moon","rocket","mars","galaxy","space","alien","star","launch"],
    "💰 Wealth":          ["888","777","lucky","fortune","gold","rich","lambo","jackpot","diamond","millionaire"],
    "🎌 Asian Culture":   ["dragon","zodiac","lunar","sakura","anime","panda","china","japan","korea","thai","viet","asia"],
    "🍜 Food":            ["ramen","sushi","pho","kimchi","boba","pizza","burger","taco","noodle"],
    "💖 Emotion":         ["love","hate","fear","hope","joy","pain","heart","rage","cry"],
    "⛓️ DeFi":            ["defi","swap","yield","stake","farm","dao","nft","web3","bridge","zk"],
    "📰 Real World":      ["war","ceasefire","breaking","news","viral","trending","scandal","crisis"],
}

def get_narrative(symbol: str) -> tuple:
    """Returns (narrative_label, strength_score)"""
    s = (symbol or "").lower()
    matches = []
    for label, keywords in NARRATIVES.items():
        for kw in keywords:
            if kw in s:
                matches.append(label)
                break
    if not matches:
        return "❓ Unclassified", 0
    # Score = 40 per match, capped at 100
    score = min(len(matches) * 40, 100)
    return " + ".join(matches), score

def narrative_score_only(symbol: str) -> float:
    _, score = get_narrative(symbol)
    return float(score)

# ─────────────────────────────────────────────
# SCORING
# ─────────────────────────────────────────────
def gem_score(pair: dict) -> float:
    try:
        vol5m   = float(pair.get("volume", {}).get("m5", 0) or 0)
        vol1h   = float(pair.get("volume", {}).get("h1", 0) or 0)
        buys5m  = int(pair.get("txns", {}).get("m5", {}).get("buys",  0) or 0)
        sells5m = int(pair.get("txns", {}).get("m5", {}).get("sells", 0) or 0)
        total5m = buys5m + sells5m or 1
        buy_pct = 100.0 * buys5m / total5m
        impulse = min(vol5m / max(vol1h / 12, 1), 4.0)
        narr    = narrative_score_only(pair.get("baseToken", {}).get("symbol", ""))
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

def get_action(g: float, r: float, age: float, momentum: str) -> tuple:
    """Returns (action_label, grade, color, is_scalp, is_watch)"""
    if r > 75:
        return "🚫 SKIP — HIGH RISK",    "F",  0xFF0000, False, False
    if momentum == "DEAD":
        return "💀 SKIP — NO ACTIVITY",  "F",  0x424242, False, False
    if g >= 70 and r <= 30:
        return "🚀 SCALP NOW",           "A+", 0x00C853, True,  False
    if g >= 60 and r <= 40 and momentum in ("IGNITING", "PUMPING"):
        return "⚡ ACT NOW — IGNITING",  "A",  0x64DD17, True,  False
    if g >= 55 and r <= 50:
        return "💥 STRONG SIGNAL",       "A",  0x76FF03, True,  False
    if g >= 45 and r <= 60 and age <= 45:
        return "🟢 ENTER — FRESH",       "B",  0xFFD600, True,  False
    if g >= 40 and r <= 65:
        return "👀 WATCH — BUILDING",    "C",  0xFF6D00, False, True
    if g >= 30 and r <= 70:
        return "📊 WATCH ONLY",          "D",  0x9E9E9E, False, True
    return     "📋 MONITOR",             "D",  0x616161, False, True

def get_momentum(pair: dict) -> str:
    vol5m  = float(pair.get("volume", {}).get("m5", 0) or 0)
    vol1h  = float(pair.get("volume", {}).get("h1", 0) or 0)
    avg5m  = vol1h / 12 if vol1h > 0 else 0
    buys5m = int(pair.get("txns", {}).get("m5", {}).get("buys",  0) or 0)
    sells5m= int(pair.get("txns", {}).get("m5", {}).get("sells", 0) or 0)
    trades5m = buys5m + sells5m

    if trades5m == 0:
        return "DEAD"
    if vol5m >= avg5m * 2.0 and buys5m > sells5m:
        return "IGNITING"
    if vol5m >= avg5m * 1.2 and buys5m > sells5m:
        return "PUMPING"
    if vol5m < avg5m * 0.5:
        return "COOLING"
    return "BUILDING"

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

def fetch_token_info(chain: str, address: str) -> dict:
    """Fetch additional token/contract info from DEXScreener token page."""
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/search?q={address}",
            timeout=10
        )
        if r.status_code != 200:
            return {}
        return r.json()
    except Exception:
        return {}

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
# WALLET ANALYSIS
# ─────────────────────────────────────────────
def analyze_wallets(pair: dict) -> dict:
    """
    DEXScreener free API doesn't expose wallet lists directly.
    We extract what we can and flag known wallets.
    For full wallet data, a paid API (Bitquery/Moralis) would be needed.
    """
    chain   = (pair.get("chainId") or "").lower()
    address = pair.get("baseToken", {}).get("address", "")

    result = {
        "has_smart_money":    False,
        "has_known_sniper":   False,
        "smart_money_count":  0,
        "sniper_count":       0,
        "wallet_note":        "",
        "bscscan_link":       "",
        "solscan_link":       "",
        "dextools_link":      "",
        "bubblemaps_link":    "",
    }

    if chain == "bsc":
        result["bscscan_link"]   = f"https://bscscan.com/token/{address}#balances"
        result["dextools_link"]  = f"https://www.dextools.io/app/en/bnb/pair-explorer/{pair.get('pairAddress','')}"
        result["bubblemaps_link"]= f"https://app.bubblemaps.io/bsc/token/{address}"
    elif chain == "solana":
        result["solscan_link"]   = f"https://solscan.io/token/{address}"
        result["dextools_link"]  = f"https://www.dextools.io/app/en/solana/pair-explorer/{pair.get('pairAddress','')}"
        result["bubblemaps_link"]= f"https://app.bubblemaps.io/sol/token/{address}"

    return result

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
    if vol_tot < MIN_VOLUME_USD:     return False, f"low volume"
    if vol5m < MIN_VOLUME_5M:        return False, f"no 5m momentum"
    if mcap and mcap < MIN_MCAP_USD: return False, f"mcap too low"
    if mcap and mcap > MAX_MCAP_USD: return False, f"mcap too high"
    if liq < MIN_LIQUIDITY_USD:      return False, f"thin liquidity"
    if buy_pct < MIN_BUY_PRESSURE:   return False, f"low buys ({buy_pct:.0f}%)"
    return True, ""

# ─────────────────────────────────────────────
# BUILD DISCORD EMBED
# ─────────────────────────────────────────────
def build_embed(pair: dict, g: float, r: float) -> dict:
    base     = pair.get("baseToken", {})
    quote    = pair.get("quoteToken", {})
    symbol   = base.get("symbol", "???")
    name     = base.get("name", "Unknown")
    address  = base.get("address", "")
    chain_id = (pair.get("chainId") or "").lower()
    chain    = chain_id.upper()
    dex      = pair.get("dexId", "unknown").upper()
    age      = pair.get("_age_minutes", 0)

    price_usd    = float(pair.get("priceUsd") or 0)
    price_native = pair.get("priceNative", "?")
    mcap         = float(pair.get("marketCap") or pair.get("fdv") or 0)
    fdv          = float(pair.get("fdv") or 0)
    liq          = float(pair.get("liquidity", {}).get("usd") or 0)

    vol5m  = float(pair.get("volume", {}).get("m5")  or 0)
    vol1h  = float(pair.get("volume", {}).get("h1")  or 0)
    vol6h  = float(pair.get("volume", {}).get("h6")  or 0)
    vol24h = float(pair.get("volume", {}).get("h24") or 0)

    pc5m   = pair.get("priceChange", {}).get("m5",  0) or 0
    pc1h   = pair.get("priceChange", {}).get("h1",  0) or 0
    pc6h   = pair.get("priceChange", {}).get("h6",  0) or 0
    pc24h  = pair.get("priceChange", {}).get("h24", 0) or 0

    buys5m   = int(pair.get("txns", {}).get("m5",  {}).get("buys",  0) or 0)
    sells5m  = int(pair.get("txns", {}).get("m5",  {}).get("sells", 0) or 0)
    buys1h   = int(pair.get("txns", {}).get("h1",  {}).get("buys",  0) or 0)
    sells1h  = int(pair.get("txns", {}).get("h1",  {}).get("sells", 0) or 0)
    buys24h  = int(pair.get("txns", {}).get("h24", {}).get("buys",  0) or 0)
    sells24h = int(pair.get("txns", {}).get("h24", {}).get("sells", 0) or 0)
    total5m  = buys5m + sells5m or 1
    buy_pct  = 100.0 * buys5m / total5m

    momentum = pair.get("_momentum", "BUILDING")
    action, grade, color, is_scalp, is_watch = get_action(g, r, age, momentum)
    narrative_label, narr_score = get_narrative(symbol)
    wallets = analyze_wallets(pair)

    dex_link       = f"https://dexscreener.com/{chain_id}/{address}"
    pair_address   = pair.get("pairAddress", "")

    def fmt_pct(v):
        try:
            f = float(v)
            arrow = "📈" if f > 0 else "📉"
            return f"{arrow} `{'+' if f>=0 else ''}{f:.1f}%`"
        except Exception:
            return "`?`"

    def fmt_usd(v):
        if v >= 1_000_000: return f"${v/1_000_000:.2f}M"
        if v >= 1_000:     return f"${v/1_000:.1f}K"
        return f"${v:.0f}"

    # Action type label
    trade_type = ""
    if is_scalp: trade_type = "⚡ **SCALP OPPORTUNITY**"
    elif is_watch: trade_type = "👁️ **WATCH — Not yet**"

    # Momentum bar
    momentum_emoji = {
        "IGNITING": "🔥🔥🔥 IGNITING",
        "PUMPING":  "🔥🔥 PUMPING",
        "BUILDING": "📶 BUILDING",
        "COOLING":  "❄️ COOLING",
        "DEAD":     "💀 DEAD",
    }.get(momentum, "📶 BUILDING")

    # Risk label
    risk_label = (
        "🟢 LOW"    if r <= 30 else
        "🟡 MEDIUM" if r <= 55 else
        "🟠 HIGH"   if r <= 70 else
        "🔴 DANGER"
    )

    # Build wallet investigation links
    wallet_links = []
    if wallets["bscscan_link"]:
        wallet_links.append(f"[📊 Holders (BscScan)]({wallets['bscscan_link']})")
    if wallets["solscan_link"]:
        wallet_links.append(f"[📊 Holders (Solscan)]({wallets['solscan_link']})")
    if wallets["dextools_link"]:
        wallet_links.append(f"[🔧 DexTools]({wallets['dextools_link']})")
    if wallets["bubblemaps_link"]:
        wallet_links.append(f"[🫧 BubbleMaps]({wallets['bubblemaps_link']})")

    embed = {
        "title": f"{action}  |  {symbol}/{quote.get('symbol','?')}  |  Grade {grade}  |  {chain}",
        "url":   dex_link,
        "color": color,
        "description": (
            f"**{name}**  •  {dex}  •  Age: `{age:.0f}m`\n"
            f"{trade_type}\n"
            f"Momentum: {momentum_emoji}  |  Risk: {risk_label}"
        ),
        "fields": [
            {
                "name": "🏷️ Narrative",
                "value": f"{narrative_label}\nStrength: `{narr_score}/100`",
                "inline": False
            },
            {
                "name": "💰 Price",
                "value": (
                    f"USD: `${price_usd:.10f}`\n"
                    f"Native: `{price_native}`"
                ),
                "inline": True
            },
            {
                "name": "📊 Market",
                "value": (
                    f"MCap: `{fmt_usd(mcap)}`\n"
                    f"FDV:  `{fmt_usd(fdv)}`\n"
                    f"Liq:  `{fmt_usd(liq)}`"
                ),
                "inline": True
            },
            {
                "name": "📈 Price Change",
                "value": (
                    f"5m:  {fmt_pct(pc5m)}\n"
                    f"1h:  {fmt_pct(pc1h)}\n"
                    f"6h:  {fmt_pct(pc6h)}\n"
                    f"24h: {fmt_pct(pc24h)}"
                ),
                "inline": True
            },
            {
                "name": "💵 Volume",
                "value": (
                    f"5m:  `{fmt_usd(vol5m)}`\n"
                    f"1h:  `{fmt_usd(vol1h)}`\n"
                    f"6h:  `{fmt_usd(vol6h)}`\n"
                    f"24h: `{fmt_usd(vol24h)}`"
                ),
                "inline": True
            },
            {
                "name": "🔄 Trades",
                "value": (
                    f"5m  → 🟢`{buys5m}` 🔴`{sells5m}` ({buy_pct:.0f}% buys)\n"
                    f"1h  → 🟢`{buys1h}` 🔴`{sells1h}`\n"
                    f"24h → 🟢`{buys24h}` 🔴`{sells24h}`"
                ),
                "inline": True
            },
            {
                "name": "🏆 Scores",
                "value": (
                    f"Gem:  `{g:.0f}/100`\n"
                    f"Risk: `{r:.0f}/100`\n"
                    f"Narr: `{narr_score}/100`"
                ),
                "inline": True
            },
            {
                "name": "🔍 Investigate Wallets & Dev",
                "value": (
                    "\n".join(wallet_links) + "\n"
                    f"[🔍 DEXScreener]({dex_link})\n"
                    f"*(Check BubbleMaps for holder concentration,\n"
                    f"DexTools for dev wallet & early buyers)*"
                ),
                "inline": False
            },
            {
                "name": "📋 Contract Address",
                "value": f"`{address}`",
                "inline": False
            },
        ],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {
            "text": f"Gem Hunter Pro • {chain} • DEXScreener • Pair: {pair_address[:16]}..."
        }
    }

    # Add smart money warning if detected
    if wallets["has_smart_money"]:
        embed["fields"].insert(0, {
            "name": "🧠 SMART MONEY DETECTED",
            "value": f"{wallets['smart_money_count']} known alpha wallet(s) in this token",
            "inline": False
        })

    return embed

# ─────────────────────────────────────────────
# SEND DISCORD
# ─────────────────────────────────────────────
def send_discord(embed: dict, webhook_url: str = ""):
    url = webhook_url or DISCORD_WEBHOOK_URL
    if not url:
        symbol = embed.get("title", "???")[:30]
        print(f"\n⚠️  No webhook — alert for: {symbol}\n")
        return
    try:
        resp = requests.post(url, json={"embeds": [embed]}, timeout=10)
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
        pair["_momentum"]    = get_momentum(pair)

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

        symbol   = pair.get("baseToken", {}).get("symbol", "???")
        momentum = pair.get("_momentum", "?")
        _, narr  = get_narrative(symbol)
        action, grade, _, is_scalp, _ = get_action(g, r, pair["_age_minutes"], momentum)

        print(f"[{chain.upper()}] {grade} {symbol} | gem={g} risk={r} age={pair['_age_minutes']:.0f}m | {momentum} | narr={narr}")

        embed = build_embed(pair, g, r)
        send_discord(embed)

        # Send to separate narrative channel if configured and narrative is strong
        if DISCORD_WEBHOOK_NARRATIVE and narr >= 40:
            send_discord(embed, DISCORD_WEBHOOK_NARRATIVE)

        # Send to smart money channel if configured
        if DISCORD_WEBHOOK_SMART and is_scalp:
            send_discord(embed, DISCORD_WEBHOOK_SMART)

    return found

def main():
    print("=" * 44)
    print("  GEM HUNTER PRO — Discord Edition v3")
    print(f"  Chains: {', '.join(c.upper() for c in CHAINS)}")
    print(f"  Scan every {SCAN_INTERVAL_SEC}s | Max age {MAX_AGE_MINUTES}m")
    print("=" * 44)

    if not DISCORD_WEBHOOK_URL:
        print("\n⚠️  DISCORD_WEBHOOK_URL not set — alerts won't send!\n")
        print("   Go to Railway → gem-hunter → Variables → add DISCORD_WEBHOOK_URL\n")

    scan_count = 0
    while True:
        scan_count += 1
        for chain in CHAINS:
            scan_chain(chain)
            time.sleep(1)
        if scan_count % 30 == 0:
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"[{ts}] ❤️  Scan #{scan_count} | alerted: {len(alerted)} tokens so far")
        time.sleep(SCAN_INTERVAL_SEC)

if __name__ == "__main__":
    main()
