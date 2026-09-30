#!/usr/bin/env python3
"""
Screen Watcher
--------------
Watches an area of the screen and, when a chosen image/pattern appears
there, plays a sound, optionally simulates a mouse click and saves a photo.

Installation:
    pip install mss opencv-python numpy pillow pynput

Run:
    python screen_watcher.py
"""
import json
import math
import os
import platform
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import wave
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import cv2
import mss
import numpy as np
from PIL import Image, ImageTk

try:
    from pynput.mouse import Button, Controller as MouseController
except Exception:  # pynput not installed or unavailable
    MouseController = None

SYSTEM = platform.system()

# On Windows, without this, Tk and screenshot coordinates don't match
# when display scaling is not 100%.
if SYSTEM == "Windows":
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

HOME = os.path.expanduser("~")
CONFIG_FILE = os.path.join(HOME, ".screen_watcher.json")
TEMPLATE_FILE = os.path.join(HOME, ".screen_watcher_template.png")
DEFAULT_BEEP = os.path.join(tempfile.gettempdir(), "screen_watcher_beep.wav")
DEFAULT_OUT_DIR = os.path.join(HOME, "ScreenWatcher_detections")


# ----------------------------------------------------------------- utilities
def grab_full():
    """Screenshot of all monitors. Returns (BGR image, monitor info)."""
    with mss.mss() as sct:
        mon = sct.monitors[0]
        img = np.array(sct.grab(mon))
    return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR), dict(mon)


def read_image(path):
    data = np.fromfile(path, dtype=np.uint8)  # also works with non-ASCII paths
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def write_image(path, img):
    ok, buf = cv2.imencode(".png", img)
    if ok:
        buf.tofile(path)


def match(frame, tmpl, gray):
    """Returns (score 0..1, top-left position or None)."""
    if frame is None or tmpl is None:
        return 0.0, None
    if gray:
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        tmpl = cv2.cvtColor(tmpl, cv2.COLOR_BGR2GRAY)
    if tmpl.shape[0] > frame.shape[0] or tmpl.shape[1] > frame.shape[1]:
        return 0.0, None
    res = cv2.matchTemplate(frame, tmpl, cv2.TM_CCOEFF_NORMED)
    res = np.nan_to_num(res, nan=0.0, posinf=0.0, neginf=0.0)
    _, mx, _, loc = cv2.minMaxLoc(res)
    return float(mx), loc


def make_beep(path):
    """Generates a double-beep WAV (no external dependencies)."""
    sr, vol = 44100, 0.6
    frames = bytearray()
    for freq, dur in ((880, 0.15), (0, 0.05), (1320, 0.2)):
        n = int(sr * dur)
        fade = sr * 0.008
        for i in range(n):
            env = min(1.0, i / fade, (n - i) / fade)
            v = int(32767 * vol * env * math.sin(2 * math.pi * freq * i / sr)) if freq else 0
            frames += struct.pack("<h", v)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(frames))


