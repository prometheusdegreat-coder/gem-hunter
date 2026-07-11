"""
Crypto Intelligence Platform — FastAPI Backend v2
All modules as REST endpoints + Discord alert workers on startup.
"""

import os
import sys
import json
import time
import threading
import requests
import feedparser
import hashlib
from datetime import datetime, timezone, timedelta
from typing import Optional
from dataclasses import dataclass, field, asdict

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from contextlib import asynccontextmanager

# ── Config ────────────────────────────────────────────────────
ANTHROPIC_API_KEY   = os.getenv("ANTHROPIC_API_KEY", "")
COINGECKO_BASE      = "https://api.coingecko.com/api/v3"
CRYPTOPANIC_KEY     = os.getenv("CRYPTOPANIC_API_KEY", "")
GITHUB_TOKEN        = os.getenv("GITHUB_TOKEN", "")
BSCSCAN_API_KEY     = os.getenv("BSCSCAN_API_KEY", "")

# Discord webhooks
DISCORD_BREAKING_NEWS       = os.getenv("DISCORD_BREAKING_NEWS", "")
DISCORD_EMERGING_CHAINS     = os.getenv("DISCORD_EMERGING_CHAINS", "")
DISCORD_OPPORTUNITIES       = os.getenv("DISCORD_OPPORTUNITIES", "")
DISCORD_SECURITY_ALERTS     = os.getenv("DISCORD_SECURITY_ALERTS", "")
DISCORD_POLYMARKET_RESEARCH = os.getenv("DISCORD_POLYMARKET_RESEARCH", "")
DISCORD_POLYMARKET_ALPHA    = os.getenv("DISCORD_POLYMARKET_ALPHA", "")
DISCORD_JOBS                = os.getenv("DISCORD_JOBS", "")

# ── In-memory cache ───────────────────────────────────────────
cache: dict = {}
alerted_news:    set = set()
alerted_chains:  set = set()
alerted_opps:    set = set()
alerted_markets: set = set()
alerted_jobs:    set = set()

def get_cache(key):
    e = cache.get(key)
    if e and time.time() - e["ts"] < e.get("ttl", 300):
        return e["data"]
    return None

def set_cache(key, data, ttl=300):
    cache[key] = {"data": data, "ts": time.time(), "ttl": ttl}

# ── Claude AI ─────────────────────────────────────────────────
SYSTEM = """You are an institutional-grade crypto research analyst.
RULES:
1. Never fabricate data
2. Separate fact from inference from speculation — label each
3. Show bull AND bear cases
4. Flag FOMO/hype explicitly
5. Security risk is first-class
6. Return ONLY valid JSON — no markdown, no preamble
"""

def claude(prompt: str, max_tokens: int = 2000) -> dict:
    if not ANTHROPIC_API_KEY:
        return {"error": "ANTHROPIC_API_KEY not set"}
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": max_tokens,
                "system": SYSTEM,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=60
        )
        if r.status_code != 200:
            return {"error": f"API {r.status_code}"}
        text = r.json()["content"][0]["text"]
        # Strip markdown fences
        text = text.strip()
        if "```" in text:
            parts = text.split("```")
            for p in parts:
                p2 = p.strip()
                if p2.startswith("json"):
                    p2 = p2[4:].strip()
                try:
                    return json.loads(p2)
                except Exception:
                    continue
        return json.loads(text)
    except Exception as e:
        return {"error": str(e)}

# ── NEWS ──────────────────────────────────────────────────────
NEWS_FEEDS = {
    "CoinDesk":   "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "The Block":  "https://www.theblock.co/rss.xml",
    "Decrypt":    "https://decrypt.co/feed",
    "Blockworks": "https://blockworks.co/feed",
    "DLNews":     "https://dlnews.com/arc/outboundfeeds/rss/",
}

def fetch_news(max_per=5) -> list:
    items, seen = [], set()
    for src, url in NEWS_FEEDS.items():
        try:
            feed = feedparser.parse(url)
            n = 0
            for e in feed.entries:
                if n >= max_per: break
                title = getattr(e, "title", "")
                h = hashlib.md5(title.lower().encode()).hexdigest()
                if h in seen: continue
                seen.add(h)
                items.append({
                    "title":     title,
                    "summary":   getattr(e, "summary", "")[:300],
                    "url":       getattr(e, "link", ""),
                    "source":    src,
                    "published": getattr(e, "published", ""),
                    "hash":      h
                })
                n += 1
        except Exception:
            pass
    return items

