[README.md](https://github.com/user-attachments/files/29438186/README.md)
# Gem Hunter Bot

Real-time BNB + Solana gem scanner using DEXScreener API.
Sends Telegram alerts within seconds of a new token meeting your criteria.

## Setup in 5 steps

### Step 1 — Create your Telegram bot
1. Open Telegram and search for @BotFather
2. Send /newbot and follow the prompts
3. Copy the token it gives you (looks like: 123456789:ABCdef...)

### Step 2 — Get your Chat ID
1. Start a chat with your new bot (search its username and press Start)
2. Open this URL in your browser (replace YOUR_TOKEN):
   https://api.telegram.org/botYOUR_TOKEN/getUpdates
3. Send any message to your bot, refresh the URL
4. Find "chat":{"id": 123456789} — that number is your CHAT_ID

### Step 3 — Deploy to Railway (free, runs 24/7)
1. Go to https://railway.app and sign up (free with GitHub)
2. Click "New Project" → "Deploy from GitHub repo"
3. Upload or push this folder to a GitHub repo first, then connect it
4. In Railway dashboard → Variables, add:
   TELEGRAM_BOT_TOKEN = (your token from step 1)
   TELEGRAM_CHAT_ID   = (your chat id from step 2)
5. Railway auto-deploys and starts the bot

### Step 4 (alternative) — Run locally
pip install -r requirements.txt
cp .env.example .env   # fill in your values
python bot.py

### Step 5 — Tune your filters
Edit the CONFIG section at the top of bot.py:
  MIN_VOLUME_USD     — raise for higher quality signals
  MAX_AGE_MINUTES    — lower for ultra-fresh tokens only
  MIN_BUY_PRESSURE   — raise for stronger buy momentum
  MAX_MCAP_USD       — lower to focus on micro-caps only

## What you get in each alert
- Gem score (0-100) and Risk score (0-100)
- Age, price, mcap, liquidity
- 5m volume and buy/sell ratio
- DEXScreener link
- Contract address
