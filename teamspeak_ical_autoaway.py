"""Set yourself away on TeamSpeak while your calendar says you're in a meeting.

Reads ICS calendar links from a config file, sleeps until the next meeting,
sets away through the TeamSpeak client, sleeps until the meeting ends and
clears away again. Calendars are re-fetched every few hours and the away state
reconciled against whatever the schedule says at that point.

Away is only cleared where this tool set it, so a manual away survives a
meeting untouched.
"""

import argparse
import base64
import json
import os
import signal
import socket
import struct
import sys
import time
import tomllib
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import icalendar
import recurring_ical_events

__version__ = "0.1.0"

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "teamspeak-ical-autoaway"
DEFAULT_CONFIG = CONFIG_DIR / "config.toml"

# Retry spacing after a failed fetch or a failed TeamSpeak call.
RETRY = timedelta(minutes=15)
TS_RETRY = timedelta(minutes=1)

# How far ahead to expand recurring events. Anything past this is picked up by
# a later refresh.
LOOKAHEAD = timedelta(days=2)

# Sleep in chunks so a suspend/resume can't overshoot a meeting boundary by
# more than this.
MAX_NAP = 600


def log(msg):
    print(msg, flush=True)


def now():
    return datetime.now(timezone.utc)


def fmt(dt):
    return dt.astimezone().strftime("%a %H:%M")


# --- config ------------------------------------------------------------------


class Config:
    def __init__(self, path):
        with open(path, "rb") as f:
            raw = tomllib.load(f)
        self.path = path
        self.calendars = raw.get("calendars", [])
        self.message = raw.get("message", "In a meeting")
        self.email = raw.get("email", "").strip().lower()
        self.refresh = timedelta(hours=float(raw.get("refresh_hours", 6)))

        ts3 = raw.get("teamspeak3", {})
        self.ts3_enabled = bool(ts3.get("enabled", "teamspeak3" in raw))
        self.ts3_host = ts3.get("host", "127.0.0.1")
        self.ts3_port = int(ts3.get("port", 25639))
        self.ts3_ini = Path(ts3.get("api_key_file", "~/.ts3client/clientquery.ini")).expanduser()

        ts6 = raw.get("teamspeak6", {})
        self.ts6_enabled = bool(ts6.get("enabled", "teamspeak6" in raw))
        self.ts6_host = ts6.get("host", "127.0.0.1")
        self.ts6_port = int(ts6.get("port", 5899))
        self.ts6_away_button = ts6.get("away_button", "away")
        self.ts6_online_button = ts6.get("online_button", self.ts6_away_button)
        self.ts6_key_file = Path(ts6.get("api_key_file", CONFIG_DIR / "teamspeak6.apikey")).expanduser()

        if not (self.ts3_enabled or self.ts6_enabled):
            raise ValueError(f"{path}: no TeamSpeak client enabled, add a [teamspeak3] or [teamspeak6] section")


# --- calendars ---------------------------------------------------------------


