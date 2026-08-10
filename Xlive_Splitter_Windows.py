#!/usr/bin/env python3
"""
XLive Splitter (Windows Edition)
-------------------
Takes the single interleaved multitrack WAV file recorded by an X32 / X-Live
card and exports each channel as its own named mono WAV file, using reusable
named presets (e.g. "Sunday Service", "Band Rehearsal Setup").

After export, it reveals the output folder in File Explorer and hands the
exported WAV files to your selected DAW (drag-and-drop-onto-the-.exe style),
which creates a new project with each file loaded as a clip.

Requires: Python 3.9+, numpy, customtkinter, Windows 10/11
Run:      python xlive_splitter_windows.py
"""

import glob
import json
import os
import re
import struct
import subprocess
import sys
import threading
import time
import queue
import wave
from collections import namedtuple

try:
    import numpy as np
except ImportError:
    print("This app requires numpy. Install it with:\n\n    pip3 install numpy\n")
    sys.exit(1)

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox
    import customtkinter as ctk
except ImportError:
    print("This app requires customtkinter. Install it with:\n\n    pip3 install customtkinter\n")
    sys.exit(1)


# ============================================================================
# Core WAV parsing / splitting logic
# ============================================================================

WavFormat = namedtuple("WavFormat", ["channels", "sample_rate", "bits_per_sample", "data_offset", "data_size"])
DTYPE_FOR_BITS = {8: np.uint8, 16: np.int16, 32: np.int32}


def parse_wav_format(path):
    with open(path, "rb") as f:
        riff = f.read(12)
        if len(riff) < 12 or riff[0:4] != b"RIFF" or riff[8:12] != b"WAVE":
            raise ValueError(f"'{os.path.basename(path)}' doesn't look like a WAV file.")
        fmt = None
        data_offset = None
        data_size = None
        while True:
            header = f.read(8)
            if len(header) < 8:
                break
            chunk_id, chunk_size = struct.unpack("<4sI", header)
            chunk_start = f.tell()
            if chunk_id == b"fmt ":
                fmt_data = f.read(chunk_size)
                audio_format, channels, sample_rate, byte_rate, block_align, bits_per_sample = \
                    struct.unpack("<HHIIHH", fmt_data[:16])
                fmt = (channels, sample_rate, bits_per_sample)
            elif chunk_id == b"data":
                data_offset = chunk_start
                data_size = chunk_size
                f.seek(chunk_size, 1)
            else:
                f.seek(chunk_size, 1)
            if chunk_size % 2 == 1:
                f.seek(1, 1)
        if fmt is None or data_offset is None:
            raise ValueError(f"'{os.path.basename(path)}': couldn't find audio format/data in file.")
        channels, sample_rate, bits_per_sample = fmt
        if bits_per_sample not in (8, 16, 24, 32):
            raise ValueError(f"'{os.path.basename(path)}': unsupported bit depth ({bits_per_sample}-bit).")
        return WavFormat(channels, sample_rate, bits_per_sample, data_offset, data_size)


def iter_deinterleaved(paths, chunk_frames=131072):
    first_fmt = parse_wav_format(paths[0])
    for p in paths[1:]:
        f2 = parse_wav_format(p)
        if (f2.channels, f2.bits_per_sample, f2.sample_rate) != \
           (first_fmt.channels, first_fmt.bits_per_sample, first_fmt.sample_rate):
            raise ValueError(
                f"'{os.path.basename(p)}' has a different format than "
                f"'{os.path.basename(paths[0])}' — make sure all selected files are "
                f"parts of the same recording."
            )

    channels = first_fmt.channels
    bits = first_fmt.bits_per_sample
    sampwidth = bits // 8
    frame_size = channels * sampwidth

    for p in paths:
        fmt = parse_wav_format(p)
        with open(p, "rb") as f:
            f.seek(fmt.data_offset)
            bytes_remaining = fmt.data_size
            while bytes_remaining > 0:
                to_read = min(chunk_frames * frame_size, bytes_remaining)
                to_read -= to_read % frame_size
                if to_read == 0:
                    break
                raw = f.read(to_read)
                if not raw:
                    break
                bytes_remaining -= len(raw)
                if sampwidth == 3:
                    yield np.frombuffer(raw, dtype=np.uint8).reshape(-1, channels, 3), True
                else:
                    dtype = DTYPE_FOR_BITS[bits]
                    yield np.frombuffer(raw, dtype=dtype).reshape(-1, channels), False


SILENCE_THRESHOLD_DB = -60.0


def _peak_per_channel(arr, is_24bit, channels, stride=64):
    """Per-channel peak amplitude (0.0-1.0) for one decoded chunk.

    `stride` subsamples frames so a full-length scan stays fast; peaks are
    still taken across the whole file, just not every single frame.
    """
    sub_arr = arr[::stride]
    if sub_arr.shape[0] == 0:
        return np.zeros(channels)

    if is_24bit:
        b = sub_arr.astype(np.int32)
        vals = b[:, :, 0] | (b[:, :, 1] << 8) | (b[:, :, 2] << 16)
        vals = np.where(vals & 0x800000, vals - 0x1000000, vals)
        return np.abs(vals).max(axis=0) / 8388608.0

    if sub_arr.dtype == np.uint8:
        vals = sub_arr.astype(np.int32) - 128
        return np.abs(vals).max(axis=0) / 128.0

    info = np.iinfo(sub_arr.dtype)
    return np.abs(sub_arr.astype(np.int64)).max(axis=0) / float(-info.min)


