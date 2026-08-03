#!/usr/bin/env python3
"""
X32 Track Splitter (Windows / Ableton Edition)
-------------------
Takes the single interleaved multitrack WAV file recorded by an X32 / X-Live
card and exports each channel as its own named mono WAV file, using reusable
named presets (e.g. "Sunday Service", "Band Rehearsal Setup").

After export, it reveals the output folder in File Explorer and hands the
exported WAV files to Ableton Live (drag-and-drop-onto-the-.exe style),
which creates a new Live Set with each file loaded as a clip.

Requires: Python 3.9+, numpy, customtkinter, Windows 10/11
Run:      python x32_track_splitter_windows.py
"""

import glob
import json
import os
import re
import struct
import subprocess
import sys
import threading
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


def split_multitrack(input_paths, output_dir, names, progress_cb=None, cancel_flag=None):
    fmt = parse_wav_format(input_paths[0])
    channels = fmt.channels
    bits = fmt.bits_per_sample
    sampwidth = bits // 8
    if len(names) != channels:
        raise ValueError(f"You have {len(names)} track names but the file has {channels} channels.")

    os.makedirs(output_dir, exist_ok=True)
    writers = []
    out_paths = []
    try:
        for i, name in enumerate(names):
            path = os.path.join(output_dir, f"{i+1:02d}_{name}.wav")
            w = wave.open(path, "wb")
            w.setnchannels(1)
            w.setsampwidth(sampwidth)
            w.setframerate(fmt.sample_rate)
            writers.append(w)
            out_paths.append(path)

        total_bytes = sum(parse_wav_format(p).data_size for p in input_paths)
        bytes_done = 0
        frame_size = channels * sampwidth

        for arr, is_24bit in iter_deinterleaved(input_paths):
            if cancel_flag is not None and cancel_flag.is_set():
                raise InterruptedError("Export cancelled.")
            if is_24bit:
                for ch in range(channels):
                    writers[ch].writeframes(arr[:, ch, :].tobytes())
            else:
                for ch in range(channels):
                    writers[ch].writeframes(arr[:, ch].tobytes())
            bytes_done += arr.shape[0] * frame_size
            if progress_cb:
                progress_cb(min(bytes_done, total_bytes), total_bytes)
    finally:
        for w in writers:
            w.close()
    return out_paths


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
    path = os.path.join(base, "X32 Track Splitter")
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
# Ableton Live discovery (Windows)
# ============================================================================

def find_ableton_executable():
    """Best-effort search for an installed Ableton Live.exe on Windows."""
    search_roots = [
        os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
        os.environ.get("PROGRAMFILES", r"C:\Program Files"),
        os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
    ]
    candidates = []
    for root in search_roots:
        if not root or not os.path.isdir(root):
            continue
        pattern = os.path.join(root, "Ableton", "Live *", "Program", "Ableton Live*.exe")
        candidates.extend(glob.glob(pattern))

    if not candidates:
        return None

    # Prefer the highest version number found (e.g. "Live 12" over "Live 10")
    def version_key(path):
        m = re.search(r"Live (\d+)", path)
        return int(m.group(1)) if m else 0

    candidates.sort(key=version_key, reverse=True)
    return candidates[0]


# ============================================================================
# GUI
# ============================================================================

