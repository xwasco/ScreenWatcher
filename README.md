# Screen Watcher

Screen Watcher is a desktop app that watches an area of your screen. When a chosen image or pattern appears in that area, it can:

- **play a sound** (the built-in beep or your own `.wav` file)
- **click the mouse**, either on the detected image or on a fixed point you choose
- **save a photo** of the watched area with the clicked point marked by a red circle, and log every detection to a CSV file

It runs on **Windows, macOS and Linux (X11)**.

---

## Requirements

- **Python 3.8 or newer**, with **Tkinter**. Tkinter ships with Python on Windows and with the python.org installer on macOS.
- These Python packages, which are listed in `requirements.txt`:

| Package | Used for |
|---|---|
| `mss` | screen capture |
| `opencv-python` | image matching |
| `numpy` | image data |
| `pillow` | previews in the interface |
| `pynput` | simulated mouse clicks |

---

## Installation

### 1. Get the files

Put these two files in the same folder:

```
screen_watcher.py
requirements.txt
```

### 2. Create a virtual environment (recommended)

**Windows (PowerShell or cmd)**
```bat
cd path\to\folder
python -m venv venv
venv\Scripts\activate
```

**macOS / Linux**
```bash
cd path/to/folder
python3 -m venv venv
source venv/bin/activate
```

### 3. Install the dependencies

```bash
pip install -r requirements.txt
```

### 4. Run the app

```bash
python screen_watcher.py
```

Use `python3` on macOS and Linux if `python` isn't found. If you created a virtual environment, activate it again before each launch.

---

## Platform-specific setup

### Windows
- Nothing extra is needed.
- If the program you want the app to click on runs **as Administrator**, run Screen Watcher as Administrator too. Otherwise Windows blocks the clicks.
- Display scaling (125%, 150% and so on) is handled automatically.

### macOS
- **If you use Homebrew Python**, Tkinter may be missing. Install it with:
  ```bash
  brew install python-tk
  ```
- **Permissions:** open *System Settings → Privacy & Security* and enable the app you launch Screen Watcher from (Terminal, iTerm, VS Code…) under two entries:
  - **Screen Recording**: without it, the captures come out blank or show only the desktop wallpaper.
  - **Accessibility**: without it, the clicks are silently ignored.

  Restart the terminal after granting the permissions.
- Retina displays are handled automatically.

### Linux
- Install Tkinter and a sound player:
  ```bash
  # Debian / Ubuntu
  sudo apt install python3-tk python3-venv pulseaudio-utils
  # Fedora
  sudo dnf install python3-tkinter pulseaudio-utils
  ```
- The app needs an **X11 session**. On Wayland, screen capture and simulated clicks are blocked. On Ubuntu and Fedora, choose "Ubuntu on Xorg" or "GNOME on Xorg" on the login screen.

---

## Usage

1. **Watched area**: click *Select area*. The screen freezes, and you drag a rectangle over the area to monitor.
2. **Image to find**: click *Capture from screen* and drag around the element to detect. Alternatively, use *Load file…* to load a PNG or JPG.
3. **Settings**:
   - *Minimum similarity*: the match threshold, 0.85 by default. Lower it if the image isn't detected, raise it if you get false alarms.
   - *Check every (ms)*: how often the area is checked.
   - *Pause between alerts (s)*: the minimum time between two alerts.
   - *Alert only when it appears*: alert once each time the image appears. Unchecked, the alert repeats for as long as the image stays visible.
   - *Ignore colors (grayscale)*: ignore colors when matching.
   - Sound on/off, a custom `.wav` file, and *Test* to play the sound.
4. **Automatic click** (optional): check the box, then choose one of:
   - *on the found image*: click the center of the detected image. An x/y offset lets you click next to it instead.
   - *on a fixed point*: click a fixed point that you pick with *Pick point*.

   You can also set the click type (left, right or double), a delay before the click, and whether the mouse pointer returns to where it was.
5. **Detection photos**: on by default, and each detection saves a PNG. Change the folder with *Change folder*, or open it with *Open folder*.
6. Press **▶ Start** to start and **■ Stop** to stop. The preview shows the watched area live, with a green box around the match.

---

## Output

By default, files are saved to `~/ScreenWatcher_detections/`:

- `detection_YYYYMMDD_HHMMSS_mmm.png`: a photo of the watched area at the moment of the click.
  - The clicked point is marked with a **red circle, 2 px in radius**.
  - If the click point lies outside the watched area, the photo is extended with a grey border so the circle stays visible.
  - If automatic click is off, the photo is saved without a circle.
- `detections.csv`: a log with one line per detection. The separator is `;`, so it opens directly in Excel with Italian or European settings. Its columns are:
  ```
  datetime;file;similarity;pattern_x;pattern_y;click_x;click_y
  ```

## Saved settings

The app remembers everything between runs, in these files in your home folder:

| File | Contents |
|---|---|
| `.screen_watcher.json` | area, options, click point, output folder |
| `.screen_watcher_template.png` | the image to find |

To reset the app, delete these two files.

---

## Troubleshooting

| Problem | Solution |
|---|---|
| `ModuleNotFoundError: No module named 'tkinter'` | Install Tkinter; see the platform-specific setup above. |
| `No module named cv2` / `mss` / `pynput` | Activate the virtual environment, then run `pip install -r requirements.txt`. |
| Red warning "Install pynput…" in the app | Run `pip install pynput`. |
| The image is never detected | Lower the threshold, or recapture the image. Matching works only at the **same size** the image was captured at, so recapture it if you change zoom or resolution. |
| Too many false alarms | Raise the threshold, or capture a more distinctive part of the element. |
| The click lands in the wrong place | Check display scaling. On macOS, grant Accessibility; on Windows, run as Administrator if the target app is elevated. |
| Black or empty captures on macOS | Grant Screen Recording permission and restart the terminal. |
| Nothing works on Linux | Log in with an X11 (Xorg) session instead of Wayland. |
| No sound on Linux | Install `pulseaudio-utils` (for `paplay`) or `alsa-utils` (for `aplay`). |
