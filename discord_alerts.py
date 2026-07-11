"""
Crypto Intelligence Platform — Discord Alerter v2
Fixed: Polymarket API format, Claude scoring, investigation links,
wallet leaderboard, Web3 jobs, chain follower filtering.
"""

import os
import time
import json
import hashlib
import threading
import requests
from datetime import datetime, timezone
from dataclasses import asdict

# ── Webhooks ──────────────────────────────────────────────────
DISCORD_BREAKING_NEWS       = os.getenv("DISCORD_BREAKING_NEWS", "")
DISCORD_EMERGING_CHAINS     = os.getenv("DISCORD_EMERGING_CHAINS", "")
DISCORD_OPPORTUNITIES       = os.getenv("DISCORD_OPPORTUNITIES", "")
DISCORD_SECURITY_ALERTS     = os.getenv("DISCORD_SECURITY_ALERTS", "")
DISCORD_POLYMARKET_RESEARCH = os.getenv("DISCORD_POLYMARKET_RESEARCH", "")
DISCORD_POLYMARKET_ALPHA    = os.getenv("DISCORD_POLYMARKET_ALPHA", "")

# ── Intervals ─────────────────────────────────────────────────
NEWS_INTERVAL_MIN   = 15
CHAINS_INTERVAL_MIN = 120
OPPS_INTERVAL_MIN   = 60
POLY_INTERVAL_HR    = 3
JOBS_INTERVAL_HR    = 6

# ── State ─────────────────────────────────────────────────────
alerted_news:    set = set()
alerted_chains:  set = set()
alerted_opps:    set = set()
alerted_markets: set = set()

def get_market_yes_price(market: dict) -> float:
    """
    Polymarket stores outcomes/prices as two PARALLEL arrays:
    outcomes = ["Yes", "No"], outcomePrices = ["0.65", "0.35"]
    Find the index of "Yes" and return its matching price.
    """
    outcomes = market.get("outcomes", [])
    prices   = market.get("outcomePrices", [])
    try:
        for i, o in enumerate(outcomes):
            if str(o).strip().upper() == "YES":
                return float(prices[i])
    except Exception:
        pass
    try:
        return float(prices[0])
    except Exception:
        return 0.5

alerted_jobs:    set = set()

# ─────────────────────────────────────────────────────────────
# DISCORD SENDER
# ─────────────────────────────────────────────────────────────
def send(webhook_url: str, embed: dict, content: str = ""):
    if not webhook_url:
        return
    try:
        payload = {"embeds": [embed]}
        if content:
            payload["content"] = content
        r = requests.post(webhook_url, json=payload, timeout=10)
        if r.status_code == 429:
            retry = r.json().get("retry_after", 5)
            time.sleep(float(retry))
            requests.post(webhook_url, json=payload, timeout=10)
        elif r.status_code not in (200, 204):
            print(f"[DISCORD] Error {r.status_code}: {r.text[:80]}")
    except Exception as e:
        print(f"[DISCORD] Send error: {e}")

def bar(score: float, length: int = 10) -> str:
    filled = int((score / 100) * length)
    return "█" * filled + "░" * (length - filled)

def safe_get(obj, *keys, default=""):
    """Safely get nested keys from dict."""
    for key in keys:
        if not isinstance(obj, dict):
            return default
        obj = obj.get(key, default)
    return obj

# ─────────────────────────────────────────────────────────────
# CLAUDE API — with debug logging
# ─────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

def call_claude(prompt: str, max_tokens: int = 2000) -> dict:
    """Call Claude and return parsed JSON dict. Returns {} on any error."""
    if not ANTHROPIC_API_KEY:
        print("[CLAUDE] ❌ ANTHROPIC_API_KEY not set")
        return {}
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json"
            },
            json={
                "model":      "claude-sonnet-4-6",
                "max_tokens": max_tokens,
                "system":     (
                    "You are an institutional crypto research analyst. "
                    "Always respond with valid JSON only. No markdown, no preamble."
                ),
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=60
        )
        if r.status_code != 200:
            print(f"[CLAUDE] API error {r.status_code}: {r.text[:200]}")
            return {}
        text = r.json()["content"][0]["text"].strip()
        # Strip markdown fences if present
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        text = text.strip()
        return json.loads(text)
    except json.JSONDecodeError as e:
        print(f"[CLAUDE] JSON parse error: {e}")
        return {}
    except Exception as e:
        print(f"[CLAUDE] Error: {e}")
        return {}

# ─────────────────────────────────────────────────────────────
# EMBED BUILDERS
# ─────────────────────────────────────────────────────────────
def build_news_embed(item: dict, analysis: dict) -> dict:
    title    = item.get("title", "")[:250]
    source   = item.get("source", "Unknown")
    url      = item.get("url", "")
    impact   = analysis.get("impact", "medium")
    fomo     = analysis.get("is_fomo_driven", False)
    sentiment = analysis.get("sentiment", "neutral")
    assets   = ", ".join(analysis.get("affected_assets", [])[:5]) or "General"
    summary  = analysis.get("summary", item.get("summary", ""))[:400]
    color    = 0xFF0000 if impact == "high" else 0xFF8C00 if impact == "medium" else 0x4CAF50

    fields = [
        {"name": "📰 Source",    "value": source,          "inline": True},
        {"name": "📊 Impact",    "value": impact.upper(),  "inline": True},
        {"name": "📈 Sentiment", "value": sentiment.upper(),"inline": True},
        {"name": "🪙 Assets",    "value": assets,          "inline": False},
        {"name": "📋 Summary",   "value": summary,         "inline": False},
    ]
    if fomo:
        fields.append({
            "name":  "🚨 FOMO/HYPE WARNING",
            "value": "This story shows signs of sentiment-driven hype. Verify before acting.",
            "inline": False
        })
    return {
        "title":     f"{'🔴' if impact=='high' else '🟡'} {title}",
        "url":       url,
        "color":     color,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": f"Crypto Intel • Breaking News • {source}"}
    }

