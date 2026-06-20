#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
# install.sh — EasyEDA Hackatime Tracker installer for macOS
#
# What this does:
#   1. Checks Python 3.6+ is available
#   2. Installs pyobjc (needed to detect browser focus via macOS APIs)
#   3. Prompts for your Hackatime API key
#   4. Writes ~/.easyeda_tracker.ini
#   5. Installs the LaunchAgent plist for auto-start at login
#   6. Sends a test heartbeat to verify everything works
#
# Usage:
#   chmod +x install.sh
#   ./install.sh
# ═══════════════════════════════════════════════════════════════════════════════

set -euo pipefail

CYAN='\033[0;36m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ── Paths ──────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_PATH="$SCRIPT_DIR/easyeda_tracker.py"
CONFIG_PATH="$HOME/.easyeda_tracker.ini"
PLIST_TEMPLATE="$SCRIPT_DIR/com.easyeda.tracker.plist.template"
PLIST_DEST="$HOME/Library/LaunchAgents/com.easyeda.tracker.plist"

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║      EasyEDA → Hackatime Tracker — Installer        ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

# ── 1. Python check ────────────────────────────────────────────────────────────
info "Checking for Python 3..."
PYTHON=""
for candidate in python3 /usr/bin/python3 /usr/local/bin/python3 "$HOME/.pyenv/shims/python3"; do
    if command -v "$candidate" &>/dev/null; then
        VER=$("$candidate" --version 2>&1 | awk '{print $2}')
        MAJOR=$(echo "$VER" | cut -d. -f1)
        MINOR=$(echo "$VER" | cut -d. -f2)
        if [ "$MAJOR" -ge 3 ] && [ "$MINOR" -ge 6 ]; then
            PYTHON="$candidate"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    error "Python 3.6+ not found. Install from https://www.python.org or via: brew install python3"
fi
success "Python found: $PYTHON ($VER)"

# ── 1b. SSL certificate check ──────────────────────────────────────────────────
# Python.org macOS installer ships WITHOUT system SSL certs linked.
# This causes every HTTPS call to fail with CERTIFICATE_VERIFY_FAILED.
# Fix: run the bundled "Install Certificates.command", or fall back to certifi.
info "Checking SSL certificates..."
if "$PYTHON" -c "
import ssl, urllib.request
urllib.request.urlopen('https://hackatime.hackclub.com', timeout=5)
" 2>/dev/null; then
    success "SSL certificates OK"
else
    warn "SSL certificate verification failed — this is the most common install issue."
    # Try the Python.org bundled cert installer first
    PYTHON_VER=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    CERT_CMD="/Applications/Python ${PYTHON_VER}/Install Certificates.command"
    if [ -f "$CERT_CMD" ]; then
        info "Running macOS Python certificate installer: $CERT_CMD"
        bash "$CERT_CMD" >/dev/null 2>&1 && success "SSL certificates installed via Python bundle" || \
            warn "Certificate installer failed — will try certifi next"
    fi
    # Verify again; if still broken, install certifi as fallback
    if ! "$PYTHON" -c "
import ssl, urllib.request
urllib.request.urlopen('https://hackatime.hackclub.com', timeout=5)
" 2>/dev/null; then
        info "Installing certifi (SSL certificate bundle fallback)..."
        "$PYTHON" -m pip install --user --upgrade certifi 2>&1 | tail -3 && \
            success "certifi installed — SSL will work via certifi fallback in the tracker" || \
            warn "certifi install also failed. You may need to run: sudo pip3 install certifi"
    fi
fi


info "Installing pyobjc (macOS API bridge for focus detection)..."
# pyobjc is needed for macOS-native focus detection; we also use subprocess
# and standard library only, so pyobjc is optional but recommended.
# The tracker gracefully falls back to AppleScript-only mode if pyobjc absent.
if "$PYTHON" -c "import AppKit" 2>/dev/null; then
    success "pyobjc already installed"
else
    info "Installing pyobjc-framework-Cocoa..."
    "$PYTHON" -m pip install --user pyobjc-framework-Cocoa 2>&1 | tail -5 || \
        warn "pyobjc install failed (non-fatal — tracker still works via AppleScript)"
fi

# ── 3. Requests check (optional, we use urllib) ────────────────────────────────
info "Verifying standard library availability..."
"$PYTHON" -c "import urllib.request, configparser, subprocess, threading, queue, logging" \
    && success "Standard library OK (no extra packages required)"

# ── 4. Collect configuration ───────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Configuration"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# API Key
if [ -f "$CONFIG_PATH" ] && grep -q "api_key" "$CONFIG_PATH"; then
    EXISTING_KEY=$(grep "^api_key" "$CONFIG_PATH" | cut -d= -f2- | tr -d ' ')
    if [ -n "$EXISTING_KEY" ] && [ "$EXISTING_KEY" != "YOUR_HACKATIME_API_KEY_HERE" ]; then
        echo ""
        info "Existing API key found in $CONFIG_PATH"
        read -rp "  Use existing key? [Y/n]: " USE_EXISTING
        if [[ "${USE_EXISTING:-Y}" =~ ^[Yy]$ ]]; then
            API_KEY="$EXISTING_KEY"
        else
            API_KEY=""
        fi
    fi
fi

if [ -z "${API_KEY:-}" ]; then
    echo ""
    echo "  Get your Hackatime API key from:"
    echo "  https://hackatime.hackclub.com/my/settings (→ API Key section)"
    echo ""
    read -rp "  Paste your Hackatime API key: " API_KEY
    if [ -z "$API_KEY" ]; then
        error "API key cannot be empty"
    fi
