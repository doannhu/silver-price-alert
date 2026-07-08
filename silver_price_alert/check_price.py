"""
Checks the "mua vào" (buy) price of a specific silver product on
https://giabac.ancarat.com/ and sends a Telegram/email alert the moment
it crosses above a threshold.

State (last price + whether we've already alerted for the current
crossing) is persisted to state.json so we only notify once per
crossing, not on every poll.

Required environment variables (set as secrets in CI):
  TELEGRAM_BOT_TOKEN   - bot token from BotFather (optional if email-only)
  TELEGRAM_CHAT_ID     - your chat id (optional if email-only)
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, EMAIL_TO
                       - SMTP creds for email alerts (optional if telegram-only)

Optional:
  PRICE_THRESHOLD      - overrides the default threshold (VND)
"""

from __future__ import annotations

import json
import os
import re
import smtplib
import sys
from dataclasses import dataclass, asdict
from email.mime.text import MIMEText
from pathlib import Path

import requests
from bs4 import BeautifulSoup

URL = "https://giabac.ancarat.com/"
PRODUCT_SLUG = "ngan-long-quang-tien-1-kilo"
PRODUCT_LABEL = "Ngân Long Quảng Tiến - 1 Kilo"
DEFAULT_THRESHOLD = 62_800_000
STATE_FILE = Path(__file__).parent / "state.json"


@dataclass
class State:
    last_mua_vao: int | None = None
    alerted: bool = False
    monitoring_broken_alerted: bool = False


def load_state() -> State:
    if STATE_FILE.exists():
        return State(**json.loads(STATE_FILE.read_text(encoding="utf-8")))
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


def send_email(subject: str, message: str) -> None:
    host = os.environ.get("SMTP_HOST")
    port = os.environ.get("SMTP_PORT")
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    to_addr = os.environ.get("EMAIL_TO")
    if not all([host, port, user, password, to_addr]):
        return

    msg = MIMEText(message, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr

    with smtplib.SMTP(host, int(port), timeout=15) as server:
        server.starttls()
        server.login(user, password)
        server.sendmail(user, [to_addr], msg.as_string())


def notify_all(subject: str, message: str) -> None:
    send_telegram(message)
    send_email(subject, message)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

    threshold = int(os.environ.get("PRICE_THRESHOLD", DEFAULT_THRESHOLD))
    state = load_state()

    try:
        ban_ra, mua_vao = fetch_prices()
    except Exception as exc:
        print(f"[error] Failed to fetch/parse price: {exc}", file=sys.stderr)
        if not state.monitoring_broken_alerted:
            notify_all(
                "Silver price monitor broken",
                f"Could not read the price for {PRODUCT_LABEL} from {URL}.\n"
                f"Error: {exc}\nThe page layout may have changed — check the scraper.",
            )
            state.monitoring_broken_alerted = True
            save_state(state)
        raise

    print(f"{PRODUCT_LABEL}: bán ra={ban_ra:,} mua vào={mua_vao:,} (threshold={threshold:,})")

    # Recovered from a previous scrape failure.
    state.monitoring_broken_alerted = False

    crossed_up = mua_vao > threshold and not state.alerted
    if crossed_up:
        notify_all(
            f"Silver price alert: {PRODUCT_LABEL}",
            f"Mua vào price for {PRODUCT_LABEL} is now {mua_vao:,} VND, "
            f"above your threshold of {threshold:,} VND.\nBán ra: {ban_ra:,} VND\nSource: {URL}",
        )
        state.alerted = True
    elif mua_vao <= threshold:
        # Reset so the next time it crosses above threshold we alert again.
        state.alerted = False

    state.last_mua_vao = mua_vao
    save_state(state)


if __name__ == "__main__":
    main()