def build_chain_embed(chain: dict, analysis: dict) -> dict:
    name     = chain.get("name", "Unknown")
    desc     = chain.get("description", "No description")[:300]
    url      = chain.get("url", "")
    stars    = chain.get("stars", 0)
    commits  = chain.get("commits_30d", 0)
    contrib  = chain.get("contributors", 0)
    lang     = chain.get("language", "Unknown")
    address  = chain.get("contract_address", "")

    opp_score = float(analysis.get("opportunity_score", 0))
    conf      = float(analysis.get("confidence_score", 0))
    rec       = analysis.get("recommendation", "monitor")
    thesis    = analysis.get("investment_thesis", "")[:300]
    sec       = analysis.get("security_assessment", {}) if isinstance(analysis.get("security_assessment"), dict) else {}
    sec_score = float(sec.get("score", 50))
    sec_flags = sec.get("flags", [])
    red_flags  = analysis.get("red_flags", [])
    green_flags = analysis.get("green_flags", [])

    color = (0x00E676 if opp_score >= 65 else
             0xFFD600 if opp_score >= 45 else
             0xFF6D00 if opp_score >= 30 else 0x9E9E9E)

    rec_emoji = {"monitor": "👁️", "research": "🔬", "avoid": "🚫"}.get(rec, "📋")

    # Investigation links
    github_link = url if url else ""
    inv_links = []
    if github_link:
        inv_links.append(f"[📁 GitHub]({github_link})")
    # Approximate chain search links
    inv_links.append(f"[📊 CoinGecko](https://www.coingecko.com/en/search?query={name})")
    inv_links.append(f"[🔍 DefiLlama](https://defillama.com/search?q={name})")
    inv_links.append(f"[📰 CryptoPanic](https://cryptopanic.com/news/{name.lower()}/)")
    inv_links.append(f"[🐦 X/Twitter](https://twitter.com/search?q={name}+blockchain)")

    fields = [
        {"name": "💡 Investment Thesis",
         "value": thesis or "Insufficient data — check GitHub for more context",
         "inline": False},
        {"name": "📊 Opportunity",
         "value": f"`{opp_score:.0f}/100` {bar(opp_score)}",
         "inline": True},
        {"name": "🔐 Security",
         "value": f"`{sec_score:.0f}/100` {bar(sec_score)}",
         "inline": True},
        {"name": "✅ Confidence",
         "value": f"`{conf:.0f}/100`",
         "inline": True},
        {"name": "⚙️ Dev Activity (30d)",
         "value": (f"⭐ Stars: `{stars}` | 📝 Commits: `{commits}`\n"
                   f"👥 Contributors: `{contrib}` | 💻 Lang: `{lang}`"),
         "inline": False},
    ]

    if green_flags:
        fields.append({
            "name":  "🟢 Green Flags",
            "value": "\n".join(f"• {g}" for g in green_flags[:3]),
            "inline": True
        })
    if red_flags:
        fields.append({
            "name":  "🔴 Red Flags",
            "value": "\n".join(f"• {r}" for r in red_flags[:3]),
            "inline": True
        })
    if sec_flags:
        fields.append({
            "name":  "🚨 Security Flags",
            "value": "\n".join(f"• {f}" for f in sec_flags[:3]),
            "inline": False
        })

    fields.append({
        "name":  "🔍 Investigate",
        "value": "  ".join(inv_links),
        "inline": False
    })

    return {
        "title":       f"⛓️ EMERGING CHAIN: {name}  |  {rec_emoji} {rec.upper()}",
        "url":         url,
        "color":       color,
        "description": desc,
        "fields":      fields,
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "footer":      {"text": "Crypto Intel • Emerging Chains Scanner • GitHub"}
    }

def build_opportunity_embed(report: dict) -> dict:
    query   = report.get("query", "")
    summary = report.get("summary", "")[:400]
    opp     = float(report.get("opportunity_score", 0))
    risk    = float(report.get("risk_score", 50))
    conf    = float(report.get("confidence_score", 0))
    horizon = report.get("time_horizon", "unknown")
    bull    = report.get("bull_case", [])[:3]
    bear    = report.get("bear_case", [])[:2]
    cats    = report.get("catalysts", [])[:3]
    risks   = report.get("risks", [])[:3]
    fomo    = report.get("fomo_flags", [])
    facts   = report.get("facts", [])[:3]
    infer   = report.get("inferences", [])[:2]

    color = (0x00E676 if opp >= 70 and risk <= 35 else
             0x64DD17 if opp >= 55 and risk <= 50 else
             0xFFD600 if opp >= 40 else 0xFF6D00)

    risk_label = ("🟢 LOW" if risk <= 30 else "🟡 MEDIUM" if risk <= 55 else
                  "🟠 HIGH" if risk <= 72 else "🔴 DANGER")

    fields = [
        {"name": "📊 Scores",
         "value": (f"Opportunity: `{opp:.0f}/100` {bar(opp)}\n"
                   f"Risk: `{risk:.0f}/100` {bar(risk)}\n"
                   f"Confidence: `{conf:.0f}/100`\n"
                   f"Risk Level: {risk_label} | Horizon: `{horizon}`"),
         "inline": False},
    ]
    if facts:
        fields.append({"name": "✅ Confirmed Facts",
                        "value": "\n".join(f"• {f}" for f in facts),
                        "inline": False})
    if bull:
        fields.append({"name": "🐂 Bull Case",
                        "value": "\n".join(f"• {b}" for b in bull),
                        "inline": True})
    if bear:
        fields.append({"name": "🐻 Bear Case",
                        "value": "\n".join(f"• {b}" for b in bear),
                        "inline": True})
    if cats:
        fields.append({"name": "⚡ Catalysts",
                        "value": "\n".join(f"• {c}" for c in cats),
                        "inline": False})
    if risks:
        fields.append({"name": "⚠️ Key Risks",
                        "value": "\n".join(f"• {r}" for r in risks),
                        "inline": False})
    if infer:
        fields.append({"name": "🔎 Inferences [NOT CONFIRMED]",
                        "value": "\n".join(f"• {i}" for i in infer),
                        "inline": False})
    if fomo:
        fields.append({"name": "🚨 FOMO/Hype Warning",
                        "value": "\n".join(f"• {f}" for f in fomo[:2]),
                        "inline": False})

    return {
        "title":       f"🎯 OPPORTUNITY: {query[:150]}",
        "color":       color,
        "description": summary,
        "fields":      fields,
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "footer":      {"text": "Crypto Intel • AI Research • Facts/Inferences labeled separately"}
    }