def play_sound(path):
    if not path or not os.path.exists(path):
        if not os.path.exists(DEFAULT_BEEP):
            make_beep(DEFAULT_BEEP)
        path = DEFAULT_BEEP
    try:
        if SYSTEM == "Windows":
            import winsound
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
        elif SYSTEM == "Darwin":
            subprocess.Popen(["afplay", path])
        else:
            for player in (["paplay"], ["aplay", "-q"], ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"]):
                if shutil.which(player[0]):
                    subprocess.Popen(player + [path])
                    return
            print("\a", end="", flush=True)
    except Exception as e:
        print("Audio error:", e)


_mouse = None


def do_click(x, y, kind="left", restore=True):
    """Simulates a click at screen coordinates (x, y)."""
    global _mouse
    if MouseController is None:
        print("pynput not installed: cannot click (pip install pynput)")
        return False
    if _mouse is None:
        _mouse = MouseController()
    old = _mouse.position
    _mouse.position = (int(x), int(y))
    time.sleep(0.05)
    if kind == "right":
        _mouse.click(Button.right, 1)
    elif kind == "double":
        _mouse.click(Button.left, 2)
    else:
        _mouse.click(Button.left, 1)
    if restore:
        time.sleep(0.05)
        _mouse.position = old
    return True


def annotate_click(frame, region, point, radius=2, margin=6):
    """Returns a copy of the area photo with a red circle (2 px radius) on the
    clicked point. If the point is outside the area, the photo is extended with
    a grey border just enough to show it."""
    img = frame.copy()
    if point is None:
        return img
    # screen coordinates -> photo pixels (Retina: 2x)
    sx = frame.shape[1] / region["width"]
    sy = frame.shape[0] / region["height"]
    px = int(round((point[0] - region["left"]) * sx))
    py = int(round((point[1] - region["top"]) * sy))
    h, w = img.shape[:2]
    left = max(0, -px + margin)
    top = max(0, -py + margin)
    right = max(0, px - (w - 1) + margin)
    bottom = max(0, py - (h - 1) + margin)
    if left or top or right or bottom:
        img = cv2.copyMakeBorder(img, top, bottom, left, right,
                                 cv2.BORDER_CONSTANT, value=(60, 60, 60))
    cv2.circle(img, (px + left, py + top), radius, (0, 0, 255), 1, cv2.LINE_8)
    return img


def open_folder(path):
    try:
        if SYSTEM == "Windows":
            os.startfile(path)
        elif SYSTEM == "Darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as e:
        print("Cannot open folder:", e)


# ------------------------------------------------------ rectangle selection
class RegionSelector:
    """Shows a full-screen screenshot and lets the user drag a rectangle
    (or click a point). Calls callback(rect, selector), or callback(None, selector) if cancelled."""

    def __init__(self, root, title, callback, point=False):
        self.callback = callback
        self.point = point
        self.img, self.mon = grab_full()
        h, w = self.img.shape[:2]
        # On macOS Retina the screenshot is 2x the logical coordinates
        self.sx = w / self.mon["width"]
        self.sy = h / self.mon["height"]

        self.top = tk.Toplevel(root)
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        self.top.geometry(f"{self.mon['width']}x{self.mon['height']}+{self.mon['left']}+{self.mon['top']}")

        rgb = cv2.cvtColor(self.img, cv2.COLOR_BGR2RGB)
        pil = Image.fromarray(rgb).resize((self.mon["width"], self.mon["height"]))
        pil = Image.blend(pil, Image.new("RGB", pil.size, (0, 0, 0)), 0.35)
        self.photo = ImageTk.PhotoImage(pil)

        self.cv = tk.Canvas(self.top, highlightthickness=0, cursor="crosshair")
        self.cv.pack(fill="both", expand=True)
        self.cv.create_image(0, 0, image=self.photo, anchor="nw")
        self.cv.create_text(20, 20, anchor="nw", fill="white", font=("Helvetica", 16, "bold"),
                            text=f"{title}  -  " + ("click on the point" if point else "drag to select")
                            + ", Esc to cancel")
        self.rect = None
        self.start = None
        self.cv.bind("<ButtonPress-1>", self.on_press)
        self.cv.bind("<B1-Motion>", self.on_drag)
        self.cv.bind("<ButtonRelease-1>", self.on_release)
        self.top.bind("<Escape>", lambda e: self.finish(None))
        self.top.focus_force()
        self.top.grab_set()

    def on_press(self, e):
        if self.point:
            self.finish((e.x, e.y, 0, 0))
            return
        self.start = (e.x, e.y)
        if self.rect:
            self.cv.delete(self.rect)
        self.rect = self.cv.create_rectangle(e.x, e.y, e.x, e.y, outline="#00ff66", width=2)

    def on_drag(self, e):
        if self.start:
            self.cv.coords(self.rect, self.start[0], self.start[1], e.x, e.y)

    def on_release(self, e):
        if not self.start:
            return
        x1, y1 = self.start
        x0, x1 = sorted((x1, e.x))
        y0, y1 = sorted((y1, e.y))
        if x1 - x0 < 5 or y1 - y0 < 5:
            return
        self.finish((x0, y0, x1 - x0, y1 - y0))

    def finish(self, rect):
        self.top.grab_release()
        self.top.destroy()
        self.callback(rect, self)

    def crop(self, rect):
        x, y, w, h = rect
        return self.img[int(y * self.sy):int((y + h) * self.sy),
                        int(x * self.sx):int((x + w) * self.sx)].copy()

    def to_screen(self, rect):
        x, y, w, h = rect
        return {"left": x + self.mon["left"], "top": y + self.mon["top"], "width": w, "height": h}


# ----------------------------------------------------------------- app
class App:
    def __init__(self, root):
        self.root = root
        root.title("Screen Watcher")
        root.resizable(False, False)

        self.region = None      # dict mss
        self.template = None    # BGR image
        self.sound_path = ""
        self.running = False
        self.stop_evt = threading.Event()
        self.thread = None
        self.last_score = 0.0
        self.last_frame = None
        self.last_loc = None
        self.hits = 0

        self.var_thr = tk.DoubleVar(value=0.85)
        self.var_interval = tk.IntVar(value=250)
        self.var_cooldown = tk.DoubleVar(value=3.0)
        self.var_gray = tk.BooleanVar(value=False)
        self.var_edge = tk.BooleanVar(value=True)
        self.var_sound_on = tk.BooleanVar(value=True)
        self.var_click_on = tk.BooleanVar(value=False)
        self.var_click_mode = tk.StringVar(value="image")   # "image" | "point"
        self.var_click_kind = tk.StringVar(value="left")   # left | right | double
        self.var_click_delay = tk.DoubleVar(value=0.0)
        self.var_click_restore = tk.BooleanVar(value=True)
        self.var_off_x = tk.IntVar(value=0)
        self.var_off_y = tk.IntVar(value=0)
        self.click_point = None  # (x, y) screen coordinates
        self.var_save_on = tk.BooleanVar(value=True)
        self.out_dir = DEFAULT_OUT_DIR
        self.last_saved = ""

        self.build_ui()
        self.load_config()
        self.refresh()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.poll()

    # ---------- UI
    def build_ui(self):
        pad = {"padx": 8, "pady": 4}
        f = ttk.Frame(self.root, padding=10)
        f.grid()

        z = ttk.LabelFrame(f, text="1. Area to watch", padding=8)
        z.grid(row=0, column=0, sticky="ew", **pad)
        self.lbl_region = ttk.Label(z, text="No area selected", width=42)
        self.lbl_region.grid(row=0, column=0, sticky="w")
        ttk.Button(z, text="Select area", command=self.select_region).grid(row=0, column=1)

        t = ttk.LabelFrame(f, text="2. Image to find", padding=8)
        t.grid(row=1, column=0, sticky="ew", **pad)
        self.lbl_tmpl = ttk.Label(t, text="No image", width=42, anchor="center")
        self.lbl_tmpl.grid(row=0, column=0, rowspan=2, sticky="w")
        ttk.Button(t, text="Capture from screen", command=self.capture_template).grid(row=0, column=1, sticky="ew")
        ttk.Button(t, text="Load file...", command=self.load_template).grid(row=1, column=1, sticky="ew")

        s = ttk.LabelFrame(f, text="3. Settings", padding=8)
        s.grid(row=2, column=0, sticky="ew", **pad)
        ttk.Label(s, text="Minimum similarity").grid(row=0, column=0, sticky="w")
        ttk.Scale(s, from_=0.5, to=1.0, variable=self.var_thr, length=180,
                  command=lambda v: self.lbl_thr.config(text=f"{float(v):.2f}")).grid(row=0, column=1)
        self.lbl_thr = ttk.Label(s, text=f"{self.var_thr.get():.2f}", width=5)
        self.lbl_thr.grid(row=0, column=2)
        ttk.Label(s, text="Check every (ms)").grid(row=1, column=0, sticky="w")
        ttk.Spinbox(s, from_=50, to=10000, increment=50, textvariable=self.var_interval, width=8).grid(row=1, column=1, sticky="w")
        ttk.Label(s, text="Pause between alerts (s)").grid(row=2, column=0, sticky="w")
        ttk.Spinbox(s, from_=0, to=600, increment=0.5, textvariable=self.var_cooldown, width=8).grid(row=2, column=1, sticky="w")
        ttk.Checkbutton(s, text="Alert only when it appears (don't repeat while it stays visible)",
                        variable=self.var_edge).grid(row=3, column=0, columnspan=3, sticky="w")
        ttk.Checkbutton(s, text="Ignore colors (grayscale)",
                        variable=self.var_gray).grid(row=4, column=0, columnspan=3, sticky="w")
        snd = ttk.Frame(s)
        snd.grid(row=5, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Checkbutton(snd, variable=self.var_sound_on).grid(row=0, column=0)
        self.lbl_sound = ttk.Label(snd, text="Sound: default beep", width=28)
        self.lbl_sound.grid(row=0, column=1)
        ttk.Button(snd, text="Choose .wav", command=self.choose_sound).grid(row=0, column=2)
        ttk.Button(snd, text="Test", command=lambda: play_sound(self.sound_path)).grid(row=0, column=3)

        c = ttk.LabelFrame(f, text="4. Automatic click", padding=8)
        c.grid(row=3, column=0, sticky="ew", **pad)
        ttk.Checkbutton(c, text="When the image is found, click:",
                        variable=self.var_click_on).grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Radiobutton(c, text="on the found image (center)", value="image",
                        variable=self.var_click_mode).grid(row=1, column=0, columnspan=2, sticky="w")
        off = ttk.Frame(c)
        off.grid(row=1, column=2, columnspan=2, sticky="w")
        ttk.Label(off, text="offset x").grid(row=0, column=0)
        ttk.Spinbox(off, from_=-2000, to=2000, textvariable=self.var_off_x, width=5).grid(row=0, column=1)
        ttk.Label(off, text=" y").grid(row=0, column=2)
        ttk.Spinbox(off, from_=-2000, to=2000, textvariable=self.var_off_y, width=5).grid(row=0, column=3)
        ttk.Radiobutton(c, text="on a fixed point:", value="point",
                        variable=self.var_click_mode).grid(row=2, column=0, sticky="w")
        self.lbl_point = ttk.Label(c, text="none", width=16)
        self.lbl_point.grid(row=2, column=1, sticky="w")
        ttk.Button(c, text="Pick point", command=self.select_point).grid(row=2, column=2, sticky="w")
        ttk.Label(c, text="Type").grid(row=3, column=0, sticky="w")
        ttk.Combobox(c, textvariable=self.var_click_kind, values=["left", "right", "double"],
                     state="readonly", width=9).grid(row=3, column=1, sticky="w")
        ttk.Label(c, text="Delay (s)").grid(row=3, column=2, sticky="e")
        ttk.Spinbox(c, from_=0, to=60, increment=0.1, textvariable=self.var_click_delay, width=5).grid(row=3, column=3, sticky="w")
        ttk.Checkbutton(c, text="Move the mouse back to where it was after clicking",
                        variable=self.var_click_restore).grid(row=4, column=0, columnspan=4, sticky="w")
        if MouseController is None:
            ttk.Label(c, text="⚠ Install pynput to enable clicking: pip install pynput",
                      foreground="#b00").grid(row=5, column=0, columnspan=4, sticky="w")

        o = ttk.LabelFrame(f, text="5. Detection photos", padding=8)
        o.grid(row=4, column=0, sticky="ew", **pad)
        ttk.Checkbutton(o, text="Save a photo of the area on every detection (clicked point = red circle)",
                        variable=self.var_save_on).grid(row=0, column=0, columnspan=3, sticky="w")
        self.lbl_outdir = ttk.Label(o, text="", width=34)
        self.lbl_outdir.grid(row=1, column=0, sticky="w")
        ttk.Button(o, text="Change folder", command=self.choose_out_dir).grid(row=1, column=1)
        ttk.Button(o, text="Open folder", command=self.open_out_dir).grid(row=1, column=2)
        self.lbl_last = ttk.Label(o, text="Last photo: none", width=60)
        self.lbl_last.grid(row=2, column=0, columnspan=3, sticky="w")

        r = ttk.LabelFrame(f, text="Area preview", padding=8)
        r.grid(row=5, column=0, sticky="ew", **pad)
        self.preview = tk.Label(r, width=50, height=8, bg="#222")
        self.preview.grid(row=0, column=0)

        b = ttk.Frame(f)
        b.grid(row=6, column=0, sticky="ew", **pad)
        self.btn_run = ttk.Button(b, text="▶  Start", command=self.toggle)
        self.btn_run.grid(row=0, column=0)
        self.lbl_status = ttk.Label(b, text="Stopped", width=40)
        self.lbl_status.grid(row=0, column=1, padx=10)

    def refresh(self):
        if self.region:
            r = self.region
            self.lbl_region.config(text=f"x={r['left']}  y={r['top']}  {r['width']}×{r['height']} px")
        if self.template is not None:
            rgb = cv2.cvtColor(self.template, cv2.COLOR_BGR2RGB)
            pil = Image.fromarray(rgb)
            pil.thumbnail((260, 90))
            self.tmpl_photo = ImageTk.PhotoImage(pil)
            h, w = self.template.shape[:2]
            self.lbl_tmpl.config(image=self.tmpl_photo, text=f"  {w}×{h}", compound="left")
        self.lbl_sound.config(text="Sound: " + (os.path.basename(self.sound_path) if self.sound_path else "default beep"))
        if self.click_point:
            self.lbl_point.config(text=f"x={self.click_point[0]}  y={self.click_point[1]}")
        d = self.out_dir
        self.lbl_outdir.config(text="Folder: " + (d if len(d) <= 30 else "…" + d[-29:]))

    def choose_out_dir(self):
        p = filedialog.askdirectory(initialdir=self.out_dir if os.path.isdir(self.out_dir) else HOME)
        if p:
            self.out_dir = p
            self.save_config()
            self.refresh()

    def open_out_dir(self):
        os.makedirs(self.out_dir, exist_ok=True)
        open_folder(self.out_dir)

    # ---------- actions
    def _with_selector(self, title, cb, point=False):
        if self.running:
            self.toggle()
        self.root.withdraw()
        self.root.after(300, lambda: RegionSelector(self.root, title, cb, point=point))

    def select_point(self):
        def done(rect, sel):
            self.root.deiconify()
            if rect:
                r = sel.to_screen(rect)
                self.click_point = (r["left"], r["top"])
                self.var_click_mode.set("point")
                self.save_config()
                self.refresh()
        self._with_selector("Point to click", done, point=True)

    def select_region(self):
        def done(rect, sel):
            self.root.deiconify()
            if rect:
                self.region = sel.to_screen(rect)
                self.save_config()
                self.refresh()
        self._with_selector("Area to watch", done)

    def capture_template(self):
        def done(rect, sel):
            self.root.deiconify()
            if rect:
                self.template = sel.crop(rect)
                write_image(TEMPLATE_FILE, self.template)
                self.save_config()
                self.refresh()
        self._with_selector("Image to find", done)

    def load_template(self):
        p = filedialog.askopenfilename(filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp"), ("All files", "*.*")])
        if not p:
            return
        img = read_image(p)
        if img is None:
            messagebox.showerror("Error", "Cannot read the image.")
            return
        self.template = img
        write_image(TEMPLATE_FILE, img)
        self.save_config()
        self.refresh()

    def choose_sound(self):
        p = filedialog.askopenfilename(filetypes=[("WAV audio", "*.wav"), ("All files", "*.*")])
        if p:
            self.sound_path = p
            self.save_config()
            self.refresh()

    def toggle(self):
        if self.running:
            self.stop_evt.set()
            self.running = False
            self.btn_run.config(text="▶  Start")
            self.lbl_status.config(text="Stopped")
            return
        if not self.region or self.template is None:
            messagebox.showwarning("Something is missing", "First select the area and the image to find.")
            return
        th, tw = self.template.shape[:2]
        if th > self.region["height"] or tw > self.region["width"]:
            messagebox.showwarning("Area too small",
                                   "The image to find is larger than the watched area. Make the area bigger.")
            return
        if self.var_click_on.get() and self.var_click_mode.get() == "point" and not self.click_point:
            messagebox.showwarning("Point missing", "Pick the point to click, or select 'on the found image'.")
            return
        self.save_config()
        self.stop_evt.clear()
        self.hits = 0
        self.running = True
        self.btn_run.config(text="■  Stop")
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    # ---------- watch loop (separate thread)
    def loop(self):
        region = dict(self.region)
        tmpl = self.template.copy()
        visible_before = False
        last_alert = 0.0
        with mss.mss() as sct:
            while not self.stop_evt.is_set():
                t0 = time.time()
                try:
                    frame = cv2.cvtColor(np.array(sct.grab(region)), cv2.COLOR_BGRA2BGR)
                    score, loc = match(frame, tmpl, self.var_gray.get())
                except Exception as e:
                    print("Capture error:", e)
                    score, loc, frame = 0.0, None, None
                found = score >= self.var_thr.get()
                now = time.time()
                if found:
                    should = (not visible_before) if self.var_edge.get() else True
                    if should and now - last_alert >= float(self.var_cooldown.get() or 0):
                        if self.var_sound_on.get():
                            play_sound(self.sound_path)
                        point, shot = None, frame
                        if self.var_click_on.get():
                            point, shot = self.perform_click(region, frame, tmpl, loc, sct)
                        if self.var_save_on.get() and shot is not None:
                            self.save_detection(shot, region, point, score, loc)
                        last_alert = time.time()
                        self.hits += 1
                visible_before = found
                self.last_score, self.last_loc, self.last_frame = score, (loc if found else None), frame
                interval = max(50, int(self.var_interval.get() or 250)) / 1000
                self.stop_evt.wait(max(0.0, interval - (time.time() - t0)))

    def perform_click(self, region, frame, tmpl, loc, sct=None):
        """Clicks and returns (clicked point in screen coordinates or None,
        photo of the area taken at the moment of the click)."""
        if self.var_click_mode.get() == "point":
            if not self.click_point:
                return None, frame
            x, y = self.click_point
        else:
            # the capture may be in physical pixels (Retina): convert back to screen coordinates
            scale_x = frame.shape[1] / region["width"]
            scale_y = frame.shape[0] / region["height"]
            th, tw = tmpl.shape[:2]
            x = region["left"] + (loc[0] + tw / 2) / scale_x + int(self.var_off_x.get() or 0)
            y = region["top"] + (loc[1] + th / 2) / scale_y + int(self.var_off_y.get() or 0)
        delay = float(self.var_click_delay.get() or 0)
        if delay > 0:
            self.stop_evt.wait(delay)
            if self.stop_evt.is_set():
                return None, frame
            # the screen may have changed during the delay: take the photo again
            if sct is not None:
                try:
                    frame = cv2.cvtColor(np.array(sct.grab(region)), cv2.COLOR_BGRA2BGR)
                except Exception:
                    pass
        try:
            ok = do_click(x, y, self.var_click_kind.get(), self.var_click_restore.get())
        except Exception as e:
            print("Click error:", e)
            ok = False
        return ((x, y) if ok else None), frame

    def save_detection(self, frame, region, point, score, loc):
        try:
            os.makedirs(self.out_dir, exist_ok=True)
            now = time.time()
            stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(now)) + f"_{int(now * 1000) % 1000:03d}"
            name = f"detection_{stamp}.png"
            path = os.path.join(self.out_dir, name)
            write_image(path, annotate_click(frame, region, point))
            # CSV log of all detections
            log = os.path.join(self.out_dir, "detections.csv")
            new = not os.path.exists(log)
            with open(log, "a", encoding="utf-8") as fh:
                if new:
                    fh.write("datetime;file;similarity;pattern_x;pattern_y;click_x;click_y\n")
                cx, cy = (f"{point[0]:.0f}", f"{point[1]:.0f}") if point else ("", "")
                px, py = (loc if loc is not None else ("", ""))
                fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))};{name};"
                         f"{score:.3f};{px};{py};{cx};{cy}\n")
            self.last_saved = name
        except Exception as e:
            print("Error saving photo:", e)

    # ---------- UI update (main thread)
    def poll(self):
        if self.running:
            frame = self.last_frame
            if frame is not None:
                img = frame.copy()
                if self.last_loc is not None and self.template is not None:
                    th, tw = self.template.shape[:2]
                    x, y = self.last_loc
                    cv2.rectangle(img, (x, y), (x + tw, y + th), (0, 255, 0), 3)
                pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
                pil.thumbnail((420, 160))
                self.prev_photo = ImageTk.PhotoImage(pil)
                self.preview.config(image=self.prev_photo, width=pil.width, height=pil.height)
            state = "FOUND ✅" if self.last_loc is not None else "searching..."
            self.lbl_status.config(text=f"{state}   similarity {self.last_score:.2f}   alerts: {self.hits}")
            if self.last_saved:
                self.lbl_last.config(text="Last photo: " + self.last_saved)
        self.root.after(150, self.poll)

    # ---------- config
    def save_config(self):
        cfg = {
            "region": self.region,
            "threshold": round(self.var_thr.get(), 3),
            "interval_ms": self.var_interval.get(),
            "cooldown_s": self.var_cooldown.get(),
            "gray": self.var_gray.get(),
            "edge": self.var_edge.get(),
            "sound": self.sound_path,
            "sound_on": self.var_sound_on.get(),
            "click_on": self.var_click_on.get(),
            "click_mode": self.var_click_mode.get(),
            "click_kind": self.var_click_kind.get(),
            "click_delay": self.var_click_delay.get(),
            "click_restore": self.var_click_restore.get(),
            "click_offset": [self.var_off_x.get(), self.var_off_y.get()],
            "click_point": self.click_point,
            "save_on": self.var_save_on.get(),
            "out_dir": self.out_dir,
        }
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, indent=2)
        except Exception as e:
            print("Cannot save settings:", e)

    def load_config(self):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as fh:
                cfg = json.load(fh)
        except Exception:
            cfg = {}
        self.region = cfg.get("region")
        self.var_thr.set(cfg.get("threshold", 0.85))
        self.lbl_thr.config(text=f"{self.var_thr.get():.2f}")
        self.var_interval.set(cfg.get("interval_ms", 250))
        self.var_cooldown.set(cfg.get("cooldown_s", 3.0))
        self.var_gray.set(cfg.get("gray", False))
        self.var_edge.set(cfg.get("edge", True))
        self.sound_path = cfg.get("sound", "")
        self.var_sound_on.set(cfg.get("sound_on", True))
        self.var_click_on.set(cfg.get("click_on", False))
        # values saved by the earlier Italian version are converted
        legacy = {"immagine": "image", "punto": "point", "sinistro": "left", "destro": "right", "doppio": "double"}
        mode = cfg.get("click_mode", "image")
        kind = cfg.get("click_kind", "left")
        self.var_click_mode.set(legacy.get(mode, mode))
        self.var_click_kind.set(legacy.get(kind, kind))
        self.var_click_delay.set(cfg.get("click_delay", 0.0))
        self.var_click_restore.set(cfg.get("click_restore", True))
        ox, oy = cfg.get("click_offset", [0, 0])
        self.var_off_x.set(ox)
        self.var_off_y.set(oy)
        cp = cfg.get("click_point")
        self.click_point = tuple(cp) if cp else None
        self.var_save_on.set(cfg.get("save_on", True))
        self.out_dir = cfg.get("out_dir") or DEFAULT_OUT_DIR
        if os.path.exists(TEMPLATE_FILE):
            self.template = read_image(TEMPLATE_FILE)

    def on_close(self):
        self.stop_evt.set()
        self.save_config()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam" if SYSTEM == "Linux" else ttk.Style().theme_use())
    except Exception:
        pass
    App(root)
    root.mainloop()
