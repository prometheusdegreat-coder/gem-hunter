import os
import time
import json
import threading
import requests
import websocket
from datetime import datetime, timezone

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
# #gems  — A and A+ only. High conviction, take action alerts.
DISCORD_GEMS   = os.getenv("DISCORD_GEMS", "")

# #radar — Pre-launch, first trade, B/C grade watches.
#           Raw signal, do your own research before acting.
DISCORD_RADAR  = os.getenv("DISCORD_RADAR", "")

BSCSCAN_API_KEY = os.getenv("BSCSCAN_API_KEY", "")
SOLANA_RPC      = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")

# DEXScreener fallback scan interval
DEXSCREENER_INTERVAL = 6   # seconds

# Pump.fun program ID on Solana
PUMP_FUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

# ─────────────────────────────────────────────
# FILTERS
# ─────────────────────────────────────────────
MAX_AGE_MINUTES   = 30     # ultra tight — only brand new
MIN_LIQUIDITY_USD = 50
MIN_BUY_PRESSURE  = 40
MIN_VOLUME_5M     = 20
MAX_MCAP_USD      = 2_000_000
CHAINS            = ["solana", "bsc"]

# ─────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────
alerted: dict  = {}   # token -> {grade, time}
lock = threading.Lock()

# ─────────────────────────────────────────────
# NARRATIVE
# ─────────────────────────────────────────────
NARRATIVES = {
    "🤖 AI/Agent":     (["ai","agent","gpt","llm","neural","agi","robot","compute"], 40),
    "🇺🇸 Political":   (["trump","maga","elon","musk","election","fed","america","potus","kamala"], 40),
    "🐸 CT Meme":      (["pepe","wojak","chad","degen","wagmi","frog","ape","meme","based"], 35),
    "🐕 Animal":       (["dog","cat","shiba","panda","bear","rabbit","shark","bonk","wif","popcat","doge","floki"], 35),
    "🌙 Moon/Pump":    (["moon","rocket","100x","pump","gem","alpha","fire","nuke","send","launch"], 30),
    "💰 Wealth":       (["888","777","rich","lambo","gold","fortune","lucky","diamond","jackpot"], 25),
    "🎌 Asian":        (["dragon","zodiac","lunar","sakura","anime","china","japan","korea","thai","viet","asia"], 25),
    "📰 Trending":     (["war","ceasefire","breaking","viral","trending","scandal","news","crisis"], 35),
    "🎮 Gaming/NFT":   (["game","nft","play","metaverse","pixel","quest","gaming","arena"], 20),
}

def get_narrative(symbol: str, name: str = "") -> tuple:
    text = ((symbol or "") + " " + (name or "")).lower()
    matches, total = [], 0
    for label, (kws, weight) in NARRATIVES.items():
        for kw in kws:
            if kw in text:
                matches.append((label, weight))
                total += weight
                break
    if not matches:
        return "❓ No narrative", 0
    matches.sort(key=lambda x: x[1], reverse=True)
    return " + ".join(m[0] for m in matches[:2]), min(total, 100)

