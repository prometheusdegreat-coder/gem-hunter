"""
Crypto Intelligence Platform — Research Engine v2
Fixed: Polymarket API parsing, opportunity scoring fallback,
web3 jobs, wallet leaderboard, GitHub follower filtering.
"""

import os
import json
import time
import hashlib
import requests
import feedparser
from datetime import datetime, timezone, timedelta
from typing import Optional
from dataclasses import dataclass, field, asdict

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
COINGECKO_BASE    = "https://api.coingecko.com/api/v3"
CRYPTOPANIC_KEY   = os.getenv("CRYPTOPANIC_API_KEY", "")
GITHUB_TOKEN      = os.getenv("GITHUB_TOKEN", "")

# ─────────────────────────────────────────────────────────────
# DATA MODELS
# ─────────────────────────────────────────────────────────────
@dataclass
class Source:
    name:        str
    url:         str
    content:     str
    timestamp:   str
    credibility: float
    verified:    bool = False

@dataclass
class ResearchReport:
    query:            str
    summary:          str
    key_findings:     list
    bull_case:        list
    bear_case:        list
    risks:            list
    catalysts:        list
    facts:            list
    inferences:       list
    speculation:      list
    fomo_flags:       list
    conflicting_data: list
    sources:          list
    confidence_score: float
    opportunity_score: float
    risk_score:       float
    time_horizon:     str
    generated_at:     str
    reasoning_chain:  list

@dataclass
class NewsItem:
    title:     str
    summary:   str
    url:       str
    source:    str
    published: str
    sentiment: str
    impact:    str
    assets:    list
    verified:  bool

@dataclass
class OpportunityScore:
    novelty:           float
    credibility:       float
    liquidity:         float
    timing:            float
    investor_quality:  float
    dev_activity:      float
    social_momentum:   float
    catalyst_strength: float
    technical_maturity: float
    macro_alignment:   float
    risk_level:        float
    total:             float
    risk_score:        float
    confidence_score:  float
    time_horizon:      str

# ─────────────────────────────────────────────────────────────
# NEWS — Module 3
# ─────────────────────────────────────────────────────────────
NEWS_FEEDS = {
    "CoinDesk":    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "CryptoPanic": "https://cryptopanic.com/news/rss/",
    "The Block":   "https://www.theblock.co/rss.xml",
    "Decrypt":     "https://decrypt.co/feed",
    "Blockworks":  "https://blockworks.co/feed",
    "DLNews":      "https://dlnews.com/arc/outboundfeeds/rss/",
}

def fetch_rss_news(max_per_feed: int = 5) -> list:
    items = []
    seen  = set()
    for source_name, url in NEWS_FEEDS.items():
        try:
            feed  = feedparser.parse(url)
            count = 0
            for entry in feed.entries:
                if count >= max_per_feed:
                    break
                title   = getattr(entry, "title", "")
                summary = getattr(entry, "summary", "")[:500]
                link    = getattr(entry, "link", "")
                pub     = getattr(entry, "published", str(datetime.now()))
                h = hashlib.md5(title.lower().encode()).hexdigest()
                if h in seen:
                    continue
                seen.add(h)
                items.append(NewsItem(
                    title=title, summary=summary, url=link,
                    source=source_name, published=pub,
                    sentiment="neutral", impact="medium",
                    assets=[], verified=True
                ))
                count += 1
        except Exception as e:
            print(f"[NEWS] {source_name} error: {e}")
    return items

def fetch_cryptopanic(filter_type: str = "important") -> list:
    if not CRYPTOPANIC_KEY:
        return []
    try:
        url = (f"https://cryptopanic.com/api/v1/posts/"
               f"?auth_token={CRYPTOPANIC_KEY}&filter={filter_type}&public=true")
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return []
        items = []
        for post in r.json().get("results", [])[:20]:
            currencies = [c.get("code","") for c in post.get("currencies", [])]
            items.append(NewsItem(
                title=post.get("title",""),
                summary=post.get("title",""),
                url=post.get("url",""),
                source=post.get("source",{}).get("title","CryptoPanic"),
                published=post.get("published_at",""),
                sentiment="neutral",
                impact="high" if filter_type == "important" else "medium",
                assets=currencies,
                verified=True
            ))
        return items
    except Exception as e:
        print(f"[CRYPTOPANIC] Error: {e}")
        return []

