import os
import time
import requests
from datetime import datetime, timezone

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
DISCORD_WEBHOOK_URL  = os.getenv("DISCORD_WEBHOOK_URL", "")
DISCORD_WEBHOOK_FIRE = os.getenv("DISCORD_WEBHOOK_FIRE", "")  # separate channel for A+ only

# ─────────────────────────────────────────────
# AGGRESSIVE EARLY HUNTER FILTERS
# Goal: catch tokens in first 0-30 minutes
# ─────────────────────────────────────────────
MAX_AGE_MINUTES      = 45      # only ultra-fresh tokens
MIN_VOLUME_USD       = 300     # low bar — catch early before volume builds
MIN_VOLUME_5M        = 30      # any 5m activity
MIN_LIQUIDITY_USD    = 50      # very low — just needs to be tradeable
MIN_BUY_PRESSURE     = 40      # any buy dominance
MIN_MCAP_USD         = 0       # no mcap floor — too early for mcap data
MAX_MCAP_USD         = 2_000_000
SCAN_INTERVAL_SEC    = 5       # scan every 5 seconds — faster than before
CHAINS               = ["solana", "bsc"]  # Solana first — faster launches

# ─────────────────────────────────────────────
# DEDUP — track alerted tokens + re-alert on
# significant momentum change
# ─────────────────────────────────────────────
alerted: dict = {}   # key -> {"grade": X, "time": t, "gem": g}

# ─────────────────────────────────────────────
# NARRATIVE ENGINE — strong narrative = early 3-10x
# ─────────────────────────────────────────────
NARRATIVES = {
    "🤖 AI/Agent":        (["ai","agent","gpt","llm","neural","agi","robot","compute","agi","intelligen"], 40),
    "🇺🇸 Political":      (["trump","maga","potus","elon","musk","election","fed","america","kamala","barron"], 40),
    "🐸 CT Meme":         (["pepe","wojak","chad","degen","wagmi","frog","ape","meme","based","jeet","ngmi"], 35),
    "🐕 Animal":          (["dog","cat","shiba","panda","bear","rabbit","monkey","shark","bonk","wif","popcat","doge","floki"], 35),
    "🌙 Moon/Pump":       (["moon","rocket","100x","pump","gem","alpha","launch","fire","nuke","send"], 30),
    "💰 Wealth":          (["888","777","rich","lambo","gold","fortune","lucky","diamond","millionaire","jackpot"], 25),
    "🎌 Asian":           (["dragon","zodiac","lunar","sakura","anime","panda","china","japan","korea","thai","viet","asia"], 25),
    "📰 Trending":        (["war","ceasefire","breaking","viral","trending","scandal","news","crisis","event"], 35),
    "🎮 Gaming/NFT":      (["game","nft","play","metaverse","pixel","quest","rpg","gaming","arena"], 20),
    "🍜 Food/Fun":        (["ramen","sushi","pizza","burger","taco","coffee","boba","food","drink"], 15),
}

def get_narrative(symbol: str, name: str = "") -> tuple:
    text = ((symbol or "") + " " + (name or "")).lower()
    matches = []
    total_score = 0
    for label, (keywords, weight) in NARRATIVES.items():
        for kw in keywords:
            if kw in text:
                matches.append((label, weight))
                total_score += weight
                break
    if not matches:
        return "❓ No narrative", 0
    matches.sort(key=lambda x: x[1], reverse=True)
    label = " + ".join(m[0] for m in matches[:2])
    return label, min(total_score, 100)