def scan_channel_peaks(input_paths, progress_cb=None, cancel_flag=None, stride=64):
    """Scan every source file and return per-channel peak level in dBFS."""
    fmt = parse_wav_format(input_paths[0])
    channels = fmt.channels
    peaks = np.zeros(channels)

    total_bytes = sum(parse_wav_format(p).data_size for p in input_paths)
    frame_size = channels * (fmt.bits_per_sample // 8)
    bytes_done = 0

    for arr, is_24bit in iter_deinterleaved(input_paths):
        if cancel_flag is not None and cancel_flag.is_set():
            raise InterruptedError("Scan cancelled.")
        peaks = np.maximum(peaks, _peak_per_channel(arr, is_24bit, channels, stride))
        bytes_done += arr.shape[0] * frame_size
        if progress_cb:
            progress_cb(min(bytes_done, total_bytes), total_bytes)

    with np.errstate(divide="ignore"):
        db = 20.0 * np.log10(np.maximum(peaks, 1e-12))
    return [float(x) for x in db]


def split_multitrack(input_paths, output_dir, names, progress_cb=None, cancel_flag=None,
                     enabled=None, order=None):
    """Split interleaved multitrack WAVs into per-channel mono files.

    `enabled` is an optional list of booleans, one per console channel; channels
    set to False are never written. `order` is an optional list of channel
    indices controlling the order of the returned paths (which is the order the
    files get handed to the DAW). Neither affects filenames: the numeric prefix
    always reflects the original console input, so skipping channel 4 leaves
    channel 5 named "05_", not "04_".
    """
    fmt = parse_wav_format(input_paths[0])
    channels = fmt.channels
    bits = fmt.bits_per_sample
    sampwidth = bits // 8
    if len(names) != channels:
        raise ValueError(f"You have {len(names)} track names but the file has {channels} channels.")

    if enabled is None:
        enabled = [True] * channels
    if len(enabled) != channels:
        raise ValueError(f"Got {len(enabled)} channel on/off flags but the file has {channels} channels.")

    active = [i for i in range(channels) if enabled[i]]
    if not active:
        raise ValueError("Every channel is set to skip — there is nothing to export.")

    os.makedirs(output_dir, exist_ok=True)
    writers = [None] * channels
    path_for_channel = {}
    try:
        try:
            for i in active:
                path = os.path.join(output_dir, f"{i+1:02d}_{names[i]}.wav")
                w = wave.open(path, "wb")
                w.setnchannels(1)
                w.setsampwidth(sampwidth)
                w.setframerate(fmt.sample_rate)
                writers[i] = w
                path_for_channel[i] = path

            total_bytes = sum(parse_wav_format(p).data_size for p in input_paths)
            bytes_done = 0
            frame_size = channels * sampwidth

            for arr, is_24bit in iter_deinterleaved(input_paths):
                if cancel_flag is not None and cancel_flag.is_set():
                    raise InterruptedError("Export cancelled.")
                if is_24bit:
                    for ch in active:
                        writers[ch].writeframes(arr[:, ch, :].tobytes())
                else:
                    for ch in active:
                        writers[ch].writeframes(arr[:, ch].tobytes())
                bytes_done += arr.shape[0] * frame_size
                if progress_cb:
                    progress_cb(min(bytes_done, total_bytes), total_bytes)
        finally:
            for w in writers:
                if w is not None:
                    w.close()
    except InterruptedError:
        # Writers are closed by now, so the half-written files can be removed.
        for p in path_for_channel.values():
            try:
                os.remove(p)
            except OSError:
                pass
        raise

    if order:
        ordered = [c for c in order if c in path_for_channel]
        ordered += [c for c in active if c not in ordered]
    else:
        ordered = active
    return [path_for_channel[c] for c in ordered]


def sanitize_name(name, fallback):
    name = name.strip()
    name = re.sub(r'[\/\\:\*\?"<>\|]', "_", name)
    name = re.sub(r"\s+", " ", name)
    return name if name else fallback


def format_duration(frames, sample_rate):
    seconds = frames / sample_rate if sample_rate else 0
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m {s:02d}s" if h else f"{m}m {s:02d}s"


# ============================================================================
# Presets
# ============================================================================

def app_support_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(base, "XLive Splitter")
    os.makedirs(path, exist_ok=True)
    return path


def presets_path():
    return os.path.join(app_support_dir(), "presets.json")


def config_path():
    return os.path.join(app_support_dir(), "config.json")


def load_presets():
    path = presets_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_presets(presets):
    with open(presets_path(), "w") as f:
        json.dump(presets, f, indent=2)


def load_config():
    path = config_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    with open(config_path(), "w") as f:
        json.dump(cfg, f, indent=2)


# ============================================================================
# DAW discovery (Windows)
# ============================================================================

def _program_roots():
    return [
        r for r in [
            os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
            os.environ.get("PROGRAMFILES", r"C:\Program Files"),
            os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
        ] if r and os.path.isdir(r)
    ]


def _glob_all(patterns):
    hits = []
    for root in _program_roots():
        for pattern in patterns:
            hits.extend(glob.glob(os.path.join(root, pattern)))
    return hits


def _find_ableton():
    candidates = _glob_all([os.path.join("Ableton", "Live *", "Program", "Ableton Live*.exe")])
    if not candidates:
        return None
    def version_key(path):
        m = re.search(r"Live (\d+)", path)
        return int(m.group(1)) if m else 0
    candidates.sort(key=version_key, reverse=True)
    return candidates[0]


def _find_reaper():
    candidates = _glob_all([os.path.join("REAPER (x64)", "reaper.exe"), os.path.join("REAPER", "reaper.exe")])
    return candidates[0] if candidates else None


def _find_fl_studio():
    candidates = _glob_all([os.path.join("Image-Line", "FL Studio *", "FL64.exe")])
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0]


def _find_studio_one():
    candidates = _glob_all([os.path.join("PreSonus", "Studio One *", "Studio One.exe")])
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0]


def _find_cubase():
    candidates = _glob_all([os.path.join("Steinberg", "Cubase *", "Cubase*.exe")])
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0]


def _find_pro_tools():
    candidates = _glob_all([os.path.join("Avid", "Pro Tools", "Pro Tools.exe")])
    return candidates[0] if candidates else None


DAW_REGISTRY = {
    "Ableton Live": _find_ableton,
    "Reaper": _find_reaper,
    "FL Studio": _find_fl_studio,
    "Studio One": _find_studio_one,
    "Cubase": _find_cubase,
    "Pro Tools": _find_pro_tools,
    "Custom…": lambda: None,
}


def find_daw_executable(name):
    finder = DAW_REGISTRY.get(name)
    return finder() if finder else None


# ============================================================================
# App-level constants
# ============================================================================

IS_MAC = sys.platform == "darwin"
MAX_RECENT = 8
EXPORT_ORDER_MODES = ["Console order", "Name (A→Z)", "Custom…"]

# Collapsible sections: key -> (content frame attr, header button attr, label)
SECTIONS = {
    "src": ("src_content_frame", "src_header_btn", "1. Source Recording"),
    "preset": ("preset_content_frame", "preset_header_btn", "2. Track Names"),
    "out": ("out_content_frame", "out_header_btn", "3. Output"),
    "automation": ("automation_content_frame", "automation_header_btn", "4. After Export"),
}


# ============================================================================
# GUI
# ============================================================================

class XLiveSplitterApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        
        # Color Palette
        self.DARK_SLATE = "#2C3E50"
        self.CARD_BG = "#34495E"
        self.MUTED_BTN = "#435B71"
        self.MUTED_HOVER = "#54718C"
        self.RUST_RED = "#C0392B"
        self.SILVER = "#BDC3C7"
        self.WARM_CREAM = "#F4F3ED"
        self.COOL_WHITE = "#EAECEE"

        # Typography System (Windows Native)
        self.font_title = ctk.CTkFont(family="Segoe UI", size=16, weight="bold")
        self.font_main = ctk.CTkFont(family="Segoe UI", size=13)
        self.font_bold = ctk.CTkFont(family="Segoe UI", size=13, weight="bold")
        self.font_small = ctk.CTkFont(family="Segoe UI", size=11)

        # Theme Initialization
        ctk.set_appearance_mode("dark")
        self.configure(fg_color=self.DARK_SLATE)
        self.title("XLive Splitter")
        self.geometry("700x800")
        self.minsize(600, 500)

        self.source_paths = []
        self.wav_format = None
        self.name_vars = []
        self.presets = load_presets()
        self.app_config = load_config()

        self.daw_choice = self.app_config.get("daw_choice", "Ableton Live")
        if self.daw_choice not in DAW_REGISTRY:
            self.daw_choice = "Ableton Live"
        self.daw_path = self.app_config.get("daw_path") or find_daw_executable(self.daw_choice)
        if self.daw_path and not os.path.exists(self.daw_path):
            self.daw_path = find_daw_executable(self.daw_choice)
        self.auto_open_folder = self.app_config.get("auto_open_folder", True)
        self.auto_open_daw = self.app_config.get("auto_open_daw", True)

        # Collapsible section state, remembered between runs. Unknown keys in a
        # stale config are ignored rather than crashing the build.
        saved_sections = self.app_config.get("section_state", {})
        self.section_state = {}
        for key in SECTIONS:
            value = saved_sections.get(key, True) if isinstance(saved_sections, dict) else True
            self.section_state[key] = bool(value)
        self._config_save_job = None

        self.recent_sessions = self._load_recent_sessions()
        self.custom_order = self._load_custom_orders()
        self.export_order_mode = self.app_config.get("export_order_mode", EXPORT_ORDER_MODES[0])
        if self.export_order_mode not in EXPORT_ORDER_MODES:
            self.export_order_mode = EXPORT_ORDER_MODES[0]

        self.enabled_vars = []
        self.channel_peaks = None

        self.export_thread = None
        self.cancel_flag = None
        self.progress_queue = queue.Queue()

        self.scan_thread = None
        self.scan_cancel = None
        self.scan_queue = queue.Queue()

        self._restore_geometry()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        pad = {"padx": 16, "pady": 10}

        btn_kwargs = {"fg_color": self.MUTED_BTN, "text_color": self.COOL_WHITE, "hover_color": self.MUTED_HOVER, "font": self.font_bold}
        label_kwargs = {"text_color": self.WARM_CREAM, "font": self.font_main}
        
        frame_kwargs = {"fg_color": self.CARD_BG, "corner_radius": 8}

        # Everything lives inside a scrollable container so sections can never
        # render below the visible window/screen edge
        self.main_scroll = ctk.CTkScrollableFrame(self, fg_color=self.DARK_SLATE)
        self.main_scroll.pack(fill="both", expand=True)

        # --- 1. Source Section ---
        src_frame = ctk.CTkFrame(self.main_scroll, **frame_kwargs)
        self.src_header_btn = ctk.CTkButton(src_frame, text="▼ 1. Source Recording", font=self.font_title, 
                                            text_color=self.COOL_WHITE, fg_color="transparent", 
                                            hover_color=self.MUTED_BTN, anchor="w", 
                                            command=self.toggle_source)
        src_frame.grid_columnconfigure(0, weight=1)
        self.src_header_btn.grid(row=0, column=0, sticky="ew", padx=6, pady=6)

        self.src_content_frame = ctk.CTkFrame(src_frame, fg_color="transparent")
        self.src_content_frame.grid(row=1, column=0, sticky="ew")

        row = ctk.CTkFrame(self.src_content_frame, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=8)
        ctk.CTkButton(row, text="📁 Choose WAV File(s)…", command=self.choose_source, height=32, corner_radius=6, **btn_kwargs).pack(side="left")
        ctk.CTkLabel(row, text="Select multiple files to stitch them together sequentially.", text_color=self.SILVER, font=self.font_small).pack(side="left", padx=10)

        self.source_list_frame = ctk.CTkFrame(self.src_content_frame, fg_color=self.DARK_SLATE, height=100, corner_radius=6)
        self.source_list_frame.pack(fill="x", padx=12, pady=(0, 8))
        
        self.refresh_source_list() 

        self.format_label = ctk.CTkLabel(self.src_content_frame, text="", text_color=self.SILVER, font=self.font_small)
        self.format_label.pack(fill="x", padx=12, pady=(0, 10))

        # --- 2. Preset Section ---
        preset_frame = ctk.CTkFrame(self.main_scroll, **frame_kwargs)
        self.preset_header_btn = ctk.CTkButton(preset_frame, text="▼ 2. Track Names", font=self.font_title, 
                                            text_color=self.COOL_WHITE, fg_color="transparent", 
                                            hover_color=self.MUTED_BTN, anchor="w", 
                                            command=self.toggle_preset)
        preset_frame.grid_columnconfigure(0, weight=1)
        self.preset_header_btn.grid(row=0, column=0, sticky="ew", padx=6, pady=6)
        
        self.preset_content_frame = ctk.CTkFrame(preset_frame, fg_color="transparent")
        self.preset_content_frame.grid(row=1, column=0, sticky="nsew")

        prow = ctk.CTkFrame(self.preset_content_frame, fg_color="transparent")
        prow.pack(fill="x", padx=12, pady=8)
        ctk.CTkLabel(prow, text="Preset:", **label_kwargs).pack(side="left")
        
        self.preset_var = ctk.StringVar()
        self.preset_combo = ctk.CTkComboBox(prow, variable=self.preset_var, values=sorted(self.presets.keys()), state="readonly",
                                            fg_color=self.DARK_SLATE, text_color=self.COOL_WHITE, button_color=self.MUTED_BTN, 
                                            button_hover_color=self.MUTED_HOVER, dropdown_fg_color=self.CARD_BG, 
                                            dropdown_text_color=self.COOL_WHITE, width=200, font=self.font_main)
        self.preset_combo.pack(side="left", padx=(8, 12))
        
        ctk.CTkButton(prow, text="📂 Load", command=self.load_preset, width=70, **btn_kwargs).pack(side="left", padx=4)
        ctk.CTkButton(prow, text="💾 Save As…", command=self.save_preset_as, width=80, **btn_kwargs).pack(side="left", padx=4)
        ctk.CTkButton(prow, text="🗑 Delete", command=self.delete_preset, width=70, **btn_kwargs).pack(side="left", padx=4)

        prow2 = ctk.CTkFrame(self.preset_content_frame, fg_color="transparent")
        prow2.pack(fill="x", padx=12, pady=(0, 8))
        ctk.CTkButton(prow2, text="🔍 Scan for Silent", command=self.scan_silent_channels,
                      width=150, **btn_kwargs).pack(side="left")
        ctk.CTkButton(prow2, text="✔ Enable All", command=self.enable_all_channels,
                      width=110, **btn_kwargs).pack(side="left", padx=6)
        ctk.CTkButton(prow2, text="✏️ Batch Rename…", command=self.open_batch_rename,
                      width=140, **btn_kwargs).pack(side="left")
        self.channel_summary_var = ctk.StringVar(value="")
        ctk.CTkLabel(prow2, textvariable=self.channel_summary_var, text_color=self.SILVER,
                     font=self.font_small).pack(side="left", padx=10)

        self.rows_frame = ctk.CTkFrame(self.preset_content_frame, fg_color=self.DARK_SLATE, corner_radius=6)
        self.rows_frame.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        self.placeholder_label = ctk.CTkLabel(self.rows_frame, text="Choose a source file to see channel rows here.", text_color=self.SILVER, font=self.font_small)
        self.placeholder_label.pack(padx=6, pady=20)

        # --- 3. Output Section ---
        out_frame = ctk.CTkFrame(self.main_scroll, **frame_kwargs)
        self.out_header_btn = ctk.CTkButton(out_frame, text="▼ 3. Output", font=self.font_title, 
                                            text_color=self.COOL_WHITE, fg_color="transparent", 
                                            hover_color=self.MUTED_BTN, anchor="w", 
                                            command=self.toggle_output)
        out_frame.grid_columnconfigure(0, weight=1)
        self.out_header_btn.grid(row=0, column=0, sticky="ew", padx=6, pady=6)

        self.out_content_frame = ctk.CTkFrame(out_frame, fg_color="transparent")
        self.out_content_frame.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 10))

        orow = ctk.CTkFrame(self.out_content_frame, fg_color="transparent")
        orow.pack(fill="x")
        ctk.CTkButton(orow, text="📁 Choose Folder…", command=self.choose_output, height=32, corner_radius=6, **btn_kwargs).pack(side="left")
        
        self.output_var = ctk.StringVar(value="(same folder as source, in a new subfolder)")
        ctk.CTkLabel(orow, textvariable=self.output_var, text_color=self.SILVER, font=self.font_small, wraplength=400).pack(side="left", padx=12)

        orow2 = ctk.CTkFrame(self.out_content_frame, fg_color="transparent")
        orow2.pack(fill="x", pady=(12, 0))
        ctk.CTkLabel(orow2, text="Export order:", **label_kwargs).pack(side="left")
        self.order_var = ctk.StringVar(value=self.export_order_mode)
        self.order_combo = ctk.CTkComboBox(orow2, variable=self.order_var, values=EXPORT_ORDER_MODES, state="readonly",
                                           fg_color=self.DARK_SLATE, text_color=self.COOL_WHITE, button_color=self.MUTED_BTN,
                                           button_hover_color=self.MUTED_HOVER, dropdown_fg_color=self.CARD_BG,
                                           dropdown_text_color=self.COOL_WHITE, width=160, font=self.font_main,
                                           command=self.on_order_selected)
        self.order_combo.pack(side="left", padx=(8, 8))
        self.reorder_btn = ctk.CTkButton(orow2, text="↕ Edit Order…", command=self.open_reorder_dialog,
                                         width=130, height=32, corner_radius=6, **btn_kwargs)
        self.reorder_btn.pack(side="left")
        ctk.CTkLabel(self.out_content_frame, text="Sets the order files are handed to the DAW. Filenames always keep the console channel number.",
                     text_color=self.SILVER, font=self.font_small, wraplength=520, justify="left").pack(anchor="w", pady=(6, 0))

        # --- 4. After Export ---
        automation_frame = ctk.CTkFrame(self.main_scroll, **frame_kwargs)
        self.automation_header_btn = ctk.CTkButton(automation_frame, text="▼ 4. After Export", font=self.font_title,
                                            text_color=self.COOL_WHITE, fg_color="transparent",
                                            hover_color=self.MUTED_BTN, anchor="w",
                                            command=self.toggle_automation)
        automation_frame.grid_columnconfigure(0, weight=1)
        self.automation_header_btn.grid(row=0, column=0, sticky="ew", padx=6, pady=6)

        self.automation_content_frame = ctk.CTkFrame(automation_frame, fg_color="transparent")
        self.automation_content_frame.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 10))

        self.auto_open_folder_var = ctk.BooleanVar(value=self.auto_open_folder)
        ctk.CTkCheckBox(self.automation_content_frame, text="Open output folder in File Explorer",
                         variable=self.auto_open_folder_var, text_color=self.WARM_CREAM, font=self.font_main,
                         command=self.on_toggle_auto_open_folder).pack(anchor="w", pady=(4, 4))

        self.auto_open_daw_var = ctk.BooleanVar(value=self.auto_open_daw)
        ctk.CTkCheckBox(self.automation_content_frame, text="Open exported tracks in a DAW",
                         variable=self.auto_open_daw_var, text_color=self.WARM_CREAM, font=self.font_main,
                         command=self.on_toggle_auto_open_daw).pack(anchor="w", pady=(4, 8))

        drow = ctk.CTkFrame(self.automation_content_frame, fg_color="transparent")
        drow.pack(fill="x", pady=(0, 4))
        ctk.CTkLabel(drow, text="DAW:", text_color=self.WARM_CREAM, font=self.font_main).pack(side="left")
        self.daw_var = ctk.StringVar(value=self.daw_choice)
        self.daw_combo = ctk.CTkComboBox(drow, variable=self.daw_var, values=list(DAW_REGISTRY.keys()), state="readonly",
                                          fg_color=self.DARK_SLATE, text_color=self.COOL_WHITE, button_color=self.MUTED_BTN,
                                          button_hover_color=self.MUTED_HOVER, dropdown_fg_color=self.CARD_BG,
                                          dropdown_text_color=self.COOL_WHITE, width=180, font=self.font_main,
                                          command=self.on_daw_selected)
        self.daw_combo.pack(side="left", padx=(8, 12))
        ctk.CTkButton(drow, text="📁 Locate…", command=self.choose_daw_path, width=90, height=32, corner_radius=6, **btn_kwargs).pack(side="left")

        self.daw_status_var = ctk.StringVar(value=self._daw_status_text())
        ctk.CTkLabel(self.automation_content_frame, textvariable=self.daw_status_var, text_color=self.SILVER,
                     font=self.font_small, wraplength=560, justify="left").pack(anchor="w", pady=(4, 0))

        # --- 5. Export ---
        export_frame = ctk.CTkFrame(self.main_scroll, fg_color="transparent")

        btn_row = ctk.CTkFrame(export_frame, fg_color="transparent")
        btn_row.pack(fill="x")

        self.export_button = ctk.CTkButton(btn_row, text="⚙️ Export Tracks", command=self.start_export, state="disabled",
                                           fg_color=self.RUST_RED, text_color=self.WARM_CREAM, hover_color="#A93226",
                                           font=ctk.CTkFont(family="Segoe UI", size=15, weight="bold"), height=40)
        self.export_button.pack(side="left", fill="x", expand=True)

        self.cancel_button = ctk.CTkButton(btn_row, text="✕ Cancel", command=self.cancel_running_job, state="disabled",
                                           fg_color=self.MUTED_BTN, text_color=self.COOL_WHITE, hover_color=self.MUTED_HOVER,
                                           font=self.font_bold, width=110, height=40, corner_radius=6)
        self.cancel_button.pack(side="left", padx=(10, 0))

        self.progress = ctk.CTkProgressBar(export_frame, progress_color=self.RUST_RED, fg_color=self.CARD_BG, height=8)
        self.progress.pack(fill="x", pady=(14, 6))
        self.progress.set(0)

        self.status_label = ctk.CTkLabel(export_frame, text="", text_color=self.SILVER, font=self.font_small)
        self.status_label.pack(fill="x")

        # --- Layout Order ---
        src_frame.pack(side="top", fill="x", **pad)
        preset_frame.pack(side="top", fill="x", **pad)
        out_frame.pack(side="top", fill="x", **pad)
        automation_frame.pack(side="top", fill="x", **pad)
        export_frame.pack(side="top", fill="x", **pad)
        
        self.output_dir_override = None

        self._build_menubar()
        self._bind_shortcuts()

        # Restore collapsed/expanded state from the last session.
        for key in SECTIONS:
            self._apply_section_state(key)
        self._update_reorder_button()

    # ---------------- UI Toggles ----------------
    def _apply_section_state(self, key):
        content_attr, header_attr, label = SECTIONS[key]
        content = getattr(self, content_attr)
        header = getattr(self, header_attr)
        if self.section_state.get(key, True):
            content.grid()
            header.configure(text=f"▼ {label}")
        else:
            content.grid_remove()
            header.configure(text=f"▶ {label}")

    def _toggle_section(self, key):
        self.section_state[key] = not self.section_state.get(key, True)
        self._apply_section_state(key)
        self.app_config["section_state"] = dict(self.section_state)
        self._queue_config_save()

    def toggle_source(self):
        self._toggle_section("src")

    def toggle_preset(self):
        self._toggle_section("preset")

    def toggle_output(self):
        self._toggle_section("out")

    def toggle_automation(self):
        self._toggle_section("automation")

    # ---------------- Config persistence ----------------
    def _queue_config_save(self, delay_ms=400):
        """Coalesce rapid config writes so clicking never blocks on disk I/O."""
        if self._config_save_job is not None:
            try:
                self.after_cancel(self._config_save_job)
            except Exception:
                pass
        self._config_save_job = self.after(delay_ms, self._flush_config_save)

    def _flush_config_save(self):
        self._config_save_job = None
        try:
            save_config(self.app_config)
        except Exception:
            pass

    def _restore_geometry(self):
        geo = self.app_config.get("window_geometry")
        if isinstance(geo, str) and geo:
            try:
                self.geometry(geo)
            except Exception:
                pass

    def _on_close(self):
        if self.export_thread is not None and self.export_thread.is_alive():
            if not messagebox.askyesno("Export in progress",
                                       "An export is still running. Quit anyway? "
                                       "Partly written files will be left behind.",
                                       parent=self):
                return
            if self.cancel_flag is not None:
                self.cancel_flag.set()
        if self.scan_cancel is not None:
            self.scan_cancel.set()
        try:
            self.app_config["window_geometry"] = self.geometry()
            self.app_config["section_state"] = dict(self.section_state)
            if self._config_save_job is not None:
                try:
                    self.after_cancel(self._config_save_job)
                except Exception:
                    pass
                self._config_save_job = None
            save_config(self.app_config)
        except Exception:
            pass
        self.destroy()

    # ---------------- Source File Handling ----------------

    def choose_source(self):
        paths = filedialog.askopenfilenames(
            parent=self,
            title="Select XLive multitrack WAV file(s)",
            initialdir=os.path.expanduser("~"),
            filetypes=[("WAV files", "*.wav *.WAV"), ("All files", "*.*")]
        )
        if not paths:
            return
        
        self.source_paths.extend(paths)
        self.source_paths = sorted(list(set(self.source_paths)))
        self._add_recent_session(self.source_paths)

        self.refresh_source_list()
        self.validate_and_update_format()

        if self.source_paths:
            base = os.path.splitext(os.path.basename(self.source_paths[0]))[0]
            default_out = os.path.join(os.path.dirname(self.source_paths[0]), f"{base}_Tracks")
            self.output_dir_override = None
            self.output_var.set(f"Default: {default_out}")

    def refresh_source_list(self):
        for child in self.source_list_frame.winfo_children():
            child.destroy()
            
        if not self.source_paths:
            placeholder = ctk.CTkLabel(self.source_list_frame, text="No files selected.", text_color=self.SILVER, font=self.font_small)
            placeholder.pack(pady=15)
            return

        for idx, p in enumerate(self.source_paths):
            row = ctk.CTkFrame(self.source_list_frame, fg_color="transparent")
            row.pack(fill="x", pady=2)

            lbl = ctk.CTkLabel(row, text=os.path.basename(p), font=self.font_main, text_color=self.COOL_WHITE)
            lbl.pack(side="left", padx=5)

            btn_del = ctk.CTkButton(row, text="⨂", width=32, height=32, corner_radius=4, fg_color="transparent", hover_color=self.RUST_RED, text_color=self.SILVER, font=self.font_bold, command=lambda i=idx: self.remove_source(i))
            btn_del.pack(side="right", padx=2)

            btn_down = ctk.CTkButton(row, text="↓", width=32, height=32, corner_radius=4, fg_color=self.MUTED_BTN, hover_color=self.MUTED_HOVER, font=self.font_bold, command=lambda i=idx: self.move_source_down(i))
            btn_down.pack(side="right", padx=2)

            btn_up = ctk.CTkButton(row, text="↑", width=32, height=32, corner_radius=4, fg_color=self.MUTED_BTN, hover_color=self.MUTED_HOVER, font=self.font_bold, command=lambda i=idx: self.move_source_up(i))
            btn_up.pack(side="right", padx=2)
            

    def move_source_up(self, idx):
        if idx > 0:
            self.source_paths[idx], self.source_paths[idx-1] = self.source_paths[idx-1], self.source_paths[idx]
            self.refresh_source_list()

    def move_source_down(self, idx):
        if idx < len(self.source_paths) - 1:
            self.source_paths[idx], self.source_paths[idx+1] = self.source_paths[idx+1], self.source_paths[idx]
            self.refresh_source_list()

    def remove_source(self, idx):
        self.source_paths.pop(idx)
        self.refresh_source_list()
        self.validate_and_update_format()

    def validate_and_update_format(self):
        if not self.source_paths:
            self.format_label.configure(text="")
            self.export_button.configure(state="disabled")
            for child in self.rows_frame.winfo_children():
                child.destroy()
            self.name_vars = []
            self.enabled_vars = []
            self.channel_peaks = None
            self.channel_summary_var.set("")
            return

        try:
            fmt = parse_wav_format(self.source_paths[0])
            for p in self.source_paths[1:]:
                f2 = parse_wav_format(p)
                if (f2.channels, f2.bits_per_sample, f2.sample_rate) != \
                   (fmt.channels, fmt.bits_per_sample, fmt.sample_rate):
                    raise ValueError(f"'{os.path.basename(p)}' has a different format and can't be "
                                      f"combined with '{os.path.basename(self.source_paths[0])}'.")
        except Exception as e:
            messagebox.showerror("Format mismatch", str(e), parent=self)
            self.source_paths.clear()
            self.refresh_source_list()
            self.validate_and_update_format()
            return

        self.wav_format = fmt
        total_data_bytes = sum(parse_wav_format(p).data_size for p in self.source_paths)
        frame_size = fmt.channels * (fmt.bits_per_sample // 8)
        total_frames = total_data_bytes // frame_size

        self.format_label.configure(
            text=f"{fmt.channels} channels  ·  {fmt.sample_rate:,} Hz  ·  {fmt.bits_per_sample}-bit  ·  "
                 f"{format_duration(total_frames, fmt.sample_rate)}"
        )

        self.build_channel_rows(fmt.channels)

    def build_channel_rows(self, n_channels):
        for child in self.rows_frame.winfo_children():
            child.destroy()
        self.name_vars = []
        self.enabled_vars = []

        preset_names = None
        if self.preset_var.get() in self.presets:
            candidate = self.presets[self.preset_var.get()]
            if len(candidate) == n_channels:
                preset_names = candidate

        header = ctk.CTkFrame(self.rows_frame, fg_color="transparent")
        header.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(header, text="On", width=42, text_color=self.SILVER, font=self.font_small).pack(side="left")
        ctk.CTkLabel(header, text="Ch", width=30, text_color=self.SILVER, font=self.font_small).pack(side="left")
        ctk.CTkLabel(header, text="Track name", text_color=self.SILVER, font=self.font_small).pack(side="left", padx=(10, 0))

        for i in range(n_channels):
            row = ctk.CTkFrame(self.rows_frame, fg_color="transparent")
            row.pack(fill="x", pady=3)

            enabled = ctk.BooleanVar(value=True)
            ctk.CTkCheckBox(row, text="", variable=enabled, width=42,
                            checkbox_width=18, checkbox_height=18, corner_radius=4,
                            fg_color=self.RUST_RED, hover_color="#A93226",
                            border_color=self.SILVER, border_width=2,
                            command=self._update_channel_summary).pack(side="left")
            self.enabled_vars.append(enabled)

            ctk.CTkLabel(row, text=f"{i+1:02d}", width=30, text_color=self.WARM_CREAM, font=self.font_bold).pack(side="left")

            default = preset_names[i] if preset_names else f"Track {i+1:02d}"
            var = ctk.StringVar(value=default)

            entry = ctk.CTkEntry(row, textvariable=var, fg_color=self.CARD_BG, text_color=self.COOL_WHITE, font=self.font_main, border_width=0)
            entry.pack(side="left", padx=(10, 0), fill="x", expand=True)
            self.name_vars.append(var)

        self.channel_peaks = None
        self._update_channel_summary()

    # ---------------- Channel enable / skip ----------------
    def _update_channel_summary(self):
        total = len(self.enabled_vars)
        if not total:
            self.channel_summary_var.set("")
            return
        on = sum(1 for v in self.enabled_vars if v.get())
        if on == total:
            self.channel_summary_var.set(f"All {total} channels will be exported")
        else:
            self.channel_summary_var.set(f"{on} of {total} channels will be exported")
        self.export_button.configure(state=("normal" if on else "disabled"))

    def enable_all_channels(self):
        for var in self.enabled_vars:
            var.set(True)
        self._update_channel_summary()

    def skip_silent_channels(self):
        """Untick channels that the last scan found to be silent."""
        if not self.channel_peaks:
            messagebox.showinfo("No scan yet",
                                "Run 'Scan for Silent' first so there is level data to work from.",
                                parent=self)
            return
        count = 0
        for i, var in enumerate(self.enabled_vars):
            if i < len(self.channel_peaks) and self.channel_peaks[i] < SILENCE_THRESHOLD_DB:
                var.set(False)
                count += 1
        self._update_channel_summary()
        return count

    def scan_silent_channels(self):
        if not self.source_paths:
            messagebox.showinfo("No source file", "Choose a source WAV file first.", parent=self)
            return
        if self.scan_thread is not None and self.scan_thread.is_alive():
            return
        if self.export_thread is not None and self.export_thread.is_alive():
            messagebox.showinfo("Export running", "Wait for the export to finish first.", parent=self)
            return

        self.scan_cancel = threading.Event()
        self.export_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.progress.set(0)
        self.status_label.configure(text="Scanning channel levels\u2026")

        def progress_cb(done, total):
            self.scan_queue.put(("progress", done, total))

        def worker():
            try:
                levels = scan_channel_peaks(self.source_paths, progress_cb=progress_cb,
                                            cancel_flag=self.scan_cancel)
                self.scan_queue.put(("done", levels, None))
            except InterruptedError:
                self.scan_queue.put(("cancelled", None, None))
            except Exception as e:
                self.scan_queue.put(("error", str(e), None))

        self.scan_thread = threading.Thread(target=worker, daemon=True)
        self.scan_thread.start()
        self.after(100, self.poll_scan)

    def poll_scan(self):
        try:
            while True:
                msg = self.scan_queue.get_nowait()
                kind = msg[0]
                if kind == "progress":
                    _, done, total = msg
                    pct = (done / total) if total else 0
                    self.progress.set(pct)
                    self.status_label.configure(text=f"Scanning channel levels\u2026 {pct*100:.0f}%")
                elif kind == "done":
                    _, levels, _ = msg
                    self.channel_peaks = levels
                    self.progress.set(1)
                    self.cancel_button.configure(state="disabled")
                    silent = self.skip_silent_channels() or 0
                    self._update_channel_summary()
                    if silent:
                        self.status_label.configure(text=f"Scan complete \u2014 {silent} silent channel(s) set to skip.")
                        messagebox.showinfo(
                            "Silent channels found",
                            f"{silent} channel(s) peaked below {SILENCE_THRESHOLD_DB:.0f} dBFS and have been "
                            "unticked. Review them before exporting \u2014 anything still ticked will be written.",
                            parent=self)
                    else:
                        self.status_label.configure(text="Scan complete \u2014 no silent channels found.")
                    return
                elif kind == "cancelled":
                    self.progress.set(0)
                    self.status_label.configure(text="Scan cancelled.")
                    self.cancel_button.configure(state="disabled")
                    self._update_channel_summary()
                    return
                elif kind == "error":
                    self.progress.set(0)
                    self.status_label.configure(text="Scan failed.")
                    self.cancel_button.configure(state="disabled")
                    self._update_channel_summary()
                    messagebox.showerror("Scan failed", msg[1], parent=self)
                    return
        except queue.Empty:
            pass
        self.after(100, self.poll_scan)

    # ---------------- Batch rename ----------------
    def open_batch_rename(self):
        if not self.name_vars:
            messagebox.showinfo("No channels", "Choose a source WAV file first.", parent=self)
            return

        win = ctk.CTkToplevel(self)
        win.title("Batch Rename Tracks")
        win.geometry("420x300")
        win.configure(fg_color=self.DARK_SLATE)
        win.transient(self)
        win.after(120, win.grab_set)

        find_var = ctk.StringVar()
        repl_var = ctk.StringVar()
        prefix_var = ctk.StringVar()
        suffix_var = ctk.StringVar()
        only_enabled = ctk.BooleanVar(value=True)

        entry_kwargs = {"fg_color": self.CARD_BG, "text_color": self.COOL_WHITE,
                        "font": self.font_main, "border_width": 0, "width": 220}

        for label, var in (("Find:", find_var), ("Replace with:", repl_var),
                           ("Add prefix:", prefix_var), ("Add suffix:", suffix_var)):
            r = ctk.CTkFrame(win, fg_color="transparent")
            r.pack(fill="x", padx=16, pady=6)
            ctk.CTkLabel(r, text=label, width=110, anchor="w",
                         text_color=self.WARM_CREAM, font=self.font_main).pack(side="left")
            ctk.CTkEntry(r, textvariable=var, **entry_kwargs).pack(side="left", fill="x", expand=True)

        ctk.CTkCheckBox(win, text="Only rename channels that are ticked",
                        variable=only_enabled, text_color=self.WARM_CREAM,
                        font=self.font_main).pack(anchor="w", padx=16, pady=(8, 4))

        def apply_rename():
            find = find_var.get()
            for i, var in enumerate(self.name_vars):
                if only_enabled.get() and i < len(self.enabled_vars) and not self.enabled_vars[i].get():
                    continue
                value = var.get()
                if find:
                    value = value.replace(find, repl_var.get())
                var.set(f"{prefix_var.get()}{value}{suffix_var.get()}")
            win.destroy()

        brow = ctk.CTkFrame(win, fg_color="transparent")
        brow.pack(fill="x", padx=16, pady=(12, 16))
        ctk.CTkButton(brow, text="Apply", command=apply_rename, fg_color=self.RUST_RED,
                      hover_color="#A93226", text_color=self.WARM_CREAM,
                      font=self.font_bold).pack(side="right")
        ctk.CTkButton(brow, text="Cancel", command=win.destroy, fg_color=self.MUTED_BTN,
                      hover_color=self.MUTED_HOVER, text_color=self.COOL_WHITE,
                      font=self.font_bold).pack(side="right", padx=8)

    # ---------------- Export order ----------------
    def _load_custom_orders(self):
        raw = self.app_config.get("custom_order", {})
        if not isinstance(raw, dict):
            return {}
        cleaned = {}
        for key, value in raw.items():
            if isinstance(value, list) and all(isinstance(x, int) for x in value):
                cleaned[str(key)] = value
        return cleaned

    def _current_order_list(self):
        """Channel indices in the order files should be handed to the DAW."""
        n = len(self.name_vars)
        mode = self.order_var.get() if hasattr(self, "order_var") else EXPORT_ORDER_MODES[0]
        if mode == EXPORT_ORDER_MODES[1]:  # Name (A-Z)
            return sorted(range(n), key=lambda i: sanitize_name(self.name_vars[i].get(), f"Track {i+1:02d}").lower())
        if mode == EXPORT_ORDER_MODES[2]:  # Custom
            saved = self.custom_order.get(str(n))
            if saved:
                order = [c for c in saved if 0 <= c < n]
                order += [c for c in range(n) if c not in order]
                return order
        return list(range(n))

    def _update_reorder_button(self):
        if not hasattr(self, "reorder_btn"):
            return
        is_custom = self.order_var.get() == EXPORT_ORDER_MODES[2]
        self.reorder_btn.configure(state=("normal" if is_custom else "disabled"))

    def on_order_selected(self, choice):
        self.export_order_mode = choice
        self.app_config["export_order_mode"] = choice
        self._queue_config_save()
        self._update_reorder_button()
        if choice == EXPORT_ORDER_MODES[2] and self.name_vars:
            self.open_reorder_dialog()

    def open_reorder_dialog(self):
        if not self.name_vars:
            messagebox.showinfo("No channels", "Choose a source WAV file first.", parent=self)
            return

        n = len(self.name_vars)
        order = self._current_order_list()

        win = ctk.CTkToplevel(self)
        win.title("Custom Export Order")
        win.geometry("400x480")
        win.configure(fg_color=self.DARK_SLATE)
        win.transient(self)
        win.after(120, win.grab_set)

        ctk.CTkLabel(win, text="Drag order is top to bottom \u2014 this is the order the\n"
                               "files are passed to the DAW on export.",
                     text_color=self.SILVER, font=self.font_small, justify="left").pack(anchor="w", padx=16, pady=(14, 6))

        listbox = tk.Listbox(win, bg=self.CARD_BG, fg=self.COOL_WHITE, selectbackground=self.RUST_RED,
                             selectforeground=self.WARM_CREAM, highlightthickness=0, borderwidth=0,
                             activestyle="none", exportselection=False)
        listbox.pack(fill="both", expand=True, padx=16, pady=6)

        def repopulate(sel=None):
            listbox.delete(0, tk.END)
            for ch in order:
                name = sanitize_name(self.name_vars[ch].get(), f"Track {ch+1:02d}")
                flag = "" if self.enabled_vars[ch].get() else "  (skipped)"
                listbox.insert(tk.END, f"{ch+1:02d}   {name}{flag}")
            if sel is not None and 0 <= sel < len(order):
                listbox.selection_set(sel)
                listbox.see(sel)

        def move(delta):
            sel = listbox.curselection()
            if not sel:
                return
            i = sel[0]
            j = i + delta
            if not (0 <= j < len(order)):
                return
            order[i], order[j] = order[j], order[i]
            repopulate(j)

        repopulate()

        mrow = ctk.CTkFrame(win, fg_color="transparent")
        mrow.pack(fill="x", padx=16, pady=(0, 6))
        btn_kwargs = {"fg_color": self.MUTED_BTN, "hover_color": self.MUTED_HOVER,
                      "text_color": self.COOL_WHITE, "font": self.font_bold, "width": 70}
        ctk.CTkButton(mrow, text="\u2191 Up", command=lambda: move(-1), **btn_kwargs).pack(side="left")
        ctk.CTkButton(mrow, text="\u2193 Down", command=lambda: move(1), **btn_kwargs).pack(side="left", padx=6)

        def reset():
            order[:] = list(range(n))
            repopulate()

        ctk.CTkButton(mrow, text="Reset", command=reset, **btn_kwargs).pack(side="left")

        def save_order():
            self.custom_order[str(n)] = list(order)
            self.app_config["custom_order"] = dict(self.custom_order)
            self.order_var.set(EXPORT_ORDER_MODES[2])
            self.export_order_mode = EXPORT_ORDER_MODES[2]
            self.app_config["export_order_mode"] = EXPORT_ORDER_MODES[2]
            self._queue_config_save()
            self._update_reorder_button()
            win.destroy()

        brow = ctk.CTkFrame(win, fg_color="transparent")
        brow.pack(fill="x", padx=16, pady=(6, 16))
        ctk.CTkButton(brow, text="Save Order", command=save_order, fg_color=self.RUST_RED,
                      hover_color="#A93226", text_color=self.WARM_CREAM,
                      font=self.font_bold).pack(side="right")
        ctk.CTkButton(brow, text="Cancel", command=win.destroy, fg_color=self.MUTED_BTN,
                      hover_color=self.MUTED_HOVER, text_color=self.COOL_WHITE,
                      font=self.font_bold).pack(side="right", padx=8)

    # ---------------- Recent sessions ----------------
    def _load_recent_sessions(self):
        raw = self.app_config.get("recent_sessions", [])
        sessions = []
        if isinstance(raw, list):
            for entry in raw:
                if isinstance(entry, list) and all(isinstance(p, str) for p in entry) and entry:
                    sessions.append(entry)
        return sessions[:MAX_RECENT]

    def _add_recent_session(self, paths):
        entry = list(paths)
        self.recent_sessions = [s for s in self.recent_sessions if s != entry]
        self.recent_sessions.insert(0, entry)
        self.recent_sessions = self.recent_sessions[:MAX_RECENT]
        self.app_config["recent_sessions"] = self.recent_sessions
        self._queue_config_save()
        self._refresh_recent_menu()

    def _refresh_recent_menu(self):
        if not hasattr(self, "recent_menu"):
            return
        self.recent_menu.delete(0, tk.END)
        if not self.recent_sessions:
            self.recent_menu.add_command(label="(nothing yet)", state="disabled")
            return
        for entry in self.recent_sessions:
            first = os.path.basename(entry[0])
            label = first if len(entry) == 1 else f"{first}  (+{len(entry)-1} more)"
            self.recent_menu.add_command(label=label, command=lambda e=entry: self.open_session(e))
        self.recent_menu.add_separator()
        self.recent_menu.add_command(label="Clear Menu", command=self.clear_recent_sessions)

    def clear_recent_sessions(self):
        self.recent_sessions = []
        self.app_config["recent_sessions"] = []
        self._queue_config_save()
        self._refresh_recent_menu()

    def open_session(self, paths):
        missing = [p for p in paths if not os.path.exists(p)]
        if missing:
            messagebox.showwarning(
                "Files missing",
                "These files have moved or been deleted:\n\n" + "\n".join(os.path.basename(p) for p in missing),
                parent=self)
            paths = [p for p in paths if os.path.exists(p)]
            if not paths:
                return
        self.source_paths = list(paths)
        self.refresh_source_list()
        self.validate_and_update_format()
        if self.source_paths:
            base = os.path.splitext(os.path.basename(self.source_paths[0]))[0]
            default_out = os.path.join(os.path.dirname(self.source_paths[0]), f"{base}_Tracks")
            self.output_dir_override = None
            self.output_var.set(f"Default: {default_out}")

    # ---------------- Menu bar / shortcuts ----------------
    def _build_menubar(self):
        mod_label = "Cmd" if IS_MAC else "Ctrl"
        self.menubar = tk.Menu(self)

        file_menu = tk.Menu(self.menubar, tearoff=0)
        file_menu.add_command(label="Open WAV Files\u2026", accelerator=f"{mod_label}+O", command=self.choose_source)
        self.recent_menu = tk.Menu(file_menu, tearoff=0)
        file_menu.add_cascade(label="Open Recent", menu=self.recent_menu)
        file_menu.add_separator()
        file_menu.add_command(label="Choose Output Folder\u2026", command=self.choose_output)
        file_menu.add_command(label="Export Tracks", accelerator=f"{mod_label}+E", command=self.start_export)
        file_menu.add_command(label="Cancel", accelerator="Esc", command=self.cancel_running_job)
        file_menu.add_separator()
        file_menu.add_command(label="Quit", command=self._on_close)
        self.menubar.add_cascade(label="File", menu=file_menu)

        edit_menu = tk.Menu(self.menubar, tearoff=0)
        edit_menu.add_command(label="Batch Rename\u2026", accelerator=f"{mod_label}+R", command=self.open_batch_rename)
        edit_menu.add_separator()
        edit_menu.add_command(label="Load Preset", command=self.load_preset)
        edit_menu.add_command(label="Save Preset As\u2026", accelerator=f"{mod_label}+S", command=self.save_preset_as)
        edit_menu.add_command(label="Delete Preset", command=self.delete_preset)
        edit_menu.add_separator()
        edit_menu.add_command(label="Enable All Channels", command=self.enable_all_channels)
        edit_menu.add_command(label="Skip Silent Channels", command=self.skip_silent_channels)
        edit_menu.add_command(label="Scan for Silent Channels", command=self.scan_silent_channels)
        edit_menu.add_separator()
        edit_menu.add_command(label="Edit Custom Export Order\u2026", command=self.open_reorder_dialog)
        self.menubar.add_cascade(label="Edit", menu=edit_menu)

        help_menu = tk.Menu(self.menubar, tearoff=0)
        help_menu.add_command(label="About XLive Splitter", command=self.show_about)
        self.menubar.add_cascade(label="Help", menu=help_menu)

        self._refresh_recent_menu()
        try:
            self.configure(menu=self.menubar)
        except Exception:
            # Older CustomTkinter builds reject unknown configure keys.
            self.tk.call(self._w, "configure", "-menu", self.menubar)

    def _bind_shortcuts(self):
        mod = "Command" if IS_MAC else "Control"
        bindings = {
            f"<{mod}-o>": lambda e: self.choose_source(),
            f"<{mod}-e>": lambda e: self.start_export(),
            f"<{mod}-s>": lambda e: self.save_preset_as(),
            f"<{mod}-r>": lambda e: self.open_batch_rename(),
            "<Escape>": lambda e: self.cancel_running_job(),
        }
        for sequence, handler in bindings.items():
            self.bind_all(sequence, handler)

    def show_about(self):
        messagebox.showinfo(
            "XLive Splitter",
            "Splits X32 / X-Live interleaved multitrack WAV recordings into named "
            "mono files, ready to drop into a DAW session.\n\n"
            "Filenames keep the original console channel number, so skipping a "
            "channel never renumbers the ones you keep.",
            parent=self)

    def cancel_running_job(self):
        """Escape / Cancel button: stops whichever background job is running."""
        if self.scan_cancel is not None and self.scan_thread is not None \
                and self.scan_thread.is_alive() and not self.scan_cancel.is_set():
            self.scan_cancel.set()
            self.status_label.configure(text="Cancelling scan\u2026")
            self.cancel_button.configure(state="disabled")
            return
        if self.cancel_flag is not None and self.export_thread is not None \
                and self.export_thread.is_alive() and not self.cancel_flag.is_set():
            self.cancel_flag.set()
            self.status_label.configure(text="Cancelling export\u2026")
            self.cancel_button.configure(state="disabled")

    # ---------------- Presets ----------------
    def load_preset(self):
        name = self.preset_var.get()
        if not name or name not in self.presets:
            messagebox.showinfo("Select a preset", "Choose a saved preset from the dropdown first.", parent=self)
            return
        names = self.presets[name]
        if not self.wav_format:
            messagebox.showinfo("Load a file first", "Choose a source WAV file before loading a preset.", parent=self)
            return
        if len(names) != self.wav_format.channels:
            if not messagebox.askyesno(
                "Channel count mismatch",
                f"Preset '{name}' has {len(names)} track names, but the loaded file has "
                f"{self.wav_format.channels} channels.\n\nApply anyway? Extra channels will keep "
                f"default names, and any extra preset names will be ignored.",
                parent=self
            ):
                return
        for i, var in enumerate(self.name_vars):
            if i < len(names):
                var.set(names[i])

    def save_preset_as(self):
        if not self.name_vars:
            messagebox.showinfo("Nothing to save", "Choose a source file and set track names first.", parent=self)
            return
        dialog = ctk.CTkInputDialog(text='Preset name (e.g. "Sunday Service"):', title="Save Preset")
        name = dialog.get_input()
        if not name:
            return
        self.presets[name] = [v.get() for v in self.name_vars]
        save_presets(self.presets)
        self.preset_combo.configure(values=sorted(self.presets.keys()))
        self.preset_var.set(name)
        messagebox.showinfo("Saved", f"Preset '{name}' saved.", parent=self)

    def delete_preset(self):
        name = self.preset_var.get()
        if not name or name not in self.presets:
            messagebox.showinfo("Select a preset", "Choose a saved preset from the dropdown first.", parent=self)
            return
        if messagebox.askyesno("Delete preset", f"Delete the preset '{name}'? This can't be undone.", parent=self):
            del self.presets[name]
            save_presets(self.presets)
            self.preset_combo.configure(values=sorted(self.presets.keys()))
            self.preset_var.set("")

    # ---------------- Output ----------------
    def choose_output(self):
        path = filedialog.askdirectory(parent=self, title="Choose output folder", initialdir=os.path.expanduser("~"))
        if path:
            self.output_dir_override = path
            self.output_var.set(path)

    # ---------------- DAW selection / automation toggles ----------------
    def _daw_status_text(self):
        if self.daw_path:
            return f"Will open: {self.daw_path}"
        return f"{self.daw_choice} not found automatically — click Locate… to select its .exe."

    def on_daw_selected(self, choice):
        self.daw_choice = choice
        self.daw_path = find_daw_executable(choice)
        self.app_config["daw_choice"] = self.daw_choice
        self.app_config["daw_path"] = self.daw_path
        save_config(self.app_config)
        self.daw_status_var.set(self._daw_status_text())

    def choose_daw_path(self):
        path = filedialog.askopenfilename(
            parent=self,
            title=f"Select {self.daw_choice} executable",
            filetypes=[("Executable", "*.exe"), ("All files", "*.*")]
        )
        if not path:
            return
        self.daw_path = path
        self.app_config["daw_choice"] = self.daw_choice
        self.app_config["daw_path"] = path
        save_config(self.app_config)
        self.daw_status_var.set(self._daw_status_text())

    def on_toggle_auto_open_folder(self):
        self.auto_open_folder = self.auto_open_folder_var.get()
        self.app_config["auto_open_folder"] = self.auto_open_folder
        save_config(self.app_config)

    def on_toggle_auto_open_daw(self):
        self.auto_open_daw = self.auto_open_daw_var.get()
        self.app_config["auto_open_daw"] = self.auto_open_daw
        save_config(self.app_config)

    # ---------------- Export ----------------
    def start_export(self):
        if not self.source_paths:
            return
        if self.export_thread is not None and self.export_thread.is_alive():
            return
        if self.scan_thread is not None and self.scan_thread.is_alive():
            messagebox.showinfo("Scan running", "Wait for the channel scan to finish first.", parent=self)
            return

        names = [sanitize_name(v.get(), f"Track {i+1:02d}") for i, v in enumerate(self.name_vars)]
        enabled = [v.get() for v in self.enabled_vars] if self.enabled_vars else [True] * len(names)

        active_names = [n for n, on in zip(names, enabled) if on]
        if not active_names:
            messagebox.showinfo("Nothing to export",
                                "Every channel is unticked. Tick at least one channel first.",
                                parent=self)
            return
        if len(set(active_names)) != len(active_names):
            if not messagebox.askyesno("Duplicate names",
                                        "Two or more tracks have the same name after sanitizing. "
                                        "Files may overwrite each other. Continue anyway?",
                                        parent=self):
                return

        if self.output_dir_override:
            out_dir = self.output_dir_override
        else:
            base = os.path.splitext(os.path.basename(self.source_paths[0]))[0]
            out_dir = os.path.join(os.path.dirname(self.source_paths[0]), f"{base}_Tracks")

        order = self._current_order_list()

        skipped = len(names) - len(active_names)
        self.export_button.configure(state="disabled", text="\u2699\ufe0f Exporting\u2026")
        self.cancel_button.configure(state="normal")
        self.progress.set(0)
        self.status_label.configure(
            text="Starting\u2026" if not skipped else f"Starting\u2026 ({skipped} channel(s) skipped)")

        self.cancel_flag = threading.Event()

        def progress_cb(done, total):
            self.progress_queue.put(("progress", done, total))

        def worker():
            try:
                out_paths = split_multitrack(self.source_paths, out_dir, names,
                                              progress_cb=progress_cb, cancel_flag=self.cancel_flag,
                                              enabled=enabled, order=order)
                self.progress_queue.put(("done", out_dir, out_paths))
            except InterruptedError:
                self.progress_queue.put(("cancelled", None, None))
            except Exception as e:
                self.progress_queue.put(("error", str(e), None))

        self.export_thread = threading.Thread(target=worker, daemon=True)
        self.export_thread.start()
        self.after(100, self.poll_progress)

    def poll_progress(self):
        try:
            while True:
                msg = self.progress_queue.get_nowait()
                kind = msg[0]
                if kind == "progress":
                    _, done, total = msg
                    pct = (done / total * 100) if total else 0
                    self.progress.set(pct / 100)
                    self.status_label.configure(text=f"Exporting\u2026 {pct:.0f}%")
                elif kind == "done":
                    _, out_dir, out_paths = msg
                    self.progress.set(1)
                    self.status_label.configure(text=f"Done \u2014 {len(out_paths)} files written.")
                    self.export_button.configure(state="normal", text="\u2699\ufe0f Export Tracks")
                    self.cancel_button.configure(state="disabled")
                    if self.auto_open_folder:
                        self.reveal_in_explorer(out_dir)
                    if self.auto_open_daw:
                        self.open_in_daw(out_paths)
                    return
                elif kind == "cancelled":
                    self.progress.set(0)
                    self.status_label.configure(text="Cancelled \u2014 partial files removed.")
                    self.export_button.configure(state="normal", text="\u2699\ufe0f Export Tracks")
                    self.cancel_button.configure(state="disabled")
                    return
                elif kind == "error":
                    self.status_label.configure(text="Error.")
                    self.export_button.configure(state="normal", text="\u2699\ufe0f Export Tracks")
                    self.cancel_button.configure(state="disabled")
                    messagebox.showerror("Export failed", msg[1], parent=self)
                    return
        except queue.Empty:
            pass
        self.after(100, self.poll_progress)

    def reveal_in_explorer(self, out_dir):
        try:
            os.startfile(out_dir)  # noqa: Windows-only API
        except Exception:
            try:
                subprocess.run(["explorer", out_dir])
            except Exception:
                pass

    def open_in_daw(self, out_paths):
        if not self.daw_path or not os.path.exists(self.daw_path):
            messagebox.showinfo(
                f"{self.daw_choice} not found",
                f"Couldn't find {self.daw_choice} automatically. Your tracks were exported "
                "successfully — use the 'Locate…' button in the After Export section, then "
                "export again, to have them open automatically next time.",
                parent=self
            )
            return
        try:
            subprocess.Popen([self.daw_path] + out_paths)
        except Exception as e:
            messagebox.showwarning(f"Couldn't open {self.daw_choice}", str(e), parent=self)
            print(f"Could not open in {self.daw_choice}: {e}")


if __name__ == "__main__":
    app = XLiveSplitterApp()
    app.mainloop()
