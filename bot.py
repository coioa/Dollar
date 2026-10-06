#!/usr/bin/env python3
"""Fetch USD/oil prices from the itsyebekhe/usd repo and post them to a Telegram channel.

One-shot script: run it every 30 minutes via systemd timer or cron.

Data sources (from the project itself):
  - market.json         -> {"usd": "271,706", "oil": "88.45", "updated": "HH:MM"}  (Tehran time)
  - api/history.json    -> {"latest": {"price_toman": int, "timestamp": epoch, ...}, ...}
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

try:
    from zoneinfo import ZoneInfo

    TEHRAN = ZoneInfo("Asia/Tehran")
except Exception:  # tzdata missing
    TEHRAN = timezone(timedelta(hours=3, minutes=30))

load_dotenv(Path(__file__).with_name(".env"))

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()
DATA_BASE_URL = os.getenv(
    "DATA_BASE_URL", "https://raw.githubusercontent.com/itsyebekhe/usd/main"
).rstrip("/")
MAX_AGE = timedelta(minutes=int(os.getenv("MAX_AGE_MINUTES", "90")))

HTTP_TIMEOUT = 15
RETRIES = 3
CLOCK_SKEW = timedelta(minutes=5)
RLM = "\u200f"  # right-to-left mark, keeps mixed Persian/Latin lines tidy
LINE = "──────────────"
FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")

log = logging.getLogger("usd-oil-bot")


@dataclass
class Quote:
    value: float
    updated_at: datetime


# ---------- helpers ----------

def now_tehran() -> datetime:
    return datetime.now(TEHRAN)


def is_fresh(updated_at: datetime, now: datetime) -> bool:
    return -CLOCK_SKEW <= (now - updated_at) <= MAX_AGE


def to_fa(text: str) -> str:
    return text.translate(FA_DIGITS)


def is_number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


# ---------- fetching ----------

def fetch_json(name: str):
    url = f"{DATA_BASE_URL}/{name}"
    for attempt in range(1, RETRIES + 1):
        try:
            r = requests.get(url, timeout=HTTP_TIMEOUT, headers={"Cache-Control": "no-cache"})
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            log.warning("fetch %s failed (attempt %d/%d): %s", name, attempt, RETRIES, type(e).__name__)
            if attempt < RETRIES:
                time.sleep(2 * attempt)
    return None


# ---------- parsing & validation ----------

def parse_usd(history, now: datetime) -> Quote | None:
    """USD/Toman from api/history.json -> latest (has a real timestamp)."""
    if not isinstance(history, dict):
        return None
    latest = history.get("latest")
    if not isinstance(latest, dict):
        return None
    price, ts = latest.get("price_toman"), latest.get("timestamp")
    if not is_number(price) or price <= 0 or not is_number(ts):
        log.warning("USD: invalid fields in history.json")
        return None
    updated = datetime.fromtimestamp(ts, TEHRAN)
    if not is_fresh(updated, now):
        log.warning("USD: data is stale (%s)", updated.isoformat())
        return None
    return Quote(float(price), updated)


def parse_market_time(text, now: datetime) -> datetime | None:
    """market.json 'updated' is HH:MM Tehran time with no date; resolve it to the latest past moment."""
    try:
        hh, mm = map(int, str(text).split(":"))
        candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    except ValueError:
        return None
    if candidate - now > CLOCK_SKEW:
        candidate -= timedelta(days=1)
    return candidate


def parse_oil(market, now: datetime) -> Quote | None:
    """Oil price from market.json ('نامشخص' means the project failed to fetch it)."""
    if not isinstance(market, dict):
        return None
    try:
        value = float(str(market.get("oil", "")).replace(",", "").strip())
    except ValueError:
        log.warning("Oil: unavailable in market.json (%r)", market.get("oil"))
        return None
    if not 0 < value < 1000:
        log.warning("Oil: value out of range (%s)", value)
        return None
    updated = parse_market_time(market.get("updated"), now)
    if updated is None or not is_fresh(updated, now):
        log.warning("Oil: data is stale or has an invalid time (%r)", market.get("updated"))
        return None
    return Quote(value, updated)


# ---------- message ----------

def build_message(usd: Quote | None, oil: Quote | None) -> str:
    sections = []
    if usd:
        sections.append(
            f"{RLM}💵 قیمت دلار آزاد\n{LINE}\n"
            f"{RLM}دلار: {to_fa(f'{usd.value:,.0f}')} تومان"
        )
    if oil:
        sections.append(
            f"{RLM}🛢 قیمت نفت\n{LINE}\n"
            f"{RLM}نفت خام (برنت/اوپک): ${oil.value:.2f}"
        )
    updated = min(q.updated_at for q in (usd, oil) if q)
    sections.append(f"{RLM}🕐 آخرین بروزرسانی: {updated:%H:%M} (به وقت تهران)")
    return "\n\n".join(sections)


# ---------- telegram ----------

def send_message(text: str) -> bool:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHANNEL_ID, "text": text, "disable_web_page_preview": True}
    for attempt in range(1, RETRIES + 1):
        try:
            r = requests.post(url, json=payload, timeout=20)
        except requests.RequestException as e:
            # never log the exception itself: its text contains the URL (and the token)
            log.warning("telegram request failed (attempt %d/%d): %s", attempt, RETRIES, type(e).__name__)
            time.sleep(2 * attempt)
            continue
        if r.ok:
            log.info("message sent")
            return True
        if r.status_code == 429:
            wait = r.json().get("parameters", {}).get("retry_after", 5)
            log.warning("rate limited, waiting %ss", wait)
            time.sleep(min(int(wait), 60))
            continue
        log.error("telegram error %s: %s", r.status_code, r.text[:300])
        if r.status_code < 500:
            return False  # 4xx: wrong token / chat id / permissions - retrying won't help
        time.sleep(2 * attempt)
    return False


# ---------- main ----------

def run(dry_run: bool) -> int:
    now = now_tehran()
    usd = parse_usd(fetch_json("api/history.json"), now)
    oil = parse_oil(fetch_json("market.json"), now)

    if not usd and not oil:
        log.error("no fresh valid data - nothing published; will retry next run")
        return 1
    if not usd or not oil:
        log.warning("partial data - publishing only the available part")

    text = build_message(usd, oil)
    if dry_run:
        print(text)
        return 0
    return 0 if send_message(text) else 1


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print the message instead of sending it")
    args = parser.parse_args()

    if not args.dry_run and not (BOT_TOKEN and CHANNEL_ID):
        log.error("BOT_TOKEN and CHANNEL_ID must be set (see .env.example)")
        return 2
    try:
        return run(args.dry_run)
    except Exception:  # last-resort guard: log and let the next scheduled run retry
        log.exception("unexpected error")
        return 1


if __name__ == "__main__":
    sys.exit(main())