# ─────────────────────────────────────────────
# MOMENTUM DETECTION
# ─────────────────────────────────────────────
def get_momentum(pair: dict) -> tuple:
    vol5m   = float(pair.get("volume", {}).get("m5", 0) or 0)
    vol1h   = float(pair.get("volume", {}).get("h1", 0) or 0)
    buys5m  = int(pair.get("txns", {}).get("m5", {}).get("buys",  0) or 0)
    sells5m = int(pair.get("txns", {}).get("m5", {}).get("sells", 0) or 0)
    buys1h  = int(pair.get("txns", {}).get("h1", {}).get("buys",  0) or 0)
    sells1h = int(pair.get("txns", {}).get("h1", {}).get("sells", 0) or 0)
    avg_5m_slice = vol1h / 12 if vol1h > 0 else 0

    trades5m = buys5m + sells5m
    if trades5m == 0:                                      return "💀 DEAD",     0
    if vol5m >= avg_5m_slice * 3.0 and buys5m > sells5m:  return "🔥🔥🔥 IGNITING", 100
    if vol5m >= avg_5m_slice * 1.5 and buys5m > sells5m:  return "🔥🔥 PUMPING",  75
    if vol5m >= avg_5m_slice * 1.0 and buys5m > sells5m:  return "📶 BUILDING",   50
    if vol5m < avg_5m_slice * 0.4:                        return "❄️ COOLING",    15
    return "📊 STEADY", 30

# ─────────────────────────────────────────────
# SCORING
# ─────────────────────────────────────────────
def score_token(pair: dict) -> tuple:
    vol5m   = float(pair.get("volume", {}).get("m5", 0) or 0)
    vol1h   = float(pair.get("volume", {}).get("h1", 0) or 0)
    buys5m  = int(pair.get("txns", {}).get("m5", {}).get("buys",  0) or 0)
    sells5m = int(pair.get("txns", {}).get("m5", {}).get("sells", 0) or 0)
    buys1h  = int(pair.get("txns", {}).get("h1", {}).get("buys",  0) or 0)
    sells1h = int(pair.get("txns", {}).get("h1", {}).get("sells", 0) or 0)
    liq     = float(pair.get("liquidity", {}).get("usd", 0) or 0)
    mcap    = float(pair.get("marketCap", 0) or 0)
    age     = pair.get("_age_minutes", 999)
    symbol  = pair.get("baseToken", {}).get("symbol", "")
    name    = pair.get("baseToken", {}).get("name", "")

    total5m = buys5m + sells5m or 1
    total1h = buys1h + sells1h or 1
    buy_pct5m = 100.0 * buys5m / total5m
    buy_pct1h = 100.0 * buys1h / total1h
    avg_5m = vol1h / 12 if vol1h > 0 else 0
    impulse = min(vol5m / max(avg_5m, 1), 5.0)

    _, narr = get_narrative(symbol, name)
    mom_label, mom_score = get_momentum(pair)

    freshness = (
        100 if age <=  5 else
         90 if age <= 10 else
         80 if age <= 20 else
         65 if age <= 30 else
         45 if age <= 45 else 20
    )

    # GEM SCORE — weighted for early launch hunting
    gem = min(
        0.25 * buy_pct5m                        # buy pressure now
      + 0.20 * min(impulse * 20, 100)           # volume spike
      + 0.20 * narr                             # narrative strength
      + 0.20 * freshness                        # how early we are
      + 0.10 * mom_score                        # momentum phase
      + 0.05 * min(buys5m * 5, 100)            # trade count
    , 100.0)

    # RISK SCORE
    conc_risk = 70 if (mcap > 80_000 and liq < 3_000) else 15
    bp_risk   = 80 if buy_pct1h < 40 else 40 if buy_pct1h < 55 else 15 if buy_pct1h < 70 else 5
    tr_risk   = (
        20 if age <= 10 and buys1h <  5 else
        10 if age <= 10 else
        70 if buys1h <  5 else
        40 if buys1h < 15 else
        15 if buys1h < 40 else 5
    )
    risk = min(0.35 * bp_risk + 0.35 * tr_risk + 0.30 * conc_risk, 100.0)

    return round(gem, 1), round(risk, 1), mom_label, narr