def build_polymarket_research_embed(market: dict, analysis: dict) -> dict:
    question  = market.get("question", market.get("title", "Unknown"))[:200]
    end_date  = str(market.get("endDate", market.get("end_date", "Unknown")))[:10]
    volume    = float(market.get("volume", 0) or 0)
    liquidity = float(market.get("liquidity", 0) or 0)
    url       = f"https://polymarket.com/event/{market.get('slug', market.get('id',''))}"

    prob_est  = float(analysis.get("probability_estimate", 0.5))
    edge      = analysis.get("edge", "fairly_priced")
    conf      = float(analysis.get("confidence_score", 0))
    bull      = analysis.get("bull_case", [])[:3]
    bear      = analysis.get("bear_case", [])[:3]
    ev        = analysis.get("evidence_quality", "unknown")
    reasoning = analysis.get("reasoning_chain", [])[:3]

    edge_color = (0x00E676 if edge == "underpriced" else
                  0xFF6D00 if edge == "overpriced" else 0x9E9E9E)
    edge_emoji = ("🟢 UNDERPRICED — Value Bet" if edge == "underpriced" else
                  "🔴 OVERPRICED — Fade It" if edge == "overpriced" else "⚪ FAIRLY PRICED")

    fields = [
        {"name": "📊 AI Probability Estimate",
         "value": (f"`{prob_est*100:.1f}%`  Edge: {edge_emoji}\n"
                   f"Evidence Quality: `{ev.upper()}`  Confidence: `{conf:.0f}/100`"),
         "inline": False},
        {"name": "💵 Market Stats",
         "value": f"Volume: `${volume:,.0f}`\nLiquidity: `${liquidity:,.0f}`\nCloses: `{end_date}`",
         "inline": True},
    ]
    if bull:
        fields.append({"name": "🐂 Bull Case (YES)",
                        "value": "\n".join(f"• {b}" for b in bull),
                        "inline": True})
    if bear:
        fields.append({"name": "🐻 Bear Case (NO)",
                        "value": "\n".join(f"• {b}" for b in bear),
                        "inline": True})
    if reasoning:
        fields.append({"name": "🔗 Reasoning Chain",
                        "value": "\n".join(f"{i+1}. {s}" for i,s in enumerate(reasoning)),
                        "inline": False})
    fields.append({"name": "⚠️ Disclaimer",
                    "value": "*AI estimate only. Not financial advice. Verify independently.*",
                    "inline": False})

    return {
        "title":     f"🔮 POLYMARKET: {question}",
        "url":       url,
        "color":     edge_color,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Crypto Intel • Polymarket Research • Evidence-Based"}
    }

def build_polymarket_alpha_embed(market: dict, analysis: dict, edge_pct: float) -> dict:
    question = market.get("question", market.get("title","Unknown"))[:200]
    prob_est = float(analysis.get("probability_estimate", 0.5))
    edge     = analysis.get("edge", "fairly_priced")
    conf     = float(analysis.get("confidence_score", 0))
    rec      = analysis.get("recommendation", "pass").upper()
    ev       = analysis.get("evidence_quality", "weak")
    volume   = float(market.get("volume", 0) or 0)
    bull     = analysis.get("bull_case", [])[:2]
    bear     = analysis.get("bear_case", [])[:2]
    reasoning = analysis.get("reasoning_chain", [])[:4]
    url      = f"https://polymarket.com/event/{market.get('slug', market.get('id',''))}"

    color = 0x00E676 if edge == "underpriced" else 0xFF0000 if edge == "overpriced" else 0x9E9E9E

    action = (
        f"**BUY YES** — Edge: `+{edge_pct:.1f}%`" if edge == "underpriced" else
        f"**BUY NO / FADE** — Edge: `+{edge_pct:.1f}%`" if edge == "overpriced" else
        "**PASS** — No meaningful edge"
    )

    fields = [
        {"name": "⚡ ACTION",   "value": action, "inline": False},
        {"name": "📊 Edge",
         "value": (f"AI Estimate: `{prob_est*100:.1f}%`\n"
                   f"Edge: `{edge_pct:.1f}%`\n"
                   f"Evidence: `{ev.upper()}`\n"
                   f"Confidence: `{conf:.0f}/100`\n"
                   f"Volume: `${volume:,.0f}`"),
         "inline": False},
    ]
    if bull:
        fields.append({"name": "🐂 Supporting (YES)",
                        "value": "\n".join(f"• {b}" for b in bull),
                        "inline": True})
    if bear:
        fields.append({"name": "🐻 Counter (NO)",
                        "value": "\n".join(f"• {b}" for b in bear),
                        "inline": True})
    if reasoning:
        fields.append({"name": "🔗 Reasoning",
                        "value": "\n".join(f"{i+1}. {s}" for i,s in enumerate(reasoning)),
                        "inline": False})
    fields.append({"name": "⚠️ Critical Disclaimer",
                    "value": "*AI estimate — NOT financial advice. Prediction markets carry total loss risk.*",
                    "inline": False})

    return {
        "title":       f"🎯 POLY ALPHA: {question}",
        "url":         url,
        "color":       color,
        "description": f"**Rec: {rec}**  |  Confidence: `{conf:.0f}/100`",
        "fields":      fields,
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "footer":      {"text": "Crypto Intel • Polymarket Alpha • AI Edge Detection"}
    }