# ── GITHUB ────────────────────────────────────────────────────
def github_search(query: str, max_results: int = 10) -> list:
    headers = {"Accept": "application/vnd.github.v3+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"
    try:
        r = requests.get(
            "https://api.github.com/search/repositories",
            params={"q": query, "sort": "updated", "per_page": max_results},
            headers=headers, timeout=10
        )
        return r.json().get("items", []) if r.status_code == 200 else []
    except Exception:
        return []

def github_scan(repo: str) -> dict:
    headers = {}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"
    result = {"stars": 0, "commits_30d": 0, "contributors": 0,
              "language": "", "description": "", "last_commit": ""}
    try:
        r = requests.get(f"https://api.github.com/repos/{repo}",
                         headers=headers, timeout=8)
        if r.status_code == 200:
            d = r.json()
            result["stars"]       = d.get("stargazers_count", 0)
            result["language"]    = d.get("language", "")
            result["description"] = d.get("description", "")

        since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        r2 = requests.get(f"https://api.github.com/repos/{repo}/commits",
                          params={"since": since, "per_page": 100},
                          headers=headers, timeout=8)
        if r2.status_code == 200:
            commits = r2.json()
            result["commits_30d"] = len(commits) if isinstance(commits, list) else 0
            if isinstance(commits, list) and commits:
                result["last_commit"] = commits[0].get("commit",{}).get("author",{}).get("date","")

        r3 = requests.get(f"https://api.github.com/repos/{repo}/contributors",
                          params={"per_page": 100}, headers=headers, timeout=8)
        if r3.status_code == 200:
            c = r3.json()
            result["contributors"] = len(c) if isinstance(c, list) else 0
    except Exception:
        pass
    return result

# ── COINGECKO ─────────────────────────────────────────────────
def get_global() -> dict:
    try:
        r = requests.get(f"{COINGECKO_BASE}/global", timeout=10)
        return r.json().get("data", {}) if r.status_code == 200 else {}
    except Exception:
        return {}

def get_trending() -> list:
    try:
        r = requests.get(f"{COINGECKO_BASE}/search/trending", timeout=10)
        return r.json().get("coins", []) if r.status_code == 200 else []
    except Exception:
        return []

# ── POLYMARKET ────────────────────────────────────────────────
def get_polymarkets(limit: int = 20) -> list:
    """Fetch active Polymarket markets — handles multiple API response formats."""
    try:
        r = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"limit": limit, "active": "true", "closed": "false"},
            timeout=10
        )
        if r.status_code != 200:
            return []
        data = r.json()
        # Handle both list and dict responses
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("markets", data.get("data", data.get("results", [])))
        return []
    except Exception as e:
        print(f"[POLYMARKET] Fetch error: {e}")
        return []

def get_poly_leaderboard() -> list:
    """Fetch Polymarket profitable wallet leaderboard."""
    try:
        # Try multiple endpoints
        endpoints = [
            "https://gamma-api.polymarket.com/leaderboard",
            "https://data-api.polymarket.com/profits?limit=50&window=all",
        ]
        for url in endpoints:
            try:
                r = requests.get(url, timeout=10)
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, list) and len(data) > 0:
                        return data
                    if isinstance(data, dict):
                        result = data.get("data", data.get("leaderboard", data.get("results", [])))
                        if result:
                            return result
            except Exception:
                continue
        return []
    except Exception as e:
        print(f"[LEADERBOARD] Error: {e}")
        return []

# ── WEB3 JOBS ─────────────────────────────────────────────────
WEB3_JOB_FEEDS = {
    "web3.career":  "https://web3.career/feed.xml",
    "crypto.jobs":  "https://crypto.jobs/jobs.rss",
}

def fetch_jobs(max_per=15) -> list:
    jobs, seen = [], set()
    for src, url in WEB3_JOB_FEEDS.items():
        try:
            feed = feedparser.parse(url)
            n = 0
            for e in feed.entries:
                if n >= max_per: break
                title = getattr(e, "title", "")
                h = hashlib.md5(title.lower().encode()).hexdigest()
                if h in seen: continue
                seen.add(h)
                t = title.lower()
                category = (
                    "Engineering"  if any(k in t for k in ["engineer","developer","solidity","rust","smart contract"]) else
                    "Research"     if any(k in t for k in ["research","analyst"]) else
                    "Security"     if any(k in t for k in ["security","audit"]) else
                    "AI"           if any(k in t for k in ["ai","machine learning","llm"]) else
                    "DevRel"       if any(k in t for k in ["devrel","developer relations"]) else
                    "Marketing"    if any(k in t for k in ["marketing","growth","content"]) else
                    "Design"       if any(k in t for k in ["design","ui","ux"]) else
                    "Data"         if any(k in t for k in ["data","analytics"]) else
                    "Operations"
                )
                jobs.append({
                    "title":     title,
                    "url":       getattr(e, "link", ""),
                    "company":   src,
                    "published": getattr(e, "published", ""),
                    "summary":   getattr(e, "summary", "")[:200],
                    "category":  category,
                    "remote":    "remote" in t,
                    "source":    src,
                    "hash":      h
                })
                n += 1
        except Exception as e:
            print(f"[JOBS] {src}: {e}")
    return jobs

# ── OPPORTUNITY SCORER ────────────────────────────────────────
def score_opportunity(data: dict) -> dict:
    def clamp(v): return max(0, min(100, v))
    commits = data.get("github_commits_30d", 0)
    contrib = data.get("github_contributors", 0)
    dev     = clamp(commits * 2 + contrib * 5)
    vol     = data.get("volume_24h", 0)
    liq     = clamp(min(vol / 10000, 100))
    inv_map = {"tier1": 100, "tier2": 70, "tier3": 40, "unknown": 20, "none": 0}
    investor = inv_map.get(data.get("investor_tier", "unknown"), 20)
    social   = clamp(data.get("news_mentions_7d", 0) * 5 + data.get("sentiment_score", 0.5) * 50)
    audit    = 40 if data.get("audited") else 0
    testnet  = 30 if data.get("testnet_live") else 0
    mainnet  = 30 if data.get("mainnet_live") else 0
    tech     = clamp(audit + testnet + mainnet)
    age      = data.get("age_days", 365)
    timing   = clamp(100 - (age / 365) * 50) if age < 730 else 20
    novelty  = clamp(100 - data.get("mainstream_score", 50))
    cats     = clamp(data.get("catalyst_count", 0) * 20)
    macro    = data.get("macro_alignment_score", 50)
    risk_f   = data.get("risk_flag_count", 0)
    risk     = clamp(risk_f * 15 + (0 if data.get("audited") else 30))
    total = (
        0.08 * novelty + 0.12 * (40 if data.get("has_documentation") else 0 +
                                  30 if data.get("open_source") else 0) +
        0.08 * liq + 0.10 * timing + 0.10 * investor +
        0.12 * dev + 0.08 * social + 0.10 * cats +
        0.12 * tech + 0.06 * macro + 0.04 * (100 - risk)
    )
    horizon = ("immediate" if timing >= 80 and cats >= 60 else
               "short"     if timing >= 60 else
               "medium"    if timing >= 40 else "long")
    return {
        "total": round(clamp(total), 1),
        "risk_score": round(risk, 1),
        "confidence_score": round((40 if data.get("has_documentation") else 0) * 0.6 + dev * 0.4, 1),
        "time_horizon": horizon,
        "dev_activity": round(dev, 1),
        "tech_maturity": round(tech, 1),
        "timing": round(timing, 1),
    }

