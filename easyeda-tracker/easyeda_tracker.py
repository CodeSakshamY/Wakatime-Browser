#!/usr/bin/env python3
"""
EasyEDA → Hackatime Tracker
============================
Detects active EasyEDA tabs in Safari (or Chrome) and sends
WakaTime-compatible heartbeats to Hackatime so EasyEDA PCB work
shows up in your coding statistics.

Architecture:
  - Poll Safari/Chrome via AppleScript every POLL_INTERVAL seconds
  - Check macOS focus/frontmost state before polling
  - Extract EasyEDA project name from page title
  - Queue heartbeats; flush to Hackatime API every HEARTBEAT_INTERVAL seconds
  - Deduplicate heartbeats; skip if identical within the dedup window
  - Robust retry with exponential backoff on network errors
  - Log to file + stdout with configurable verbosity

Usage:
  python3 easyeda_tracker.py [--config /path/to/config.ini] [--debug]
"""

import argparse
import base64
import configparser
import hashlib
import json
import logging
import logging.handlers
import os
import queue
import re
import signal
import ssl
import subprocess
import sys
import time
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib import request, error as urllib_error

# ──────────────────────────────────────────────────────────────────────────────
# Constants & defaults
# ──────────────────────────────────────────────────────────────────────────────

VERSION = "1.0.0"
USER_AGENT = f"easyeda-hackatime-tracker/{VERSION} (macOS) Python/3"

DEFAULT_CONFIG = {
    "api_key": "",
    # Hackatime WakaTime-compat endpoint
    "api_url": "https://hackatime.hackclub.com/api/hackatime/v1",
    "browser": "Safari",               # Safari or Chrome
    "poll_interval": "5",              # seconds between Safari polls
    "heartbeat_interval": "30",        # seconds between API sends
    "idle_timeout": "120",             # seconds without EasyEDA focus → stop
    "dedup_window": "30",              # seconds: skip identical heartbeat
    "default_project": "EasyEDA PCB Design",
    "log_file": "~/.easyeda_tracker.log",
    "log_level": "INFO",
    "retry_max": "5",
    "retry_base_delay": "2",           # seconds (doubles each retry)
    "entity_prefix": "easyeda://",
}

EASYEDA_DOMAINS = {
    "easyeda.com",
    "pro.easyeda.com",
    "oshwhub.com",
}

# ──────────────────────────────────────────────────────────────────────────────
# AppleScript helpers
# ──────────────────────────────────────────────────────────────────────────────

SAFARI_SCRIPT = """\
tell application "System Events"
    set safariRunning to (name of processes) contains "Safari"
    if safariRunning is false then
        return "NOTRUNNING"
    end if
    set frontApp to name of first process whose frontmost is true
    if frontApp is not "Safari" then
        return "NOTFRONT:" & frontApp
    end if
end tell
tell application "Safari"
    try
        set theURL to URL of current tab of front window
        set theTitle to name of current tab of front window
        return theURL & "|||" & theTitle
    on error
        return "ERROR"
    end try
end tell
"""

CHROME_SCRIPT = """\
tell application "System Events"
    set chromeRunning to (name of processes) contains "Google Chrome"
    if chromeRunning is false then
        return "NOTRUNNING"
    end if
    set frontApp to name of first process whose frontmost is true
    if frontApp is not "Google Chrome" then
        return "NOTFRONT:" & frontApp
    end if
end tell
tell application "Google Chrome"
    try
        set theURL to URL of active tab of front window
        set theTitle to title of active tab of front window
        return theURL & "|||" & theTitle
    on error
        return "ERROR"
    end try
end tell
"""

# Safari: execute JS in current tab to get the EasyEDA project name from the DOM
# This reads the project title that EasyEDA displays in the browser tab / page header
SAFARI_PROJECT_SCRIPT = """\
tell application "Safari"
    try
        set jsResult to do JavaScript "
            (function() {
                // EasyEDA Standard: project name in .project-name or .file-title
                var el = document.querySelector('.project-name, .file-title, .schematic-title');
                if (el) return el.innerText.trim();
                // EasyEDA Pro: editor title bar
                var pro = document.querySelector('.editor-title, .project-title-text');
                if (pro) return pro.innerText.trim();
                // Fallback: page <title> minus ' - EasyEDA'
                var t = document.title.replace(/ [-|] EasyEDA.*$/i, '').trim();
                return t || '';
            })()
        " in current tab of front window
        return jsResult
    on error
        return ""
    end try
end tell
"""