# ─────────────────────────────────────────────
# SCORING
# ─────────────────────────────────────────────
def score_pair(pair: dict) -> tuple:
    vol5m   = float(pair.get("volume", {}).get("m5", 0) or 0)
    vol1h   = float(pair.get("volume", {}).get("h1", 0) or 0)
    buys5m  = int(pair.get("txns", {}).get("m5", {}).get("buys",  0) or 0)
    sells5m = int(pair.get("txns", {}).get("m5", {}).get("sells", 0) or 0)
    buys1h  = int(pair.get("txns", {}).get("h1", {}).get("buys",  0) or 0)
    sells1h = int(pair.get("txns", {}).get("h1", {}).get("sells", 0) or 0)
    liq     = float(pair.get("liquidity", {}).get("usd", 0) or 0)
    mcap    = float(pair.get("marketCap", 0) or 0)
    age     = float(pair.get("_age_minutes", 99))
    symbol  = pair.get("baseToken", {}).get("symbol", "")
    name    = pair.get("baseToken", {}).get("name", "")

    tot5m   = buys5m + sells5m or 1
    tot1h   = buys1h + sells1h or 1
    bp5m    = 100.0 * buys5m / tot5m
    bp1h    = 100.0 * buys1h / tot1h
    avg5m   = vol1h / 12 if vol1h > 0 else 0
    impulse = min(vol5m / max(avg5m, 1), 5.0)
    _, narr = get_narrative(symbol, name)

    freshness = (
        100 if age <=  3 else
         95 if age <=  7 else
         88 if age <= 15 else
         75 if age <= 25 else
         55 if age <= 35 else 30
    )

    # Momentum
    if buys5m + sells5m == 0:
        mom = "💀 DEAD"; mom_score = 0
    elif vol5m >= avg5m * 3.0 and buys5m > sells5m:
        mom = "🔥🔥🔥 IGNITING"; mom_score = 100
    elif vol5m >= avg5m * 1.5 and buys5m > sells5m:
        mom = "🔥🔥 PUMPING"; mom_score = 75
    elif buys5m > sells5m:
        mom = "📶 BUILDING"; mom_score = 50
    elif vol5m < avg5m * 0.4:
        mom = "❄️ COOLING"; mom_score = 15
    else:
        mom = "📊 STEADY"; mom_score = 30

    gem = round(min(
        0.25 * bp5m
      + 0.20 * min(impulse * 20, 100)
      + 0.20 * narr
      + 0.20 * freshness
      + 0.10 * mom_score
      + 0.05 * min(buys5m * 5, 100)
    , 100.0), 1)

    conc = 70 if (mcap > 80_000 and liq < 3_000) else 15
    bp_r = 80 if bp1h < 40 else 40 if bp1h < 55 else 15 if bp1h < 70 else 5
    tr_r = (20 if age <= 5 and buys1h < 5 else
            10 if age <= 10 else
            65 if buys1h < 5 else
            35 if buys1h < 15 else
            12 if buys1h < 40 else 5)
    risk = round(min(0.35 * bp_r + 0.35 * tr_r + 0.30 * conc, 100.0), 1)

    return gem, risk, mom, narr

def get_grade(gem, risk, age, mom, narr):
    if risk > 78:             return "F",  "🚫 HIGH RISK — SKIP",         0xFF0000, False
    if "DEAD" in mom:         return "F",  "💀 DEAD",                      0x424242, False
    if gem >= 65 and risk <= 28 and "IGNITING" in mom:
        return "A+", "🚀🚀 EARLY GEM — SCALP NOW",   0x00E676, True
    if gem >= 60 and risk <= 33 and age <= 10:
        return "A+", "⚡ ULTRA FRESH — ACT NOW",      0x00C853, True
    if gem >= 55 and risk <= 40:
        return "A",  "🔥 STRONG SIGNAL — ENTER",     0x64DD17, True
    if gem >= 45 and risk <= 45 and age <= 15 and narr >= 30:
        return "A",  "🎯 NARRATIVE GEM — ENTER",     0x76FF03, True
    if gem >= 42 and risk <= 55:
        return "B",  "👀 GOOD SIGNAL — CONSIDER",    0xFFD600, True
    if gem >= 32 and risk <= 65:
        return "C",  "📊 EARLY WATCH",               0xFF6D00, False
    return                    "D",  "📋 MONITOR",                  0x757575, False

# ─────────────────────────────────────────────
# LINKS
# ─────────────────────────────────────────────
def build_links(chain_id, address, pair_address):
    c = chain_id.lower()
    if c == "bsc":
        return {
            "chart":      f"https://dexscreener.com/bsc/{address}",
            "bubblemaps": f"https://app.bubblemaps.io/bsc/token/{address}",
            "dextools":   f"https://www.dextools.io/app/en/bnb/pair-explorer/{pair_address}",
            "holders":    f"https://bscscan.com/token/{address}#balances",
            "poocoin":    f"https://poocoin.app/tokens/{address}",
            "scan":       f"https://bscscan.com/token/{address}",
        }
    return {
        "chart":      f"https://dexscreener.com/solana/{address}",
        "bubblemaps": f"https://app.bubblemaps.io/sol/token/{address}",
        "dextools":   f"https://www.dextools.io/app/en/solana/pair-explorer/{pair_address}",
        "birdeye":    f"https://birdeye.so/token/{address}?chain=solana",
        "rugcheck":   f"https://rugcheck.xyz/tokens/{address}",
        "holders":    f"https://solscan.io/token/{address}#holders",
        "pumpfun":    f"https://pump.fun/{address}",
        "scan":       f"https://solscan.io/token/{address}",
    }