EXPLOIT_PATTERNS = [
    "unaudited","no audit","rug","exploit","hack","bridge vulnerability",
    "flash loan","reentrancy","private key","centralized admin",
    "proxy upgrade","infinite mint","oracle manipulation"
]

def quick_security_scan(text: str) -> list:
    t = text.lower()
    return [p for p in EXPLOIT_PATTERNS if p in t]

# ── DISCORD SENDER ────────────────────────────────────────────
def send_discord(webhook_url: str, embed: dict, content: str = ""):
    if not webhook_url:
        return
    try:
        payload = {"embeds": [embed]}
        if content:
            payload["content"] = content
        r = requests.post(webhook_url, json=payload, timeout=10)
        if r.status_code == 429:
            time.sleep(r.json().get("retry_after", 5))
            requests.post(webhook_url, json=payload, timeout=10)
        elif r.status_code not in (200, 204):
            print(f"[DISCORD] Error {r.status_code}")
    except Exception as e:
        print(f"[DISCORD] {e}")

def bar(score: float, length: int = 10) -> str:
    filled = int((score / 100) * length)
    return "█" * filled + "░" * (length - filled)

# ── EMBED BUILDERS ────────────────────────────────────────────
def embed_news(item: dict, impact: str, fomo: bool) -> dict:
    color = 0xFF0000 if impact == "high" else 0xFF8C00 if impact == "medium" else 0x4CAF50
    fields = [
        {"name": "📰 Source",  "value": item.get("source","?"),  "inline": True},
        {"name": "📊 Impact",  "value": impact.upper(),           "inline": True},
        {"name": "📋 Summary", "value": item.get("summary","")[:300], "inline": False},
        {"name": "🔗 Read",    "value": f"[Full Article]({item.get('url','')})", "inline": False},
    ]
    if fomo:
        fields.append({"name": "🚨 FOMO/HYPE WARNING",
                        "value": "Sentiment-driven language detected. Verify before acting.",
                        "inline": False})
    return {
        "title":     f"{'🔴' if impact=='high' else '🟡'} {item.get('title','')[:200]}",
        "url":       item.get("url",""),
        "color":     color,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": f"Crypto Intel • {item.get('source','')}"}
    }

def embed_chain(chain: dict, analysis: dict, opp: dict, sec_flags: list) -> dict:
    name    = chain.get("name","?")
    desc    = chain.get("description","")[:250]
    url     = chain.get("url","")
    stars   = chain.get("stars", 0)
    commits = chain.get("commits_30d", 0)
    contrib = chain.get("contributors", 0)
    lang    = chain.get("language","?")
    full    = chain.get("full_name","")

    opp_score = float(analysis.get("opportunity_score", opp.get("total", 0)))
    conf      = float(analysis.get("confidence_score",  opp.get("confidence_score", 0)))
    rec       = analysis.get("recommendation", "monitor")
    thesis    = analysis.get("investment_thesis","")[:250]
    sec       = analysis.get("security_assessment", {})
    sec_score = sec.get("score", max(0, 60 - len(sec_flags)*15)) if isinstance(sec, dict) else 50
    red_flags = analysis.get("red_flags", [])[:3]
    green_flags = analysis.get("green_flags", [])[:3]

    color = (0x00E676 if opp_score >= 65 else
             0xFFD600 if opp_score >= 45 else
             0xFF6D00 if opp_score >= 30 else 0x9E9E9E)

    rec_emoji = {"monitor":"👁️","research":"🔬","avoid":"🚫"}.get(rec,"📋")

    # Investigation links
    inv_links = []
    if url:
        inv_links.append(f"[🐙 GitHub]({url})")
    if full:
        inv_links.append(f"[📊 Commits](https://github.com/{full}/commits)")
        inv_links.append(f"[👥 Contributors](https://github.com/{full}/graphs/contributors)")
    inv_links.append(f"[🔍 Search X](https://twitter.com/search?q={name})")

    fields = [
        {"name": "💡 Investment Thesis",
         "value": thesis or "Insufficient data",
         "inline": False},
        {"name": "📊 Opportunity",
         "value": f"`{opp_score:.0f}/100` {bar(opp_score)}",
         "inline": True},
        {"name": "🔐 Security",
         "value": f"`{sec_score}/100` {bar(sec_score)}",
         "inline": True},
        {"name": "✅ Confidence",
         "value": f"`{conf:.0f}/100`",
         "inline": True},
        {"name": "⚙️ Dev Activity",
         "value": (f"⭐ Stars: `{stars}`\n"
                   f"📝 Commits 30d: `{commits}`\n"
                   f"👥 Contributors: `{contrib}`\n"
                   f"💻 Language: `{lang}`"),
         "inline": True},
        {"name": "🎯 Action",
         "value": f"{rec_emoji} **{rec.upper()}**",
         "inline": True},
    ]

    if green_flags:
        fields.append({"name": "🟢 Green Flags",
                        "value": "\n".join(f"• {g}" for g in green_flags),
                        "inline": True})
    if red_flags:
        fields.append({"name": "🔴 Red Flags",
                        "value": "\n".join(f"• {r}" for r in red_flags),
                        "inline": True})
    if sec_flags:
        fields.append({"name": "🚨 Security Flags",
                        "value": "\n".join(f"• {f}" for f in sec_flags[:3]),
                        "inline": False})

    fields.append({"name": "🔍 Investigate",
                    "value": "  ".join(inv_links),
                    "inline": False})

    return {
        "title":       f"⛓️ EMERGING CHAIN: {name}  |  {rec_emoji} {rec.upper()}",
        "url":         url,
        "color":       color,
        "description": desc,
        "fields":      fields,
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "footer":      {"text": "Crypto Intel • Emerging Chains Scanner"}
    }