CHROME_PROJECT_SCRIPT = """\
tell application "Google Chrome"
    try
        set jsResult to execute active tab of front window javascript "
            (function() {
                var el = document.querySelector('.project-name, .file-title, .schematic-title');
                if (el) return el.innerText.trim();
                var pro = document.querySelector('.editor-title, .project-title-text');
                if (pro) return pro.innerText.trim();
                var t = document.title.replace(/ [-|] EasyEDA.*$/i, '').trim();
                return t || '';
            })()
        "
        return jsResult
    on error
        return ""
    end try
end tell
"""


def run_applescript(script: str, timeout: int = 5) -> Optional[str]:
    """Run an AppleScript and return stdout, or None on failure."""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        # Log stderr so permission errors ("osascript not allowed") are visible
        err = result.stderr.strip()
        if err:
            logging.getLogger("applescript").debug("osascript failed (rc=%d): %s", result.returncode, err)
        return None
    except subprocess.TimeoutExpired:
        logging.getLogger("applescript").debug("osascript timed out after %ds", timeout)
        return None
    except Exception as e:
        logging.getLogger("applescript").debug("osascript exception: %s", e)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Browser state detection
# ──────────────────────────────────────────────────────────────────────────────

class BrowserState:
    """Result of a single browser poll."""
    __slots__ = ("active", "url", "title", "not_running", "not_front")

    def __init__(self):
        self.active = False
        self.url = ""
        self.title = ""
        self.not_running = False
        self.not_front = False


def poll_browser(browser: str) -> BrowserState:
    state = BrowserState()
    script = SAFARI_SCRIPT if browser == "Safari" else CHROME_SCRIPT
    raw = run_applescript(script)

    if raw is None or raw == "ERROR":
        return state
    if raw == "NOTRUNNING":
        state.not_running = True
        return state
    if raw.startswith("NOTFRONT:"):
        state.not_front = True
        return state

    if "|||" not in raw:
        return state

    url, title = raw.split("|||", 1)
    url = url.strip()
    title = title.strip()

    # Check if URL is EasyEDA
    domain = url.split("/")[2] if url.startswith("http") else ""
    domain = domain.lower().lstrip("www.")
    if any(domain == d or domain.endswith("." + d) for d in EASYEDA_DOMAINS):
        state.active = True
        state.url = url
        state.title = title

    return state


def get_project_name_from_dom(browser: str, default: str) -> str:
    """Try JS injection to get EasyEDA project name; fall back to title parse."""
    script = SAFARI_PROJECT_SCRIPT if browser == "Safari" else CHROME_PROJECT_SCRIPT
    name = run_applescript(script, timeout=4)
    if name and len(name) > 1 and name not in ("undefined", "null"):
        # Sanitise: strip surrounding quotes that AppleScript sometimes adds
        name = name.strip('"').strip("'").strip()
        if name:
            return name
    return default


def extract_project_from_title(title: str, default: str) -> str:
    """
    Parse EasyEDA project name from page title.
    Typical formats:
      "MyProject - EasyEDA"
      "EasyEDA - MyProject"
      "MyProject | EasyEDA"
      "EasyEDA Pro - MyProject"
      "MyProject - EasyEDA Pro"
    Uses regex so both orderings ("X - EasyEDA" and "EasyEDA - X") are handled.
    """
    title = title.strip()
    patterns = [
        (r'^(.+?)\s*-\s*EasyEDA\s+Pro\b', 1),   # 'MyProject - EasyEDA Pro'
        (r'^EasyEDA\s+Pro\s*-\s*(.+)', 1),        # 'EasyEDA Pro - MyProject'
        (r'^(.+?)\s*-\s*EasyEDA\b', 1),           # 'MyProject - EasyEDA'
        (r'^EasyEDA\s*-\s*(.+)', 1),              # 'EasyEDA - MyProject'
        (r'^(.+?)\s*\|\s*EasyEDA', 1),            # 'MyProject | EasyEDA'
        (r'^EasyEDA\s*\|\s*(.+)', 1),             # 'EasyEDA | MyProject'
    ]
    for pat, grp in patterns:
        m = re.match(pat, title, re.IGNORECASE)
        if m:
            candidate = m.group(grp).strip()
            if candidate and candidate.lower() not in ("easyeda", "easyeda pro", ""):
                return candidate
    # Fallback: strip any trailing '- EasyEDA...' suffix
    cleaned = re.sub(r'\s*[-|]\s*EasyEDA.*$', '', title, flags=re.IGNORECASE).strip()
    return cleaned if cleaned else default