# ─────────────────────────────────────────────
# BSCSCAN — dev wallet + first buyers
# ─────────────────────────────────────────────
def get_deployer(address):
    if not BSCSCAN_API_KEY or not address.startswith("0x"):
        return ""
    try:
        r = requests.get(
            f"https://api.bscscan.com/api?module=contract&action=getcontractcreation"
            f"&contractaddresses={address}&apikey={BSCSCAN_API_KEY}",
            timeout=5
        )
        d = r.json()
        if d.get("status") == "1" and d.get("result"):
            return d["result"][0].get("contractCreator", "")
    except Exception:
        pass
    return ""

def get_first_buyers(address):
    if not BSCSCAN_API_KEY or not address.startswith("0x"):
        return []
    try:
        r = requests.get(
            f"https://api.bscscan.com/api?module=account&action=tokentx"
            f"&contractaddress={address}&page=1&offset=10&sort=asc"
            f"&apikey={BSCSCAN_API_KEY}",
            timeout=5
        )
        d = r.json()
        if d.get("status") == "1":
            seen, wallets = set(), []
            for tx in d.get("result", []):
                w = tx.get("from", "")
                if w and w not in seen and w.lower() != address.lower():
                    seen.add(w); wallets.append(w)
                if len(wallets) >= 5: break
            return wallets
    except Exception:
        pass
    return []

# ─────────────────────────────────────────────
# DEXScreener
# ─────────────────────────────────────────────
def dex_fetch_profiles(chain):
    try:
        r = requests.get("https://api.dexscreener.com/token-profiles/latest/v1", timeout=7)
        if r.status_code != 200: return []
        return [p for p in (r.json() if isinstance(r.json(), list) else [])
                if (p.get("chainId") or "").lower() == chain]
    except Exception: return []

def dex_fetch_boosted(chain):
    try:
        r = requests.get("https://api.dexscreener.com/token-boosts/latest/v1", timeout=7)
        if r.status_code != 200: return []
        return [p for p in (r.json() if isinstance(r.json(), list) else [])
                if (p.get("chainId") or "").lower() == chain]
    except Exception: return []

def dex_get_pair(chain, address):
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{address}", timeout=7)
        if r.status_code != 200: return None
        pairs = [p for p in (r.json().get("pairs") or [])
                 if (p.get("chainId") or "").lower() == chain]
        if not pairs: return None
        pairs.sort(key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0), reverse=True)
        return pairs[0]
    except Exception: return None

def age_min(pair):
    try:
        c = pair.get("pairCreatedAt")
        if c: return (datetime.now(timezone.utc).timestamp() * 1000 - float(c)) / 60_000
    except Exception: pass
    return 9999

# ─────────────────────────────────────────────
# DISCORD
# ─────────────────────────────────────────────
def send_discord(embed, channel="radar"):
    """
    channel = "gems"  -> #gems  (A / A+ action alerts only)
    channel = "radar" -> #radar (pre-launch, first trade, B/C watches)
    """
    url = DISCORD_GEMS if channel == "gems" else DISCORD_RADAR
    if not url:
        label = "#gems" if channel == "gems" else "#radar"
        print(f"  ⚠️  {label} webhook not set")
        return
    try:
        r = requests.post(url, json={"embeds": [embed]}, timeout=10)
        if r.status_code == 429:
            time.sleep(5)
            requests.post(url, json={"embeds": [embed]}, timeout=10)
        elif r.status_code not in (200, 204):
            print(f"  ⚠️  Discord [{channel}] {r.status_code}: {r.text[:80]}")
    except Exception as e:
        print(f"  ⚠️  Discord [{channel}] error: {e}")