def embed_opportunity(report: dict) -> dict:
    opp   = float(report.get("opportunity_score", 0))
    risk  = float(report.get("risk_score", 50))
    conf  = float(report.get("confidence_score", 0))
    color = (0x00E676 if opp >= 70 and risk <= 35 else
             0x64DD17 if opp >= 55 else
             0xFFD600 if opp >= 40 else 0xFF6D00)
    risk_label = ("🟢 LOW" if risk<=30 else "🟡 MEDIUM" if risk<=55 else "🟠 HIGH" if risk<=72 else "🔴 DANGER")
    fields = [
        {"name": "📊 Scores",
         "value": (f"Opportunity: `{opp:.0f}/100` {bar(opp)}\n"
                   f"Risk: `{risk:.0f}/100` {bar(risk)}\n"
                   f"Confidence: `{conf:.0f}/100` | {risk_label}\n"
                   f"Horizon: `{report.get('time_horizon','?')}`"),
         "inline": False},
    ]
    for b in report.get("bull_case",[])[:2]:
        fields.append({"name":"🐂 Bull","value":b,"inline":True})
    for b in report.get("bear_case",[])[:2]:
        fields.append({"name":"🐻 Bear","value":b,"inline":True})
    for c in report.get("catalysts",[])[:2]:
        fields.append({"name":"⚡ Catalyst","value":c,"inline":True})
    for r in report.get("risks",[])[:2]:
        fields.append({"name":"⚠️ Risk","value":r,"inline":True})
    for f in report.get("fomo_flags",[])[:1]:
        fields.append({"name":"🚨 FOMO Warning","value":f,"inline":False})
    return {
        "title":       f"🎯 OPPORTUNITY: {report.get('query','')[:150]}",
        "color":       color,
        "description": report.get("summary","")[:400],
        "fields":      fields,
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "footer":      {"text": "Crypto Intel • AI Research • Fact/Inference labeled"}
    }

def embed_poly_research(market: dict, analysis: dict) -> dict:
    q         = str(market.get("question", market.get("title","?")))[:200]
    prob_est  = float(analysis.get("probability_estimate", 0.5))
    edge      = analysis.get("edge","fairly_priced")
    conf      = float(analysis.get("confidence_score", 0))
    ev        = analysis.get("evidence_quality","unknown")
    volume    = float(market.get("volume", market.get("volumeNum", 0)) or 0)
    liquidity = float(market.get("liquidity", market.get("liquidityNum", 0)) or 0)
    end_date  = str(market.get("endDate", market.get("end_date","?")))[:10]
    poly_url  = f"https://polymarket.com/market/{market.get('conditionId', market.get('id',''))}"
    bull      = analysis.get("bull_case",[])[:2]
    bear      = analysis.get("bear_case",[])[:2]
    reasoning = analysis.get("reasoning_chain",[])[:3]

    edge_color = (0x00E676 if edge=="underpriced" else
                  0xFF6D00 if edge=="overpriced"  else 0x9E9E9E)
    edge_label = ("🟢 UNDERPRICED — Value Bet" if edge=="underpriced" else
                  "🔴 OVERPRICED — Fade It"    if edge=="overpriced"  else "⚪ FAIRLY PRICED")

    fields = [
        {"name": "📊 AI Probability",
         "value": (f"`{prob_est*100:.1f}%` estimated\n"
                   f"Edge: {edge_label}\n"
                   f"Evidence: `{ev.upper()}`  Confidence: `{conf:.0f}/100`"),
         "inline": False},
        {"name": "💵 Market",
         "value": f"Volume: `${volume:,.0f}`\nLiquidity: `${liquidity:,.0f}`\nCloses: `{end_date}`",
         "inline": True},
    ]
    if bull:
        fields.append({"name":"🐂 Bull (YES)","value":"\n".join(f"• {b}" for b in bull),"inline":True})
    if bear:
        fields.append({"name":"🐻 Bear (NO)","value":"\n".join(f"• {b}" for b in bear),"inline":True})
    if reasoning:
        fields.append({"name":"🔗 Reasoning",
                        "value":"\n".join(f"{i+1}. {s}" for i,s in enumerate(reasoning)),
                        "inline":False})
    fields.append({"name":"🔗 Trade","value":f"[Open on Polymarket]({poly_url})","inline":False})
    fields.append({"name":"⚠️ Disclaimer",
                    "value":"AI estimate only. Not financial advice. Verify independently.",
                    "inline":False})
    return {
        "title":     f"🔮 POLY RESEARCH: {q}",
        "url":       poly_url,
        "color":     edge_color,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Crypto Intel • Polymarket Research • Evidence-Based"}
    }

