"""
Checks the "mua vào" (buy) price of a specific silver product on
https://giabac.ancarat.com/ and sends a Telegram alert whenever it
reaches a new price level above a threshold.

Levels are threshold, threshold + LEVEL_GAP, threshold + 2*LEVEL_GAP,
etc. Only the highest level reached since the price last dropped back
to/below threshold triggers an alert, so a price oscillating within
one level doesn't spam — but continued climbing still gets one alert
per level. State (last price + highest level already alerted) is
persisted to state.json.

Required environment variables:
  TELEGRAM_BOT_TOKEN   - bot token from BotFather
  TELEGRAM_CHAT_ID     - your chat id
  PRICE_THRESHOLD      - base alert level in VND
  LEVEL_GAP            - VND gap between consecutive alert levels
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, asdict, fields
from pathlib import Path

import requests
from bs4 import BeautifulSoup

URL = "https://giabac.ancarat.com/"
PRODUCT_SLUG = "ngan-long-quang-tien-1-kilo"
PRODUCT_LABEL = "Ngân Long Quảng Tiến - 1 Kilo"
STATE_FILE = Path(__file__).parent / "state.json"


@dataclass
class State:
    last_mua_vao: int | None = None
    last_alerted_level: int | None = None
    monitoring_broken_alerted: bool = False


def load_state() -> State:
    if STATE_FILE.exists():
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        known = {f.name for f in fields(State)}
        return State(**{k: v for k, v in raw.items() if k in known})
    return State()


def save_state(state: State) -> None:
    STATE_FILE.write_text(json.dumps(asdict(state), ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_prices() -> tuple[int, int]:
    """Returns (ban_ra, mua_vao) in VND for the target product."""
    resp = requests.get(URL, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    link = soup.find("a", href=lambda h: h and PRODUCT_SLUG in h)
    if link is None:
        raise ValueError(f"Could not find product row for slug '{PRODUCT_SLUG}' — page layout may have changed")

    row = link.find_parent("tr")
    cells = row.find_all("td")
    if len(cells) < 3:
        raise ValueError(f"Expected 3 columns in product row, found {len(cells)}")

    def parse_vnd(text: str) -> int:
        digits = re.sub(r"[^\d]", "", text)
        return int(digits)

    ban_ra = parse_vnd(cells[1].get_text())
    mua_vao = parse_vnd(cells[2].get_text())
    return ban_ra, mua_vao


def send_telegram(message: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": message},
        timeout=15,
    )
    if not resp.ok:
        print(f"[warn] Telegram send failed: {resp.status_code} {resp.text}", file=sys.stderr)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

    threshold_env = os.environ.get("PRICE_THRESHOLD", "").strip()
    if not threshold_env:
        raise SystemExit("PRICE_THRESHOLD environment variable is required")
    threshold = int(threshold_env)

    gap_env = os.environ.get("LEVEL_GAP", "").strip()
    if not gap_env:
        raise SystemExit("LEVEL_GAP environment variable is required")
    level_gap = int(gap_env)

    state = load_state()

    try:
        ban_ra, mua_vao = fetch_prices()
    except Exception as exc:
        print(f"[error] Failed to fetch/parse price: {exc}", file=sys.stderr)
        if not state.monitoring_broken_alerted:
            send_telegram(
                f"Silver price monitor broken\n"
                f"Could not read the price for {PRODUCT_LABEL} from {URL}.\n"
                f"Error: {exc}\nThe page layout may have changed — check the scraper.",
            )
            state.monitoring_broken_alerted = True
            save_state(state)
        raise

    print(f"{PRODUCT_LABEL}: bán ra={ban_ra:,} mua vào={mua_vao:,} (threshold={threshold:,})")

    # Recovered from a previous scrape failure.
    state.monitoring_broken_alerted = False

    if mua_vao > threshold:
        current_level = threshold + (mua_vao - threshold) // level_gap * level_gap
        if state.last_alerted_level is None or current_level > state.last_alerted_level:
            send_telegram(
                f"Cảnh báo giá bạc Ancarat: {PRODUCT_LABEL}\n"
                f"Giá mua vào đã đạt mốc {current_level:,} VND (ngưỡng: {threshold:,} VND).\n"
                f"Giá mua vào hiện tại: {mua_vao:,} VND\nBán ra: {ban_ra:,} VND\nSource: {URL}",
            )
            state.last_alerted_level = current_level
    else:
        # Reset so the next climb above threshold starts a fresh set of levels.
        state.last_alerted_level = None

    state.last_mua_vao = mua_vao
    save_state(state)


if __name__ == "__main__":
    main()