def build_embed(pair, gem, risk, grade, action, color, mom, narr_label,
                narr_score, source, deployer="", first_buyers=None):
    if first_buyers is None:
        first_buyers = []

    base   = pair.get("baseToken", {})
    quote  = pair.get("quoteToken", {})
    sym    = base.get("symbol", "???")
    name   = base.get("name", "Unknown")
    addr   = base.get("address", "")
    chain  = (pair.get("chainId") or "").upper()
    dex    = pair.get("dexId", "?").upper()
    age    = float(pair.get("_age_minutes", 0))
    pair_a = pair.get("pairAddress", "")

    px    = float(pair.get("priceUsd") or 0)
    pxn   = pair.get("priceNative", "?")
    mcap  = float(pair.get("marketCap") or pair.get("fdv") or 0)
    fdv   = float(pair.get("fdv") or 0)
    liq   = float(pair.get("liquidity", {}).get("usd") or 0)
    v5m   = float(pair.get("volume", {}).get("m5")  or 0)
    v1h   = float(pair.get("volume", {}).get("h1")  or 0)
    v6h   = float(pair.get("volume", {}).get("h6")  or 0)
    v24h  = float(pair.get("volume", {}).get("h24") or 0)
    pc5m  = float(pair.get("priceChange", {}).get("m5",  0) or 0)
    pc1h  = float(pair.get("priceChange", {}).get("h1",  0) or 0)
    pc6h  = float(pair.get("priceChange", {}).get("h6",  0) or 0)
    pc24h = float(pair.get("priceChange", {}).get("h24", 0) or 0)
    b5m   = int(pair.get("txns", {}).get("m5",  {}).get("buys",  0) or 0)
    s5m   = int(pair.get("txns", {}).get("m5",  {}).get("sells", 0) or 0)
    b1h   = int(pair.get("txns", {}).get("h1",  {}).get("buys",  0) or 0)
    s1h   = int(pair.get("txns", {}).get("h1",  {}).get("sells", 0) or 0)
    b24h  = int(pair.get("txns", {}).get("h24", {}).get("buys",  0) or 0)
    s24h  = int(pair.get("txns", {}).get("h24", {}).get("sells", 0) or 0)
    bp    = 100.0 * b5m / (b5m + s5m or 1)

    links = build_links((pair.get("chainId") or "").lower(), addr, pair_a)

    def fp(v): 
        a = "📈" if v > 0 else "📉" if v < 0 else "➡️"
        return f"{a} `{'+' if v>=0 else ''}{v:.1f}%`"
    def fv(v):
        if v >= 1_000_000: return f"${v/1_000_000:.2f}M"
        if v >= 1_000:     return f"${v/1_000:.1f}K"
        return f"${v:.0f}"

    risk_label = ("🟢 LOW" if risk<=30 else "🟡 MEDIUM" if risk<=55 else
                  "🟠 HIGH" if risk<=72 else "🔴 DANGER")

    inv = []
    for k, label in [("chart","📊 Chart"),("bubblemaps","🫧 BubbleMaps"),
                      ("dextools","🔧 DexTools"),("birdeye","🦅 Birdeye"),
                      ("rugcheck","🛡️ RugCheck"),("pumpfun","🎯 Pump.fun"),
                      ("holders","👥 Holders"),("poocoin","💩 PooCoin")]:
        if links.get(k): inv.append(f"[{label}]({links[k]})")

    dev = (f"[`{deployer[:6]}...{deployer[-4:]}`]({links.get('scan','')})"
           if deployer else "*Check BubbleMaps / DexTools*")

    buyers = ("\n".join(f"`{w[:6]}...{w[-4:]}`" for w in first_buyers[:5])
              if first_buyers else f"[View on Holders page]({links.get('holders','')})")

    source_emoji = {"pumpfun": "🎯 Pump.fun", "dexscreener": "📡 DEXScreener",
                    "bscscan":  "🔗 BSCScan"}.get(source, "📡 DEXScreener")

    return {
        "title": f"{action}  |  Grade {grade}  |  {chain}  |  {source_emoji}",
        "url":   links.get("chart", ""),
        "color": color,
        "description": (
            f"## {sym} / {quote.get('symbol','?')}\n"
            f"**{name}**  •  {dex}  •  ⏱️ `{age:.1f} min old`\n"
            f"Momentum: {mom}  •  Risk: {risk_label}"
        ),
        "fields": [
            {"name": "🏷️ Narrative",
             "value": f"{narr_label}\nStrength: `{narr_score}/100`", "inline": False},
            {"name": "💰 Price",
             "value": f"USD: `${px:.10f}`\nNative: `{pxn}`", "inline": True},
            {"name": "📊 Market",
             "value": f"MCap: `{fv(mcap)}`\nFDV: `{fv(fdv)}`\nLiq: `{fv(liq)}`", "inline": True},
            {"name": "🏆 Scores",
             "value": f"Gem: `{gem}/100`\nRisk: `{risk}/100`\nNarr: `{narr_score}/100`", "inline": True},
            {"name": "📈 Price Change",
             "value": f"5m: {fp(pc5m)}\n1h: {fp(pc1h)}\n6h: {fp(pc6h)}\n24h: {fp(pc24h)}", "inline": True},
            {"name": "💵 Volume",
             "value": f"5m: `{fv(v5m)}`\n1h: `{fv(v1h)}`\n6h: `{fv(v6h)}`\n24h: `{fv(v24h)}`", "inline": True},
            {"name": "🔄 Trades",
             "value": (f"5m → 🟢`{b5m}` 🔴`{s5m}` ({bp:.0f}% buys)\n"
                       f"1h → 🟢`{b1h}` 🔴`{s1h}`\n"
                       f"24h → 🟢`{b24h}` 🔴`{s24h}`"), "inline": True},
            {"name": "👨‍💻 Dev / Deployer",
             "value": dev, "inline": True},
            {"name": "🎯 First Buyers",
             "value": buyers, "inline": True},
            {"name": "🔍 Investigate",
             "value": "  ".join(inv), "inline": False},
            {"name": "📋 Contract Address",
             "value": f"`{addr}`", "inline": False},
        ],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": f"Gem Hunter Pro v5 • {chain} • Source: {source_emoji}"}
    }

