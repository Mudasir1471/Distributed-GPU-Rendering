#!/usr/bin/env python3
"""
Client Desktop GUI  (CustomTkinter)

    pip install customtkinter
    python app.py

All networking happens in a background thread; the GUI thread only reads a
thread-safe queue (tkinter widgets must never be touched from other threads).
"""
import json
import os
import queue
import threading
import time
from tkinter import filedialog, messagebox

import customtkinter as ctk

from common import PORT, PRESETS, RESOLUTIONS
from network import OffloadClient, OffloadError

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "client_config.json")
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("GPU Task Offloader - Client")
        self.geometry("960x780")
        self.minsize(860, 700)
        self.events = queue.Queue()
        self.cancel_evt = threading.Event()
        self.busy = False
        self._build_ui()
        self._load_config()
        self.after(100, self._poll)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(5, weight=1)

        ctk.CTkLabel(self, text="Distributed GPU Task Offloader",
                     font=ctk.CTkFont(size=22, weight="bold")).grid(
            row=0, column=0, padx=20, pady=(16, 4), sticky="w")

        # --- connection
        f = ctk.CTkFrame(self)
        f.grid(row=1, column=0, padx=20, pady=6, sticky="ew")
        f.grid_columnconfigure(7, weight=1)
        ctk.CTkLabel(f, text="Server IP").grid(row=0, column=0, padx=(12, 4), pady=12)
        self.ip = ctk.CTkEntry(f, width=140, placeholder_text="192.168.1.1")
        self.ip.grid(row=0, column=1, padx=4)
        ctk.CTkLabel(f, text="Port").grid(row=0, column=2, padx=(12, 4))
        self.port = ctk.CTkEntry(f, width=70)
        self.port.grid(row=0, column=3, padx=4)
        ctk.CTkLabel(f, text="Token").grid(row=0, column=4, padx=(12, 4))
        self.token = ctk.CTkEntry(f, width=130, show="*", placeholder_text="(optional)")
        self.token.grid(row=0, column=5, padx=4)
        self.test_btn = ctk.CTkButton(f, text="Test Connection", width=130,
                                      command=self.on_test)
        self.test_btn.grid(row=0, column=6, padx=12)
        self.status = ctk.CTkLabel(f, text="● not checked", text_color="gray", anchor="w")
        self.status.grid(row=0, column=7, padx=4, sticky="ew")

        # --- files
        f = ctk.CTkFrame(self)
        f.grid(row=2, column=0, padx=20, pady=6, sticky="ew")
        f.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(f, text="Input video").grid(row=0, column=0, padx=12, pady=(12, 6), sticky="w")
        self.in_entry = ctk.CTkEntry(f)
        self.in_entry.grid(row=0, column=1, padx=4, pady=(12, 6), sticky="ew")
        ctk.CTkButton(f, text="Browse...", width=100, command=self.pick_input).grid(
            row=0, column=2, padx=12, pady=(12, 6))
        ctk.CTkLabel(f, text="Save output as").grid(row=1, column=0, padx=12, pady=(6, 12), sticky="w")
        self.out_entry = ctk.CTkEntry(f)
        self.out_entry.grid(row=1, column=1, padx=4, pady=(6, 12), sticky="ew")
        ctk.CTkButton(f, text="Browse...", width=100, command=self.pick_output).grid(
            row=1, column=2, padx=12, pady=(6, 12))

        # --- render settings
        f = ctk.CTkFrame(self)
        f.grid(row=3, column=0, padx=20, pady=6, sticky="ew")
        ctk.CTkLabel(f, text="Resolution").grid(row=0, column=0, padx=(12, 4), pady=12)
        self.res = ctk.CTkOptionMenu(f, values=list(RESOLUTIONS), width=110)
        self.res.set("720p")
        self.res.grid(row=0, column=1, padx=4)
        ctk.CTkLabel(f, text="Bitrate").grid(row=0, column=2, padx=(20, 4))
        self.bitrate = ctk.CTkComboBox(f, values=["2M", "4M", "8M", "16M", "25M"], width=90)
        self.bitrate.set("4M")
        self.bitrate.grid(row=0, column=3, padx=4)
        ctk.CTkLabel(f, text="Preset").grid(row=0, column=4, padx=(20, 4))
        self.preset = ctk.CTkSegmentedButton(f, values=PRESETS)
        self.preset.set("Balanced")
        self.preset.grid(row=0, column=5, padx=4)

        # --- actions + progress
        f = ctk.CTkFrame(self)
        f.grid(row=4, column=0, padx=20, pady=6, sticky="ew")
        f.grid_columnconfigure(2, weight=1)
        self.start_btn = ctk.CTkButton(f, text="▶  Start Render", width=150, height=36,
                                       command=self.on_start)
        self.start_btn.grid(row=0, column=0, padx=12, pady=12)
        self.cancel_btn = ctk.CTkButton(f, text="Cancel", width=90, height=36, state="disabled",
                                        fg_color="#8a2f2f", hover_color="#a33a3a",
                                        command=self.on_cancel)
        self.cancel_btn.grid(row=0, column=1, padx=4)
        self.stage_lbl = ctk.CTkLabel(f, text="Idle", anchor="w")
        self.stage_lbl.grid(row=0, column=2, padx=12, sticky="ew")
        self.bar = ctk.CTkProgressBar(f)
        self.bar.set(0)
        self.bar.grid(row=1, column=0, columnspan=3, padx=12, pady=(0, 12), sticky="ew")

        # --- log terminal
        self.logbox = ctk.CTkTextbox(self, font=ctk.CTkFont(family="Consolas", size=12),
                                     state="disabled", wrap="word")
        self.logbox.grid(row=5, column=0, padx=20, pady=(6, 16), sticky="nsew")

    # ------------------------------------------------------------- helpers
    def log(self, msg):
        """Thread-safe: may be called from any thread."""
        self.events.put(("log", msg))

    def _write_log(self, msg):
        self.logbox.configure(state="normal")
        self.logbox.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def _poll(self):
        try:
            while True:
                kind, *data = self.events.get_nowait()
                if kind == "log":
                    self._write_log(data[0])
                elif kind == "progress":
                    stage, pct, detail = data
                    if pct < 0:
                        self.bar.configure(mode="indeterminate")
                        self.bar.start()
                        self.stage_lbl.configure(text=detail)
                    else:
                        self.bar.stop()
                        self.bar.configure(mode="determinate")
                        self.bar.set(pct / 100)
                        self.stage_lbl.configure(text=f"{stage.upper()}  {pct:5.1f}%   {detail}")
                elif kind == "conn_ok":
                    info = data[0]
                    self.status.configure(
                        text=f"● online | {info['latency_avg_ms']:.1f} ms | {info['gpu']}",
                        text_color="#3ec46d")
                    self.test_btn.configure(state="normal")
                elif kind == "conn_fail":
                    self.status.configure(text="● unreachable", text_color="#e05252")
                    self.test_btn.configure(state="normal")
                    self._write_log(f"ERROR: {data[0]}")
                elif kind == "done":
                    self._finish(True, data[0])
                elif kind == "error":
                    self._write_log(f"ERROR: {data[0]}")
                    self._finish(False, None)
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _set_busy(self, busy):
        self.busy = busy
        self.start_btn.configure(state="disabled" if busy else "normal")
        self.test_btn.configure(state="disabled" if busy else "normal")
        self.cancel_btn.configure(state="normal" if busy else "disabled")

    def _finish(self, ok, metrics):
        self._set_busy(False)
        self.bar.stop()
        self.bar.configure(mode="determinate")
        if ok:
            self.bar.set(1)
            self.stage_lbl.configure(text="Finished ✔")
            self._write_log(
                "─── RESULT ───\n"
                f"  upload   : {metrics['upload_s']:.2f}s\n"
                f"  render   : {metrics['encode_s']:.2f}s ({metrics['server_stats'].get('encoder')})\n"
                f"  download : {metrics['download_s']:.2f}s\n"
                f"  total    : {metrics['total_s']:.2f}s  "
                f"(network overhead {metrics['overhead_s']:.2f}s)\n"
                f"  GPU util : avg {metrics['server_stats'].get('gpu_util_avg', 'n/a')}% "
                f"/ max {metrics['server_stats'].get('gpu_util_max', 'n/a')}%")
            messagebox.showinfo("Done", "Render finished and verified.")
        else:
            self.bar.set(0)
            self.stage_lbl.configure(text="Failed / cancelled")

    def _client(self):
        host = self.ip.get().strip()
        if not host:
            raise ValueError("Enter the server IP address")
        try:
            port = int(self.port.get() or PORT)
        except ValueError:
            raise ValueError("Port must be a number")
        self._save_config()
        return OffloadClient(
            host, port, self.token.get(), on_log=self.log,
            on_progress=lambda s, p, d: self.events.put(("progress", s, p, d)))

    # ------------------------------------------------------------- actions
    def pick_input(self):
        p = filedialog.askopenfilename(
            title="Select input video",
            filetypes=[("Video", "*.mp4 *.mkv *.mov *.avi *.webm *.flv *.wmv"), ("All", "*.*")])
        if p:
            self.in_entry.delete(0, "end")
            self.in_entry.insert(0, p)
            root, _ = os.path.splitext(p)
            self.out_entry.delete(0, "end")
            self.out_entry.insert(0, root + "_offloaded.mp4")

    def pick_output(self):
        p = filedialog.asksaveasfilename(defaultextension=".mp4",
                                         filetypes=[("MP4", "*.mp4")])
        if p:
            self.out_entry.delete(0, "end")
            self.out_entry.insert(0, p)

    def on_test(self):
        try:
            client = self._client()
        except ValueError as e:
            messagebox.showwarning("Check input", str(e))
            return
        self.test_btn.configure(state="disabled")
        self.status.configure(text="● checking...", text_color="gray")

        def work():
            try:
                self.events.put(("conn_ok", client.check_worker()))
            except OffloadError as e:
                self.events.put(("conn_fail", str(e)))
        threading.Thread(target=work, daemon=True).start()

    def on_start(self):
        in_path, out_path = self.in_entry.get().strip(), self.out_entry.get().strip()
        if not os.path.isfile(in_path):
            messagebox.showwarning("Check input", "Select a valid input video file")
            return
        if not out_path:
            messagebox.showwarning("Check input", "Choose where to save the output")
            return
        try:
            client = self._client()
        except ValueError as e:
            messagebox.showwarning("Check input", str(e))
            return
        settings = {"resolution": self.res.get(), "bitrate": self.bitrate.get().strip(),
                    "preset": self.preset.get()}
        self.cancel_evt.clear()
        self._set_busy(True)
        self.bar.set(0)
        self.log(f"Starting job: {os.path.basename(in_path)} | {settings}")

        def work():
            try:
                self.events.put(("done", client.run_job(in_path, out_path, settings,
                                                        self.cancel_evt)))
            except OffloadError as e:
                self.events.put(("error", str(e)))
            except Exception as e:  # never let the thread die silently
                self.events.put(("error", f"Unexpected error: {e!r}"))
        threading.Thread(target=work, daemon=True).start()

    def on_cancel(self):
        self.cancel_evt.set()
        self.log("Cancel requested ...")

    # -------------------------------------------------------------- config
    def _load_config(self):
        cfg = {}
        try:
            with open(CONFIG_FILE) as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            pass
        self.ip.insert(0, cfg.get("host", "192.168.1.1"))
        self.port.insert(0, str(cfg.get("port", PORT)))
        self.log("Ready. Enter the server IP, press 'Test Connection', pick a video, press Start.")

    def _save_config(self):
        try:
            with open(CONFIG_FILE, "w") as f:
                json.dump({"host": self.ip.get().strip(), "port": self.port.get().strip()}, f)
        except OSError:
            pass

    def _on_close(self):
        self.cancel_evt.set()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