# ─────────────────────────────────────────────
# GRADE + ACTION
# ─────────────────────────────────────────────
def get_grade(gem: float, risk: float, age: float, mom: str, narr: int) -> tuple:
    # Hard skips
    if risk > 78:          return "F",  "🚫 SKIP — HIGH RISK",      0xFF0000, False
    if "DEAD" in mom:      return "F",  "💀 DEAD — NO ACTIVITY",    0x424242, False

    # A+ — early igniting with narrative
    if gem >= 65 and risk <= 30 and "IGNITING" in mom and narr >= 30:
        return "A+", "🚀🚀 EARLY GEM — SCALP NOW",   0x00E676, True
    # A+ — ultra fresh pumping
    if gem >= 60 and risk <= 35 and age <= 15 and "PUMPING" in mom:
        return "A+", "⚡ ULTRA FRESH — ACT NOW",      0x00C853, True
    # A
    if gem >= 55 and risk <= 40:
        return "A",  "🔥 STRONG SIGNAL — ENTER",     0x64DD17, True
    # A — narrative + fresh
    if gem >= 45 and risk <= 45 and age <= 20 and narr >= 35:
        return "A",  "🎯 NARRATIVE GEM — ENTER",     0x76FF03, True
    # B
    if gem >= 45 and risk <= 55:
        return "B",  "👀 GOOD SIGNAL — CONSIDER",    0xFFD600, True
    # B — watch building
    if gem >= 38 and risk <= 60 and "BUILD" in mom:
        return "B",  "📶 BUILDING — WATCH CLOSE",    0xFFC107, False
    # C
    if gem >= 30 and risk <= 65:
        return "C",  "📊 EARLY WATCH",               0xFF6D00, False
    return             "D",  "📋 MONITOR ONLY",              0x757575, False

# ─────────────────────────────────────────────
# DEXSCREENER
# ─────────────────────────────────────────────
def fetch_latest_profiles(chain: str) -> list:
    try:
        r = requests.get(
            "https://api.dexscreener.com/token-profiles/latest/v1",
            timeout=8
        )
        if r.status_code != 200: return []
        data = r.json()
        return [p for p in (data if isinstance(data, list) else [])
                if (p.get("chainId") or "").lower() == chain.lower()]
    except Exception as e:
        print(f"[{chain}] profile error: {e}")
        return []

def fetch_boosted(chain: str) -> list:
    """Also scan boosted/trending tokens — often have strong narrative."""
    try:
        r = requests.get(
            "https://api.dexscreener.com/token-boosts/latest/v1",
            timeout=8
        )
        if r.status_code != 200: return []
        data = r.json()
        return [p for p in (data if isinstance(data, list) else [])
                if (p.get("chainId") or "").lower() == chain.lower()]
    except Exception as e:
        return []

def fetch_pair_data(chain: str, token_address: str) -> dict:
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{token_address}",
            timeout=8
        )
        if r.status_code != 200: return {}
        pairs = r.json().get("pairs") or []
        chain_pairs = [p for p in pairs if (p.get("chainId") or "").lower() == chain.lower()]
        if not chain_pairs: return {}
        # Pick highest liquidity pair
        chain_pairs.sort(key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0), reverse=True)
        return chain_pairs[0]
    except Exception as e:
        return {}

def age_minutes(pair: dict) -> float:
    try:
        c = pair.get("pairCreatedAt")
        if c:
            return (datetime.now(timezone.utc).timestamp() * 1000 - float(c)) / 60_000
    except Exception:
        pass
    return 9999

# ─────────────────────────────────────────────
# BSCSCAN — free dev wallet lookup for BNB
# ─────────────────────────────────────────────
BSCSCAN_API_KEY = os.getenv("BSCSCAN_API_KEY", "")

