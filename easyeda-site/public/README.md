# EasyEDA → Hackatime Tracker — Setup Guide

Tracks time you spend in EasyEDA (in Safari) and sends it to Hackatime so it
shows up in your coding stats.

---

## Before you start

You need:
- A Mac running macOS (any version from Mojave 2018 onwards)
- Safari (default) or Chrome
- A Hackatime account at [hackatime.hackclub.com](https://hackatime.hackclub.com)
- A fresh Hackatime API key (see Step 1)

> **⚠️ If you shared your API key anywhere** (chat, GitHub, etc.) — regenerate
> it first at Hackatime Settings before continuing.

---

## Step 1 — Get a Hackatime API key

1. Go to [https://hackatime.hackclub.com](https://hackatime.hackclub.com) and sign in
2. Click your avatar (top right) → **Settings**
3. Find the **API Key** section
4. Click **Regenerate** if you need a fresh one, then copy it

It looks like: `32d7f043-3dae-4880-8e2f-2dca26149b6d`

Keep it somewhere safe — you'll need it in Step 3.

---

## Step 2 — Put the files in a permanent folder

Open Terminal and run:

```bash
mkdir -p ~/tools/easyeda-tracker
```

Then move all four downloaded files into that folder:

```
~/tools/easyeda-tracker/
├── easyeda_tracker.py
├── install.sh
├── com.easyeda.tracker.plist.template
└── README.md
```

You can also drag them there in Finder — just make sure they all end up in the
same folder, and **don't move them after installing**.

---

## Step 3 — Run the installer

In Terminal:

```bash
cd ~/tools/easyeda-tracker
chmod +x install.sh
./install.sh
```

The installer will ask you three questions — just press Enter to accept the
defaults for questions 2 and 3:

```
Paste your Hackatime API key:   ← paste your key here
Browser [Safari/Chrome]:        ← press Enter (defaults to Safari)
Default project name:           ← press Enter (defaults to "EasyEDA PCB Design")
Choose API URL [1/2/3]:         ← press Enter (defaults to Hackatime)
Install LaunchAgent? [Y/n]:     ← press Enter (installs auto-start)
```

At the end you should see:
```
✓ Connection test passed!
✓ Test heartbeat delivered!
✓ Installation complete!
```

If the connection test fails, double-check your API key and try again.

---

## Step 4 — Allow macOS permissions

The tracker uses AppleScript to read your Safari tab URL. macOS will block
this until you grant permission.

**The first time you run it**, a dialog will pop up asking if Terminal can
control Safari. Click **OK**.

If no dialog appears and things aren't working:

1. Open **System Settings**
2. Go to **Privacy & Security → Automation**
3. Find **Terminal** in the list
4. Make sure **Safari** has a checkmark ✓ next to it

---

## Step 5 — Enable JavaScript in Safari (for project name detection)

This lets the tracker read your EasyEDA project name from the page, so
each project shows up separately in Hackatime instead of all under one name.

1. Open **Safari**
2. Go to **Safari → Settings** (or press ⌘,)
3. Click the **Advanced** tab
4. Check **"Show features for web developers"**

---

## Step 6 — Verify it's working

1. Open Safari and go to [easyeda.com](https://easyeda.com) or [pro.easyeda.com](https://pro.easyeda.com)
2. Open a project and work on it for a minute
3. Check your Hackatime dashboard — activity should appear within 2–5 minutes

To watch the tracker live in Terminal:

```bash
tail -f ~/.easyeda_tracker.log
```

You'll see lines like:
```
2025-06-19 14:32:05 [INFO] ✓ Connected to Hackatime as user: yourname
2025-06-19 14:32:10 [INFO] ⏺  Queued heartbeat | project='My PCB Board' | entity='easyeda://My PCB Board'
2025-06-19 14:32:40 [INFO] ✓ Sent 2 heartbeat(s) successfully (HTTP 201)
```

---

## That's it

The tracker now runs automatically every time you log in to your Mac.
You don't need to do anything else — just open EasyEDA in Safari and work.

---

## Useful commands

```bash
# Check if the tracker is running
launchctl list | grep easyeda

# Stop the tracker
launchctl unload ~/Library/LaunchAgents/com.easyeda.tracker.plist

# Start it again
launchctl load ~/Library/LaunchAgents/com.easyeda.tracker.plist

# Restart it
launchctl kickstart -k gui/$(id -u)/com.easyeda.tracker

# Watch live logs
tail -f ~/.easyeda_tracker.log

# Run manually (useful for debugging)
python3 ~/tools/easyeda-tracker/easyeda_tracker.py --debug

# Test your API key
python3 ~/tools/easyeda-tracker/easyeda_tracker.py --test-connection
```

---

## Update your API key

If you need to change your API key (e.g. after regenerating it):

```bash
nano ~/.easyeda_tracker.ini
```

Find the line `api_key = ...` and replace the value, then save (Ctrl+O, Enter, Ctrl+X).

Then restart the tracker:

```bash
launchctl kickstart -k gui/$(id -u)/com.easyeda.tracker
```

---

## Troubleshooting

**"osascript not allowed" error in logs**
→ Go to System Settings → Privacy & Security → Automation → Terminal → Safari ✓

**Heartbeats sent but nothing showing in Hackatime**
→ Wait 5 minutes. Check you're looking at the right Hackatime account.
→ Run `python3 ~/tools/easyeda-tracker/easyeda_tracker.py --test-connection`

**Project always shows as "EasyEDA PCB Design" (not your actual project name)**
→ Enable "Show features for web developers" in Safari Advanced settings (Step 5)

**401 Unauthorized in logs**
→ Your API key is wrong or expired. Get a new one from Hackatime Settings.

**Tracker not starting at login**
→ Run `launchctl load ~/Library/LaunchAgents/com.easyeda.tracker.plist` manually,
  then check `~/.easyeda_tracker_stderr.log` for errors.
