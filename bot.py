#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx", "python-dotenv", "matplotlib"]
# ///
"""Keep one Discord message showing my live heart rate from Pulsoid."""

import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

from chart import render_chart

TICK_SECONDS = 5
OFFLINE_AFTER_SECONDS = 60
CHART_SECONDS = 60
HISTORY_SECONDS = 12 * 60 * 60
REQUIRED_CHANNEL_NAME = "fallow"
STATE_FILE = Path(__file__).with_name("state.json")
HISTORY_FILE = Path(__file__).with_name("history.db")
CHART_NAME = "heart-rate.png"
CHART_URL = f"attachment://{CHART_NAME}"

ALERT_ROLE_ID = "1569219369056346153"
ALERT_BPM = 160
ALERT_RESET_BPM = 140  # an episode ends once it drops below this
ALERT_HOLD_READINGS = 3  # consecutive readings at ALERT_BPM, so sensor spikes don't ping
ALERT_COOLDOWN_SECONDS = 30 * 60

PULSOID_URL = "https://dev.pulsoid.net/api/v1/data/heart_rate/latest"
DISCORD_URL = "https://discord.com/api/v10"
UNKNOWN_MESSAGE = 10008

RED = 0xE5484D
GREY = 0x747F8D

log = logging.getLogger("heartrate")


def render(reading: tuple[int, int] | None, now: float) -> dict:
    """Build the embed for a (bpm, measured_at_seconds) reading, or None if there is no data."""
    if reading is None:
        return {"title": "💤 Offline", "description": "no readings yet", "color": GREY}
    bpm, measured_at = reading
    if now - measured_at > OFFLINE_AFTER_SECONDS:
        return {"title": "💤 Offline", "description": f"last seen <t:{measured_at}:R>", "color": GREY}
    return {"title": f"❤️ {bpm} BPM", "description": f"updated <t:{measured_at}:R>", "color": RED}


def ping_text(bpm: int) -> str:
    return f"🚨 <@&{ALERT_ROLE_ID}> {bpm} BPM. lord have mercy"


def duration_text(seconds: int) -> str:
    minutes = round(seconds / 60)
    if minutes < 1:
        return "under a minute"
    return "1 minute" if minutes == 1 else f"{minutes} minutes"


class History:
    """The last 12 hours of readings, kept on disk so charts survive restarts."""

    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS samples (measured_at INTEGER PRIMARY KEY, bpm INTEGER NOT NULL)")

    def add(self, measured_at: int, bpm: int, now: float) -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO samples VALUES (?, ?)", (measured_at, bpm))
            self.db.execute("DELETE FROM samples WHERE measured_at < ?", (now - HISTORY_SECONDS,))

    def samples(self) -> list[tuple[int, int]]:
        return self.db.execute("SELECT measured_at, bpm FROM samples ORDER BY measured_at").fetchall()


