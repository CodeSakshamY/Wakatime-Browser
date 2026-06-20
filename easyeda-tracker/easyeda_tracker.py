#!/usr/bin/env python3
# tracks time spent in EasyEDA (Safari or Chrome) and sends it to Hackatime
# runs as a background process, launched by the LaunchAgent at login

import argparse
import configparser
import json
import logging
import logging.handlers
import os
import re
import signal
import ssl
import subprocess
import sys
import time
import threading
import uuid
from pathlib import Path
from urllib import request, error as urllib_error


VERSION = "1.0.0"
USER_AGENT = f"easyeda-hackatime-tracker/{VERSION} (macOS) Python/3"

DEFAULT_CONFIG = {
    "api_key": "",
    "api_url": "https://hackatime.hackclub.com/api/hackatime/v1",
    "browser": "Safari",
    "poll_interval": "5",
    "heartbeat_interval": "30",
    "idle_timeout": "300",   # stop after 5 mins idle
    "dedup_window": "30",
    "default_project": "EasyEDA PCB Design",
    "log_file": "~/.easyeda_tracker.log",
    "log_level": "INFO",
    "retry_max": "5",
    "retry_base_delay": "2",
    "entity_prefix": "easyeda://",
}

EASYEDA_DOMAINS = {"easyeda.com", "pro.easyeda.com", "oshwhub.com"}

log = logging.getLogger("easyeda_tracker")


# AppleScript strings 

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

# JS selectors for EasyEDA Standard and Pro.
# Standard uses #editorTitle and breadcrumbs.
# Pro uses .editor-title and .lc-header-title.
# Only works if Safari has "Show features for web developers" turned on.
SAFARI_PROJECT_SCRIPT = """\
tell application "Safari"
    try
        set jsResult to do JavaScript "(function() { var pro = document.querySelector('.editor-title, .project-title-text, .lc-header-title'); if (pro && pro.innerText.trim()) return pro.innerText.trim(); var std = document.querySelector('#editorTitle, .editorTitle, .project-name, [class*=projectName]'); if (std && std.innerText.trim()) return std.innerText.trim(); var crumb = document.querySelector('.ant-breadcrumb li:last-child, .breadcrumb-item.active'); if (crumb && crumb.innerText.trim()) return crumb.innerText.trim(); return ''; })()" in current tab of front window
        return jsResult
    on error
        return ""
    end try
end tell
"""

CHROME_PROJECT_SCRIPT = """\
tell application "Google Chrome"
    try
        set jsResult to execute active tab of front window javascript "(function() { var pro = document.querySelector('.editor-title, .project-title-text, .lc-header-title'); if (pro && pro.innerText.trim()) return pro.innerText.trim(); var std = document.querySelector('#editorTitle, .editorTitle, .project-name, [class*=projectName]'); if (std && std.innerText.trim()) return std.innerText.trim(); var crumb = document.querySelector('.ant-breadcrumb li:last-child, .breadcrumb-item.active'); if (crumb && crumb.innerText.trim()) return crumb.innerText.trim(); return ''; })()"
        return jsResult
    on error
        return ""
    end try
end tell
"""


def run_applescript(script, timeout=5):
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        if result.stderr.strip():
            log.debug("osascript error: %s", result.stderr.strip())
        return None
    except subprocess.TimeoutExpired:
        log.debug("osascript timed out")
        return None
    except Exception as e:
        log.debug("osascript exception: %s", e)
        return None


def poll_browser(browser):
    # returns (active, url, title)
    # active is False if browser isn't running, isn't front, or tab isn't EasyEDA
    script = SAFARI_SCRIPT if browser == "Safari" else CHROME_SCRIPT
    raw = run_applescript(script)

    if not raw or raw == "ERROR":
        return False, "", ""
    if raw == "NOTRUNNING":
        log.debug("%s is not running", browser)
        return False, "", ""
    if raw.startswith("NOTFRONT:"):
        log.debug("%s is not the frontmost app", browser)
        return False, "", ""
    if "|||" not in raw:
        return False, "", ""

    url, title = raw.split("|||", 1)
    url = url.strip()
    title = title.strip()

    domain = url.split("/")[2].lower().lstrip("www.") if url.startswith("http") else ""
    on_easyeda = any(domain == d or domain.endswith("." + d) for d in EASYEDA_DOMAINS)
    if not on_easyeda:
        log.debug("tab is not EasyEDA (domain: %s)", domain)
        return False, "", ""

    return True, url, title