def get_deployer_wallet(token_address: str) -> str:
    """Get the wallet that deployed this contract on BNB."""
    if not BSCSCAN_API_KEY or not token_address.startswith("0x"):
        return ""
    try:
        url = (
            f"https://api.bscscan.com/api?module=contract&action=getcontractcreation"
            f"&contractaddresses={token_address}&apikey={BSCSCAN_API_KEY}"
        )
        r = requests.get(url, timeout=6)
        data = r.json()
        if data.get("status") == "1" and data.get("result"):
            return data["result"][0].get("contractCreator", "")
    except Exception:
        pass
    return ""

def get_first_buyers(token_address: str, pair_address: str) -> list:
    """Get first 5 wallets that traded this token on BNB."""
    if not BSCSCAN_API_KEY or not token_address.startswith("0x"):
        return []
    try:
        url = (
            f"https://api.bscscan.com/api?module=account&action=tokentx"
            f"&contractaddress={token_address}&page=1&offset=10&sort=asc"
            f"&apikey={BSCSCAN_API_KEY}"
        )
        r = requests.get(url, timeout=6)
        data = r.json()
        if data.get("status") == "1":
            txns = data.get("result", [])
            wallets = []
            seen = set()
            for tx in txns:
                w = tx.get("from", "")
                if w and w not in seen and w.lower() != token_address.lower():
                    seen.add(w)
                    wallets.append(w)
                if len(wallets) >= 5:
                    break
            return wallets
    except Exception:
        pass
    return []

# ─────────────────────────────────────────────
# BUILD ALERT LINKS
# ─────────────────────────────────────────────
def build_links(chain_id: str, token_address: str, pair_address: str) -> dict:
    chain = chain_id.lower()
    links = {}
    if chain == "bsc":
        links["chart"]      = f"https://dexscreener.com/bsc/{token_address}"
        links["bscscan"]    = f"https://bscscan.com/token/{token_address}"
        links["holders"]    = f"https://bscscan.com/token/{token_address}#balances"
        links["deployer"]   = f"https://bscscan.com/token/{token_address}#info"
        links["bubblemaps"] = f"https://app.bubblemaps.io/bsc/token/{token_address}"
        links["dextools"]   = f"https://www.dextools.io/app/en/bnb/pair-explorer/{pair_address}"
        links["poocoin"]    = f"https://poocoin.app/tokens/{token_address}"
    elif chain == "solana":
        links["chart"]      = f"https://dexscreener.com/solana/{token_address}"
        links["solscan"]    = f"https://solscan.io/token/{token_address}"
        links["holders"]    = f"https://solscan.io/token/{token_address}#holders"
        links["bubblemaps"] = f"https://app.bubblemaps.io/sol/token/{token_address}"
        links["dextools"]   = f"https://www.dextools.io/app/en/solana/pair-explorer/{pair_address}"
        links["birdeye"]    = f"https://birdeye.so/token/{token_address}?chain=solana"
        links["rugcheck"]   = f"https://rugcheck.xyz/tokens/{token_address}"
    return links