def build_wallet_embed(wallet: dict, rank: int) -> dict:
    address  = wallet.get("proxyWalletAddress", wallet.get("address","Unknown"))
    pnl      = float(wallet.get("profit", wallet.get("realizedPnl", 0)) or 0)
    volume   = float(wallet.get("volume", 0) or 0)
    trades   = int(wallet.get("tradesCount", wallet.get("numTrades", 0)) or 0)
    win_rate = float(wallet.get("winRate", wallet.get("percentPositive", 0)) or 0)

    color = 0x00E676 if pnl > 10000 else 0xFFD600 if pnl > 1000 else 0x9E9E9E
    short = f"{address[:6]}...{address[-4:]}" if len(address) > 10 else address
    poly_url = f"https://polymarket.com/profile/{address}"

    return {
        "title":       f"🏆 #{rank} Profitable Wallet: {short}",
        "url":         poly_url,
        "color":       color,
        "description": f"Consistently profitable Polymarket trader",
        "fields": [
            {"name": "💰 Realized PnL",  "value": f"`${pnl:,.2f}`",       "inline": True},
            {"name": "📊 Volume",         "value": f"`${volume:,.0f}`",     "inline": True},
            {"name": "🔄 Trades",         "value": f"`{trades}`",           "inline": True},
            {"name": "🎯 Win Rate",       "value": f"`{win_rate:.1f}%`",    "inline": True},
            {"name": "🔍 Profile",        "value": f"[View on Polymarket]({poly_url})", "inline": False},
        ],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Crypto Intel • Polymarket Wallet Leaderboard"}
    }

def build_job_embed(job: dict) -> dict:
    title    = job.get("title","Unknown Role")[:200]
    company  = job.get("company","Unknown")
    url      = job.get("url","")
    summary  = job.get("summary","")[:300]
    category = job.get("category","Operations")
    remote   = "🌍 Remote" if job.get("remote") else "🏢 On-site/Hybrid"
    pub      = str(job.get("published",""))[:10]

    cat_emoji = {
        "Engineering": "⚙️", "Research": "🔬", "Security": "🔐",
        "Data": "📊", "AI": "🤖", "DevRel": "🤝",
        "Marketing": "📣", "Design": "🎨", "Community": "👥",
        "Operations": "📋"
    }.get(category, "💼")

    color = {
        "Engineering": 0x00E676, "AI": 0x7C4DFF, "Security": 0xFF6D00,
        "Research": 0x00BCD4, "Data": 0xFFD600
    }.get(category, 0x9E9E9E)

    return {
        "title":       f"{cat_emoji} {title}",
        "url":         url,
        "color":       color,
        "description": f"**{company}** • {remote} • {category}",
        "fields": [
            {"name": "📋 Description", "value": summary or "See link for details", "inline": False},
            {"name": "📅 Posted",      "value": f"`{pub}`",                         "inline": True},
            {"name": "🏷️ Category",    "value": category,                            "inline": True},
            {"name": "🔗 Apply",       "value": f"[View Job]({url})" if url else "N/A", "inline": True},
        ],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": f"Crypto Intel • Web3 Jobs • {company}"}
    }

# ─────────────────────────────────────────────────────────────
# WORKERS
# ─────────────────────────────────────────────────────────────
def news_worker():
    from research_engine import fetch_rss_news, fetch_cryptopanic
    print("[DISCORD] 📰 News worker started")
    while True:
        try:
            news  = fetch_rss_news(max_per_feed=5)
            cp    = fetch_cryptopanic("important")
            all_n = news + cp
            if not all_n:
                time.sleep(NEWS_INTERVAL_MIN * 60)
                continue

            # Quick AI batch analysis
            titles = "\n".join(f"- {n.title}" for n in all_n[:20])
            result = call_claude(f"""
Analyze these crypto news headlines and return JSON:
{{
  "high_impact": ["titles of genuinely market-moving news"],
  "fomo_signals": ["titles that appear hype/FOMO driven"],
  "market_sentiment": "bullish|bearish|neutral",
  "top_story_summary": "2 sentence summary of most important story"
}}
Headlines:
{titles}
""", max_tokens=800)

            high_impact  = result.get("high_impact", [])
            fomo_signals = set(result.get("fomo_signals", []))

            for item in all_n:
                h = hashlib.md5(item.title.lower().encode()).hexdigest()
                if h in alerted_news:
                    continue
                is_high = any(item.title[:40] in hi for hi in high_impact)
                is_cp   = item.source == "CryptoPanic" and item.impact == "high"
                if not (is_high or is_cp):
                    continue

                alerted_news.add(h)
                analysis = {
                    "impact":          "high" if is_high else "medium",
                    "sentiment":       result.get("market_sentiment","neutral"),
                    "affected_assets": item.assets,
                    "summary":         item.summary,
                    "is_fomo_driven":  item.title in fomo_signals,
                }
                embed = build_news_embed(
                    {"title": item.title, "source": item.source,
                     "url": item.url, "published": item.published},
                    analysis
                )
                send(DISCORD_BREAKING_NEWS, embed)
                print(f"[DISCORD] 📰 {item.title[:60]}")
                time.sleep(1)

        except Exception as e:
            print(f"[NEWS WORKER] Error: {e}")
        time.sleep(NEWS_INTERVAL_MIN * 60)