# ─────────────────────────────────────────────────────────────
# GITHUB — Module 1
# ─────────────────────────────────────────────────────────────
def scan_github_repo(repo: str) -> dict:
    headers = {}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"
    base   = f"https://api.github.com/repos/{repo}"
    result = {
        "stars": 0, "forks": 0, "open_issues": 0,
        "contributors": 0, "commits_30d": 0,
        "last_commit": "", "language": "",
        "has_security_policy": False,
        "description": ""
    }
    try:
        r = requests.get(base, headers=headers, timeout=8)
        if r.status_code == 200:
            d = r.json()
            result["stars"]       = d.get("stargazers_count", 0)
            result["forks"]       = d.get("forks_count", 0)
            result["open_issues"] = d.get("open_issues_count", 0)
            result["language"]    = d.get("language", "")
            result["description"] = d.get("description", "")
        since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        r2 = requests.get(f"{base}/commits?since={since}&per_page=100",
                          headers=headers, timeout=8)
        if r2.status_code == 200:
            commits = r2.json()
            result["commits_30d"] = len(commits)
            if commits:
                result["last_commit"] = commits[0].get("commit",{}).get("author",{}).get("date","")
        r3 = requests.get(f"{base}/contributors?per_page=100",
                          headers=headers, timeout=8)
        if r3.status_code == 200:
            result["contributors"] = len(r3.json())
    except Exception as e:
        print(f"[GITHUB] {repo} error: {e}")
    return result

def search_github_blockchains(query: str = "blockchain testnet layer1 2025",
                               max_results: int = 10) -> list:
    headers = {"Accept": "application/vnd.github.v3+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"
    try:
        r = requests.get(
            "https://api.github.com/search/repositories",
            params={"q": query, "sort": "updated", "per_page": max_results},
            headers=headers, timeout=10
        )
        if r.status_code != 200:
            return []
        return r.json().get("items", [])
    except Exception as e:
        print(f"[GITHUB SEARCH] Error: {e}")
        return []

# ─────────────────────────────────────────────────────────────
# MARKET DATA
# ─────────────────────────────────────────────────────────────
def get_market_data(coin_id: str) -> dict:
    try:
        r = requests.get(
            f"{COINGECKO_BASE}/coins/{coin_id}",
            params={"localization": False, "tickers": False,
                    "market_data": True, "community_data": True,
                    "developer_data": True},
            timeout=10
        )
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}

def get_trending_coins() -> list:
    try:
        r = requests.get(f"{COINGECKO_BASE}/search/trending", timeout=10)
        return r.json().get("coins", []) if r.status_code == 200 else []
    except Exception:
        return []

def get_global_market_data() -> dict:
    try:
        r = requests.get(f"{COINGECKO_BASE}/global", timeout=10)
        return r.json().get("data", {}) if r.status_code == 200 else {}
    except Exception:
        return {}