# ─────────────────────────────────────────────
# BUILD DISCORD EMBED
# ─────────────────────────────────────────────
def build_embed(pair: dict, gem: float, risk: float, grade: str,
                action: str, color: int, mom: str, narr_label: str,
                narr_score: int, deployer: str, first_buyers: list) -> dict:

    base         = pair.get("baseToken", {})
    quote        = pair.get("quoteToken", {})
    symbol       = base.get("symbol", "???")
    name         = base.get("name", "Unknown")
    address      = base.get("address", "")
    chain_id     = (pair.get("chainId") or "").lower()
    chain        = chain_id.upper()
    dex          = pair.get("dexId", "?").upper()
    age          = pair.get("_age_minutes", 0)
    pair_address = pair.get("pairAddress", "")

    price_usd    = float(pair.get("priceUsd") or 0)
    price_native = pair.get("priceNative", "?")
    mcap         = float(pair.get("marketCap") or pair.get("fdv") or 0)
    fdv          = float(pair.get("fdv") or 0)
    liq          = float(pair.get("liquidity", {}).get("usd") or 0)

    vol5m  = float(pair.get("volume", {}).get("m5")  or 0)
    vol1h  = float(pair.get("volume", {}).get("h1")  or 0)
    vol6h  = float(pair.get("volume", {}).get("h6")  or 0)
    vol24h = float(pair.get("volume", {}).get("h24") or 0)

    pc5m   = float(pair.get("priceChange", {}).get("m5",  0) or 0)
    pc1h   = float(pair.get("priceChange", {}).get("h1",  0) or 0)
    pc6h   = float(pair.get("priceChange", {}).get("h6",  0) or 0)
    pc24h  = float(pair.get("priceChange", {}).get("h24", 0) or 0)

    buys5m   = int(pair.get("txns", {}).get("m5",  {}).get("buys",  0) or 0)
    sells5m  = int(pair.get("txns", {}).get("m5",  {}).get("sells", 0) or 0)
    buys1h   = int(pair.get("txns", {}).get("h1",  {}).get("buys",  0) or 0)
    sells1h  = int(pair.get("txns", {}).get("h1",  {}).get("sells", 0) or 0)
    buys24h  = int(pair.get("txns", {}).get("h24", {}).get("buys",  0) or 0)
    sells24h = int(pair.get("txns", {}).get("h24", {}).get("sells", 0) or 0)
    total5m  = buys5m + sells5m or 1
    buy_pct  = 100.0 * buys5m / total5m

    links = build_links(chain_id, address, pair_address)

    def fp(v):
        arrow = "📈" if v > 0 else "📉" if v < 0 else "➡️"
        return f"{arrow} `{'+' if v>=0 else ''}{v:.1f}%`"

    def fv(v):
        if v >= 1_000_000: return f"${v/1_000_000:.2f}M"
        if v >= 1_000:     return f"${v/1_000:.1f}K"
        return f"${v:.0f}"

    risk_label = (
        "🟢 LOW"    if risk <= 30 else
        "🟡 MEDIUM" if risk <= 55 else
        "🟠 HIGH"   if risk <= 72 else
        "🔴 DANGER"
    )

    # Build investigation links string
    inv_links = []
    if links.get("chart"):      inv_links.append(f"[📊 Chart]({links['chart']})")
    if links.get("bubblemaps"): inv_links.append(f"[🫧 BubbleMaps]({links['bubblemaps']})")
    if links.get("dextools"):   inv_links.append(f"[🔧 DexTools]({links['dextools']})")
    if links.get("birdeye"):    inv_links.append(f"[🦅 Birdeye]({links['birdeye']})")
    if links.get("rugcheck"):   inv_links.append(f"[🛡️ RugCheck]({links['rugcheck']})")
    if links.get("holders"):    inv_links.append(f"[👥 Holders]({links['holders']})")
    if links.get("poocoin"):    inv_links.append(f"[💩 PooCoin]({links['poocoin']})")

    # Dev info
    dev_info = ""
    if deployer:
        scan = links.get("bscscan", links.get("solscan", ""))
        dev_info = f"[`{deployer[:6]}...{deployer[-4:]}`]({scan})"
    else:
        dev_info = "*Check BubbleMaps + DexTools for dev wallet*"

    # First buyers
    buyers_info = ""
    if first_buyers:
        buyers_info = "\n".join([f"`{w[:6]}...{w[-4:]}`" for w in first_buyers[:5]])
    else:
        buyers_info = "*Check Holders link above*"

    embed = {
        "title": f"{action}  |  Grade {grade}  |  {chain}",
        "url": links.get("chart", ""),
        "color": color,
        "description": (
            f"## {symbol} / {quote.get('symbol','?')}\n"
            f"**{name}**  •  {dex}  •  ⏱️ Age: `{age:.0f} min`\n"
            f"Momentum: {mom}\n"
            f"Risk: {risk_label}"
        ),
        "fields": [
            {
                "name": "🏷️ Narrative",
                "value": f"{narr_label}\nStrength: `{narr_score}/100`",
                "inline": False
            },
            {
                "name": "💰 Price",
                "value": f"USD: `${price_usd:.10f}`\nNative: `{price_native}`",
                "inline": True
            },
            {
                "name": "📊 Market",
                "value": f"MCap: `{fv(mcap)}`\nFDV: `{fv(fdv)}`\nLiq: `{fv(liq)}`",
                "inline": True
            },
            {
                "name": "🏆 Scores",
                "value": f"Gem: `{gem}/100`\nRisk: `{risk}/100`\nNarr: `{narr_score}/100`",
                "inline": True
            },
            {
                "name": "📈 Price Change",
                "value": f"5m: {fp(pc5m)}\n1h: {fp(pc1h)}\n6h: {fp(pc6h)}\n24h: {fp(pc24h)}",
                "inline": True
            },
            {
                "name": "💵 Volume",
                "value": f"5m: `{fv(vol5m)}`\n1h: `{fv(vol1h)}`\n6h: `{fv(vol6h)}`\n24h: `{fv(vol24h)}`",
                "inline": True
            },
            {
                "name": "🔄 Trades",
                "value": (
                    f"5m → 🟢`{buys5m}` 🔴`{sells5m}` ({buy_pct:.0f}% buys)\n"
                    f"1h → 🟢`{buys1h}` 🔴`{sells1h}`\n"
                    f"24h → 🟢`{buys24h}` 🔴`{sells24h}`"
                ),
                "inline": True
            },
            {
                "name": "👨‍💻 Dev / Deployer Wallet",
                "value": dev_info,
                "inline": False
            },
            {
                "name": "🎯 First Buyers (early wallets)",
                "value": buyers_info,
                "inline": False
            },
            {
                "name": "🔍 Investigate",
                "value": "  ".join(inv_links),
                "inline": False
            },
            {
                "name": "📋 Contract",
                "value": f"`{address}`",
                "inline": False
            },
        ],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": f"Gem Hunter Pro v4 • {chain} • {dex} • Pair: {pair_address[:20]}..."}
    }
    return embed