def chains_worker():
    from research_engine import search_github_blockchains, scan_github_repo, quick_security_scan
    print("[DISCORD] ⛓️ Chains worker started")

    queries = [
        "new blockchain layer1 consensus 2025",
        "blockchain testnet stealth 2025",
        "zk rollup layer2 protocol 2025",
        "blockchain less than 200 followers new launch",
    ]

    while True:
        try:
            for query in queries:
                repos = search_github_blockchains(query, max_results=10)
                for repo in repos:
                    name  = repo.get("name","")
                    full  = repo.get("full_name","")
                    stars = repo.get("stargazers_count", 0)

                    # Only alert low-exposure projects (under 250 stars = proxy for <250 followers)
                    if stars > 5000:
                        continue

                    key = full.lower()
                    if key in alerted_chains:
                        continue

                    github = scan_github_repo(full)
                    chain_data = {
                        "name":         name,
                        "description":  repo.get("description",""),
                        "stars":        stars,
                        "commits_30d":  github.get("commits_30d", 0),
                        "contributors": github.get("contributors", 0),
                        "language":     github.get("language",""),
                        "url":          repo.get("html_url",""),
                        "topics":       repo.get("topics",[]),
                        "testnet_live": any(t in repo.get("topics",[])
                                            for t in ["testnet","mainnet","devnet"]),
                    }

                    # Must have real dev activity
                    if chain_data["commits_30d"] < 3 and chain_data["contributors"] < 2:
                        continue

                    # Score opportunity
                    commits   = chain_data["commits_30d"]
                    contrib   = chain_data["contributors"]
                    dev_score = min(commits * 2 + contrib * 5, 100)
                    sec_flags = quick_security_scan(chain_data["description"] + " " + name)
                    opp_total = max(0, min(100,
                        0.3 * dev_score +
                        0.2 * (30 if chain_data["testnet_live"] else 0) +
                        0.2 * (20 if chain_data["description"] else 0) +
                        0.15 * max(0, 100 - stars / 50) +
                        0.15 * max(0, 60 - len(sec_flags) * 15)
                    ))

                    if opp_total < 35:
                        continue

                    # AI analysis
                    analysis = call_claude(f"""
Analyze this emerging blockchain project:
Name: {name}
Description: {chain_data['description']}
Stars: {stars} | Commits(30d): {commits} | Contributors: {contrib}
Language: {chain_data['language']} | Testnet: {chain_data['testnet_live']}
Topics: {', '.join(chain_data['topics'][:5])}

Return JSON:
{{
  "investment_thesis": "2-3 sentence thesis",
  "security_assessment": {{"score": 0-100, "flags": [], "contract_risk": "high|medium|low"}},
  "developer_assessment": "strong|moderate|weak",
  "green_flags": ["up to 3 positive signals"],
  "red_flags": ["up to 3 concerns"],
  "recommendation": "monitor|research|avoid",
  "opportunity_score": 0-100,
  "confidence_score": 0-100,
  "reasoning": ["step 1", "step 2"]
}}
""", max_tokens=1000)

                    if not analysis:
                        analysis = {
                            "investment_thesis": "Insufficient data for analysis",
                            "security_assessment": {"score": max(0, 60 - len(sec_flags)*15), "flags": sec_flags},
                            "recommendation": "monitor",
                            "opportunity_score": opp_total,
                            "confidence_score": 30,
                            "green_flags": [],
                            "red_flags": sec_flags[:2]
                        }

                    analysis["opportunity_score"] = float(analysis.get("opportunity_score", opp_total))
                    analysis["confidence_score"]  = float(analysis.get("confidence_score", 30))

                    alerted_chains.add(key)
                    embed = build_chain_embed(chain_data, analysis)
                    send(DISCORD_EMERGING_CHAINS, embed)
                    print(f"[DISCORD] ⛓️ {name} (score={analysis['opportunity_score']:.0f} stars={stars})")

                    # Security alert if flags
                    if sec_flags and float((analysis.get("security_assessment") or {}).get("score", 100)) < 40:
                        sec_embed = {
                            "title":   f"🚨 SECURITY FLAG: {name}",
                            "color":   0xFF0000,
                            "description": f"Potential security concerns in {name}",
                            "fields":  [{"name": "⚠️ Flags", "value": "\n".join(f"• {f}" for f in sec_flags), "inline": False}],
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "footer":  {"text": "Crypto Intel • Security Monitor"}
                        }
                        send(DISCORD_SECURITY_ALERTS, sec_embed)

                    time.sleep(2)

        except Exception as e:
            print(f"[CHAINS WORKER] Error: {e}")
        time.sleep(CHAINS_INTERVAL_MIN * 60)