def embed_poly_alpha(market: dict, analysis: dict, edge_pct: float) -> dict:
    q         = str(market.get("question", market.get("title","?")))[:200]
    prob_est  = float(analysis.get("probability_estimate", 0.5))
    edge      = analysis.get("edge","fairly_priced")
    conf      = float(analysis.get("confidence_score", 0))
    rec       = analysis.get("recommendation","pass").upper()
    volume    = float(market.get("volume", market.get("volumeNum", 0)) or 0)
    poly_url  = f"https://polymarket.com/market/{market.get('conditionId', market.get('id',''))}"

    # Get market price
    market_price = 0.5
    try:
        op = market.get("outcomePrices","")
        if op:
            prices = json.loads(op) if isinstance(op, str) else op
            if isinstance(prices, list) and prices:
                market_price = float(prices[0])
    except Exception:
        pass

    color = 0x00E676 if edge=="underpriced" else 0xFF0000
    action = (f"**BUY YES** — AI: `{prob_est*100:.1f}%` vs Market: `{market_price*100:.1f}%`\nEdge: `+{edge_pct:.1f}%`"
              if edge=="underpriced" else
              f"**BUY NO / FADE** — AI: `{prob_est*100:.1f}%` vs Market: `{market_price*100:.1f}%`\nEdge: `+{edge_pct:.1f}%`")

    fields = [
        {"name": "⚡ ACTION",    "value": action,                                "inline": False},
        {"name": "📊 Stats",
         "value": (f"AI Prob: `{prob_est*100:.1f}%`\n"
                   f"Market:  `{market_price*100:.1f}%`\n"
                   f"Edge:    `{edge_pct:.1f}%`\n"
                   f"Conf:    `{conf:.0f}/100`\n"
                   f"Volume:  `${volume:,.0f}`"),
         "inline": True},
        {"name": "🔗 Trade",     "value": f"[Open on Polymarket]({poly_url})",   "inline": False},
        {"name": "⚠️ Disclaimer","value": "AI estimate. NOT financial advice. Total loss risk on prediction markets.", "inline": False},
    ]
    return {
        "title":       f"🎯 POLY ALPHA: {q}",
        "url":         poly_url,
        "color":       color,
        "description": f"**Recommendation: {rec}**  |  Confidence: `{conf:.0f}/100`",
        "fields":      fields,
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "footer":      {"text": "Crypto Intel • Polymarket Alpha • AI Edge Detection"}
    }

def embed_leaderboard(wallets: list) -> dict:
    if not wallets:
        return {}
    rows = []
    for i, w in enumerate(wallets[:10], 1):
        addr    = str(w.get("proxyWalletAddress", w.get("address", w.get("user","?"))))
        profit  = float(w.get("profit", w.get("pnl", w.get("profitAndLoss", 0))) or 0)
        roi     = float(w.get("roi", w.get("returnOnInvestment", 0)) or 0)
        markets = int(w.get("marketsTraded", w.get("numTrades", 0)) or 0)
        short_addr = f"`{addr[:6]}...{addr[-4:]}`" if len(addr) > 10 else f"`{addr}`"
        poly_url = f"https://polymarket.com/profile/{addr}"
        rows.append(f"{i}. [{short_addr}]({poly_url}) | 💰 `${profit:,.0f}` | ROI: `{roi:.1f}%` | 🏦 `{markets}` markets")

    return {
        "title":       "🏆 POLYMARKET WALLET LEADERBOARD",
        "color":       0x00E676,
        "description": "Top performing wallets by realized profit (3+ months history)",
        "fields": [
            {"name": "Rank | Wallet | Profit | ROI | Markets",
             "value": "\n".join(rows) if rows else "No data available",
             "inline": False},
            {"name": "⚠️ Note",
             "value": "Past performance ≠ future results. Research wallets before copying trades.",
             "inline": False}
        ],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Crypto Intel • Polymarket Leaderboard"}
    }

def embed_job(job: dict) -> dict:
    color_map = {
        "Engineering": 0x2196F3, "Security": 0xFF5722,
        "AI": 0x9C27B0, "Research": 0x4CAF50,
        "DevRel": 0xFF9800, "Design": 0xE91E63,
        "Data": 0x00BCD4
    }
    cat   = job.get("category","Operations")
    color = color_map.get(cat, 0x607D8B)
    return {
        "title":       f"💼 {job.get('title','?')[:150]}",
        "url":         job.get("url",""),
        "color":       color,
        "description": job.get("summary","")[:300],
        "fields": [
            {"name": "🏷️ Category", "value": cat,                              "inline": True},
            {"name": "🌍 Remote",   "value": "✅ Yes" if job.get("remote") else "❌ No", "inline": True},
            {"name": "📰 Source",   "value": job.get("source","?"),            "inline": True},
            {"name": "🔗 Apply",    "value": f"[View Job]({job.get('url','')})", "inline": False},
        ],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": f"Crypto Intel • Web3 Jobs • {job.get('source','')}"}
    }