def fetch(source):
    if source.startswith("webcal://"):
        source = "https://" + source[len("webcal://") :]
    if "://" not in source:
        return Path(source).expanduser().read_bytes()
    req = urllib.request.Request(source, headers={"User-Agent": f"teamspeak-ical-autoaway/{__version__}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def aware(dt):
    """Normalise what the ICS library hands back to a UTC datetime."""
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc)


def declined(event, email):
    if not email:
        return False
    attendees = event.get("ATTENDEE", [])
    if not isinstance(attendees, list):
        attendees = [attendees]
    for attendee in attendees:
        address = str(attendee).lower().removeprefix("mailto:")
        if address == email and attendee.params.get("PARTSTAT", "").upper() == "DECLINED":
            return True
    return False


def busy_events(data, start, end, email):
    """Yields (start, end, summary) for events that should count as a meeting."""
    cal = icalendar.Calendar.from_ical(data)
    for event in recurring_ical_events.of(cal).between(start, end):
        dtstart = event.get("DTSTART")
        dtend = event.get("DTEND")
        if dtstart is None or dtend is None:
            continue
        # All-day events come back as dates, not datetimes.
        if not isinstance(dtstart.dt, datetime):
            continue
        if str(event.get("STATUS", "")).upper() == "CANCELLED":
            continue
        if str(event.get("TRANSP", "")).upper() == "TRANSPARENT":
            continue
        if declined(event, email):
            continue
        yield aware(dtstart.dt), aware(dtend.dt), str(event.get("SUMMARY", "")).strip()


def build_schedule(cfg, at):
    """Fetches every calendar and returns merged (start, end, summary) blocks."""
    if not cfg.calendars:
        raise ValueError(f"{cfg.path}: 'calendars' is empty, add at least one ICS link")
    window_start = at - timedelta(days=1)
    window_end = at + LOOKAHEAD
    events = []
    for source in cfg.calendars:
        events.extend(busy_events(fetch(source), window_start, window_end, cfg.email))
    events.sort(key=lambda e: e[0])

    merged = []
    for start, end, summary in events:
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            prev_start, prev_end, prev_summary = merged[-1]
            merged[-1] = (prev_start, max(prev_end, end), prev_summary)
        else:
            merged.append((start, end, summary))
    return merged


# --- backends ----------------------------------------------------------------


class BackendError(Exception):
    pass


class TeamSpeak3:
    """Talks to the TeamSpeak 3 client's ClientQuery plugin."""

    name = "teamspeak3"

    def __init__(self, cfg):
        self.host = cfg.ts3_host
        self.port = cfg.ts3_port
        self.ini = cfg.ts3_ini

    def api_key(self):
        for line in self.ini.read_text().splitlines():
            if line.startswith("api_key="):
                return line[len("api_key=") :].strip()
        raise BackendError(f"no api_key in {self.ini}")

    @staticmethod
    def escape(value):
        return value.replace("\\", "\\\\").replace("/", "\\/").replace("|", "\\p").replace(" ", "\\s")

    def query(self, *commands):
        """Runs commands on a fresh connection and returns the reply lines.

        Waits for every command's status line before quitting, since a quit
        that races the last command can drop it.
        """
        with socket.create_connection((self.host, self.port), timeout=3) as sock:
            f = sock.makefile("rwb", buffering=0)
            f.write(f"auth apikey={self.api_key()}\n".encode())
            for cmd in commands:
                f.write(f"{cmd}\n".encode())
            lines = []
            pending = len(commands) + 1
            while pending:
                raw = f.readline()
                if not raw:
                    break
                line = raw.decode(errors="replace").strip("\r\n")
                lines.append(line)
                if line.startswith("error id="):
                    pending -= 1
                    if not line.startswith("error id=0 "):
                        f.write(b"quit\n")
                        raise BackendError(line)
            f.write(b"quit\n")
        return lines

    def connected_tabs(self):
        """Returns [(schandlerid, clid)] for every server tab that's connected."""
        tabs = []
        for line in self.query("serverconnectionhandlerlist"):
            if not line.startswith("schandlerid="):
                continue
            for part in line.split("|"):
                handler = part.split("=", 1)[1]
                try:
                    reply = self.query(f"use {handler}", "whoami")
                except BackendError:
                    continue
                clid = next(l for l in reply if l.startswith("clid=")).split()[0].split("=")[1]
                tabs.append((handler, clid))
        return tabs

    def is_away(self, handler, clid):
        reply = self.query(f"use {handler}", f"clientvariable clid={clid} client_away")
        return any("client_away=1" in line for line in reply)

    def set_away(self, message):
        """Sets away on every connected tab that isn't already away.

        Returns the tabs it touched, or None when no tab is connected so the
        caller retries.
        """
        tabs = self.connected_tabs()
        if not tabs:
            return None
        touched = []
        for handler, clid in tabs:
            if self.is_away(handler, clid):
                log(f"{self.name}: tab {handler} is already away, leaving it alone")
                continue
            self.query(f"use {handler}", f"clientupdate client_away=1 client_away_message={self.escape(message)}")
            touched.append(handler)
        return touched

    def clear_away(self, handlers):
        for handler in handlers:
            self.query(f"use {handler}", "clientupdate client_away=0 client_away_message=")


class TeamSpeak6:
    """Talks to the TeamSpeak 6 client's remote apps WebSocket.

    The API has no "set away" call. It lets a remote app send virtual key
    presses, which the user binds to client actions under Settings > Key
    Bindings. So this sends one button for "Away" and another for "Set
    Online"; the away message is whatever the client has configured.
    """

    name = "teamspeak6"
    IDENTIFIER = "com.zealsprince.teamspeak-ical-autoaway"

    def __init__(self, cfg):
        self.host = cfg.ts6_host
        self.port = cfg.ts6_port
        self.key_file = cfg.ts6_key_file
        self.away_button = cfg.ts6_away_button
        self.online_button = cfg.ts6_online_button

    # Just enough WebSocket (RFC 6455) to avoid a dependency.

    def _connect(self):
        sock = socket.create_connection((self.host, self.port), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall(
            (
                f"GET / HTTP/1.1\r\nHost: {self.host}:{self.port}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                raise BackendError("websocket handshake failed")
            resp += chunk
        head, _, rest = resp.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise BackendError(f"websocket handshake refused: {head.decode(errors='replace')}")
        return sock, rest

    @staticmethod
    def _send(sock, obj):
        data = json.dumps(obj).encode()
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            header = bytes([0x81, 0x80 | n])
        elif n < 65536:
            header = bytes([0x81, 0x80 | 126]) + struct.pack(">H", n)
        else:
            header = bytes([0x81, 0x80 | 127]) + struct.pack(">Q", n)
        sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    @staticmethod
    def _frames(sock, buf, timeout):
        """Yields decoded text frames until the timeout passes or the peer closes."""
        deadline = time.monotonic() + timeout
        sock.settimeout(1)
        while time.monotonic() < deadline:
            while len(buf) >= 2:
                opcode, length = buf[0] & 0x0F, buf[1] & 0x7F
                offset = 2
                if length == 126:
                    if len(buf) < 4:
                        break
                    length, offset = struct.unpack(">H", buf[2:4])[0], 4
                elif length == 127:
                    if len(buf) < 10:
                        break
                    length, offset = struct.unpack(">Q", buf[2:10])[0], 10
                if buf[1] & 0x80:
                    offset += 4
                if len(buf) < offset + length:
                    break
                payload, buf = buf[offset : offset + length], buf[offset + length :]
                if opcode == 8:
                    return
                if opcode == 9:
                    sock.sendall(bytes([0x8A, 0x80]) + b"\0\0\0\0")
                elif opcode == 1:
                    yield payload.decode(errors="replace")
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                return
            buf += chunk

    def _auth(self, sock, buf, timeout):
        api_key = self.key_file.read_text().strip() if self.key_file.exists() else ""
        self._send(
            sock,
            {
                "type": "auth",
                "payload": {
                    "identifier": self.IDENTIFIER,
                    "version": __version__,
                    "name": "teamspeak-ical-autoaway",
                    "description": "Sets you away during calendar meetings",
                    "content": {"apiKey": api_key},
                },
            },
        )
        for text in self._frames(sock, buf, timeout):
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                continue
            if msg.get("type") != "auth":
                continue
            if msg.get("status", {}).get("code", 0) != 0:
                raise BackendError(f"auth refused: {msg['status']}")
            new_key = msg.get("payload", {}).get("apiKey")
            if new_key and new_key != api_key:
                self.key_file.parent.mkdir(parents=True, exist_ok=True)
                self.key_file.write_text(new_key)
                self.key_file.chmod(0o600)
            return msg.get("payload", {})
        raise BackendError(
            "no auth reply, allow the app under Settings > Remote Apps > Permission Requests"
            if not api_key
            else "no auth reply from the client"
        )

    def press(self, button, auth_timeout=10):
        """Sends a full down/up key press for the given button name.

        Returns the auth payload, which carries the state before the press.
        """
        sock, buf = self._connect()
        with sock:
            payload = self._auth(sock, buf, auth_timeout)
            self._send(sock, {"type": "buttonPress", "payload": {"button": button, "state": True}})
            time.sleep(0.1)
            self._send(sock, {"type": "buttonPress", "payload": {"button": button, "state": False}})
            time.sleep(0.1)
        return payload

    def state(self):
        """Returns {connection id: own away flag} for every connected server."""
        sock, buf = self._connect()
        with sock:
            payload = self._auth(sock, buf, 10)
        return self._self_away(payload)

    @staticmethod
    def _self_away(payload):
        away = {}
        for conn in payload.get("connections", []):
            me = str(conn.get("clientId"))
            for client in conn.get("clientInfos", []):
                if str(client.get("id")) == me:
                    away[conn.get("id")] = bool(client.get("properties", {}).get("away"))
        return away

    def set_away(self, message):
        """Presses the away button unless already away.

        Returns True when it set away, [] when there was nothing to do, or None
        when no server is connected so the caller retries.
        """
        before = self.state()
        if not before:
            return None
        if all(before.values()):
            log(f"{self.name}: already away, leaving it alone")
            return []
        self.press(self.away_button)
        time.sleep(0.5)
        after = self.state()
        if not any(after.values()):
            log(f"{self.name}: pressed '{self.away_button}' but the client isn't away, is the key binding set up?")
            return []
        return True

    def clear_away(self, handle):
        if not handle:
            return
        # With a Toggle binding a press while already back would set away
        # again, so only press when the client still shows away.
        if any(self.state().values()):
            self.press(self.online_button)


def backends_for(cfg):
    backends = []
    if cfg.ts3_enabled:
        backends.append(TeamSpeak3(cfg))
    if cfg.ts6_enabled:
        backends.append(TeamSpeak6(cfg))
    return backends


# --- main loop ---------------------------------------------------------------


def format_message(cfg, block):
    start, end, summary = block
    return cfg.message.format_map({"summary": summary, "end": end.astimezone().strftime("%H:%M")})


def sleep_until(deadline):
    while True:
        remaining = (deadline - now()).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(remaining, MAX_NAP))


def clear_all(backends, away_on):
    for backend in backends:
        if backend.name in away_on:
            try:
                backend.clear_away(away_on[backend.name])
            except (OSError, BackendError) as e:
                log(f"{backend.name}: couldn't clear away: {e}")
    away_on.clear()


def run(cfg):
    backends = backends_for(cfg)
    schedule = []
    next_refresh = now()
    away_on = {}

    def stop(*_):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    try:
        while True:
            at = now()
            ts_retry_at = None

            if at >= next_refresh:
                try:
                    schedule = build_schedule(cfg, at)
                    next_refresh = at + cfg.refresh
                    upcoming = [b for b in schedule if b[1] > at]
                    if upcoming:
                        s, e, summary = upcoming[0]
                        log(f"calendars refreshed, next: {summary or '(untitled)'} {fmt(s)} to {fmt(e)}")
                    else:
                        log(f"calendars refreshed, nothing in the next {LOOKAHEAD.days} days")
                except Exception as e:
                    next_refresh = at + RETRY
                    log(f"calendar refresh failed ({e}), retrying in {RETRY}")

            current = next((b for b in schedule if b[0] <= at < b[1]), None)

            if current:
                message = format_message(cfg, current)
                for backend in backends:
                    if backend.name in away_on:
                        continue
                    try:
                        handle = backend.set_away(message)
                        if handle is None:
                            ts_retry_at = at + TS_RETRY
                        else:
                            away_on[backend.name] = handle
                            if handle:
                                log(f"{backend.name}: away until {fmt(current[1])}: {message}")
                    except (OSError, BackendError) as e:
                        log(f"{backend.name}: couldn't set away ({e}), retrying in {TS_RETRY}")
                        ts_retry_at = at + TS_RETRY
            elif away_on:
                clear_all(backends, away_on)
                log("meeting over, back")

            wake = [next_refresh]
            if current:
                wake.append(current[1])
            else:
                upcoming = next((b for b in schedule if b[0] > at), None)
                if upcoming:
                    wake.append(upcoming[0])
            if ts_retry_at:
                wake.append(ts_retry_at)
            sleep_until(min(wake))
    finally:
        clear_all(backends, away_on)


def check(cfg):
    """Fetches the calendars once and prints what would count as meetings."""
    blocks = build_schedule(cfg, now())
    if not blocks:
        print(f"nothing in the next {LOOKAHEAD.days} days")
    for start, end, summary in blocks:
        print(f"{fmt(start)} to {fmt(end)}  {summary or '(untitled)'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-c", "--config", default=DEFAULT_CONFIG, help=f"config file (default: {DEFAULT_CONFIG})")
    parser.add_argument("--check", action="store_true", help="fetch the calendars, print the upcoming meetings and exit")
    parser.add_argument(
        "--press",
        metavar="BUTTON",
        help="send a TeamSpeak 6 key press for BUTTON (away or online) so it can be bound under Settings > Key Bindings",
    )
    parser.add_argument(
        "--delay",
        metavar="SECONDS",
        type=float,
        default=0,
        help="wait before sending the key press, leaving time to focus the client and start recording a binding",
    )
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args()

    try:
        cfg = Config(args.config)
    except FileNotFoundError:
        sys.exit(f"no config at {args.config}, see config.example.toml")
    except ValueError as e:
        sys.exit(str(e))

    try:
        if args.check:
            check(cfg)
        elif args.press:
            ts6 = TeamSpeak6(cfg)
            button = {"away": ts6.away_button, "online": ts6.online_button}.get(args.press, args.press)
            if args.delay:
                print(f"sending '{button}' in {args.delay:g}s", flush=True)
                time.sleep(args.delay)
            # First run waits for the user to allow the app in the client.
            ts6.press(button, auth_timeout=120)
            print(f"sent key press '{button}'")
        else:
            run(cfg)
    except (OSError, BackendError, ValueError) as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