def opportunities_worker():
    from research_engine import get_trending_coins, get_global_market_data, fetch_cryptopanic
    print("[DISCORD] 🎯 Opportunities worker started")

    TOPICS = [
        "Most promising DeFi protocol with real revenue and under $100M market cap right now",
        "Best emerging RWA tokenization opportunity with institutional backing",
        "Undervalued L1 or L2 blockchain with strong developer activity and low market cap",
        "Most significant upcoming crypto catalyst or protocol upgrade in next 30 days",
        "AI and crypto convergence — best risk-adjusted investment opportunity",
        "Cross-chain bridge or interoperability protocol with strong fundamentals",
        "Highest conviction crypto trade setup based on on-chain data and fundamentals",
        "Institutional crypto adoption catalyst — what is most likely to move markets",
    ]
    idx = 0

    while True:
        try:
            trending = get_trending_coins()
            global_m = get_global_market_data()
            cp_news  = fetch_cryptopanic("important")

            context = []
            if trending:
                names = [c["item"]["name"] for c in trending[:5]]
                context.append(f"Currently trending: {', '.join(names)}")
            if global_m:
                context.append(
                    f"Market 24h change: {global_m.get('market_cap_change_percentage_24h_usd',0):.1f}% | "
                    f"BTC dominance: {global_m.get('btc_dominance',0):.1f}%"
                )
            if cp_news:
                context.append("Recent news: " + " | ".join(n.title[:50] for n in cp_news[:3]))

            topic = TOPICS[idx % len(TOPICS)]
            idx += 1

            context_str = "\n".join(context)
            print(f"[DISCORD] 🎯 Researching: {topic}")

            result = call_claude(f"""
You are an institutional crypto research analyst. Research this topic:
"{topic}"

Current market context:
{context_str}

Return JSON (all fields required):
{{
  "summary": "2-3 sentence executive summary",
  "key_findings": ["finding 1", "finding 2", "finding 3"],
  "bull_case": ["bull point 1", "bull point 2"],
  "bear_case": ["bear point 1", "bear point 2"],
  "risks": ["risk 1", "risk 2"],
  "catalysts": ["catalyst 1", "catalyst 2"],
  "facts": ["confirmed fact 1", "confirmed fact 2"],
  "inferences": ["[INFERENCE] deduction 1"],
  "fomo_flags": ["any hype or FOMO signals detected"],
  "opportunity_score": 0-100,
  "risk_score": 0-100,
  "confidence_score": 0-100,
  "time_horizon": "immediate|short|medium|long",
  "reasoning_chain": ["step 1", "step 2", "step 3"]
}}

Rules: opportunity_score reflects potential vs risk. confidence_score reflects evidence quality.
Always include at least one bear case and one fomo_flag check.
Label all inferences with [INFERENCE].
""", max_tokens=2000)

            if not result:
                print(f"[DISCORD] 🎯 Claude returned empty — check API key")
                time.sleep(OPPS_INTERVAL_MIN * 60)
                continue

            opp = float(result.get("opportunity_score", 0))
            print(f"[DISCORD] 🎯 Score: {opp}/100 for: {topic[:50]}")

            if opp < 45:
                print(f"[DISCORD] 🎯 Score too low ({opp}) — skipping")
                time.sleep(OPPS_INTERVAL_MIN * 60)
                continue

            h = hashlib.md5(topic.lower().encode()).hexdigest()
            if h in alerted_opps:
                time.sleep(OPPS_INTERVAL_MIN * 60)
                continue
            alerted_opps.add(h)

            result["query"] = topic
            embed = build_opportunity_embed(result)
            send(DISCORD_OPPORTUNITIES, embed)
            print(f"[DISCORD] 🎯 Sent: {topic[:50]} (score={opp})")

        except Exception as e:
            print(f"[OPPS WORKER] Error: {e}")
        time.sleep(OPPS_INTERVAL_MIN * 60)