# ── BACKGROUND WORKERS ────────────────────────────────────────
def news_worker():
    print("[DISCORD] 📰 News worker started")
    while True:
        try:
            items = fetch_news(max_per=5)
            if not items:
                time.sleep(900); continue

            # Batch classify with Claude
            news_text = "\n".join(f"[{n['source']}] {n['title']}" for n in items[:20])
            result = claude(f"""Classify these crypto news items. Return JSON:
{{
  "high_impact": ["exact title strings that are high market-moving"],
  "fomo_titles": ["titles with hype/FOMO language"],
  "market_sentiment": "bullish|bearish|neutral"
}}
News:\n{news_text}""", max_tokens=800)

            high_impact  = result.get("high_impact", []) if isinstance(result, dict) else []
            fomo_titles  = set(result.get("fomo_titles", []) if isinstance(result, dict) else [])

            for item in items:
                h = item.get("hash","")
                if h in alerted_news: continue
                title = item.get("title","")
                is_high = any(title[:50] in hi for hi in high_impact)
                if not is_high: continue
                alerted_news.add(h)
                fomo = title in fomo_titles
                send_discord(DISCORD_BREAKING_NEWS, embed_news(item, "high", fomo))
                print(f"[NEWS] Sent: {title[:60]}")
                time.sleep(1)
        except Exception as e:
            print(f"[NEWS WORKER] Error: {e}")
        time.sleep(900)  # 15 min

def chains_worker():
    print("[DISCORD] ⛓️ Chains worker started")
    queries = [
        "blockchain layer1 testnet 2025",
        "new blockchain consensus protocol 2025",
        "zk rollup layer2 launch 2025",
        "blockchain stealth mainnet launch",
    ]
    while True:
        try:
            for query in queries:
                repos = github_search(query, max_results=8)
                for repo in repos:
                    name  = repo.get("name","")
                    full  = repo.get("full_name","")
                    stars = repo.get("stargazers_count", 0)
                    if stars > 5000: continue
                    key = full.lower()
                    if key in alerted_chains: continue

                    github = github_scan(full)
                    chain_data = {
                        "name":        name,
                        "full_name":   full,
                        "description": repo.get("description",""),
                        "stars":       stars,
                        "commits_30d": github.get("commits_30d", 0),
                        "contributors":github.get("contributors", 0),
                        "language":    github.get("language",""),
                        "url":         repo.get("html_url",""),
                        "topics":      repo.get("topics",[]),
                        "testnet_live":any(t in repo.get("topics",[]) for t in ["testnet","mainnet","devnet"])
                    }

                    opp = score_opportunity({
                        "github_commits_30d":  chain_data["commits_30d"],
                        "github_contributors": chain_data["contributors"],
                        "audited":             False,
                        "testnet_live":        chain_data["testnet_live"],
                        "has_documentation":   bool(chain_data["description"]),
                        "open_source":         True,
                        "mainstream_score":    min(stars / 50, 100),
                        "risk_flag_count":     len(quick_security_scan(chain_data["description"])),
                        "age_days":            90,
                    })

                    if opp["total"] < 40: continue

                    analysis = claude(f"""Analyze this emerging blockchain. Return JSON:
{{
  "investment_thesis": "2-3 sentence thesis",
  "security_assessment": {{"score": 0-100, "flags": []}},
  "recommendation": "monitor|research|avoid",
  "confidence_score": 0-100,
  "opportunity_score": 0-100,
  "red_flags": [],
  "green_flags": []
}}
Chain: {json.dumps(chain_data, default=str)}""", max_tokens=1000)

                    if not isinstance(analysis, dict):
                        analysis = {}
                    analysis["opportunity_score"] = opp["total"]
                    analysis["confidence_score"]  = opp["confidence_score"]

                    sec_flags = quick_security_scan(chain_data["description"] + " " + name)
                    alerted_chains.add(key)

                    embed = embed_chain(chain_data, analysis, opp, sec_flags)
                    send_discord(DISCORD_EMERGING_CHAINS, embed)
                    print(f"[CHAINS] Sent: {name} (score={opp['total']})")

                    if sec_flags and isinstance(analysis.get("security_assessment"), dict):
                        if analysis["security_assessment"].get("score", 100) < 40:
                            sec_embed = {
                                "title": f"🚨 SECURITY RISK: {name}",
                                "color": 0xFF0000,
                                "description": f"Chain `{name}` has security flags detected.",
                                "fields": [{"name":"⚠️ Flags","value":"\n".join(f"• {f}" for f in sec_flags),"inline":False}],
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                                "footer": {"text":"Crypto Intel • Security Monitor"}
                            }
                            send_discord(DISCORD_SECURITY_ALERTS, sec_embed)
                    time.sleep(3)
        except Exception as e:
            print(f"[CHAINS WORKER] Error: {e}")
        time.sleep(7200)  # 2 hours