# ──────────────────────────────────────────────────────────────────────────────
# Heartbeat data structure
# ──────────────────────────────────────────────────────────────────────────────

class Heartbeat:
    def __init__(self, project: str, entity: str, timestamp: float):
        self.project = project
        self.entity = entity
        self.timestamp = timestamp
        self.id = str(uuid.uuid4())

    def to_dict(self) -> dict:
        return {
            "type": "file",
            "category": "designing",     # 'designing' maps to PCB / CAD work
            "time": self.timestamp,
            "entity": self.entity,
            "project": self.project,
            "language": "EasyEDA",
            "is_write": False,
        }

    def dedup_key(self) -> str:
        return f"{self.project}::{self.entity}"


# ──────────────────────────────────────────────────────────────────────────────
# Hackatime API client
# ──────────────────────────────────────────────────────────────────────────────

class HackatimeClient:
    def __init__(self, api_key: str, api_url: str,
                 retry_max: int = 5, retry_base_delay: float = 2.0,
                 logger: logging.Logger = None):
        self.api_key = api_key
        # Ensure no trailing slash, add heartbeats path
        self.api_url = api_url.rstrip("/")
        self.heartbeat_url = f"{self.api_url}/users/current/heartbeats.bulk"
        self.retry_max = retry_max
        self.retry_base_delay = retry_base_delay
        self.log = logger or logging.getLogger("hackatime_client")

    def _auth_header(self) -> str:
        """Hackatime (Wakapi-compat) accepts Bearer token or Basic base64."""
        return f"Bearer {self.api_key}"

    def _ssl_context(self) -> ssl.SSLContext:
        """
        Return an SSL context that works on macOS Python.org installs.

        Python.org's macOS Python does NOT link to system SSL certs by default.
        Running 'Install Certificates.command' (bundled with Python) fixes it
        permanently, but as a runtime fallback we try certifi first.
        """
        try:
            import certifi
            return ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            pass
        return ssl.create_default_context()

    def send_heartbeats(self, heartbeats: list[Heartbeat]) -> bool:
        """
        POST heartbeats to Hackatime bulk endpoint.
        Returns True on success.

        Hackatime endpoint: POST /api/hackatime/v1/users/current/heartbeats.bulk
        Body: JSON array of heartbeat objects
        Auth: Authorization: Bearer <api_key>
        Success: HTTP 201 or 202
        """
        if not heartbeats:
            return True

        payload = json.dumps([h.to_dict() for h in heartbeats]).encode("utf-8")
        self.log.debug("Sending %d heartbeat(s) to %s", len(heartbeats), self.heartbeat_url)
        self.log.debug("Payload: %s", payload.decode())

        delay = self.retry_base_delay
        for attempt in range(1, self.retry_max + 1):
            try:
                req = request.Request(
                    self.heartbeat_url,
                    data=payload,
                    method="POST",
                    headers={
                        "Authorization": self._auth_header(),
                        "Content-Type": "application/json",
                        "User-Agent": USER_AGENT,
                        "Accept": "application/json",
                    },
                )
                with request.urlopen(req, timeout=10, context=self._ssl_context()) as resp:
                    status = resp.status
                    body = resp.read().decode("utf-8", errors="replace")
                    self.log.debug("API response %d: %s", status, body)
                    if status in (201, 202):
                        self.log.info(
                            "✓ Sent %d heartbeat(s) successfully (HTTP %d)",
                            len(heartbeats), status
                        )
                        return True
                    else:
                        self.log.warning(
                            "Unexpected HTTP %d from API (attempt %d/%d): %s",
                            status, attempt, self.retry_max, body
                        )

            except urllib_error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace") if e.fp else ""
                self.log.warning(
                    "HTTP error %d (attempt %d/%d): %s", e.code, attempt, self.retry_max, body
                )
                # 401 = bad API key; don't retry
                if e.code == 401:
                    self.log.error("API key rejected (HTTP 401). Check your api_key in config.")
                    return False
                # 429 = rate limited; back off longer
                if e.code == 429:
                    delay = delay * 3

            except urllib_error.URLError as e:
                self.log.warning(
                    "Network error (attempt %d/%d): %s", attempt, self.retry_max, e.reason
                )
            except Exception as e:
                self.log.warning(
                    "Unexpected error (attempt %d/%d): %s", attempt, self.retry_max, e
                )

            if attempt < self.retry_max:
                self.log.debug("Retrying in %.1fs…", delay)
                time.sleep(delay)
                delay = min(delay * 2, 60)  # cap at 60s

        self.log.error("Failed to send heartbeats after %d attempts", self.retry_max)
        return False

    def test_connection(self) -> bool:
        """
        Verify credentials by hitting the Hackatime statusbar endpoint.

        Hackatime does NOT implement the bare WakaTime /users/current endpoint
        (it returns 404). The correct probe endpoint is:
            GET /users/current/statusbar/today
        which returns 200 with today's stats when the API key is valid.
        """
        url = f"{self.api_url}/users/current/statusbar/today"
        try:
            req = request.Request(
                url,
                headers={
                    "Authorization": self._auth_header(),
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                },
            )
            with request.urlopen(req, timeout=10, context=self._ssl_context()) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read())
                    # Statusbar response has { "data": { "grand_total": {...}, ... } }
                    username = (
                        data.get("data", {}).get("username")
                        or data.get("username")
                        or "authenticated"
                    )
                    self.log.info("✓ Connected to Hackatime as user: %s", username)
                    return True
                self.log.error("Connection test failed: HTTP %d", resp.status)
                return False
        except urllib_error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace") if e.fp else ""
            self.log.error("Connection test failed: HTTP %d — %s", e.code, body)
            if e.code == 401:
                self.log.error("→ API key is invalid or expired. Check ~/.easyeda_tracker.ini")
            return False
        except Exception as e:
            self.log.error("Connection test failed: %s", e)
            return False