def polymarket_worker():
    print("[DISCORD] 🔮 Polymarket worker started")

    MIN_EDGE       = 8.0
    MIN_CONFIDENCE = 45.0
    MIN_VOLUME     = 3000

    crypto_keywords = [
        "bitcoin","btc","ethereum","eth","crypto","sec","etf","fed",
        "interest rate","inflation","blockchain","defi","stablecoin",
        "regulation","trump","election","solana","sol","bnb","xrp",
        "coinbase","polymarket","cbdc","tariff","macro"
    ]

    while True:
        try:
            print("[POLYMARKET] Scanning markets...")

            # Fetch markets — handle both list and dict responses
            try:
                r = requests.get(
                    "https://gamma-api.polymarket.com/markets",
                    params={"limit": 50, "active": "true", "closed": "false"},
                    timeout=10
                )
                raw = r.json()
                # API may return list or dict with results key
                if isinstance(raw, list):
                    markets = raw
                elif isinstance(raw, dict):
                    markets = raw.get("results", raw.get("markets", []))
                else:
                    markets = []
            except Exception as e:
                print(f"[POLYMARKET] Fetch error: {e}")
                time.sleep(POLY_INTERVAL_HR * 3600)
                continue

            print(f"[POLYMARKET] Got {len(markets)} markets")

            # Filter to crypto-relevant with volume
            relevant = []
            for m in markets:
                if not isinstance(m, dict):
                    continue
                q   = (m.get("question","") + " " + m.get("title","")).lower()
                vol = float(m.get("volume", 0) or 0)
                if vol < MIN_VOLUME:
                    continue
                if any(kw in q for kw in crypto_keywords):
                    relevant.append(m)

            print(f"[POLYMARKET] {len(relevant)} relevant markets")

            for market in relevant:
                mid = str(market.get("id","") or market.get("conditionId",""))
                if not mid or mid in alerted_markets:
                    continue

                question = market.get("question", market.get("title",""))[:150]
                volume   = float(market.get("volume",0) or 0)

                # Get market implied YES price
                market_price = 0.5
                outcomes = market.get("outcomes", [])
                if isinstance(outcomes, list):
                    for o in outcomes:
                        if isinstance(o, dict) and str(o.get("value","")).upper() in ("YES","TRUE","1"):
                            try: market_price = float(o.get("price", 0.5))
                            except: pass
                # Also try outcomePrices
                op = market.get("outcomePrices")
                if op and isinstance(op, (list, str)):
                    try:
                        prices = json.loads(op) if isinstance(op, str) else op
                        if prices: market_price = float(prices[0])
                    except: pass

                # AI analysis
                analysis = call_claude(f"""
You are a prediction market analyst. Analyze this Polymarket market:

Question: {question}
Current YES price: {market_price:.1%}
Volume: ${volume:,.0f}

Research the question and provide probability estimate based on evidence.

Return JSON:
{{
  "probability_estimate": 0.0-1.0,
  "edge": "underpriced|overpriced|fairly_priced",
  "bull_case": ["reason YES more likely"],
  "bear_case": ["reason NO more likely"],
  "key_risks": ["risk 1"],
  "key_catalysts": ["catalyst 1"],
  "evidence_quality": "strong|moderate|weak",
  "confidence_score": 0-100,
  "recommendation": "yes|no|pass",
  "reasoning_chain": ["step 1", "step 2", "step 3"]
}}

Base probability on evidence NOT just the market price.
Express uncertainty — give range like 45-65% not just 55%.
""", max_tokens=1200)

                if not analysis or not isinstance(analysis, dict):
                    continue

                prob_est = float(analysis.get("probability_estimate", 0.5))
                edge     = analysis.get("edge", "fairly_priced")
                conf     = float(analysis.get("confidence_score", 0))
                ev       = analysis.get("evidence_quality", "weak")
                rec      = analysis.get("recommendation", "pass")
                edge_pct = abs(prob_est - market_price) * 100

                ev_rank = {"weak": 0, "moderate": 1, "strong": 2}.get(ev, 0)

                # Research channel — any moderate+ confidence
                if conf >= MIN_CONFIDENCE and ev_rank >= 1:
                    embed = build_polymarket_research_embed(market, analysis)
                    send(DISCORD_POLYMARKET_RESEARCH, embed)
                    print(f"[POLYMARKET] 📊 Research: {question[:50]} (conf={conf:.0f} edge={edge_pct:.1f}%)")
                    time.sleep(1)

                # Alpha channel — strong mispricing only
                if (edge_pct >= MIN_EDGE and conf >= MIN_CONFIDENCE + 10
                        and ev_rank >= 1 and rec in ("yes","no")
                        and edge != "fairly_priced"):
                    alerted_markets.add(mid)
                    alpha_embed = build_polymarket_alpha_embed(market, analysis, edge_pct)
                    send(DISCORD_POLYMARKET_ALPHA, alpha_embed,
                         content="@here 🎯 **STRONG EDGE DETECTED**")
                    print(f"[POLYMARKET] 🎯 ALPHA: {question[:50]} edge={edge_pct:.1f}%")
                    time.sleep(2)

            # Leaderboard — send top 5 profitable wallets
            try:
                lr = requests.get(
                    "https://gamma-api.polymarket.com/leaderboard",
                    params={"limit": 10, "window": "all"},
                    timeout=10
                )
                leaderboard = lr.json() if lr.status_code == 200 else []
                if isinstance(leaderboard, list):
                    wallets = [w for w in leaderboard if isinstance(w, dict)][:5]
                elif isinstance(leaderboard, dict):
                    wallets = leaderboard.get("results", leaderboard.get("data", []))[:5]
                else:
                    wallets = []

                for i, wallet in enumerate(wallets, 1):
                    pnl = float(wallet.get("profit", wallet.get("realizedPnl", 0)) or 0)
                    if pnl < 100:
                        continue
                    embed = build_wallet_embed(wallet, i)
                    send(DISCORD_POLYMARKET_RESEARCH, embed)
                    time.sleep(1)

                if wallets:
                    print(f"[POLYMARKET] 🏆 Sent {len(wallets)} wallet leaderboard entries")

            except Exception as e:
                print(f"[POLYMARKET] Leaderboard error: {e}")

        except Exception as e:
            print(f"[POLYMARKET WORKER] Error: {e}")

        print(f"[POLYMARKET] Scan complete. Next in {POLY_INTERVAL_HR}h")
        time.sleep(POLY_INTERVAL_HR * 3600)


def jobs_worker():
    print("[DISCORD] 💼 Jobs worker started")

    WEB3_JOB_FEEDS = {
        "web3.career":  "https://web3.career/feed.xml",
        "crypto.jobs":  "https://crypto.jobs/jobs.rss",
    }

    while True:
        try:
            import feedparser
            jobs_found = 0

            for source, url in WEB3_JOB_FEEDS.items():
                try:
                    feed = feedparser.parse(url)
                    for entry in feed.entries[:8]:
                        title = getattr(entry, "title", "")
                        link  = getattr(entry, "link", "")
                        summary = getattr(entry, "summary", "")[:300]
                        pub   = getattr(entry, "published", "")

                        h = hashlib.md5((title + link).lower().encode()).hexdigest()
                        if h in alerted_jobs:
                            continue

                        # Categorize
                        t = title.lower()
                        category = (
                            "Engineering"  if any(k in t for k in ["engineer","developer","dev","smart contract","solidity","rust","blockchain developer"]) else
                            "Research"     if any(k in t for k in ["research","analyst","economist"]) else
                            "Security"     if any(k in t for k in ["security","audit","penetration"]) else
                            "Data"         if any(k in t for k in ["data","analytics","scientist"]) else
                            "AI"           if any(k in t for k in ["ai","machine learning","ml","llm"]) else
                            "DevRel"       if any(k in t for k in ["devrel","developer relations","advocate"]) else
                            "Marketing"    if any(k in t for k in ["marketing","growth","content","writer"]) else
                            "Design"       if any(k in t for k in ["design","ui","ux","product"]) else
                            "Community"    if any(k in t for k in ["community","social","moderator","discord"]) else
                            "Operations"
                        )

                        alerted_jobs.add(h)
                        job = {
                            "title":     title,
                            "url":       link,
                            "company":   source,
                            "published": pub,
                            "summary":   summary,
                            "category":  category,
                            "remote":    "remote" in t,
                            "source":    source
                        }
                        embed = build_job_embed(job)
                        send(DISCORD_OPPORTUNITIES, embed)
                        jobs_found += 1
                        time.sleep(1)

                except Exception as e:
                    print(f"[JOBS] {source} error: {e}")

            print(f"[JOBS] Sent {jobs_found} job alerts")

        except Exception as e:
            print(f"[JOBS WORKER] Error: {e}")

        time.sleep(JOBS_INTERVAL_HR * 3600)


