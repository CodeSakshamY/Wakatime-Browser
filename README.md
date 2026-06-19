EasyEDA → Hackatime Tracker

Tracks time you spend in EasyEDA (Safari/Chrome) and sends it to Hackatime so your PCB/design work shows up in your activity stats.

Stack
Python 3
AppleScript (osascript)
macOS LaunchAgent (launchd)
Hackatime API (heartbeats)
What it does (simple)

Checks your active browser tab → detects EasyEDA → extracts project name → sends time to Hackatime.

No extension. No API hooks. Just a background script polling your browser.

Features
Works with Safari (default) and Chrome
Detects active EasyEDA tab
Pulls project name automatically
Sends Hackatime heartbeats
Deduplicates spam requests
Runs silently in background via LaunchAgent
Auto-restarts on login/crash
Setup (Mac)
1. Clone + setup folder
mkdir -p ~/tools/easyeda-tracker
cd ~/tools/easyeda-tracker

Put all files here:

easyeda_tracker.py
install.sh
com.easyeda.tracker.plist.template
2. Install
chmod +x install.sh
./install.sh

Paste:

Hackatime API key
Press Enter for defaults
3. macOS permissions

Allow Terminal → Safari access:

System Settings → Privacy & Security → Automation → Terminal → Safari ✓
4. Enable project detection (Safari)

Safari → Settings → Advanced →
✔ Show features for web developers

Run / Debug
# live logs
tail -f ~/.easyeda_tracker.log

# manual run
python3 easyeda_tracker.py --debug

# test API
python3 easyeda_tracker.py --test-connection
Install as background service
./install.sh

Runs automatically on login via LaunchAgent.

Manage service
# check status
launchctl list | grep easyeda

# restart
launchctl kickstart -k gui/$(id -u)/com.easyeda.tracker

# stop
launchctl unload ~/Library/LaunchAgents/com.easyeda.tracker.plist
Troubleshooting

Nothing showing in Hackatime

wait 2–5 min
check correct API key

401 Unauthorized

API key invalid → regenerate from Hackatime

Project name not detected

enable Safari “Web Developer features”

No logs / not running

restart LaunchAgent
Why this exists

Most time trackers ignore PCB design work.
This bridges EasyEDA → Hackatime using a dead-simple browser poller.