# ──────────────────────────────────────────────────────────────────────────────
# Main tracker
# ──────────────────────────────────────────────────────────────────────────────

class EasyEDATracker:
    def __init__(self, config: configparser.ConfigParser):
        cfg = config["tracker"]

        # Config values
        self.api_key = cfg["api_key"]
        self.api_url = cfg["api_url"]
        self.browser = cfg["browser"]
        self.poll_interval = float(cfg["poll_interval"])
        self.heartbeat_interval = float(cfg["heartbeat_interval"])
        self.idle_timeout = float(cfg["idle_timeout"])
        self.dedup_window = float(cfg["dedup_window"])
        self.default_project = cfg["default_project"]
        self.entity_prefix = cfg["entity_prefix"]
        self.retry_max = int(cfg["retry_max"])
        self.retry_base_delay = float(cfg["retry_base_delay"])

        # State
        self._pending: list[Heartbeat] = []
        self._pending_lock = threading.Lock()
        self._last_sent: dict[str, float] = {}   # dedup_key → timestamp
        self._last_easyeda_seen: float = 0.0
        self._running = False
        self._poll_count: int = 0               # for periodic alive log

        # Logging
        self.log = logging.getLogger("easyeda_tracker")

        # API client
        self.client = HackatimeClient(
            api_key=self.api_key,
            api_url=self.api_url,
            retry_max=self.retry_max,
            retry_base_delay=self.retry_base_delay,
            logger=self.log,
        )

    # ── Deduplication ──────────────────────────────────────────────────────────

    def _should_send(self, hb: Heartbeat) -> bool:
        key = hb.dedup_key()
        last = self._last_sent.get(key, 0.0)
        if hb.timestamp - last >= self.dedup_window:
            self._last_sent[key] = hb.timestamp
            return True
        self.log.debug("Dedup: skipping duplicate heartbeat for '%s'", hb.project)
        return False

    # ── Poll loop ──────────────────────────────────────────────────────────────

    def _poll_once(self):
        self._poll_count += 1
        # Log "still running" every 60 polls (~5 min at default interval) so
        # tail -f shows activity even when EasyEDA isn't open
        if self._poll_count % 60 == 0:
            self.log.info("⟳  Tracker alive | polls=%d | browser=%s", self._poll_count, self.browser)

        state = poll_browser(self.browser)
        now = time.time()

        if state.not_running:
            self.log.debug("%s is not running", self.browser)
            return
        if state.not_front:
            self.log.debug("%s is not the frontmost app", self.browser)
            return
        if not state.active:
            self.log.debug("Active tab is not EasyEDA")
            return

        self._last_easyeda_seen = now

        # Try to get real project name from DOM first, fall back to title parse
        project = "EasyEDA-PCB"
        if not project:
            project = extract_project_from_title(state.title, self.default_project)
        if not project:
            project = self.default_project

        # Build entity: easyeda://<project>/<url-path>
        url_path = state.url.split("?")[0].rstrip("/")
        entity = f"{self.entity_prefix}{project}"

        hb = Heartbeat(project=project, entity=entity, timestamp=now)

        if self._should_send(hb):
            with self._pending_lock:
                self._pending.append(hb)
            self.log.info(
                "⏺  Queued heartbeat | project='%s' | entity='%s'",
                project, entity
            )
        else:
            self.log.debug("Heartbeat queued but deduplicated for project='%s'", project)

    # ── Flush loop ─────────────────────────────────────────────────────────────

    def _flush_loop(self):
        while self._running:
            time.sleep(self.heartbeat_interval)
            self._flush()

    def _flush(self):
        with self._pending_lock:
            batch = list(self._pending)
            self._pending.clear()

        if not batch:
            self.log.debug("Flush: nothing to send")
            return

        # WakaTime/Hackatime bulk limit is 25 heartbeats
        chunks = [batch[i:i+25] for i in range(0, len(batch), 25)]
        for chunk in chunks:
            success = self.client.send_heartbeats(chunk)
            if not success:
                # Re-queue on failure so we retry next flush
                with self._pending_lock:
                    self.log.warning("Re-queuing %d heartbeat(s) after send failure", len(chunk))
                    self._pending[:0] = chunk  # prepend to retry next cycle

    # ── Main run ───────────────────────────────────────────────────────────────

    def run(self):
        self.log.info("=" * 60)
        self.log.info("EasyEDA Hackatime Tracker v%s starting", VERSION)
        self.log.info("Browser : %s", self.browser)
        self.log.info("API URL : %s", self.api_url)
        self.log.info("Poll    : every %.0fs", self.poll_interval)
        self.log.info("Flush   : every %.0fs", self.heartbeat_interval)
        self.log.info("Dedup   : %.0fs window", self.dedup_window)
        self.log.info("Idle    : stop after %.0fs", self.idle_timeout)
        self.log.info("=" * 60)

        # Verify credentials before starting
        if not self.client.test_connection():
            self.log.error("Cannot connect to Hackatime. Check api_key and api_url.")
            sys.exit(1)

        self._running = True

        # Start flush thread
        flush_thread = threading.Thread(target=self._flush_loop, daemon=True)
        flush_thread.start()

        # Handle SIGTERM / SIGINT for clean shutdown
        def _shutdown(sig, frame):
            self.log.info("Received signal %s — shutting down…", sig)
            self._running = False

        signal.signal(signal.SIGTERM, _shutdown)
        signal.signal(signal.SIGINT, _shutdown)

        # Poll loop
        last_idle_warning = 0.0
        while self._running:
            try:
                self._poll_once()
            except Exception as e:
                self.log.exception("Unexpected error in poll loop: %s", e)

            time.sleep(self.poll_interval)

            # Idle detection logging (not a hard stop — just warns)
            now = time.time()
            if self._last_easyeda_seen > 0:
                idle = now - self._last_easyeda_seen
                if idle > self.idle_timeout and now - last_idle_warning > 60:
                    self.log.info(
                        "⏸  EasyEDA not in focus for %.0fs (idle)", idle
                    )
                    last_idle_warning = now

        # Final flush on exit
        self.log.info("Performing final heartbeat flush…")
        self._flush()
        self.log.info("EasyEDA Tracker stopped.")