# ─────────────────────────────────────────────
# PROCESS + ALERT
# ─────────────────────────────────────────────
def process_pair(pair, source="dexscreener"):
    sym   = pair.get("baseToken", {}).get("symbol", "???")
    addr  = pair.get("baseToken", {}).get("address", "")
    chain = (pair.get("chainId") or "").lower()
    key   = f"{chain}:{addr}"
    age   = float(pair.get("_age_minutes", 99))

    # Filters
    liq  = float(pair.get("liquidity", {}).get("usd", 0) or 0)
    mcap = float(pair.get("marketCap", 0) or 0)
    b5m  = int(pair.get("txns", {}).get("m5", {}).get("buys",  0) or 0)
    s5m  = int(pair.get("txns", {}).get("m5", {}).get("sells", 0) or 0)
    v5m  = float(pair.get("volume", {}).get("m5", 0) or 0)
    v1h  = float(pair.get("volume", {}).get("h1", 0) or 0)
    bp   = 100.0 * b5m / (b5m + s5m or 1)

    if age > MAX_AGE_MINUTES:              return
    if liq < MIN_LIQUIDITY_USD:            return
    if mcap and mcap > MAX_MCAP_USD:       return
    if v5m + v1h < 100:                    return
    if v5m < MIN_VOLUME_5M:               return
    if bp < MIN_BUY_PRESSURE:             return

    gem, risk, mom, narr_score = score_pair(pair)
    narr_label, _ = get_narrative(
        pair.get("baseToken", {}).get("symbol", ""),
        pair.get("baseToken", {}).get("name", "")
    )
    grade, action, color, is_entry = get_grade(gem, risk, age, mom, narr_score)

    if grade == "D": return

    with lock:
        prev = alerted.get(key)
        should_alert = False
        if not prev:
            should_alert = True
        elif grade in ("A+","A") and prev.get("grade") in ("B","C"):
            should_alert = True
            action = "⬆️ UPGRADED — " + action
        if not should_alert:
            return
        alerted[key] = {"grade": grade, "time": time.time()}

    print(f"  [{chain.upper()}][{source}] {grade} {sym} | gem={gem} risk={risk} age={age:.1f}m | {mom}")

    deployer, first_buyers = "", []
    if chain == "bsc" and BSCSCAN_API_KEY:
        deployer     = get_deployer(addr)
        first_buyers = get_first_buyers(addr)

    embed = build_embed(pair, gem, risk, grade, action, color, mom,
                        narr_label, narr_score, source, deployer, first_buyers)
    channel = "gems" if grade in ("A+", "A") else "radar"
    send_discord(embed, channel)