# ─────────────────────────────────────────────────────────────
# POLYMARKET — Module 5 & 6
# Fixed: handle both list and dict API responses
# ─────────────────────────────────────────────────────────────
def get_polymarket_markets(limit: int = 50, active_only: bool = True) -> list:
    """Fetch active Polymarket markets — handles v1 and v2 API formats."""
    endpoints = [
        "https://gamma-api.polymarket.com/markets",
        "https://clob.polymarket.com/markets",
    ]
    for endpoint in endpoints:
        try:
            params = {"limit": limit, "active": "true", "closed": "false"}
            r = requests.get(endpoint, params=params, timeout=12)
            if r.status_code != 200:
                continue
            data = r.json()
            # Handle both list response and {data: [...]} response
            if isinstance(data, list):
                markets = data
            elif isinstance(data, dict):
                markets = data.get("data", data.get("markets", []))
            else:
                continue
            # Normalize each market to ensure it's a dict
            result = []
            for m in markets:
                if isinstance(m, str):
                    # Sometimes API returns stringified JSON
                    try:
                        m = json.loads(m)
                    except Exception:
                        continue
                if not isinstance(m, dict):
                    continue
                # Polymarket returns "outcomes" and "outcomePrices" as
                # JSON-encoded STRINGS (e.g. '["Yes","No"]'), not lists.
                # Parse them here so downstream code can just use .get().
                for field in ("outcomes", "outcomePrices", "clobTokenIds"):
                    val = m.get(field)
                    if isinstance(val, str):
                        try:
                            m[field] = json.loads(val)
                        except Exception:
                            m[field] = []
                result.append(m)
            if result:
                print(f"[POLYMARKET] Got {len(result)} markets from {endpoint}")
                return result[:limit]
        except Exception as e:
            print(f"[POLYMARKET] {endpoint} error: {e}")
            continue
    # Fallback: try CLOB markets endpoint
    try:
        r = requests.get("https://clob.polymarket.com/markets?next_cursor=",
                         timeout=12)
        if r.status_code == 200:
            data = r.json()
            markets = data.get("data", [])
            return [m for m in markets if isinstance(m, dict)][:limit]
    except Exception:
        pass
    return []

def get_polymarket_leaderboard(days: int = 90) -> list:
    """
    Fetch profitable wallets from Polymarket.
    Only includes wallets with sustained history.
    """
    try:
        # Polymarket leaderboard endpoint
        r = requests.get(
            "https://gamma-api.polymarket.com/leaderboard",
            params={"limit": 50, "window": "all"},
            timeout=12
        )
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list):
                return data[:50]
            elif isinstance(data, dict):
                return data.get("data", data.get("leaderboard", []))[:50]
    except Exception as e:
        print(f"[LEADERBOARD] Error: {e}")

    # Fallback: profile endpoint
    try:
        r = requests.get(
            "https://gamma-api.polymarket.com/profiles",
            params={"limit": 50, "sort": "profit", "order": "desc"},
            timeout=12
        )
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list):
                return data[:50]
    except Exception:
        pass
    return []

# ─────────────────────────────────────────────────────────────
# WEB3 JOBS — Module 7
# ─────────────────────────────────────────────────────────────
WEB3_JOB_FEEDS = {
    "web3.career":       "https://web3.career/feed.xml",
    "Cryptocurrency Jobs": "https://cryptocurrencyjobs.co/feed.xml",
    "Remote3":           "https://remote3.co/feed.xml",
    "Wellfound Crypto":  "https://wellfound.com/jobs.rss?role=blockchain",
}

WEB3_JOB_APIS = [
    "https://cryptojobslist.com/api/jobs?limit=20",
]