class X32SplitterApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        
        # Color Palette
        self.DARK_SLATE = "#2C3E50"
        self.CARD_BG = "#34495E"      # Layered lighter background for cards
        self.MUTED_BTN = "#435B71"    # Accent Restraint: Subtle dark gray buttons
        self.MUTED_HOVER = "#54718C"
        self.RUST_RED = "#C0392B"     # Reserved for Primary Actions
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
        self.title("X32 Track Splitter")
        self.geometry("680x860")
        self.minsize(600, 680)

        self.source_paths = []
        self.wav_format = None
        self.name_vars = []
        self.presets = load_presets()
        self.config = load_config()
        self.ableton_path = self.config.get("ableton_path") or find_ableton_executable()
        if self.ableton_path and not os.path.exists(self.ableton_path):
            self.ableton_path = find_ableton_executable()
        self.export_thread = None
        self.cancel_flag = None
        self.progress_queue = queue.Queue()

        self._build_ui()

    def _build_ui(self):
        pad = {"padx": 16, "pady": 10}

        # --- Base styling configs for widgets ---
        btn_kwargs = {"fg_color": self.MUTED_BTN, "text_color": self.COOL_WHITE, "hover_color": self.MUTED_HOVER, "font": self.font_bold}
        label_kwargs = {"text_color": self.WARM_CREAM, "font": self.font_main}
        title_kwargs = {"text_color": self.COOL_WHITE, "font": self.font_title}
        
        # Card-based Design: No borders, rounded corners, layered color depth
        frame_kwargs = {"fg_color": self.CARD_BG, "corner_radius": 8}

        # ==========================================
        # 1. Source Section (Collapsible)
        # ==========================================
        src_frame = ctk.CTkFrame(self, **frame_kwargs)
        
        self.src_expanded = True
        
        self.src_header_btn = ctk.CTkButton(src_frame, text="▼ 1. Source Recording", font=self.font_title, 
                                            text_color=self.COOL_WHITE, fg_color="transparent", 
                                            hover_color=self.MUTED_BTN, anchor="w", 
                                            command=self.toggle_source)
        self.src_header_btn.pack(fill="x", padx=6, pady=6)

        self.src_content_frame = ctk.CTkFrame(src_frame, fg_color="transparent")
        self.src_content_frame.pack(fill="x", padx=0, pady=0)

        row = ctk.CTkFrame(self.src_content_frame, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=8)
        ctk.CTkButton(row, text="📁 Choose WAV File(s)…", command=self.choose_source, **btn_kwargs).pack(side="left")
        ctk.CTkLabel(row, text="Select multiple files to stitch them together sequentially.", text_color=self.SILVER, font=self.font_small).pack(side="left", padx=10)

        self.source_list_frame = ctk.CTkScrollableFrame(self.src_content_frame, fg_color=self.DARK_SLATE, height=100, corner_radius=6)
        self.source_list_frame.pack(fill="x", padx=12, pady=(0, 8))
        
        self.refresh_source_list() # Loads the placeholder text initially

        self.format_label = ctk.CTkLabel(self.src_content_frame, text="", text_color=self.SILVER, font=self.font_small)
        self.format_label.pack(fill="x", padx=12, pady=(0, 10))


        # ==========================================
        # 2. Preset Section (Collapsible)
        # ==========================================
        preset_frame = ctk.CTkFrame(self, **frame_kwargs)
        
        self.preset_expanded = True
        
        self.preset_header_btn = ctk.CTkButton(preset_frame, text="▼ 2. Track Names", font=self.font_title, 
                                            text_color=self.COOL_WHITE, fg_color="transparent", 
                                            hover_color=self.MUTED_BTN, anchor="w", 
                                            command=self.toggle_preset)
        self.preset_header_btn.pack(fill="x", padx=6, pady=6)
        
        self.preset_content_frame = ctk.CTkFrame(preset_frame, fg_color="transparent")
        self.preset_content_frame.pack(fill="both", expand=True, padx=0, pady=0)

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

        # Inset dark slate area for tracks
        self.rows_frame = ctk.CTkScrollableFrame(self.preset_content_frame, fg_color=self.DARK_SLATE, corner_radius=6)
        self.rows_frame.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        self.placeholder_label = ctk.CTkLabel(self.rows_frame, text="Choose a source file to see channel rows here.", text_color=self.SILVER, font=self.font_small)
        self.placeholder_label.pack(padx=6, pady=20)


        # ==========================================
        # 3. Output Section (Collapsible)
        # ==========================================
        out_frame = ctk.CTkFrame(self, **frame_kwargs)
        
        self.out_expanded = True
        
        self.out_header_btn = ctk.CTkButton(out_frame, text="▼ 3. Output", font=self.font_title, 
                                            text_color=self.COOL_WHITE, fg_color="transparent", 
                                            hover_color=self.MUTED_BTN, anchor="w", 
                                            command=self.toggle_output)
        self.out_header_btn.pack(fill="x", padx=6, pady=6)

        self.out_content_frame = ctk.CTkFrame(out_frame, fg_color="transparent")
        self.out_content_frame.pack(fill="x", padx=12, pady=(0, 10))

        orow = ctk.CTkFrame(self.out_content_frame, fg_color="transparent")
        orow.pack(fill="x")
        ctk.CTkButton(orow, text="📁 Choose Folder…", command=self.choose_output, **btn_kwargs).pack(side="left")
        
        self.output_var = ctk.StringVar(value="(same folder as source, in a new subfolder)")
        ctk.CTkLabel(orow, textvariable=self.output_var, text_color=self.SILVER, font=self.font_small, wraplength=400).pack(side="left", padx=12)

        arow = ctk.CTkFrame(self.out_content_frame, fg_color="transparent")
        arow.pack(fill="x", pady=(10, 0))
        ctk.CTkButton(arow, text="🎚 Locate Ableton Live…", command=self.choose_ableton_path, **btn_kwargs).pack(side="left")
        self.ableton_var = ctk.StringVar(value=self._ableton_status_text())
        ctk.CTkLabel(arow, textvariable=self.ableton_var, text_color=self.SILVER, font=self.font_small, wraplength=380).pack(side="left", padx=12)


        # ==========================================
        # 4. Export (Action reserved for Rust Red)
        # ==========================================
        export_frame = ctk.CTkFrame(self, fg_color="transparent")
        
        self.export_button = ctk.CTkButton(export_frame, text="⚙️ Export Tracks", command=self.start_export, state="disabled",
                                           fg_color=self.RUST_RED, text_color=self.WARM_CREAM, hover_color="#A93226", 
                                           font=ctk.CTkFont(family="Segoe UI", size=15, weight="bold"), height=40)
        self.export_button.pack(fill="x")

        self.progress = ctk.CTkProgressBar(export_frame, progress_color=self.RUST_RED, fg_color=self.CARD_BG, height=8)
        self.progress.pack(fill="x", pady=(14, 6))
        self.progress.set(0)
        
        self.status_label = ctk.CTkLabel(export_frame, text="", text_color=self.SILVER, font=self.font_small)
        self.status_label.pack(fill="x")

        # ==========================================
        # Layout Order Fix (The "Pack Order Trick")
        # ==========================================
        src_frame.pack(side="top", fill="x", **pad)
        export_frame.pack(side="bottom", fill="x", **pad)
        out_frame.pack(side="bottom", fill="x", **pad)
        preset_frame.pack(side="top", fill="both", expand=True, **pad)
        
        self.output_dir_override = None

    # ---------------- UI Toggles ----------------
    def toggle_source(self):
        if self.src_expanded:
            self.src_content_frame.pack_forget()
            self.src_header_btn.configure(text="▶ 1. Source Recording")
            self.src_expanded = False
        else:
            self.src_content_frame.pack(fill="x", padx=0, pady=0)
            self.src_header_btn.configure(text="▼ 1. Source Recording")
            self.src_expanded = True

    def toggle_preset(self):
        if self.preset_expanded:
            self.preset_content_frame.pack_forget()
            self.preset_header_btn.configure(text="▶ 2. Track Names")
            self.preset_expanded = False
        else:
            self.preset_content_frame.pack(fill="both", expand=True, padx=0, pady=0)
            self.preset_header_btn.configure(text="▼ 2. Track Names")
            self.preset_expanded = True

    def toggle_output(self):
        if self.out_expanded:
            self.out_content_frame.pack_forget()
            self.out_header_btn.configure(text="▶ 3. Output")
            self.out_expanded = False
        else:
            self.out_content_frame.pack(fill="x", padx=12, pady=(0, 10))
            self.out_header_btn.configure(text="▼ 3. Output")
            self.out_expanded = True


    # ---------------- Source File Handling ----------------
    def choose_source(self):
        paths = filedialog.askopenfilenames(
            title="Select X32 multitrack WAV file(s)",
            filetypes=[("WAV files", "*.wav *.WAV"), ("All files", "*.*")]
        )
        if not paths:
            return
        
        self.source_paths.extend(paths)
        self.source_paths = sorted(list(set(self.source_paths)))
        
        self.refresh_source_list()
        self.validate_and_update_format()

        if self.source_paths:
            base = os.path.splitext(os.path.basename(self.source_paths[0]))[0]
            default_out = os.path.join(os.path.dirname(self.source_paths[0]), f"{base}_Tracks")
            self.output_dir_override = None
            self.output_var.set(f"Default: {default_out}")

    def refresh_source_list(self):
        # Clear existing rows
        for child in self.source_list_frame.winfo_children():
            child.destroy()
            
        # Display placeholder if empty
        if not self.source_paths:
            placeholder = ctk.CTkLabel(self.source_list_frame, text="No files selected.", text_color=self.SILVER, font=self.font_small)
            placeholder.pack(pady=15)
            return

        # Build custom UI rows with inline buttons
        for idx, p in enumerate(self.source_paths):
            row = ctk.CTkFrame(self.source_list_frame, fg_color="transparent")
            row.pack(fill="x", pady=2)

            lbl = ctk.CTkLabel(row, text=os.path.basename(p), font=self.font_main, text_color=self.COOL_WHITE)
            lbl.pack(side="left", padx=5)

            # Red hover reserved for destructive actions
            btn_del = ctk.CTkButton(row, text="⨂", width=28, height=28, fg_color="transparent", hover_color=self.RUST_RED, text_color=self.SILVER, font=self.font_bold, command=lambda i=idx: self.remove_source(i))
            btn_del.pack(side="right", padx=2)

            btn_down = ctk.CTkButton(row, text="↓", width=28, height=28, fg_color=self.MUTED_BTN, hover_color=self.MUTED_HOVER, font=self.font_bold, command=lambda i=idx: self.move_source_down(i))
            btn_down.pack(side="right", padx=2)

            btn_up = ctk.CTkButton(row, text="↑", width=28, height=28, fg_color=self.MUTED_BTN, hover_color=self.MUTED_HOVER, font=self.font_bold, command=lambda i=idx: self.move_source_up(i))
            btn_up.pack(side="right", padx=2)

    def move_source_up(self, idx):
        if idx > 0:
            self.source_paths[idx], self.source_paths[idx-1] = self.source_paths[idx-1], self.source_paths[idx]
            self.refresh_source_list()
            self.validate_and_update_format()

    def move_source_down(self, idx):
        if idx < len(self.source_paths) - 1:
            self.source_paths[idx], self.source_paths[idx+1] = self.source_paths[idx+1], self.source_paths[idx]
            self.refresh_source_list()
            self.validate_and_update_format()

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
            messagebox.showerror("Format mismatch", str(e))
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
        self.export_button.configure(state="normal")

    def build_channel_rows(self, n_channels):
        for child in self.rows_frame.winfo_children():
            child.destroy()
        self.name_vars = []

        preset_names = None
        if self.preset_var.get() in self.presets:
            candidate = self.presets[self.preset_var.get()]
            if len(candidate) == n_channels:
                preset_names = candidate

        header = ctk.CTkFrame(self.rows_frame, fg_color="transparent")
        header.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(header, text="Ch", width=30, text_color=self.SILVER, font=self.font_small).pack(side="left")
        ctk.CTkLabel(header, text="Track name", text_color=self.SILVER, font=self.font_small).pack(side="left", padx=(10, 0))

        for i in range(n_channels):
            row = ctk.CTkFrame(self.rows_frame, fg_color="transparent")
            row.pack(fill="x", pady=3)
            ctk.CTkLabel(row, text=f"{i+1:02d}", width=30, text_color=self.WARM_CREAM, font=self.font_bold).pack(side="left")
            
            default = preset_names[i] if preset_names else f"Track {i+1:02d}"
            var = ctk.StringVar(value=default)
            
            # Flush entry fields inside the dark frame
            entry = ctk.CTkEntry(row, textvariable=var, fg_color=self.CARD_BG, text_color=self.COOL_WHITE, font=self.font_main, border_width=0)
            entry.pack(side="left", padx=(10, 0), fill="x", expand=True)
            self.name_vars.append(var)


    # ---------------- Presets ----------------
    def load_preset(self):
        name = self.preset_var.get()
        if not name or name not in self.presets:
            messagebox.showinfo("Select a preset", "Choose a saved preset from the dropdown first.")
            return
        names = self.presets[name]
        if not self.wav_format:
            messagebox.showinfo("Load a file first", "Choose a source WAV file before loading a preset.")
            return
        if len(names) != self.wav_format.channels:
            if not messagebox.askyesno(
                "Channel count mismatch",
                f"Preset '{name}' has {len(names)} track names, but the loaded file has "
                f"{self.wav_format.channels} channels.\n\nApply anyway? Extra channels will keep "
                f"default names, and any extra preset names will be ignored."
            ):
                return
        for i, var in enumerate(self.name_vars):
            if i < len(names):
                var.set(names[i])

    def save_preset_as(self):
        if not self.name_vars:
            messagebox.showinfo("Nothing to save", "Choose a source file and set track names first.")
            return
        dialog = ctk.CTkInputDialog(text='Preset name (e.g. "Sunday Service"):', title="Save Preset")
        name = dialog.get_input()
        if not name:
            return
        self.presets[name] = [v.get() for v in self.name_vars]
        save_presets(self.presets)
        self.preset_combo.configure(values=sorted(self.presets.keys()))
        self.preset_var.set(name)
        messagebox.showinfo("Saved", f"Preset '{name}' saved.")

    def delete_preset(self):
        name = self.preset_var.get()
        if not name or name not in self.presets:
            messagebox.showinfo("Select a preset", "Choose a saved preset from the dropdown first.")
            return
        if messagebox.askyesno("Delete preset", f"Delete the preset '{name}'? This can't be undone."):
            del self.presets[name]
            save_presets(self.presets)
            self.preset_combo.configure(values=sorted(self.presets.keys()))
            self.preset_var.set("")

    # ---------------- Output ----------------
    def choose_output(self):
        path = filedialog.askdirectory(title="Choose output folder")
        if path:
            self.output_dir_override = path
            self.output_var.set(path)

    # ---------------- Ableton Live path ----------------
    def _ableton_status_text(self):
        if self.ableton_path:
            return f"Will open: {self.ableton_path}"
        return "Ableton Live not found automatically — click Locate to select it."

    def choose_ableton_path(self):
        path = filedialog.askopenfilename(
            title="Select Ableton Live.exe",
            filetypes=[("Ableton Live", "Ableton Live*.exe"), ("Executable", "*.exe"), ("All files", "*.*")]
        )
        if not path:
            return
        self.ableton_path = path
        self.config["ableton_path"] = path
        save_config(self.config)
        self.ableton_var.set(self._ableton_status_text())

    # ---------------- Export ----------------
    def start_export(self):
        if not self.source_paths:
            return
        names = [sanitize_name(v.get(), f"Track {i+1:02d}") for i, v in enumerate(self.name_vars)]
        if len(set(names)) != len(names):
            if not messagebox.askyesno("Duplicate names",
                                        "Two or more tracks have the same name after sanitizing. "
                                        "Files may overwrite each other. Continue anyway?"):
                return

        if self.output_dir_override:
            out_dir = self.output_dir_override
        else:
            base = os.path.splitext(os.path.basename(self.source_paths[0]))[0]
            out_dir = os.path.join(os.path.dirname(self.source_paths[0]), f"{base}_Tracks")

        self.export_button.configure(state="disabled", text="⚙️ Exporting…")
        self.progress.set(0)
        self.status_label.configure(text="Starting…")

        self.cancel_flag = threading.Event()

        def progress_cb(done, total):
            self.progress_queue.put(("progress", done, total))

        def worker():
            try:
                out_paths = split_multitrack(self.source_paths, out_dir, names,
                                              progress_cb=progress_cb, cancel_flag=self.cancel_flag)
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
                    self.status_label.configure(text=f"Exporting… {pct:.0f}%")
                elif kind == "done":
                    _, out_dir, out_paths = msg
                    self.progress.set(1)
                    self.status_label.configure(text=f"Done — {len(out_paths)} files written.")
                    self.export_button.configure(state="normal", text="⚙️ Export Tracks")
                    self.reveal_in_explorer(out_dir)
                    self.open_in_ableton(out_paths)
                    return
                elif kind == "cancelled":
                    self.status_label.configure(text="Cancelled.")
                    self.export_button.configure(state="normal", text="⚙️ Export Tracks")
                    return
                elif kind == "error":
                    self.status_label.configure(text="Error.")
                    self.export_button.configure(state="normal", text="⚙️ Export Tracks")
                    messagebox.showerror("Export failed", msg[1])
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

    def open_in_ableton(self, out_paths):
        # Kept name for internal call-site compatibility; opens Ableton Live on Windows.
        if not self.ableton_path or not os.path.exists(self.ableton_path):
            messagebox.showinfo(
                "Ableton Live not found",
                "Couldn't find Ableton Live automatically. Your tracks were exported "
                "successfully — use the 'Locate Ableton Live…' button in the Output "
                "section, then export again, to have them open automatically next time."
            )
            return
        try:
            # Passing WAV files as args mimics dragging them onto the Ableton Live
            # icon: it creates a new Live Set with each file loaded as a clip.
            subprocess.Popen([self.ableton_path] + out_paths)
        except Exception as e:
            messagebox.showwarning("Couldn't open Ableton Live", str(e))
            print(f"Could not open in Ableton Live: {e}")


if __name__ == "__main__":
    app = X32SplitterApp()
    app.mainloop()