# ─────────────────────────────────────────────
# SOURCE 1 — PUMP.FUN WebSocket (Solana)
# Catches tokens the MOMENT they are created
# BEFORE first trade — 0-3 second detection
# ─────────────────────────────────────────────
class PumpFunListener:
    def __init__(self):
        self.ws = None
        self.running = False
        self.pending: dict = {}   # mint -> creation data

    def on_message(self, ws, message):
        try:
            data = json.loads(message)
            # New token created event
            if data.get("txType") == "create":
                mint   = data.get("mint", "")
                symbol = data.get("symbol", "???")
                name   = data.get("name", "Unknown")
                if not mint: return

                narr_label, narr_score = get_narrative(symbol, name)
                print(f"  [PUMPFUN] 🆕 {symbol} created | narr={narr_score} | {mint[:8]}...")

                # Store pending — wait for first trade to get price data
                self.pending[mint] = {
                    "symbol": symbol, "name": name,
                    "narr_label": narr_label, "narr_score": narr_score,
                    "created_at": time.time()
                }

                # Alert immediately for strong narrative even before trade
                if narr_score >= 35:
                    links = build_links("solana", mint, "")
                    inv = []
                    for k, label in [("pumpfun","🎯 Pump.fun"),("rugcheck","🛡️ RugCheck"),
                                     ("birdeye","🦅 Birdeye"),("chart","📊 DEXScreener")]:
                        if links.get(k): inv.append(f"[{label}]({links[k]})")

                    embed = {
                        "title": f"🆕 PRE-LAUNCH DETECTED  |  Solana  |  🎯 Pump.fun",
                        "url":   links.get("pumpfun", ""),
                        "color": 0xAA00FF,
                        "description": (
                            f"## {symbol}\n**{name}**\n"
                            f"⏱️ `Just created — no trades yet`\n"
                            f"🏷️ Narrative: {narr_label} `({narr_score}/100)`\n\n"
                            f"*Watch this token — strong narrative. "
                            f"Check Pump.fun for bonding curve progress.*"
                        ),
                        "fields": [
                            {"name": "🔍 Links",
                             "value": "  ".join(inv), "inline": False},
                            {"name": "📋 Contract",
                             "value": f"`{mint}`", "inline": False},
                        ],
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "footer": {"text": "Gem Hunter Pro v5 • Pump.fun PRE-LAUNCH"}
                    }
                    send_discord(embed, "radar")

            # Trade event — token now has price data
            elif data.get("txType") in ("buy", "sell"):
                mint = data.get("mint", "")
                if mint in self.pending:
                    pending = self.pending[mint]
                    age_sec = time.time() - pending["created_at"]
                    # Fetch full pair data from DEXScreener
                    threading.Thread(
                        target=self._enrich_and_alert,
                        args=(mint, pending, age_sec),
                        daemon=True
                    ).start()

        except Exception as e:
            pass

    def _enrich_and_alert(self, mint, pending, age_sec):
        time.sleep(2)  # small delay for DEXScreener to index
        pair = dex_get_pair("solana", mint)
        if pair:
            pair["_age_minutes"] = age_sec / 60
            process_pair(pair, source="pumpfun")
        else:
            # DEXScreener hasn't indexed yet — send basic alert
            narr_label = pending.get("narr_label", "❓ No narrative")
            narr_score = pending.get("narr_score", 0)
            if narr_score >= 25:
                links = build_links("solana", mint, "")
                inv = [f"[🎯 Pump.fun]({links['pumpfun']})",
                       f"[🛡️ RugCheck]({links['rugcheck']})",
                       f"[📊 DEXScreener]({links['chart']})"]
                embed = {
                    "title": f"🔥 FIRST TRADE  |  {pending['symbol']}  |  Solana",
                    "url":   links.get("pumpfun", ""),
                    "color": 0x00BCD4,
                    "description": (
                        f"## {pending['symbol']}\n**{pending['name']}**\n"
                        f"⏱️ `{age_sec:.0f} seconds old — FIRST TRADE JUST HAPPENED`\n"
                        f"🏷️ {narr_label} `({narr_score}/100)`"
                    ),
                    "fields": [
                        {"name": "🔍 Links", "value": "  ".join(inv), "inline": False},
                        {"name": "📋 Contract", "value": f"`{mint}`", "inline": False},
                    ],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "footer": {"text": "Gem Hunter Pro v5 • Pump.fun FIRST TRADE"}
                }
                send_discord(embed, "radar")

    def on_error(self, ws, error):
        print(f"  [PUMPFUN] WebSocket error: {error}")

    def on_close(self, ws, *args):
        print("  [PUMPFUN] WebSocket closed — reconnecting in 5s...")
        self.running = False

    def on_open(self, ws):
        print("  [PUMPFUN] ✅ Connected to Pump.fun WebSocket")
        # Subscribe to new token + trade events
        ws.send(json.dumps({"method": "subscribeNewToken"}))
        ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": [PUMP_FUN_PROGRAM]}))

    def run(self):
        while True:
            try:
                self.running = True
                self.ws = websocket.WebSocketApp(
                    "wss://pumpportal.fun/api/data",
                    on_open=self.on_open,
                    on_message=self.on_message,
                    on_error=self.on_error,
                    on_close=self.on_close,
                )
                self.ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                print(f"  [PUMPFUN] Connection failed: {e}")
            time.sleep(5)