class Bot:
    def __init__(
        self,
        pulsoid: httpx.Client,
        discord: httpx.Client,
        channel_id: str,
        state_file: Path = STATE_FILE,
        history_file: Path = HISTORY_FILE,
    ):
        self.pulsoid = pulsoid
        self.discord = discord
        self.channel_id = channel_id
        self.state_file = state_file
        self.history = History(history_file)
        state = self.load_state()
        self.message_id: str | None = state.get("message_id")
        self.last_embed: dict | None = None
        self.has_chart = False
        self.chart_at = 0.0
        self.ping_id: str | None = state.get("ping_id")
        self.pinged_at: float = state.get("pinged_at", 0)
        self.episode: dict | None = state.get("episode")  # {bpm, peak, started, last} while a ping is active
        self.streak: list[tuple[int, int]] = []  # consecutive high readings before a ping
        self.alert_checked_at: int | None = None

    def load_state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text())
        except (FileNotFoundError, ValueError):
            return {}

    def save_state(self) -> None:
        state = {
            "message_id": self.message_id,
            "ping_id": self.ping_id,
            "pinged_at": self.pinged_at,
            "episode": self.episode,
        }
        self.state_file.write_text(json.dumps(state))

    def remember(self, message_id: str | None) -> None:
        self.message_id = message_id
        self.has_chart = False
        self.save_state()

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
        """Refuse to run anywhere but a channel explicitly named for this."""
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

    def chart_due(self, embed: dict, now: float) -> bool:
        if not self.message_id or not self.has_chart or not self.last_embed:
            return True
        if embed["color"] != self.last_embed["color"]:  # just went online or offline
            return True
        return embed["color"] == RED and now - self.chart_at >= CHART_SECONDS

    def make_chart(self, now: float) -> bytes | None:
        samples = self.history.samples()
        if not samples:
            return None
        try:
            chart = render_chart(samples, now)
        except Exception:  # a broken chart shouldn't take the live number down with it
            log.exception("couldn't draw the chart")
            return None
        self.chart_at = now
        return chart

    def send(self, method: str, path: str, embed: dict, chart: bytes | None) -> httpx.Response:
        """Send the embed, uploading a new chart or reusing the one already on the message."""
        if chart:
            embed = {**embed, "image": {"url": CHART_URL}}
            payload = {"embeds": [embed], "attachments": [{"id": 0, "filename": CHART_NAME}]}
            response = self.request(
                method,
                path,
                data={"payload_json": json.dumps(payload)},
                files={"files[0]": (CHART_NAME, chart, "image/png")},
            )
        else:
            if self.has_chart:  # must stay attachment://, or Discord shows the file a second time
                embed = {**embed, "image": {"url": CHART_URL}}
            response = self.request(method, path, json={"embeds": [embed]})
        if response.is_success:
            embeds = response.json().get("embeds") or [{}]
            self.has_chart = "image" in embeds[0]
        return response

    def publish(self, embed: dict, chart: bytes | None = None) -> None:
        if self.message_id and embed == self.last_embed and chart is None:
            return
        path = f"/channels/{self.channel_id}/messages"
        if self.message_id:
            response = self.send("PATCH", f"{path}/{self.message_id}", embed, chart)
            if response.status_code == 404 and response.json().get("code") == UNKNOWN_MESSAGE:
                log.info("message is gone, sending a new one")
                self.remember(None)
            else:
                response.raise_for_status()
                self.last_embed = embed
                log.info("%s", embed["title"])
                return
        response = self.send("POST", path, embed, chart)
        response.raise_for_status()
        has_chart = self.has_chart
        self.remember(response.json()["id"])
        self.has_chart = has_chart
        self.last_embed = embed
        log.info("sent message %s: %s", self.message_id, embed["title"])

    def keep_at_bottom(self) -> None:
        """If our message is no longer the newest in the channel, drop it so a new one gets sent."""
        if not self.message_id:
            return
        path = f"/channels/{self.channel_id}/messages"
        response = self.request("GET", path, params={"limit": 1})
        response.raise_for_status()
        newest = response.json()
        if newest and newest[0]["id"] == self.message_id:
            return
        deleted = self.request("DELETE", f"{path}/{self.message_id}")
        if deleted.status_code != 404:
            deleted.raise_for_status()
        log.info("no longer the newest message, moving to the bottom")
        self.remember(None)

    def send_ping(self, bpm: int, now: float) -> None:
        path = f"/channels/{self.channel_id}/messages"
        response = self.request(
            "POST",
            path,
            json={"content": ping_text(bpm), "allowed_mentions": {"roles": [ALERT_ROLE_ID]}},
        )
        response.raise_for_status()
        previous = self.ping_id
        self.ping_id = response.json()["id"]
        self.pinged_at = now
        self.episode = {
            "bpm": bpm,
            "peak": max(high for high, _ in self.streak),
            "started": self.streak[0][1],
            "last": self.streak[-1][1],
        }
        self.streak = []
        self.save_state()
        log.info("pinged at %d BPM", bpm)
        if previous:
            deleted = self.request("DELETE", f"{path}/{previous}")
            if deleted.status_code not in (204, 404):
                log.warning("couldn't delete old ping (HTTP %d)", deleted.status_code)

    def finish_episode(self) -> None:
        """Add how the episode went to its ping. Editing doesn't notify anyone again."""
        episode, self.episode = self.episode, None
        self.save_state()
        summary = f"peaked at {episode['peak']} BPM · lasted {duration_text(episode['last'] - episode['started'])}"
        response = self.request(
            "PATCH",
            f"/channels/{self.channel_id}/messages/{self.ping_id}",
            json={"content": f"{ping_text(episode['bpm'])}\n{summary}"},
        )
        if response.status_code != 404:  # someone deleted the ping, nothing to update
            response.raise_for_status()
        log.info("episode over: %s", summary)

    def update_alert(self, reading: tuple[int, int] | None, now: float) -> None:
        if reading is None or now - reading[1] > OFFLINE_AFTER_SECONDS:
            self.streak = []
            if self.episode:
                self.finish_episode()
            return
        bpm, measured_at = reading
        if measured_at == self.alert_checked_at:
            return
        self.alert_checked_at = measured_at

        if self.episode:
            self.episode["last"] = measured_at
            if bpm > self.episode["peak"]:
                self.episode["peak"] = bpm
                self.save_state()
            if bpm < ALERT_RESET_BPM:
                self.finish_episode()
            return

        self.streak = [*self.streak, (bpm, measured_at)] if bpm >= ALERT_BPM else []
        if len(self.streak) >= ALERT_HOLD_READINGS and now - self.pinged_at >= ALERT_COOLDOWN_SECONDS:
            self.send_ping(bpm, now)

    def tick(self) -> None:
        now = time.time()
        reading = self.read_heart_rate()
        if reading:
            bpm, measured_at = reading
            self.history.add(measured_at, bpm, now)
        try:
            self.update_alert(reading, now)
        except (httpx.HTTPError, ValueError, KeyError) as error:  # a failed ping shouldn't stop the live number
            log.warning("alert failed: %r", error)
        self.keep_at_bottom()
        embed = render(reading, now)
        self.publish(embed, self.make_chart(now) if self.chart_due(embed, now) else None)

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