# ──────────────────────────────────────────────────────────────────────────────
# Config loading
# ──────────────────────────────────────────────────────────────────────────────

def load_config(config_path: str) -> configparser.ConfigParser:
    config = configparser.ConfigParser()
    # Fill defaults
    config["tracker"] = dict(DEFAULT_CONFIG)

    path = Path(config_path).expanduser()
    if path.exists():
        config.read(str(path))
        logging.getLogger().info("Loaded config from %s", path)
    else:
        logging.getLogger().warning(
            "Config file not found at %s — using defaults. "
            "Run with --create-config to generate one.", path
        )

    # Environment variable overrides (useful for secrets)
    env_key = os.environ.get("HACKATIME_API_KEY") or os.environ.get("WAKATIME_API_KEY")
    if env_key:
        config["tracker"]["api_key"] = env_key

    env_url = os.environ.get("HACKATIME_API_URL")
    if env_url:
        config["tracker"]["api_url"] = env_url

    return config


def create_default_config(path: str):
    config = configparser.ConfigParser()
    config["tracker"] = dict(DEFAULT_CONFIG)
    config["tracker"]["api_key"] = "YOUR_HACKATIME_API_KEY_HERE"

    out = Path(path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as f:
        config.write(f)
    print(f"Default config written to: {out}")
    print("Edit it to add your Hackatime API key before running.")


# ──────────────────────────────────────────────────────────────────────────────
# Logging setup
# ──────────────────────────────────────────────────────────────────────────────

def setup_logging(log_file: str, log_level: str, debug_override: bool):
    level = logging.DEBUG if debug_override else getattr(logging, log_level.upper(), logging.INFO)
    log_path = Path(log_file).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(
            str(log_path), maxBytes=5 * 1024 * 1024, backupCount=3
        ),
    ]
    logging.basicConfig(level=level, format=fmt, datefmt=datefmt, handlers=handlers)