# ─────────────────────────────────────────────
# SOURCE 2 — DEXScreener polling (BNB + Solana)
# Catches tokens 30-120 seconds after first trade
# ─────────────────────────────────────────────
def dexscreener_loop():
    print("  [DEXSCREENER] ✅ Polling started")
    scan = 0
    while True:
        scan += 1
        for chain in CHAINS:
            profiles = dex_fetch_profiles(chain) + dex_fetch_boosted(chain)
            seen = set()
            for profile in profiles:
                addr = profile.get("tokenAddress") or profile.get("address")
                if not addr or addr in seen: continue
                seen.add(addr)
                pair = dex_get_pair(chain, addr)
                if not pair: continue
                pair["_age_minutes"] = age_min(pair)
                process_pair(pair, source="dexscreener")
            time.sleep(1)
        if scan % 20 == 0:
            print(f"  [DEXSCREENER] Scan #{scan} | alerted: {len(alerted)}")
        time.sleep(DEXSCREENER_INTERVAL)

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    print("=" * 50)
    print("  🔥 GEM HUNTER PRO v5 — Multi-Source Hunter")
    print("  Sources: Pump.fun WS + DEXScreener")
    print(f"  Chains: SOLANA (pre-launch) + BNB")
    print(f"  Discord: {'✅ configured' if DISCORD_GEMS else '❌ NOT SET'}")
    print(f"  BSCScan: {'✅ dev wallet ON' if BSCSCAN_API_KEY else '⚠️  not set'}")
    print("=" * 50)

    if not DISCORD_WEBHOOK_URL:
        print("\n❌ DISCORD_WEBHOOK_URL not set in Railway Variables!\n")

    # Thread 1 — Pump.fun WebSocket (Solana pre-launch)
    pump = PumpFunListener()
    t1 = threading.Thread(target=pump.run, daemon=True, name="PumpFun")
    t1.start()

    # Thread 2 — DEXScreener polling (BNB + Solana)
    t2 = threading.Thread(target=dexscreener_loop, daemon=True, name="DEXScreener")
    t2.start()

    print("\n  All sources running. Waiting for gems...\n")

    # Keep main thread alive
    while True:
        time.sleep(60)
        print(f"  ❤️  [{datetime.now().strftime('%H:%M:%S')}] Running | "
              f"Alerted: {len(alerted)} | "
              f"PumpFun pending: {len(pump.pending)}")

if __name__ == "__main__":
    main()
