# XLive-Splitter

<!--
Combines the limited 4gb files into merged audio tracks and splits X32 Xlive .wav files into multitracks, allowing for custom naming templates and automatically opening them into your selected daw.

Optional badges row — build status, license, platform support. Delete
if you don't want these. If you keep the build badge, replace OWNER/REPO
with your actual GitHub path — it'll auto-update based on your Actions runs.
-->
![Build Status](https://github.com/yoyomon999/XLive-Splitter/actions/workflows/build.yml/badge.svg)
![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Windows-informational)
<img width="1433" height="760" alt="Screenshot 2026-08-03 at 12 06 34 AM" src="https://github.com/user-attachments/assets/3af1fd37-155e-4b04-a34c-2c8d2db140d1" />
<img width="1432" height="754" alt="Screenshot 2026-08-03 at 12 06 52 AM" src="https://github.com/user-attachments/assets/218f1bb2-842a-4dae-941d-de927f4ea597" />


<!--
Optional: a screenshot or short GIF of the app in use. Drag an image
into the GitHub file editor to get its URL, then paste it below.
This is often the single most useful thing in a README — people
decide whether to keep reading based on this.

-->


---

## Features

<!--
Bullet list, 4-8 items max. Lead with the thing that matters most to
a first-time reader, not the thing that was hardest to build. Keep
each bullet to one line if you can.
Example bullets to adapt:
- Splits interleaved multitrack WAV files into individually named mono tracks
- Reusable named presets (e.g. "Sunday Service") so you don't retype channel names
- Auto-opens the output folder and hands the tracks to your DAW of choice
- Works on both macOS and Windows
-->
- Split X32 Xlive .wav multitracks
- Combines 4bg files into single tracks
- Track Name Presets
- Autimatically open exported tracks into your selected DAW

---

## Download

<!--
This is the "I just want the app" section — put it near the top,
before any setup/build instructions. Link straight to your GitHub
Releases page and/or the specific latest files if you have them.
-->

Grab the latest build from the [Releases page](https://github.com/yoyomon999/XLive-Splitter/releases).

| Platform | Download | Notes |
|---|---|---|
| macOS | [Xlive Splitter Mac.zip](https://github.com/yoyomon999/XLive-Splitter/releases/tag/v1.0_Mac) | 
| Windows | [XLive_Splitter_Windows.exe](https://github.com/yoyomon999/XLive-Splitter/releases/tag/v1.0_Mac) | 

---

## Installation / First-Run Setup

<!--
Walk through exactly what someone needs to do the very first time,
step by step, per platform. Include anything non-obvious — e.g.
Gatekeeper warnings on Mac, antivirus false positives on Windows,
needing to locate their DAW manually the first time, etc.
-->

### macOS
1. Download `Xlive Splitter.app.zip` from the [Releases page](https://github.com/yoyomon999/XLive-Splitter/releases) and unzip it.
2. Drag `Xlive Splitter.app` into your **Applications** folder
3. Open XLive Splitter
4. Clear app from ***Gate Keeper*** in ***Security***

### Windows
1. Download `Xlive Splitter.exe` from the [Releases page](https://github.com/yoyomon999/XLive-Splitter/releases).
2. Open XLive Splitter 
---

## How to Use


Numbered walkthrough of an actual export, start to finish. This is
the core of the README — write it as if guiding someone through
their first export live. Keep each step to one action.
Example structure to adapt:
1. Choose your source WAV file(s) from the X-Live / SD card
2. Name each channel (or load a saved preset)
3. Choose an output folder (optional — defaults next to the source)
4. Click Export
5. Tracks are written, and [folder opens / DAW opens] automatically


---

## Building From Source

<!--
For anyone (including future-you) who wants to run/modify the Python
source directly instead of using the prebuilt app. List exact commands.
-->

### Requirements
- Python 3.9+
- numpy
- customtkinter

### Run locally
```bash
pip install -r requirements.txt
python xlive_splitter.py        # macOS
python xlive_splitter_windows.py  # Windows
```

### Build the app/exe yourself
<!--
Summarize your PyInstaller command(s) here, or just link to the
GitHub Actions workflow if that's how you build releases.
-->
```bash
pyinstaller --onefile --windowed --name "[App Name]" [script.py]
```

Or trigger the automated build: **Actions tab → [workflow name] → Run workflow.**

---

## Configuration & Data Storage

<!--
Where does the app save its local settings? This matters if someone
migrates machines or wants to back up their presets. Fill in the
actual paths your app uses.
-->
| What | macOS location | Windows location |
|---|---|---|
| Presets | `~/Library/Application Support/[App Name]/presets.json` | `%APPDATA%\[App Name]\presets.json` |
| Settings (DAW path, toggles) | `~/Library/Application Support/[App Name]/config.json` | `%APPDATA%\[App Name]\config.json` |