def opportunities_worker():
    print("[DISCORD] 🎯 Opportunities worker started")
    TOPICS = [
        "Most promising DeFi protocol with strong fundamentals right now",
        "Emerging RWA tokenization opportunity with institutional backing",
        "Undervalued blockchain ecosystem with active developer community",
        "High conviction crypto opportunity based on upcoming catalysts",
        "AI and blockchain convergence investment opportunities",
        "Cross-chain interoperability protocol opportunities",
        "Institutional crypto adoption catalysts and investment thesis",
        "Emerging Web3 gaming or NFT infrastructure opportunity",
    ]
    idx = 0
    while True:
        try:
            # Get market context
            gm      = get_global()
            trending = get_trending()
            context = []
            if gm:
                context.append(f"Market: {gm.get('market_cap_change_percentage_24h_usd',0):.1f}% 24h | BTC dom: {gm.get('btc_dominance',0):.1f}%")
            if trending:
                context.append("Trending: " + ", ".join(c["item"]["name"] for c in trending[:5]))

            topic = TOPICS[idx % len(TOPICS)]
            idx += 1
            print(f"[OPPS] Researching: {topic}")

            ctx_str = "\n".join(context)
            result = claude(f"""Research this crypto opportunity. Context: {ctx_str}
Query: {topic}
Return JSON:
{{
  "summary": "2-3 sentence summary",
  "opportunity_score": 0-100,
  "risk_score": 0-100,
  "confidence_score": 0-100,
  "time_horizon": "immediate|short|medium|long",
  "bull_case": ["point 1","point 2"],
  "bear_case": ["point 1","point 2"],
  "catalysts": ["catalyst 1","catalyst 2"],
  "risks": ["risk 1","risk 2"],
  "fomo_flags": ["any hype signals detected"],
  "facts": ["confirmed fact 1"],
  "inferences": ["[INFERENCE] logical deduction"],
  "query": "{topic}"
}}""", max_tokens=1500)

            if not isinstance(result, dict) or result.get("error"):
                print(f"[OPPS] Claude error: {result}")
                time.sleep(3600); continue

            result["query"] = topic
            opp_score = float(result.get("opportunity_score", 0))
            if opp_score < 45:
                print(f"[OPPS] Score too low ({opp_score}) — skipping")
                time.sleep(3600); continue

            h = hashlib.md5(topic.lower().encode()).hexdigest()
            if h in alerted_opps:
                time.sleep(3600); continue
            alerted_opps.add(h)

            send_discord(DISCORD_OPPORTUNITIES, embed_opportunity(result))
            print(f"[OPPS] Sent: {topic[:50]} score={opp_score}")

        except Exception as e:
            print(f"[OPPS WORKER] Error: {e}")
        time.sleep(3600)  # 1 hour

def polymarket_worker():
    print("[DISCORD] 🔮 Polymarket worker started")
    leaderboard_sent = False
    while True:
        try:
            # Send leaderboard once per run
            if not leaderboard_sent:
                lb = get_poly_leaderboard()
                if lb:
                    lb_embed = embed_leaderboard(lb)
                    if lb_embed:
                        send_discord(DISCORD_POLYMARKET_RESEARCH, lb_embed)
                        print(f"[POLYMARKET] Sent leaderboard ({len(lb)} wallets)")
                        leaderboard_sent = True
                else:
                    print("[POLYMARKET] No leaderboard data — will retry next scan")

            print("[POLYMARKET] Scanning markets...")
            markets = get_polymarkets(limit=50)
            if not markets:
                print("[POLYMARKET] No markets returned")
                time.sleep(10800); continue

            # Filter to crypto-relevant markets
            crypto_kw = ["bitcoin","btc","ethereum","eth","crypto","sec","etf",
                         "fed","blockchain","defi","stablecoin","regulation",
                         "solana","sol","xrp","coinbase","trump","election"]
            relevant = []
            for m in markets:
                if not isinstance(m, dict): continue
                q   = str(m.get("question", m.get("title",""))).lower()
                vol = float(m.get("volume", m.get("volumeNum", 0)) or 0)
                if vol < 3000: continue
                if any(kw in q for kw in crypto_kw):
                    relevant.append(m)

            print(f"[POLYMARKET] {len(relevant)} relevant markets")

            for market in relevant[:15]:  # Limit to avoid rate limits
                mid = str(market.get("conditionId", market.get("id","")))
                if not mid or mid in alerted_markets: continue

                q = str(market.get("question", market.get("title","?")))

                analysis = claude(f"""Research this Polymarket prediction market. Return JSON:
{{
  "probability_estimate": 0.0-1.0,
  "edge": "underpriced|overpriced|fairly_priced",
  "bull_case": ["reason 1","reason 2"],
  "bear_case": ["reason 1","reason 2"],
  "evidence_quality": "strong|moderate|weak",
  "confidence_score": 0-100,
  "recommendation": "yes|no|pass",
  "reasoning_chain": ["step 1","step 2","step 3"]
}}
Market: {q}
Volume: ${float(market.get('volume', market.get('volumeNum', 0)) or 0):,.0f}
""", max_tokens=800)

                if not isinstance(analysis, dict) or analysis.get("error"):
                    print(f"[POLYMARKET] Analysis error for {q[:40]}")
                    continue

                prob_est = float(analysis.get("probability_estimate", 0.5))
                edge     = analysis.get("edge","fairly_priced")
                conf     = float(analysis.get("confidence_score", 0))
                ev       = analysis.get("evidence_quality","weak")

                # Market price
                market_price = 0.5
                try:
                    op = market.get("outcomePrices","")
                    if op:
                        prices = json.loads(op) if isinstance(op, str) else op
                        if isinstance(prices, list) and prices:
                            market_price = float(prices[0])
                except Exception:
                    pass

                edge_pct = abs(prob_est - market_price) * 100

                # Send to research if decent confidence
                if conf >= 40 and ev in ("moderate","strong"):
                    send_discord(DISCORD_POLYMARKET_RESEARCH,
                                 embed_poly_research(market, analysis))
                    print(f"[POLYMARKET] Research: {q[:50]} conf={conf:.0f} edge={edge_pct:.1f}%")
                    time.sleep(1)

                # Send to alpha if strong edge
                if edge_pct >= 8 and conf >= 55 and ev in ("moderate","strong") and edge != "fairly_priced":
                    alerted_markets.add(mid)
                    send_discord(DISCORD_POLYMARKET_ALPHA,
                                 embed_poly_alpha(market, analysis, edge_pct),
                                 content="@here 🎯 **STRONG EDGE DETECTED**")
                    print(f"[POLYMARKET] ALPHA: {q[:50]} edge={edge_pct:.1f}%")

                time.sleep(3)

        except Exception as e:
            print(f"[POLYMARKET WORKER] Error: {e}")
        time.sleep(10800)  # 3 hours