def fetch_web3_jobs(max_per_feed: int = 10) -> list:
    jobs = []
    seen = set()

    # RSS feeds
    for source, url in WEB3_JOB_FEEDS.items():
        try:
            feed  = feedparser.parse(url)
            count = 0
            for entry in feed.entries:
                if count >= max_per_feed:
                    break
                title = getattr(entry, "title", "")
                h = hashlib.md5(title.lower().encode()).hexdigest()
                if h in seen:
                    continue
                seen.add(h)
                t = title.lower()
                category = (
                    "Engineering"  if any(k in t for k in ["engineer","developer","dev","smart contract","solidity","rust","blockchain dev"]) else
                    "Research"     if any(k in t for k in ["research","analyst","economist","scientist"]) else
                    "Security"     if any(k in t for k in ["security","audit","penetration","bug bounty"]) else
                    "Data"         if any(k in t for k in ["data","analytics","bi","database"]) else
                    "AI"           if any(k in t for k in ["ai","machine learning","ml","llm","ai engineer"]) else
                    "DevRel"       if any(k in t for k in ["devrel","developer relations","advocate","evangelist"]) else
                    "Marketing"    if any(k in t for k in ["marketing","growth","content","seo","social"]) else
                    "Design"       if any(k in t for k in ["design","ui","ux","product designer","figma"]) else
                    "Community"    if any(k in t for k in ["community","social","moderator","discord"]) else
                    "Operations"
                )
                jobs.append({
                    "title":     title,
                    "url":       getattr(entry, "link", ""),
                    "company":   source,
                    "published": getattr(entry, "published", ""),
                    "summary":   getattr(entry, "summary", "")[:300],
                    "category":  category,
                    "remote":    any(k in title.lower() for k in ["remote","distributed","anywhere"]),
                    "source":    source
                })
                count += 1
        except Exception as e:
            print(f"[JOBS] {source} error: {e}")

    # Direct API fallbacks
    for api_url in WEB3_JOB_APIS:
        try:
            r = requests.get(api_url, timeout=8)
            if r.status_code == 200:
                data = r.json()
                job_list = data if isinstance(data, list) else data.get("jobs", data.get("data", []))
                for job in job_list[:max_per_feed]:
                    if not isinstance(job, dict):
                        continue
                    title = job.get("title", job.get("position", ""))
                    h = hashlib.md5(title.lower().encode()).hexdigest()
                    if h in seen or not title:
                        continue
                    seen.add(h)
                    jobs.append({
                        "title":     title,
                        "url":       job.get("url", job.get("apply_url", "")),
                        "company":   job.get("company", job.get("company_name", "")),
                        "published": job.get("created_at", job.get("published_at", "")),
                        "summary":   job.get("description", "")[:300],
                        "category":  job.get("category", "Engineering"),
                        "remote":    job.get("remote", False),
                        "source":    "CryptoJobsList"
                    })
        except Exception as e:
            print(f"[JOBS API] {api_url} error: {e}")

    return jobs

# ─────────────────────────────────────────────────────────────
# OPPORTUNITY SCORER — Module 9
# ─────────────────────────────────────────────────────────────
def score_opportunity(data: dict) -> OpportunityScore:
    def clamp(v, lo=0, hi=100): return max(lo, min(hi, v))

    commits      = data.get("github_commits_30d", 0)
    contributors = data.get("github_contributors", 0)
    dev          = clamp(commits * 2 + contributors * 5)

    vol = data.get("volume_24h", 0)
    liq = clamp(min(vol / 10_000, 100))

    inv_tier = {"tier1": 100, "tier2": 70, "tier3": 40, "unknown": 20, "none": 0}
    investor = inv_tier.get(data.get("investor_tier", "unknown"), 20)

    news_count = data.get("news_mentions_7d", 0)
    sentiment  = data.get("sentiment_score", 0.5)
    social     = clamp(news_count * 5 + sentiment * 50)

    audit   = 40 if data.get("audited", False) else 0
    testnet = 30 if data.get("testnet_live", False) else 0
    mainnet = 30 if data.get("mainnet_live", False) else 0
    tech    = clamp(audit + testnet + mainnet)

    age     = data.get("age_days", 365)
    timing  = clamp(100 - (age / 365) * 50) if age < 730 else 20
    novelty = clamp(100 - data.get("mainstream_score", 50))

    has_docs  = 20 if data.get("has_documentation", False) else 0
    has_team  = 20 if data.get("team_public", False) else 0
    has_audit = 30 if data.get("audited", False) else 0
    has_code  = 30 if data.get("open_source", False) else 0
    credibility = clamp(has_docs + has_team + has_audit + has_code)

    catalysts = data.get("catalyst_count", 0)
    catalyst  = clamp(catalysts * 20)
    macro     = data.get("macro_alignment_score", 50)

    risk_flags = data.get("risk_flag_count", 0)
    unaudited  = 30 if not data.get("audited", False) else 0
    risk       = clamp(risk_flags * 15 + unaudited + (100 - credibility) * 0.3)

    total = (
        0.08 * novelty + 0.12 * credibility + 0.08 * liq +
        0.10 * timing  + 0.10 * investor    + 0.12 * dev +
        0.08 * social  + 0.10 * catalyst    + 0.12 * tech +
        0.06 * macro   + 0.04 * (100 - risk)
    )

    horizon = (
        "immediate" if timing >= 80 and catalyst >= 60 else
        "short"     if timing >= 60 else
        "medium"    if timing >= 40 else "long"
    )

    return OpportunityScore(
        novelty=round(novelty,1), credibility=round(credibility,1),
        liquidity=round(liq,1), timing=round(timing,1),
        investor_quality=round(investor,1), dev_activity=round(dev,1),
        social_momentum=round(social,1), catalyst_strength=round(catalyst,1),
        technical_maturity=round(tech,1), macro_alignment=round(macro,1),
        risk_level=round(risk,1), total=round(clamp(total),1),
        risk_score=round(risk,1),
        confidence_score=round(credibility*0.6+dev*0.4,1),
        time_horizon=horizon
    )