# ─────────────────────────────────────────────────────────────
# START ALL WORKERS
# ─────────────────────────────────────────────────────────────
def start_discord_workers():
    channels = sum(1 for c in [
        DISCORD_BREAKING_NEWS, DISCORD_EMERGING_CHAINS,
        DISCORD_OPPORTUNITIES, DISCORD_SECURITY_ALERTS,
        DISCORD_POLYMARKET_RESEARCH, DISCORD_POLYMARKET_ALPHA,
    ] if c)

    if channels == 0:
        print("[DISCORD] ⚠️  No webhooks configured")
        return

    if not ANTHROPIC_API_KEY:
        print("[DISCORD] ⚠️  ANTHROPIC_API_KEY not set — AI features disabled")

    print(f"[DISCORD] ✅ Starting workers ({channels}/6 channels configured)")

    workers = [
        ("NewsWorker",       news_worker),
        ("ChainsWorker",     chains_worker),
        ("OppsWorker",       opportunities_worker),
        ("PolymarketWorker", polymarket_worker),
        ("JobsWorker",       jobs_worker),
    ]

    for name, fn in workers:
        t = threading.Thread(target=fn, name=name, daemon=True)
        t.start()
        print(f"[DISCORD] ✅ {name} started")

    print("[DISCORD] All workers running")

# ─────────────────────────────────────────────────────────────
# WEB3 JOBS WORKER — Module 7
# ─────────────────────────────────────────────────────────────
DISCORD_JOBS = os.getenv("DISCORD_JOBS", "")  # #web3-jobs
JOBS_INTERVAL_HOURS = 6

alerted_jobs: set = set()

def build_job_embed(job: dict) -> dict:
    title    = job.get("title", "Unknown role")[:200]
    company  = job.get("company", "Unknown")
    category = job.get("category", "Other")
    remote   = "🌍 Remote" if job.get("remote") else "📍 On-site/Hybrid"
    url      = job.get("url", "")
    summary  = job.get("summary", "")[:300]

    return {
        "title":       f"💼 {title}",
        "url":         url,
        "color":       0x5865F2,
        "description": summary,
        "fields": [
            {"name": "🏢 Source",   "value": company,  "inline": True},
            {"name": "🏷️ Category", "value": category, "inline": True},
            {"name": "📌 Location", "value": remote,    "inline": True},
        ],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Crypto Intel • Web3 Jobs"}
    }

def jobs_worker():
    """Runs every 6 hours. Aggregates Web3 job listings, sends new ones to Discord."""
    import sys, os, hashlib
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from research_engine import fetch_web3_jobs

    print("[DISCORD] 💼 Jobs worker started")

    while True:
        try:
            jobs = fetch_web3_jobs(max_per_feed=15)
            print(f"[JOBS] Found {len(jobs)} listings")

            for job in jobs:
                title = job.get("title", "")
                h = hashlib.md5((title + job.get("company","")).lower().encode()).hexdigest()
                if h in alerted_jobs:
                    continue
                alerted_jobs.add(h)

                embed = build_job_embed(job)
                send(DISCORD_JOBS, embed)
                print(f"[JOBS] Sent: {title[:50]}")
                time.sleep(1)

        except Exception as e:
            print(f"[JOBS WORKER] Error: {e}")

        time.sleep(JOBS_INTERVAL_HOURS * 3600)

# ─────────────────────────────────────────────────────────────
# WALLET LEADERBOARD WORKER — Module 6
# ─────────────────────────────────────────────────────────────
DISCORD_WALLET_LEADERBOARD = os.getenv("DISCORD_WALLET_LEADERBOARD", "")  # #wallet-leaderboard
LEADERBOARD_INTERVAL_HOURS = 12

def build_leaderboard_embed(wallets: list) -> dict:
    top = wallets[:15]
    lines = []
    for i, w in enumerate(top, 1):
        addr    = w.get("address", w.get("wallet", "Unknown"))
        addr_short = f"{addr[:6]}...{addr[-4:]}" if len(addr) > 12 else addr
        pnl     = w.get("profit", w.get("pnl", w.get("realizedPnl", 0)))
        roi     = w.get("roi", 0)
        try:
            pnl = float(pnl)
            roi = float(roi)
        except Exception:
            pnl, roi = 0, 0
        lines.append(f"`{i:2}.` **{addr_short}** — PnL: `${pnl:,.0f}`  ROI: `{roi:.1f}%`")

    return {
        "title":       "🏆 Polymarket Profitable Wallet Leaderboard",
        "color":       0xFFD700,
        "description": "\n".join(lines) if lines else "No leaderboard data available",
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "footer":      {"text": "Crypto Intel • Ranked by long-term consistency"}
    }

def wallet_leaderboard_worker():
    """Runs every 12 hours. Posts top profitable Polymarket wallets."""
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from research_engine import get_polymarket_leaderboard

    print("[DISCORD] 🏆 Wallet leaderboard worker started")

    while True:
        try:
            wallets = get_polymarket_leaderboard()
            if wallets:
                embed = build_leaderboard_embed(wallets)
                send(DISCORD_WALLET_LEADERBOARD, embed)
                print(f"[LEADERBOARD] Sent top {min(len(wallets),15)} wallets")
            else:
                print("[LEADERBOARD] No wallet data returned from Polymarket API")

        except Exception as e:
            print(f"[LEADERBOARD WORKER] Error: {e}")

        time.sleep(LEADERBOARD_INTERVAL_HOURS * 3600)
