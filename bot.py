#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx", "python-dotenv"]
# ///
"""Keep one Discord message showing my live heart rate from Pulsoid."""

import json
import logging
import os
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

TICK_SECONDS = 5
OFFLINE_AFTER_SECONDS = 60
REQUIRED_CHANNEL_NAME = "fallow"
STATE_FILE = Path(__file__).with_name("state.json")

PULSOID_URL = "https://dev.pulsoid.net/api/v1/data/heart_rate/latest"
DISCORD_URL = "https://discord.com/api/v10"
UNKNOWN_MESSAGE = 10008

RED = 0xE5484D
GREY = 0x747F8D

DISCORD_EPOCH_MS = 1420070400000
BULK_DELETE_MAX_AGE_SECONDS = 13 * 24 * 60 * 60  # Discord's hard limit is 14 days
MAX_SINGLE_DELETES_PER_TICK = 5

log = logging.getLogger("heartrate")


def render(reading: tuple[int, int] | None, now: float) -> dict:
    """Build the embed for a (bpm, measured_at_seconds) reading, or None if there is no data."""
    if reading is None:
        return {"title": "💤 Offline", "description": "no readings yet", "color": GREY}
    bpm, measured_at = reading
    if now - measured_at > OFFLINE_AFTER_SECONDS:
        return {"title": "💤 Offline", "description": f"last seen <t:{measured_at}:R>", "color": GREY}
    return {"title": f"❤️ {bpm} BPM", "description": f"updated <t:{measured_at}:R>", "color": RED}


def message_age_seconds(message_id: str, now: float) -> float:
    created_ms = (int(message_id) >> 22) + DISCORD_EPOCH_MS
    return now - created_ms / 1000


class Bot:
    def __init__(self, pulsoid: httpx.Client, discord: httpx.Client, channel_id: str, state_file: Path = STATE_FILE):
        self.pulsoid = pulsoid
        self.discord = discord
        self.channel_id = channel_id
        self.state_file = state_file
        self.message_id = self.load_message_id()
        self.last_embed: dict | None = None
        self.undeletable: set[str] = set()

    def load_message_id(self) -> str | None:
        try:
            return json.loads(self.state_file.read_text())["message_id"]
        except (FileNotFoundError, ValueError, KeyError):
            return None

    def remember(self, message_id: str | None) -> None:
        self.message_id = message_id
        self.state_file.write_text(json.dumps({"message_id": message_id}))

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Call Discord, waiting out any 429s."""
        for _ in range(5):
            response = self.discord.request(method, path, **kwargs)
            if response.status_code != 429:
                return response
            wait = float(response.headers.get("retry-after", 5))
            log.warning("rate limited on %s %s, waiting %.1fs", method, path, wait)
            time.sleep(wait)
        response.raise_for_status()
        return response

    def check_channel(self) -> None:
        """Refuse to run anywhere but a channel explicitly named for this, since it deletes messages."""
        response = self.request("GET", f"/channels/{self.channel_id}")
        response.raise_for_status()
        name = response.json().get("name") or ""
        if REQUIRED_CHANNEL_NAME not in name.lower():
            sys.exit(f'Refusing to run: channel "{name}" does not have "{REQUIRED_CHANNEL_NAME}" in its name.')
        log.info("running in #%s", name)

    def read_heart_rate(self) -> tuple[int, int] | None:
        response = self.pulsoid.get(PULSOID_URL)
        if response.status_code == 412:  # account has no heart rate data at all
            return None
        response.raise_for_status()
        body = response.json()
        return body["data"]["heart_rate"], body["measured_at"] // 1000

    def publish(self, embed: dict) -> None:
        if self.message_id and embed == self.last_embed:
            return
        path = f"/channels/{self.channel_id}/messages"
        if self.message_id:
            response = self.request("PATCH", f"{path}/{self.message_id}", json={"embeds": [embed]})
            if response.status_code == 404 and response.json().get("code") == UNKNOWN_MESSAGE:
                log.info("message is gone, sending a new one")
                self.remember(None)
            else:
                response.raise_for_status()
                self.last_embed = embed
                log.info("%s", embed["title"])
                return
        response = self.request("POST", path, json={"embeds": [embed]})
        response.raise_for_status()
        self.remember(response.json()["id"])
        self.last_embed = embed
        log.info("sent message %s: %s", self.message_id, embed["title"])

    def clean(self, now: float) -> None:
        """Delete everything in the channel except our message, a batch at a time."""
        if not self.message_id:
            return
        path = f"/channels/{self.channel_id}/messages"
        response = self.request("GET", path, params={"limit": 100})
        response.raise_for_status()
        ids = [message["id"] for message in response.json()]

        # An unchanged embed is never PATCHed, so this is how we notice a deletion while offline.
        if self.message_id not in ids and len(ids) < 100:
            log.info("message is gone, will send a new one")
            self.remember(None)
            self.last_embed = None

        others = [i for i in ids if i != self.message_id and i not in self.undeletable]
        recent = [i for i in others if message_age_seconds(i, now) < BULK_DELETE_MAX_AGE_SECONDS]
        old = [i for i in others if i not in recent]

        if len(recent) >= 2:
            bulk = self.request("POST", f"{path}/bulk-delete", json={"messages": recent})
            bulk.raise_for_status()
            log.info("deleted %d messages", len(recent))
            singles = old
        else:
            singles = recent + old

        for message_id in singles[:MAX_SINGLE_DELETES_PER_TICK]:
            deleted = self.request("DELETE", f"{path}/{message_id}")
            if deleted.status_code not in (204, 404):
                log.warning("can't delete %s (HTTP %d), skipping it", message_id, deleted.status_code)
                self.undeletable.add(message_id)
            else:
                log.info("deleted message %s", message_id)

    def tick(self) -> None:
        now = time.time()
        self.publish(render(self.read_heart_rate(), now))
        self.clean(now)

    def run(self) -> None:
        self.check_channel()
        while True:
            started = time.monotonic()
            try:
                self.tick()
            except (httpx.HTTPError, ValueError, KeyError) as error:
                log.warning("tick failed, will retry: %r", error)
            time.sleep(max(0, TICK_SECONDS - (time.monotonic() - started)))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    load_dotenv(Path(__file__).with_name(".env"))

    names = ["PULSOID_TOKEN", "DISCORD_TOKEN", "DISCORD_CHANNEL_ID"]
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        sys.exit(f"Missing from .env: {', '.join(missing)}")

    pulsoid = httpx.Client(headers={"Authorization": f"Bearer {os.environ['PULSOID_TOKEN']}"}, timeout=10)
    discord = httpx.Client(
        base_url=DISCORD_URL,
        headers={
            "Authorization": f"Bot {os.environ['DISCORD_TOKEN']}",
            "User-Agent": "DiscordBot (heartrate-bot, 1.0)",
        },
        timeout=10,
    )
    try:
        Bot(pulsoid, discord, os.environ["DISCORD_CHANNEL_ID"]).run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