# ─────────────────────────────────────────────
# SEND DISCORD
# ─────────────────────────────────────────────
def send_discord(embed: dict, url: str = ""):
    target = url or DISCORD_WEBHOOK_URL
    if not target:
        print(f"  ⚠️  No webhook configured")
        return
    try:
        r = requests.post(target, json={"embeds": [embed]}, timeout=10)
        if r.status_code == 429:
            print("  ⚠️  Discord rate limit — waiting 5s")
            time.sleep(5)
            requests.post(target, json={"embeds": [embed]}, timeout=10)
        elif r.status_code not in (200, 204):
            print(f"  ⚠️  Discord error {r.status_code}: {r.text[:100]}")
    except Exception as e:
        print(f"  ⚠️  Discord send error: {e}")

# ─────────────────────────────────────────────
# FILTER
# ─────────────────────────────────────────────
def passes(pair: dict) -> tuple:
    age    = pair.get("_age_minutes", 9999)
    vol5m  = float(pair.get("volume", {}).get("m5", 0) or 0)
    vol1h  = float(pair.get("volume", {}).get("h1", 0) or 0)
    liq    = float(pair.get("liquidity", {}).get("usd", 0) or 0)
    mcap   = float(pair.get("marketCap", 0) or 0)
    buys5m = int(pair.get("txns", {}).get("m5", {}).get("buys",  0) or 0)
    sel5m  = int(pair.get("txns", {}).get("m5", {}).get("sells", 0) or 0)
    tot5m  = buys5m + sel5m or 1
    bp     = 100.0 * buys5m / tot5m

    if age > MAX_AGE_MINUTES:              return False, f"old ({age:.0f}m)"
    if vol5m + vol1h < MIN_VOLUME_USD:     return False, f"low vol"
    if vol5m < MIN_VOLUME_5M:             return False, f"no 5m vol"
    if liq < MIN_LIQUIDITY_USD:            return False, f"no liq"
    if mcap and mcap > MAX_MCAP_USD:       return False, f"mcap too high"
    if bp < MIN_BUY_PRESSURE:              return False, f"low buys ({bp:.0f}%)"
    return True, ""