def get_project_from_dom(browser, fallback):
    # tries JS injection — only works if Safari dev features are on
    # falls back silently, title parsing covers it either way
    script = SAFARI_PROJECT_SCRIPT if browser == "Safari" else CHROME_PROJECT_SCRIPT
    name = run_applescript(script, timeout=4)
    if name and len(name) > 1 and name not in ("undefined", "null", ""):
        name = name.strip('"').strip("'").strip()
        if name:
            return name
    return fallback


def get_project_from_title(title, default):
    # EasyEDA standard titles: "My Board - EasyEDA" or "EasyEDA - My Board"
    # EasyEDA Pro titles:      "My Board - EasyEDA Pro" or "EasyEDA Pro - My Board"
    # also handles | separator and en-dashes (Pro sometimes uses those)
    title = title.strip()
    if not title:
        return default

    # titles like "EasyEDA" or "EasyEDA - Schematic" have no project name in them
    bare = ("easyeda", "easyeda pro", "easyeda - schematic",
            "easyeda - pcb", "easyeda - symbol")
    if title.lower() in bare:
        return ""

    patterns = [
        r'^(.+?)\s*[-–]\s*EasyEDA\s+Pro\b',
        r'^EasyEDA\s+Pro\s*[-–]\s*(.+)',
        r'^(.+?)\s*[-–]\s*EasyEDA\b',
        r'^EasyEDA\s*[-–]\s*(.+)',
        r'^(.+?)\s*\|\s*EasyEDA',
        r'^EasyEDA\s*\|\s*(.+)',
    ]
    for pat in patterns:
        m = re.match(pat, title, re.IGNORECASE)
        if m:
            candidate = m.group(1).strip()
            skip = ("easyeda", "easyeda pro", "schematic", "pcb", "symbol", "")
            if candidate and candidate.lower() not in skip:
                return candidate

    # nothing matched, strip from the last separator and see what's left
    cleaned = re.sub(r'\s*[-–|]\s*EasyEDA.*$', '', title, flags=re.IGNORECASE).strip()
    return cleaned if cleaned else default


def get_project_from_url(url):
    # last resort for EasyEDA standard — project name is sometimes in the URL path
    # e.g. easyeda.com/username/my-board-name
    try:
        path = url.split("?")[0].split("#")[0].rstrip("/")
        parts = [p for p in path.split("/") if p and p not in ("editor",)]
        if parts:
            last = parts[-1]
            # skip it if it looks like a raw ID (long hex string)
            if not re.match(r'^[a-f0-9]{8,}$', last, re.IGNORECASE):
                return last.replace("-", " ").replace("_", " ").title()
    except Exception:
        pass
    return ""


#  SSL helper 

def make_ssl_context():
    # Python.org macOS builds don't ship with system certs, so HTTPS breaks.
    # certifi fixes this if installed, otherwise fall back to default.
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


#  Heartbeat 

class Heartbeat:
    def __init__(self, project, entity, timestamp):
        self.project = project
        self.entity = entity
        self.timestamp = timestamp
        self.id = str(uuid.uuid4())

    def to_dict(self):
        return {
            "type": "file",
            "category": "designing",
            "time": self.timestamp,
            "entity": self.entity,
            "project": self.project,
            "language": "EasyEDA",
            "is_write": False,
        }

    def dedup_key(self):
        return f"{self.project}::{self.entity}"


#  API client 