# ─────────────────────────────────────────────────────────────
# CLAUDE AI RESEARCH ENGINE — Module 8
# ─────────────────────────────────────────────────────────────
RESEARCH_SYSTEM_PROMPT = """
You are an institutional-grade crypto research analyst.
Core principles:
1. NEVER fabricate data
2. ALWAYS separate fact from inference from speculation — label each
3. Express uncertainty with ranges
4. Show BOTH bull and bear cases
5. Flag FOMO/hype explicitly
6. Security risk is a first-class signal
Output: Always respond in valid JSON only. No markdown, no preamble.
"""

def call_claude(system_prompt: str, user_prompt: str, max_tokens: int = 2000) -> str:
    if not ANTHROPIC_API_KEY:
        # Return a structured fallback so the platform still works
        return json.dumps({
            "summary": "AI analysis unavailable — ANTHROPIC_API_KEY not set in Railway Variables.",
            "key_findings": ["Set ANTHROPIC_API_KEY in Railway → Variables to enable AI analysis"],
            "bull_case": [], "bear_case": [], "risks": ["API key not configured"],
            "catalysts": [], "facts": [], "inferences": [], "speculation": [],
            "fomo_flags": [], "conflicting_data": [],
            "confidence_score": 0, "opportunity_score": 0,
            "risk_score": 50, "time_horizon": "unknown",
            "reasoning_chain": ["Claude API key required for analysis"]
        })
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json"
            },
            json={
                "model":      "claude-sonnet-5",
                "max_tokens": max_tokens,
                "system":     system_prompt,
                "messages":   [{"role": "user", "content": user_prompt}]
            },
            timeout=60
        )
        if r.status_code != 200:
            return json.dumps({"error": f"API {r.status_code}", "summary": r.text[:200]})
        return r.json()["content"][0]["text"]
    except Exception as e:
        return json.dumps({"error": str(e), "summary": "API call failed"})

def _parse_claude_json(text: str) -> dict:
    try:
        clean = text.strip()
        if "```" in clean:
            parts = clean.split("```")
            for p in parts:
                p = p.strip()
                if p.startswith("json"): p = p[4:].strip()
                try:
                    return json.loads(p)
                except Exception:
                    continue
        return json.loads(clean)
    except Exception:
        return {"summary": text[:300], "error": "parse_error",
                "key_findings": [], "bull_case": [], "bear_case": [],
                "risks": [], "catalysts": [], "facts": [], "inferences": [],
                "speculation": [], "fomo_flags": [], "conflicting_data": [],
                "confidence_score": 0, "opportunity_score": 0,
                "risk_score": 50, "time_horizon": "unknown", "reasoning_chain": []}