# ──────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ──────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Track EasyEDA PCB work and send heartbeats to Hackatime"
    )
    parser.add_argument(
        "--config",
        default="~/.easyeda_tracker.ini",
        help="Path to config file (default: ~/.easyeda_tracker.ini)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--create-config",
        action="store_true",
        help="Write a default config file and exit",
    )
    parser.add_argument(
        "--test-connection",
        action="store_true",
        help="Test Hackatime API connection and exit",
    )
    parser.add_argument(
        "--send-test-heartbeat",
        action="store_true",
        help="Send a single test heartbeat to Hackatime and exit",
    )
    args = parser.parse_args()

    if args.create_config:
        create_default_config(args.config)
        return

    config = load_config(args.config)
    cfg = config["tracker"]

    setup_logging(cfg["log_file"], cfg["log_level"], args.debug)

    if args.test_connection:
        client = HackatimeClient(
            api_key=cfg["api_key"],
            api_url=cfg["api_url"],
            logger=logging.getLogger("test"),
        )
        success = client.test_connection()
        sys.exit(0 if success else 1)

    if args.send_test_heartbeat:
        client = HackatimeClient(
            api_key=cfg["api_key"],
            api_url=cfg["api_url"],
            logger=logging.getLogger("test"),
        )
        hb = Heartbeat(
            project=cfg["default_project"],
            entity=f"{cfg['entity_prefix']}Test Project",
            timestamp=time.time(),
        )
        success = client.send_heartbeats([hb])
        print("✓ Test heartbeat sent!" if success else "✗ Test heartbeat failed.")
        sys.exit(0 if success else 1)

    if not cfg["api_key"] or cfg["api_key"] == "YOUR_HACKATIME_API_KEY_HERE":
        print("ERROR: No api_key set. Edit ~/.easyeda_tracker.ini or set HACKATIME_API_KEY env var.")
        sys.exit(1)

    tracker = EasyEDATracker(config)
    tracker.run()


if __name__ == "__main__":
    main()