class HackatimeClient:
    def __init__(self, api_key, api_url, retry_max=5, retry_base_delay=2.0):
        self.api_key = api_key
        self.base_url = api_url.rstrip("/")
        self.retry_max = retry_max
        self.retry_base_delay = retry_base_delay

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }

    def send_heartbeats(self, heartbeats):
        if not heartbeats:
            return True

        url = f"{self.base_url}/users/current/heartbeats.bulk"
        payload = json.dumps([h.to_dict() for h in heartbeats]).encode("utf-8")
        log.debug("Sending %d heartbeat(s) to %s", len(heartbeats), url)

        delay = self.retry_base_delay
        for attempt in range(1, self.retry_max + 1):
            try:
                req = request.Request(url, data=payload, method="POST", headers=self._headers())
                with request.urlopen(req, timeout=10, context=make_ssl_context()) as resp:
                    body = resp.read().decode("utf-8", errors="replace")
                    log.debug("API response %d: %s", resp.status, body)
                    if resp.status in (201, 202):
                        log.info("✓ Sent %d heartbeat(s) (HTTP %d)", len(heartbeats), resp.status)
                        return True
                    log.warning("Unexpected HTTP %d (attempt %d/%d): %s",
                                resp.status, attempt, self.retry_max, body)

            except urllib_error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace") if e.fp else ""
                log.warning("HTTP %d (attempt %d/%d): %s", e.code, attempt, self.retry_max, body)
                if e.code == 401:
                    log.error("API key rejected — check api_key in ~/.easyeda_tracker.ini")
                    return False
                if e.code == 429:
                    delay = delay * 3  # rate limited, back off harder

            except urllib_error.URLError as e:
                log.warning("Network error (attempt %d/%d): %s", attempt, self.retry_max, e.reason)
            except Exception as e:
                log.warning("Unexpected error (attempt %d/%d): %s", attempt, self.retry_max, e)

            if attempt < self.retry_max:
                log.debug("Retrying in %.1fs", delay)
                time.sleep(delay)
                delay = min(delay * 2, 60)

        log.error("Giving up after %d attempts", self.retry_max)
        return False

    def test_connection(self):
        # /users/current returns 404 on Hackatime, statusbar/today works instead
        url = f"{self.base_url}/users/current/statusbar/today"
        try:
            req = request.Request(url, headers=self._headers())
            with request.urlopen(req, timeout=10, context=make_ssl_context()) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read())
                    username = (data.get("data", {}).get("username")
                                or data.get("username")
                                or "authenticated")
                    log.info("✓ Connected to Hackatime as: %s", username)
                    return True
                log.error("Connection test failed: HTTP %d", resp.status)
                return False
        except urllib_error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace") if e.fp else ""
            log.error("Connection test failed: HTTP %d — %s", e.code, body)
            if e.code == 401:
                log.error("API key is invalid or expired")
            return False
        except Exception as e:
            log.error("Connection test failed: %s", e)
            return False


#  Main tracker 

class EasyEDATracker:
    def __init__(self, config):
        cfg = config["tracker"]

        self.api_key = cfg["api_key"]
        self.api_url = cfg["api_url"]
        self.browser = cfg["browser"]
        self.poll_interval = float(cfg["poll_interval"])
        self.heartbeat_interval = float(cfg["heartbeat_interval"])
        self.idle_timeout = float(cfg["idle_timeout"])
        self.dedup_window = float(cfg["dedup_window"])
        self.default_project = cfg["default_project"]
        self.entity_prefix = cfg["entity_prefix"]

        self._pending = []
        self._pending_lock = threading.Lock()
        self._last_sent = {}   # dedup_key -> timestamp
        self._last_easyeda_seen = 0.0
        self._running = False
        self._poll_count = 0

        self.client = HackatimeClient(
            api_key=self.api_key,
            api_url=self.api_url,
            retry_max=int(cfg["retry_max"]),
            retry_base_delay=float(cfg["retry_base_delay"]),
        )

    def _should_send(self, hb):
        key = hb.dedup_key()
        last = self._last_sent.get(key, 0.0)
        if hb.timestamp - last >= self.dedup_window:
            self._last_sent[key] = hb.timestamp
            return True
        log.debug("skipping duplicate heartbeat for '%s'", hb.project)
        return False

    def _poll_once(self):
        self._poll_count += 1
        if self._poll_count % 60 == 0:
            log.info("still running | polls=%d | browser=%s", self._poll_count, self.browser)

        active, url, title = poll_browser(self.browser)
        if not active:
            return

        now = time.time()
        self._last_easyeda_seen = now

        # try title first (always available), DOM upgrades it if Safari allows JS,
        # URL is last resort for EasyEDA standard where title has no project name
        project = get_project_from_title(title, "")
        dom_name = get_project_from_dom(self.browser, "")
        if dom_name:
            project = dom_name
        if not project:
            project = get_project_from_url(url)
        if not project:
            project = self.default_project

        entity = f"{self.entity_prefix}{project}"
        hb = Heartbeat(project=project, entity=entity, timestamp=now)

        if self._should_send(hb):
            with self._pending_lock:
                self._pending.append(hb)
            log.info("⏺  queued | project='%s' | entity='%s'", project, entity)

    def _flush_loop(self):
        while self._running:
            time.sleep(self.heartbeat_interval)
            self._flush()

    def _flush(self):
        with self._pending_lock:
            batch = list(self._pending)
            self._pending.clear()

        if not batch:
            return

        # Hackatime's bulk endpoint caps at 25 per request
        chunks = [batch[i:i+25] for i in range(0, len(batch), 25)]
        for chunk in chunks:
            ok = self.client.send_heartbeats(chunk)
            if not ok:
                with self._pending_lock:
                    log.warning("re-queuing %d heartbeat(s) for next flush", len(chunk))
                    self._pending[:0] = chunk

    def run(self):
        log.info("=" * 55)
        log.info("EasyEDA Tracker v%s starting", VERSION)
        log.info("browser=%s  poll=%ss  flush=%ss", self.browser,
                 int(self.poll_interval), int(self.heartbeat_interval))
        log.info("=" * 55)

        if not self.client.test_connection():
            log.error("Can't connect to Hackatime — check api_key and api_url in config")
            sys.exit(1)

        self._running = True

        flush_thread = threading.Thread(target=self._flush_loop, daemon=True)
        flush_thread.start()

        def _shutdown(sig, frame):
            log.info("shutting down (signal %s)", sig)
            self._running = False

        signal.signal(signal.SIGTERM, _shutdown)
        signal.signal(signal.SIGINT, _shutdown)

        while self._running:
            try:
                self._poll_once()
            except Exception as e:
                log.exception("error in poll loop: %s", e)

            time.sleep(self.poll_interval)

            # stop after idle_timeout seconds without seeing EasyEDA in focus
            if self._last_easyeda_seen > 0:
                idle_secs = time.time() - self._last_easyeda_seen
                if idle_secs > self.idle_timeout:
                    log.info("EasyEDA not in focus for %.0fs — stopping", idle_secs)
                    self._running = False
                    break

        log.info("flushing before exit...")
        self._flush()
        log.info("stopped.")