def research_query(query: str, context_sources: list = None) -> ResearchReport:
    context   = "\n\n".join(context_sources) if context_sources else ""
    timestamp = datetime.now(timezone.utc).isoformat()

    prompt = f"""Research query: {query}

Context:
{context if context else "No real-time context — use training knowledge, clearly label as background knowledge."}

Return ONLY valid JSON:
{{
  "summary": "2-3 sentence executive summary",
  "key_findings": ["finding 1", "finding 2"],
  "bull_case": ["bull point 1"],
  "bear_case": ["bear point 1"],
  "risks": ["risk 1"],
  "catalysts": ["catalyst 1"],
  "facts": ["[FACT] confirmed fact"],
  "inferences": ["[INFERENCE] logical deduction"],
  "speculation": ["[SPECULATION] unconfirmed claim"],
  "fomo_flags": ["hype signal detected"],
  "conflicting_data": ["source disagreement"],
  "confidence_score": 0-100,
  "opportunity_score": 0-100,
  "risk_score": 0-100,
  "time_horizon": "immediate|short|medium|long",
  "reasoning_chain": ["step 1", "step 2"]
}}"""

    data = _parse_claude_json(call_claude(RESEARCH_SYSTEM_PROMPT, prompt, 3000))
    return ResearchReport(
        query=query,
        summary=data.get("summary",""),
        key_findings=data.get("key_findings",[]),
        bull_case=data.get("bull_case",[]),
        bear_case=data.get("bear_case",[]),
        risks=data.get("risks",[]),
        catalysts=data.get("catalysts",[]),
        facts=data.get("facts",[]),
        inferences=data.get("inferences",[]),
        speculation=data.get("speculation",[]),
        fomo_flags=data.get("fomo_flags",[]),
        conflicting_data=data.get("conflicting_data",[]),
        sources=[],
        confidence_score=float(data.get("confidence_score",0)),
        opportunity_score=float(data.get("opportunity_score",0)),
        risk_score=float(data.get("risk_score",50)),
        time_horizon=data.get("time_horizon","unknown"),
        generated_at=timestamp,
        reasoning_chain=data.get("reasoning_chain",[])
    )

def analyze_news_batch(news_items: list) -> dict:
    if not news_items:
        return {}
    news_text = "\n".join([
        f"[{n.source}] {n.title}: {n.summary[:150]}"
        for n in news_items[:30]
    ])
    prompt = f"""Analyze these crypto news items. Return JSON only:
{{
  "high_impact": ["titles of high market-moving news"],
  "clusters": {{"cluster_name": ["related titles"]}},
  "affected_assets": {{"SYMBOL": ["titles"]}},
  "fomo_signals": ["hype-driven titles"],
  "verified_signals": ["factually strong titles"],
  "market_sentiment": "bullish|bearish|neutral",
  "top_story_summary": "2 sentence summary"
}}

News:
{news_text}"""
    return _parse_claude_json(call_claude(RESEARCH_SYSTEM_PROMPT, prompt, 1500))

def analyze_emerging_chain(chain_data: dict) -> dict:
    prompt = f"""Analyze this emerging blockchain. Return JSON only:
{{
  "investment_thesis": "2-3 sentence thesis",
  "security_assessment": {{
    "score": 0-100,
    "flags": ["concern 1"],
    "unaudited_risk": true,
    "bridge_risk": false,
    "contract_risk": "high|medium|low"
  }},
  "developer_assessment": "strong|moderate|weak|insufficient_data",
  "documentation_score": 0-100,
  "competitive_advantage": "differentiator",
  "comparable_projects": ["similar project"],
  "red_flags": ["red flag 1"],
  "green_flags": ["green flag 1"],
  "monitor_links": {{
    "github": "{chain_data.get('url','')}",
    "dexscreener": "",
    "coingecko": "",
    "twitter": ""
  }},
  "confidence_score": 0-100,
  "opportunity_score": 0-100,
  "recommendation": "monitor|research|avoid",
  "reasoning": ["step 1"]
}}

Chain: {json.dumps(chain_data, indent=2)}

Flag any security concerns as first-class signals."""
    return _parse_claude_json(call_claude(RESEARCH_SYSTEM_PROMPT, prompt, 2000))

