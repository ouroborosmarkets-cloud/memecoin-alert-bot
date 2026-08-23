"""
Crypto Alert Bot — sends Telegram notifications for:
  1. Price spikes/drops (% change over a rolling window)
  2. Volume breakouts (current volume vs recent average)
  3. Crypto news matching your keywords (via RSS)

No trading, no keys to an exchange account needed — this ONLY reads
public market data and sends you alerts. Nothing is executed on your behalf.
"""

import os
import time
import json
import logging
import requests
import feedparser
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("alert-bot")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

WATCH_SYMBOLS = os.getenv("WATCH_SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT,DOGEUSDT,PEPEUSDT").split(",")

PRICE_CHANGE_PCT_THRESHOLD = float(os.getenv("PRICE_CHANGE_PCT_THRESHOLD", "5"))
VOLUME_SPIKE_MULTIPLIER = float(os.getenv("VOLUME_SPIKE_MULTIPLIER", "3"))
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))
PRICE_WINDOW_MINUTES = int(os.getenv("PRICE_WINDOW_MINUTES", "15"))

NEWS_KEYWORDS = [k.strip().lower() for k in os.getenv(
    "NEWS_KEYWORDS", "bitcoin,ethereum,solana,sec,etf,hack,regulation"
).split(",")]
NEWS_RSS_FEEDS = [
    "https://cointelegraph.com/rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
]
NEWS_CHECK_INTERVAL_SECONDS = int(os.getenv("NEWS_CHECK_INTERVAL_SECONDS", "600"))

STATE_FILE = "state.json"

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"seen_news": [], "price_history": {}}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured — printing instead:\n%s", message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": False,
        }, timeout=10)
        if resp.status_code != 200:
            log.error("Telegram send failed: %s", resp.text)
    except Exception as e:
        log.error("Telegram send error: %s", e)

def get_ticker_data(symbol: str):
    url = "https://api.binance.com/api/v3/ticker/24hr"
    resp = requests.get(url, params={"symbol": symbol}, timeout=10)
    resp.raise_for_status()
    return resp.json()

def check_price_and_volume(state):
    now = time.time()
    for symbol in WATCH_SYMBOLS:
        symbol = symbol.strip()
        try:
            data = get_ticker_data(symbol)
        except Exception as e:
            log.error("Failed to fetch %s: %s", symbol, e)
            continue

        price = float(data["lastPrice"])
        volume = float(data["volume"])
        change_24h = float(data["priceChangePercent"])

        history = state["price_history"].setdefault(symbol, [])
        history.append({"t": now, "price": price, "volume": volume})
        history[:] = [h for h in history if now - h["t"] <= 3600]

        window_cutoff = now - (PRICE_WINDOW_MINUTES * 60)
        window_samples = [h for h in history if h["t"] >= window_cutoff]
        if len(window_samples) >= 2:
            old_price = window_samples[0]["price"]
            pct_change = ((price - old_price) / old_price) * 100
            if abs(pct_change) >= PRICE_CHANGE_PCT_THRESHOLD:
                direction = "🚀 SPIKE" if pct_change > 0 else "📉 DROP"
                send_telegram(
                    f"*{direction}: {symbol}*\n"
                    f"{pct_change:+.2f}% in ~{PRICE_WINDOW_MINUTES} min\n"
                    f"Price: ${price:,.6f}\n"
                    f"24h change: {change_24h:+.2f}%"
                )
                history[:] = [history[-1]]

        if len(history) >= 5:
            avg_volume = sum(h["volume"] for h in history[:-1]) / (len(history) - 1)
            if avg_volume > 0 and volume >= avg_volume * VOLUME_SPIKE_MULTIPLIER:
                send_telegram(
                    f"*📊 VOLUME SPIKE: {symbol}*\n"
                    f"Current: {volume:,.0f} vs avg {avg_volume:,.0f} "
                    f"({volume/avg_volume:.1f}x)\n"
                    f"Price: ${price:,.6f}"
                )

    save_state(state)

def check_news(state):
    for feed_url in NEWS_RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
        except Exception as e:
            log.error("Failed to parse feed %s: %s", feed_url, e)
            continue

        for entry in feed.entries[:20]:
            entry_id = entry.get("id", entry.get("link", entry.get("title", "")))
            if entry_id in state["seen_news"]:
                continue

            title = entry.get("title", "")
            title_lower = title.lower()
            if any(kw in title_lower for kw in NEWS_KEYWORDS):
                send_telegram(f"*📰 NEWS:* {title}\n{entry.get('link', '')}")

            state["seen_news"].append(entry_id)

    state["seen_news"] = state["seen_news"][-500:]
    save_state(state)

def main():
    log.info("Starting alert bot. Watching: %s", WATCH_SYMBOLS)
    send_telegram(f"✅ Alert bot started at {datetime.now().strftime('%Y-%m-%d %H:%M')}. Watching: {', '.join(WATCH_SYMBOLS)}")

    state = load_state()
    last_news_check = 0

    while True:
        try:
            check_price_and_volume(state)
            if time.time() - last_news_check >= NEWS_CHECK_INTERVAL_SECONDS:
                check_news(state)
                last_news_check = time.time()
        except Exception as e:
            log.error("Loop error: %s", e)
        time.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