#  Config 

def load_config(config_path):
    config = configparser.ConfigParser()
    config["tracker"] = dict(DEFAULT_CONFIG)

    path = Path(config_path).expanduser()
    if path.exists():
        config.read(str(path))
    else:
        logging.warning("Config not found at %s — using defaults", path)

    # env vars override config file (useful for the LaunchAgent)
    env_key = os.environ.get("HACKATIME_API_KEY") or os.environ.get("WAKATIME_API_KEY")
    if env_key:
        config["tracker"]["api_key"] = env_key

    env_url = os.environ.get("HACKATIME_API_URL")
    if env_url:
        config["tracker"]["api_url"] = env_url

    return config


def create_default_config(path):
    config = configparser.ConfigParser()
    config["tracker"] = dict(DEFAULT_CONFIG)
    config["tracker"]["api_key"] = "YOUR_HACKATIME_API_KEY_HERE"

    out = Path(path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as f:
        config.write(f)
    print(f"Config written to {out} — edit it to add your API key")


def setup_logging(log_file, log_level, debug_mode):
    level = logging.DEBUG if debug_mode else getattr(logging, log_level.upper(), logging.INFO)
    log_path = Path(log_file).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(str(log_path), maxBytes=5*1024*1024, backupCount=3),
    ]
    logging.basicConfig(level=level, format=fmt, datefmt=datefmt, handlers=handlers)


#  CLI 

def main():
    parser = argparse.ArgumentParser(description="Track EasyEDA time → Hackatime")
    parser.add_argument("--config", default="~/.easyeda_tracker.ini")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--create-config", action="store_true")
    parser.add_argument("--test-connection", action="store_true")
    parser.add_argument("--send-test-heartbeat", action="store_true")
    args = parser.parse_args()

    if args.create_config:
        create_default_config(args.config)
        return

    config = load_config(args.config)
    cfg = config["tracker"]
    setup_logging(cfg["log_file"], cfg["log_level"], args.debug)

    if args.test_connection:
        client = HackatimeClient(api_key=cfg["api_key"], api_url=cfg["api_url"])
        sys.exit(0 if client.test_connection() else 1)

    if args.send_test_heartbeat:
        client = HackatimeClient(api_key=cfg["api_key"], api_url=cfg["api_url"])
        hb = Heartbeat(
            project=cfg["default_project"],
            entity=f"{cfg['entity_prefix']}Test Project",
            timestamp=time.time(),
        )
        ok = client.send_heartbeats([hb])
        print("✓ Test heartbeat sent!" if ok else "✗ Test heartbeat failed.")
        sys.exit(0 if ok else 1)

    if not cfg["api_key"] or cfg["api_key"] == "YOUR_HACKATIME_API_KEY_HERE":
        print("ERROR: no api_key set. Edit ~/.easyeda_tracker.ini or set HACKATIME_API_KEY")
        sys.exit(1)

    EasyEDATracker(config).run()


if __name__ == "__main__":
    main()