def jobs_worker():
    print("[DISCORD] 💼 Jobs worker started")
    while True:
        try:
            jobs = fetch_jobs(max_per=15)
            sent = 0
            for job in jobs:
                h = job.get("hash","")
                if h in alerted_jobs: continue
                alerted_jobs.add(h)
                send_discord(DISCORD_JOBS, embed_job(job))
                sent += 1
                print(f"[JOBS] Sent: {job.get('title','?')[:60]}")
                time.sleep(2)
            print(f"[JOBS] Scan complete — sent {sent} new jobs")
        except Exception as e:
            print(f"[JOBS WORKER] Error: {e}")
        time.sleep(21600)  # 6 hours

# ── FASTAPI APP ───────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app):
    channels = sum(1 for c in [
        DISCORD_BREAKING_NEWS, DISCORD_EMERGING_CHAINS,
        DISCORD_OPPORTUNITIES, DISCORD_SECURITY_ALERTS,
        DISCORD_POLYMARKET_RESEARCH, DISCORD_POLYMARKET_ALPHA,
    ] if c)
    print(f"[DISCORD] ✅ Starting alert workers ({channels}/6 channels configured)")

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
    yield

app = FastAPI(title="Crypto Intel Platform", version="2.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

class ResearchReq(BaseModel):
    query: str
    context: Optional[list] = None

@app.get("/")
def root():
    return {
        "status": "online",
        "platform": "Crypto Intelligence Platform v2",
        "ai_ready": bool(ANTHROPIC_API_KEY),
        "workers": ["news","chains","opportunities","polymarket","jobs"],
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

@app.get("/health")
def health():
    return {"status": "ok", "cache_entries": len(cache),
            "alerted": {"news": len(alerted_news), "chains": len(alerted_chains),
                        "opps": len(alerted_opps), "markets": len(alerted_markets),
                        "jobs": len(alerted_jobs)}}

@app.post("/api/research")
def research(req: ResearchReq):
    ctx = "\n".join(req.context) if req.context else ""
    result = claude(f"""Research: {req.query}
Context: {ctx}
Return JSON with: summary, opportunity_score, risk_score, confidence_score,
time_horizon, bull_case, bear_case, catalysts, risks, fomo_flags, facts, inferences, query""",
                    max_tokens=2000)
    if isinstance(result, dict): result["query"] = req.query
    return result

@app.get("/api/news")
def get_news():
    c = get_cache("news"); 
    if c: return {"cached": True, "items": c}
    items = fetch_news()
    set_cache("news", items, ttl=120)
    return {"items": items, "count": len(items)}

@app.get("/api/polymarket/markets")
def poly_markets(limit: int = 20):
    c = get_cache(f"poly:{limit}")
    if c: return {"cached": True, "markets": c}
    m = get_polymarkets(limit)
    set_cache(f"poly:{limit}", m, ttl=300)
    return {"markets": m, "count": len(m)}

@app.get("/api/polymarket/leaderboard")
def poly_leaderboard():
    c = get_cache("poly:lb")
    if c: return {"cached": True, "wallets": c}
    lb = get_poly_leaderboard()
    set_cache("poly:lb", lb, ttl=600)
    return {"wallets": lb, "count": len(lb)}

@app.get("/api/jobs")
def jobs(category: Optional[str] = None, remote_only: bool = False):
    c = get_cache("jobs")
    if not c:
        c = fetch_jobs()
        set_cache("jobs", c, ttl=3600)
    if category: c = [j for j in c if j.get("category","").lower() == category.lower()]
    if remote_only: c = [j for j in c if j.get("remote")]
    return {"jobs": c, "count": len(c)}

@app.get("/api/market/global")
def global_market():
    return get_global()

@app.get("/api/market/trending")
def trending():
    return {"trending": get_trending()}

@app.get("/api/chains/emerging")
def emerging(query: str = "blockchain layer1 testnet 2025"):
    c = get_cache(f"chains:{query}")
    if c: return {"cached": True, "chains": c}
    repos = github_search(query, max_results=15)
    chains = [{"name": r.get("name",""), "full_name": r.get("full_name",""),
               "description": r.get("description",""), "stars": r.get("stargazers_count",0),
               "url": r.get("html_url",""), "topics": r.get("topics",[]),
               "language": r.get("language","")} for r in repos]
    set_cache(f"chains:{query}", chains, ttl=1800)
    return {"chains": chains, "count": len(chains)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), reload=False)