fi

# Browser choice
echo ""
read -rp "  Browser [Safari/Chrome] (default: Safari): " BROWSER
BROWSER="${BROWSER:-Safari}"
if [[ "$BROWSER" != "Safari" && "$BROWSER" != "Chrome" ]]; then
    warn "Unknown browser '$BROWSER', defaulting to Safari"
    BROWSER="Safari"
fi

# Default project name
echo ""
read -rp "  Default project name (default: 'EasyEDA PCB Design'): " DEFAULT_PROJECT
DEFAULT_PROJECT="${DEFAULT_PROJECT:-EasyEDA PCB Design}"

# API URL (Hackatime vs self-hosted)
echo ""
echo "  Hackatime API URL options:"
echo "    1) https://hackatime.hackclub.com/api/hackatime/v1  (Hack Club Hackatime — default)"
echo "    2) https://wakatime.com/api/v1                      (WakaTime directly)"
echo "    3) Custom URL"
read -rp "  Choose [1/2/3] (default: 1): " API_CHOICE
case "${API_CHOICE:-1}" in
    2) API_URL="https://wakatime.com/api/v1" ;;
    3) read -rp "  Enter custom API URL: " API_URL ;;
    *) API_URL="https://hackatime.hackclub.com/api/hackatime/v1" ;;
esac

# ── 5. Write config file ───────────────────────────────────────────────────────
info "Writing config to $CONFIG_PATH ..."
cat > "$CONFIG_PATH" <<EOF
[tracker]
api_key = $API_KEY
api_url = $API_URL
browser = $BROWSER
default_project = $DEFAULT_PROJECT

; Timing (seconds)
poll_interval = 5
heartbeat_interval = 30
idle_timeout = 120
dedup_window = 30

; Logging
log_file = ~/.easyeda_tracker.log
log_level = INFO

; Network
retry_max = 5
retry_base_delay = 2

; Entity prefix used in heartbeat 'entity' field
entity_prefix = easyeda://
EOF
success "Config written to $CONFIG_PATH"

# ── 6. Test connection ─────────────────────────────────────────────────────────
echo ""
info "Testing Hackatime connection..."
if "$PYTHON" "$SCRIPT_PATH" --config "$CONFIG_PATH" --test-connection; then
    success "Connection test passed!"
else
    warn "Connection test failed. Double-check your API key and URL."
    warn "You can re-test later with: python3 $SCRIPT_PATH --test-connection"
fi

# ── 7. Test heartbeat ──────────────────────────────────────────────────────────
echo ""
info "Sending a test heartbeat..."
if "$PYTHON" "$SCRIPT_PATH" --config "$CONFIG_PATH" --send-test-heartbeat; then
    success "Test heartbeat delivered! Check your Hackatime dashboard in ~2 minutes."
else
    warn "Test heartbeat failed — check the logs at ~/.easyeda_tracker.log"
fi

# ── 8. LaunchAgent installation ────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Auto-start at login (LaunchAgent)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
read -rp "  Install LaunchAgent to run at login? [Y/n]: " INSTALL_AGENT
if [[ "${INSTALL_AGENT:-Y}" =~ ^[Yy]$ ]]; then
    mkdir -p "$HOME/Library/LaunchAgents"

    # Substitute placeholders in template
    sed \
        -e "s|__PYTHON_PATH__|$PYTHON|g" \
        -e "s|__SCRIPT_PATH__|$SCRIPT_PATH|g" \
        -e "s|__SCRIPT_DIR__|$SCRIPT_DIR|g" \
        -e "s|__CONFIG_PATH__|$CONFIG_PATH|g" \
        -e "s|__API_KEY__|$API_KEY|g" \
        -e "s|__HOME_PATH__|$HOME|g" \
        "$PLIST_TEMPLATE" > "$PLIST_DEST"

    # Unload any previous version
    launchctl unload "$PLIST_DEST" 2>/dev/null || true

    # Load the new plist
    if launchctl load "$PLIST_DEST" 2>/dev/null; then
        success "LaunchAgent installed and started: $PLIST_DEST"
        success "The tracker will now auto-start whenever you log in."
    else
        warn "launchctl load failed. Try manually:"
        warn "  launchctl load $PLIST_DEST"
    fi
else
    info "Skipping LaunchAgent. To start manually:"
    echo "  python3 $SCRIPT_PATH --config $CONFIG_PATH"
fi

# ── 9. Safari/macOS permissions reminder ──────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ⚠  macOS Permissions Required"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "  The tracker uses AppleScript to read your browser tab."
echo "  macOS will prompt for permissions the first time it runs."
echo ""
echo "  If you see 'osascript is not allowed to send keystrokes':"
echo "    System Settings → Privacy & Security → Automation"
echo "    → Allow Terminal (or your Python app) to control Safari/Chrome"
echo ""
echo "  For JavaScript injection (project name detection):"
echo "    Safari → Settings → Advanced → ✓ Show features for web developers"
echo ""

# ── Done ───────────────────────────────────────────────────────────────────────
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
success "Installation complete!"
echo ""
echo "  Manual start:   python3 $SCRIPT_PATH"
echo "  Debug mode:     python3 $SCRIPT_PATH --debug"
echo "  View logs:      tail -f ~/.easyeda_tracker.log"
echo "  Stop agent:     launchctl unload $PLIST_DEST"
echo "  Restart agent:  launchctl kickstart -k gui/\$(id -u)/com.easyeda.tracker"
echo ""