# ─────────────────────────────────────────────
# SCAN
# ─────────────────────────────────────────────
def scan_chain(chain: str):
    # Combine latest profiles + boosted
    profiles = fetch_latest_profiles(chain) + fetch_boosted(chain)
    seen_addresses = set()

    for profile in profiles:
        token_address = profile.get("tokenAddress") or profile.get("address")
        if not token_address or token_address in seen_addresses:
            continue
        seen_addresses.add(token_address)

        pair = fetch_pair_data(chain, token_address)
        if not pair:
            continue

        pair["_age_minutes"] = age_minutes(pair)
        ok, reason = passes(pair)
        if not ok:
            continue

        gem, risk, mom, narr_score = score_token(pair)
        narr_label, _ = get_narrative(
            pair.get("baseToken", {}).get("symbol", ""),
            pair.get("baseToken", {}).get("name", "")
        )
        grade, action, color, is_entry = get_grade(gem, risk, pair["_age_minutes"], mom, narr_score)

        # Skip D grade entirely — too noisy
        if grade == "D":
            continue

        key = f"{chain}:{token_address}"
        prev = alerted.get(key)

        # Re-alert if grade improved significantly (e.g. C→A)
        should_alert = False
        if not prev:
            should_alert = True
        elif grade in ("A+", "A") and prev.get("grade") in ("B", "C"):
            should_alert = True
            action = "⬆️ UPGRADED — " + action

        if not should_alert:
            continue

        alerted[key] = {"grade": grade, "time": time.time(), "gem": gem}

        symbol = pair.get("baseToken", {}).get("symbol", "???")
        age    = pair.get("_age_minutes", 0)
        print(f"  [{chain.upper()}] {grade} {symbol} | gem={gem} risk={risk} age={age:.0f}m | {mom} | {narr_label}")

        # Get dev wallet for BNB
        deployer    = ""
        first_buyers = []
        address = pair.get("baseToken", {}).get("address", "")
        if chain == "bsc" and BSCSCAN_API_KEY:
            deployer     = get_deployer_wallet(address)
            first_buyers = get_first_buyers(address, pair.get("pairAddress", ""))

        embed = build_embed(pair, gem, risk, grade, action, color,
                            mom, narr_label, narr_score, deployer, first_buyers)
        send_discord(embed)

        # Send to fire channel for A+ only
        if grade == "A+" and DISCORD_WEBHOOK_FIRE:
            send_discord(embed, DISCORD_WEBHOOK_FIRE)

        time.sleep(0.5)  # small gap to avoid Discord rate limits

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    print("=" * 46)
    print("  🔥 GEM HUNTER PRO v4 — Early Launch Hunter")
    print(f"  Chains: {' + '.join(c.upper() for c in CHAINS)}")
    print(f"  Scan every {SCAN_INTERVAL_SEC}s | Max age {MAX_AGE_MINUTES}m")
    print(f"  BSCScan API: {'✓ dev wallet enabled' if BSCSCAN_API_KEY else '✗ not set (BNB dev info disabled)'}")
    print(f"  Discord: {'✓ configured' if DISCORD_WEBHOOK_URL else '✗ NOT SET'}")
    print("=" * 46)

    scan_count = 0
    while True:
        scan_count += 1
        ts = datetime.now().strftime("%H:%M:%S")
        if scan_count % 10 == 1:
            print(f"\n[{ts}] Scan #{scan_count} | alerted: {len(alerted)} tokens")
        for chain in CHAINS:
            scan_chain(chain)
        time.sleep(SCAN_INTERVAL_SEC)

if __name__ == "__main__":
    main()
