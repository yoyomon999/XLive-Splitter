# XLive-Splitter

Combines the 4 GB split files from an X32 / X-Live card back into continuous recordings and splits them into individually named mono tracks, with reusable naming presets and automatic handoff to your DAW.

![Build Status](https://github.com/yoyomon999/XLive-Splitter/actions/workflows/build.yml/badge.svg)
![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Windows-informational)

<img width="1433" height="760" alt="Screenshot 2026-08-03 at 12 06 34 AM" src="https://github.com/user-attachments/assets/3af1fd37-155e-4b04-a34c-2c8d2db140d1" />
<img width="1432" height="754" alt="Screenshot 2026-08-03 at 12 06 52 AM" src="https://github.com/user-attachments/assets/218f1bb2-842a-4dae-941d-de927f4ea597" />

---

## Features

- Splits X32 / X-Live interleaved multitrack WAV files into named mono tracks
- Stitches the card's 4 GB split files back together into continuous recordings
- Reusable track name presets, so you don't retype channel names every week
- Per-channel skip, so unused inputs never reach your session as empty tracks
- Silent channel detection  scans the recording and flags dead inputs before export
- Export order control for how tracks land in the DAW
- Opens the output folder and hands the tracks to your DAW automatically
- macOS and Windows

---

## Download

Grab the latest build from the [Releases page](https://github.com/yoyomon999/XLive-Splitter/releases).

| Platform | Download |
|---|---|
| macOS | [XLive Splitter Mac](https://github.com/yoyomon999/XLive-Splitter/releases/tag/V1.7_Mac) |
| Windows | [XLive Splitter Windows](https://github.com/yoyomon999/XLive-Splitter/releases/tag/V1.7_Windows) |

---

## Installation / First-Run Setup

### macOS
1. Download `XLive_Splitter_macOS.zip` from the [Releases page](https://github.com/yoyomon999/XLive-Splitter/releases) and unzip it.
2. Drag **XLive Splitter.app** into your Applications folder.
3. The first launch will be blocked by Gatekeeper, since the app isn't code signed. Right click the app and choose **Open**, then confirm — or clear it under **System Settings → Privacy & Security**.
4. On first export, if your DAW isn't found automatically, use **Locate…** in the After Export section to point at it.

### Windows
1. Download the Windows zip from the [Releases page](https://github.com/yoyomon999/XLive-Splitter/releases) and unzip it.
2. Move the extracted **XLive Splitter** folder wherever you'd like to keep it, and run `XLive Splitter.exe` from inside it. Keep the folder intact the .exe needs the files alongside it.
3. SmartScreen may warn on first run, since the app isn't signed. Choose **More info → Run anyway**.

---

## How to Use

1. **Choose your source WAV file(s)** from the X-Live card. Select several at once and they're stitched together in order; use the arrows to reorder them if needed.
2. **Name each channel**, or load a saved preset. Use **Batch Rename…** to add a prefix or find and replace across every track at once.
3. **Untick any channels you don't want.** Click **Scan for Silent** to have the app check the recording and untick anything that never rises above −60 dBFS. Review its suggestions  anything left ticked gets exported.
4. **Choose an output folder** (defaults to a new subfolder beside the source) and set the export order if your session expects something other than console order.
5. **Click Export.** Progress is shown as it runs, and the export can be cancelled at any point.
6. The output folder opens and the tracks are handed to your DAW, depending on your After Export settings.

Exported files are named with the console channel number followed by your track name, e.g. `05_Lead Vox.wav`. **Skipping channels never renumbers the rest** skip channel 4 and channel 5 is still `05_`, so the stems always match the desk.

### Keyboard shortcuts

| Action | macOS | Windows |
|---|---|---|
| Open WAV files | `Cmd+O` | `Ctrl+O` |
| Export tracks | `Cmd+E` | `Ctrl+E` |
| Save preset | `Cmd+S` | `Ctrl+S` |
| Batch rename | `Cmd+R` | `Ctrl+R` |
| Cancel export or scan | `Esc` | `Esc` |

Recently opened file sets are under **File → Open Recent**.

---

## Known Issues

1. **Neither build is code signed**, so macOS Gatekeeper and Windows SmartScreen will both object on first launch. See the setup steps above.

---

## Building From Source

### Requirements
- Python 3.9+ (3.12 recommended)
- numpy
- customtkinter

On macOS, the Tcl/Tk version underneath Python matters  use a build with **8.6.13 or newer**. Check yours with:

```bash
python3 -c "import tkinter; print(tkinter.Tcl().eval('info patchlevel'))"
```

Homebrew's Python often ships without Tk entirely; the python.org installer is the safer choice.

### Run locally
```bash
pip install numpy customtkinter
python3 Xlive_Splitter_Mac.py        # macOS
python Xlive_Splitter_Windows.py     # Windows
```

### Build the app/exe yourself
```bash
pip install pyinstaller numpy customtkinter

# macOS
pyinstaller --noconfirm --windowed --name "XLive Splitter" \
  --add-data "$(python3 -c 'import customtkinter, os; print(os.path.dirname(customtkinter.__file__))'):customtkinter/" \
  "Xlive_Splitter_Mac.py"

# Windows
pyinstaller --noconfirm --windowed --name "XLive Splitter" ^
  --collect-all customtkinter "Xlive_Splitter_Windows.py"
```

Or trigger the automated build: **Actions tab → Build XLive Splitter → Run workflow.** Pushes to `main` build only the platform whose source file changed; a manual run always builds both.

---

## Configuration & Data Storage

| What | macOS | Windows |
|---|---|---|
| Presets | `~/Library/Application Support/XLive Splitter/presets.json` | `%APPDATA%\XLive Splitter\presets.json` |
| Settings | `~/Library/Application Support/XLive Splitter/config.json` | `%APPDATA%\XLive Splitter\config.json` |

`config.json` holds your DAW choice and path, the After Export toggles, window size and position, which sections are collapsed, recent file sets, and any custom export orders. Delete it to reset the app to defaults presets are stored separately and won't be affected.