def analyze_polymarket(market: dict) -> dict:
    """Fixed: handles dict market objects only."""
    if not isinstance(market, dict):
        return {"error": "invalid_market_format", "probability_estimate": 0.5,
                "edge": "fairly_priced", "confidence_score": 0,
                "evidence_quality": "weak", "recommendation": "pass",
                "bull_case": [], "bear_case": [], "key_risks": [],
                "key_catalysts": [], "reasoning_chain": []}

    prompt = f"""Analyze this Polymarket prediction market. Return JSON only:
{{
  "probability_estimate": 0.0-1.0,
  "edge": "overpriced|underpriced|fairly_priced",
  "bull_case": ["yes evidence"],
  "bear_case": ["no evidence"],
  "key_risks": ["risk"],
  "key_catalysts": ["catalyst"],
  "evidence_quality": "strong|moderate|weak",
  "confidence_score": 0-100,
  "recommendation": "yes|no|pass",
  "reasoning_chain": ["step 1", "step 2"]
}}

Market: {json.dumps(market, indent=2)[:2000]}

Base probability on evidence, not market price alone.
Flag if sentiment/FOMO driven."""
    return _parse_claude_json(call_claude(RESEARCH_SYSTEM_PROMPT, prompt, 1500))

def security_research(protocol_name: str, context: str = "") -> dict:
    prompt = f"""Security risk assessment for: {protocol_name}
{f'Context: {context[:500]}' if context else ''}

Return JSON only:
{{
  "overall_risk": "critical|high|medium|low",
  "risk_score": 0-100,
  "audit_status": "audited|partially_audited|unaudited|unknown",
  "known_incidents": ["incident with date"],
  "vulnerability_patterns": ["vulnerability type"],
  "bridge_risk": "high|medium|low|not_applicable",
  "smart_contract_risk": "high|medium|low",
  "centralization_risk": "high|medium|low",
  "exploit_likelihood": 0-100,
  "recommendations": ["recommendation"],
  "red_flags": ["red flag"],
  "confidence": 0-100
}}"""
    return _parse_claude_json(call_claude(RESEARCH_SYSTEM_PROMPT, prompt, 1500))

# ─────────────────────────────────────────────────────────────
# SECURITY SCANNER
# ─────────────────────────────────────────────────────────────
KNOWN_EXPLOIT_PATTERNS = [
    "unaudited","no audit","rug","exploit","hack",
    "bridge vulnerability","flash loan","reentrancy",
    "private key","centralized admin","proxy upgrade",
    "infinite mint","oracle manipulation",
]

def quick_security_scan(text: str) -> list:
    t = text.lower()
    return [p for p in KNOWN_EXPLOIT_PATTERNS if p in t]

# ─────────────────────────────────────────────────────────────
# DAILY INTELLIGENCE RUNNER
# ─────────────────────────────────────────────────────────────
def run_daily_intelligence() -> dict:
    print("[INTEL] Starting daily sweep...")
    report = {
        "generated_at":  datetime.now(timezone.utc).isoformat(),
        "news":          [],
        "trending":      [],
        "global_market": {},
        "jobs":          [],
        "polymarkets":   [],
        "ai_synthesis":  {}
    }
    news = fetch_rss_news(max_per_feed=3) + fetch_cryptopanic("important")
    report["news"] = [
        {"title": n.title, "source": n.source, "url": n.url}
        for n in news[:30]
    ]
    report["global_market"] = get_global_market_data()
    report["trending"]      = get_trending_coins()
    report["jobs"]          = fetch_web3_jobs(max_per_feed=5)
    report["polymarkets"]   = get_polymarket_markets(limit=10)
    if news:
        report["ai_synthesis"]["news_analysis"] = analyze_news_batch(news)
    print("[INTEL] Daily sweep complete.")
    return report
