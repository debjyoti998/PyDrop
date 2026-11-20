import os
import queue
import subprocess
import sys
import threading
import time
from tkinter import filedialog

import customtkinter as ctk
import qrcode
from PIL import Image, ImageOps

from server import HAS_CRYPTO, PyDropHTTPServer, format_bytes, get_local_ipv4_addresses

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".ogg"}
ARCHIVE_EXTS = {".zip", ".rar", ".7z", ".tar", ".gz"}


def open_with_default_app(filepath: str) -> bool:
    if not os.path.exists(filepath):
        return False
    if sys.platform.startswith("win"):
        os.startfile(filepath)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", filepath])
    else:
        subprocess.Popen(["xdg-open", filepath])
    return True


def reveal_in_folder(filepath: str) -> bool:
    if not os.path.exists(filepath):
        return False
    if sys.platform.startswith("win"):
        subprocess.Popen(["explorer", "/select,", os.path.normpath(filepath)])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", filepath])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(filepath)])
    return True


def get_badge_style(ext: str) -> tuple[str, str]:
    ext = ext.lower()
    if ext in IMAGE_EXTS:
        return ("#9d174d", ext.lstrip(".").upper() or "IMG")
    if ext in VIDEO_EXTS:
        return ("#6b21a8", ext.lstrip(".").upper() or "VID")
    if ext in AUDIO_EXTS:
        return ("#065f46", ext.lstrip(".").upper() or "AUD")
    if ext in ARCHIVE_EXTS:
        return ("#92400e", ext.lstrip(".").upper() or "ZIP")
    label = ext.lstrip(".").upper()[:4] if ext else "FILE"
    return ("#1e40af", label)


def make_thumbnail(filepath: str, size: tuple[int, int] = (42, 42)) -> ctk.CTkImage | None:
    ext = os.path.splitext(filepath)[1].lower()
    if ext not in IMAGE_EXTS or not os.path.isfile(filepath):
        return None
    try:
        with Image.open(filepath) as img:
            img = ImageOps.exif_transpose(img).convert("RGB")
            img.thumbnail(size, Image.Resampling.LANCZOS)
            canvas = Image.new("RGB", size, (30, 41, 59))
            offset = ((size[0] - img.width) // 2, (size[1] - img.height) // 2)
            canvas.paste(img, offset)
            return ctk.CTkImage(light_image=canvas, dark_image=canvas, size=size)
    except Exception:
        return None


class PyDropApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("PyDrop — Local P2P File Transfer & QR Pair Engine")
        self.geometry("1120x760")
        self.minsize(960, 650)

        self.event_queue: queue.Queue = queue.Queue()
        self.server: PyDropHTTPServer | None = None
        self.server_thread: threading.Thread | None = None
        self.receive_dir = os.path.join(os.path.expanduser("~"), "Downloads", "PyDrop")
        os.makedirs(self.receive_dir, exist_ok=True)

        self.active_transfer_widgets: dict[str, tuple[ctk.CTkFrame, ctk.CTkLabel, ctk.CTkProgressBar]] = {}
        self.sent_history: list[dict] = []
        self.received_meta: dict[str, tuple[str, str]] = {}
        self._saved_shared_files: list[str] = []
        self._last_device_snapshot: list[tuple] = []
        self.qr_ctk_image = None
        self.https_var = ctk.BooleanVar(value=HAS_CRYPTO)

        self._build_ui()
        self._refresh_received_files()
        self._start_server()
        self.after(100, self._poll_events)
        self.after(1500, self._poll_devices)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=0, minsize=315)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # --- Left Sidebar ---
        sidebar = ctk.CTkFrame(self, corner_radius=12)
        sidebar.grid(row=0, column=0, padx=(14, 7), pady=14, sticky="nsew")

        ctk.CTkLabel(sidebar, text="PyDrop Engine", font=ctk.CTkFont(size=22, weight="bold")).pack(pady=(16, 2))
        ctk.CTkLabel(sidebar, text="Offline Local Wi-Fi File Portal", text_color="gray").pack(pady=(0, 10))

        self.qr_label = ctk.CTkLabel(sidebar, text="Starting server...", width=220, height=220)
        self.qr_label.pack(pady=4)

        self.url_label = ctk.CTkLabel(sidebar, text="", font=ctk.CTkFont(size=11), wraplength=270, text_color="#60a5fa")
        self.url_label.pack(pady=(4, 10))

        self.https_switch = ctk.CTkSwitch(
            sidebar,
            text="HTTPS Mode (Android fix)",
            variable=self.https_var,
            command=self._restart_with_new_mode,
        )
        self.https_switch.pack(fill="x", padx=18, pady=(0, 10))
        if not HAS_CRYPTO:
            self.https_switch.configure(state="disabled", text="HTTPS (install cryptography)")

        ctk.CTkLabel(sidebar, text="Network Interface (IPv4):", anchor="w").pack(fill="x", padx=18)
        self.ip_list = get_local_ipv4_addresses()
        self.ip_var = ctk.StringVar(value=self.ip_list[0])
        self.ip_menu = ctk.CTkOptionMenu(sidebar, values=self.ip_list, variable=self.ip_var, command=lambda _: self._update_qr())
        self.ip_menu.pack(fill="x", padx=18, pady=(4, 10))

        ctk.CTkLabel(sidebar, text="Receive Folder:", anchor="w").pack(fill="x", padx=18)
        self.dir_label = ctk.CTkLabel(sidebar, text=self.receive_dir, font=ctk.CTkFont(size=11), wraplength=260, text_color="gray")
        self.dir_label.pack(fill="x", padx=18, pady=(2, 6))
        ctk.CTkButton(sidebar, text="Change Receive Folder", command=self._choose_receive_dir).pack(fill="x", padx=18, pady=(0, 10))

        self.toggle_btn = ctk.CTkButton(sidebar, text="Stop Server", fg_color="#dc2626", hover_color="#b91c1c", command=self._toggle_server)
        self.toggle_btn.pack(fill="x", padx=18, pady=(4, 16))

        # --- Right Main Panel ---
        main_panel = ctk.CTkFrame(self, fg_color="transparent")
        main_panel.grid(row=0, column=1, padx=(7, 14), pady=14, sticky="nsew")
        main_panel.grid_columnconfigure(0, weight=1)
        main_panel.grid_rowconfigure(0, weight=0)
        main_panel.grid_rowconfigure(1, weight=0)
        main_panel.grid_rowconfigure(2, weight=1)

        # Connected Devices Card
        devices_card = ctk.CTkFrame(main_panel, corner_radius=12)
        devices_card.grid(row=0, column=0, sticky="ew", pady=(0, 10))

        dev_header = ctk.CTkFrame(devices_card, fg_color="transparent")
        dev_header.pack(fill="x", padx=14, pady=(10, 4))
        ctk.CTkLabel(dev_header, text="Connected Devices", font=ctk.CTkFont(size=14, weight="bold")).pack(side="left")
        self.conn_status_pill = ctk.CTkLabel(
            dev_header,
            text="●  No devices connected",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#94a3b8",
        )
        self.conn_status_pill.pack(side="right")

        self.devices_scroll = ctk.CTkScrollableFrame(devices_card, height=62, fg_color="transparent")
        self.devices_scroll.pack(fill="x", padx=8, pady=(0, 8))
        self._render_devices([])

        # Active Transfers Card
        active_card = ctk.CTkFrame(main_panel, corner_radius=12)
        active_card.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        ctk.CTkLabel(active_card, text="Active Transfers", font=ctk.CTkFont(size=14, weight="bold"), anchor="w").pack(fill="x", padx=14, pady=(10, 4))
        self.transfers_scroll = ctk.CTkScrollableFrame(active_card, height=80, fg_color="transparent")
        self.transfers_scroll.pack(fill="x", padx=8, pady=(0, 8))
        self.idle_transfer_lbl = ctk.CTkLabel(self.transfers_scroll, text="No active transfers — waiting for devices...", text_color="gray")
        self.idle_transfer_lbl.pack(pady=16)

        self.tabs = ctk.CTkTabview(main_panel, corner_radius=12)
        self.tabs.grid(row=2, column=0, sticky="nsew")

        tab_recv = self.tabs.add("Received Files")
        tab_share = self.tabs.add("Share to Mobile")
        tab_sent = self.tabs.add("Sent Files")
        tab_logs = self.tabs.add("Session Logs")

        # 1. Received Files Tab
        recv_top = ctk.CTkFrame(tab_recv, fg_color="transparent")
        recv_top.pack(fill="x", padx=6, pady=(4, 6))
        self.recv_count_lbl = ctk.CTkLabel(recv_top, text="0 files in receive folder", font=ctk.CTkFont(size=13, weight="bold"))
        self.recv_count_lbl.pack(side="left")
        ctk.CTkButton(recv_top, text="Open Folder", width=110, command=self._open_receive_folder).pack(side="right", padx=(6, 0))
        ctk.CTkButton(recv_top, text="Refresh", width=85, fg_color="#475569", command=self._refresh_received_files).pack(side="right")

        self.recv_scroll = ctk.CTkScrollableFrame(tab_recv, fg_color="transparent")
        self.recv_scroll.pack(fill="both", expand=True)

        # 2. Share to Mobile Tab
        share_top = ctk.CTkFrame(tab_share, fg_color="transparent")
        share_top.pack(fill="x", padx=6, pady=(4, 6))
        self.share_count_lbl = ctk.CTkLabel(share_top, text="Files available for mobile download", font=ctk.CTkFont(size=13, weight="bold"))
        self.share_count_lbl.pack(side="left")
        ctk.CTkButton(share_top, text="Clear All", width=85, fg_color="#475569", command=self._clear_shared).pack(side="right", padx=(6, 0))
        ctk.CTkButton(share_top, text="+ Add Files", width=110, command=self._add_shared_files).pack(side="right")

        self.share_scroll = ctk.CTkScrollableFrame(tab_share, fg_color="transparent")
        self.share_scroll.pack(fill="both", expand=True)

        # 3. Sent Files Tab
        sent_top = ctk.CTkFrame(tab_sent, fg_color="transparent")
        sent_top.pack(fill="x", padx=6, pady=(4, 6))
        self.sent_count_lbl = ctk.CTkLabel(sent_top, text="0 files sent this session", font=ctk.CTkFont(size=13, weight="bold"))
        self.sent_count_lbl.pack(side="left")
        ctk.CTkButton(sent_top, text="Clear History", width=105, fg_color="#475569", command=self._clear_sent_history).pack(side="right")

        self.sent_scroll = ctk.CTkScrollableFrame(tab_sent, fg_color="transparent")
        self.sent_scroll.pack(fill="both", expand=True)

        # 4. Session Logs Tab
        self.log_box = ctk.CTkTextbox(tab_logs, state="disabled")
        self.log_box.pack(fill="both", expand=True, padx=6, pady=6)

    def _render_devices(self, devices: list[dict]):
        for w in self.devices_scroll.winfo_children():
            w.destroy()

        if not self.server:
            self.conn_status_pill.configure(text="●  Server stopped", text_color="#64748b")
            ctk.CTkLabel(
                self.devices_scroll,
                text="Start the server to accept device connections.",
                text_color="gray",
            ).pack(pady=12)
            return

        if not devices:
            self.conn_status_pill.configure(text="●  No devices connected", text_color="#94a3b8")
            ctk.CTkLabel(
                self.devices_scroll,
                text="Scan the QR code on your phone or tablet to pair.",
                text_color="gray",
            ).pack(pady=12)
            return

        count = len(devices)
        self.conn_status_pill.configure(
            text=f"●  {count} device{'s' if count != 1 else ''} connected",
            text_color="#22c55e",
        )

        for dev in devices:
            row = ctk.CTkFrame(self.devices_scroll, corner_radius=8, fg_color=("#e2e8f0", "#1e293b"))
            row.pack(fill="x", padx=4, pady=3)

            badge = ctk.CTkLabel(
                row,
                text="LIVE",
                width=46,
                height=24,
                corner_radius=6,
                fg_color="#15803d",
                text_color="#ffffff",
                font=ctk.CTkFont(size=10, weight="bold"),
            )
            badge.pack(side="left", padx=(10, 8), pady=7)

            info_text = f"{dev['name']}  ({dev['ip']})  •  {dev['platform']}"
            ctk.CTkLabel(
                row,
                text=info_text,
                font=ctk.CTkFont(size=12, weight="bold"),
                anchor="w",
            ).pack(side="left", fill="x", expand=True)

            state_txt = (
                f"{dev['active_transfers']} active transfer{'s' if dev['active_transfers'] != 1 else ''}"
                if dev["active_transfers"] > 0
                else f"Connected {dev['connected_at']}"
            )
            ctk.CTkLabel(
                row,
                text=state_txt,
                font=ctk.CTkFont(size=11),
                text_color="#94a3b8",
            ).pack(side="right", padx=10)

    def _poll_devices(self):
        try:
            devices = self.server.get_active_devices() if self.server else []
            snapshot = [
                (d["ip"], d["name"], d["platform"], d["connected_at"], d["active_transfers"], bool(self.server))
                for d in devices
            ]
            if snapshot != self._last_device_snapshot:
                self._last_device_snapshot = snapshot
                self._render_devices(devices)
        except Exception:
            pass
        finally:
            self.after(1500, self._poll_devices)

    def _create_file_card(self, parent, filepath: str, subtitle: str, actions: list[tuple[str, str, callable]]):
        card = ctk.CTkFrame(parent, corner_radius=10, fg_color=("#e2e8f0", "#1e293b"))
        card.pack(fill="x", padx=4, pady=4)

        filename = os.path.basename(filepath)
        ext = os.path.splitext(filename)[1]
        thumb = make_thumbnail(filepath)

        if thumb:
            card._thumb_ref = thumb
            icon_lbl = ctk.CTkLabel(card, image=thumb, text="", width=42, height=42)
        else:
            bg_color, tag = get_badge_style(ext)
            icon_lbl = ctk.CTkLabel(
                card, text=tag, width=42, height=42, corner_radius=8,
                fg_color=bg_color, text_color="#ffffff", font=ctk.CTkFont(size=11, weight="bold"),
            )
        icon_lbl.pack(side="left", padx=(10, 10), pady=8)

        info = ctk.CTkFrame(card, fg_color="transparent")
        info.pack(side="left", fill="both", expand=True, pady=6)
        ctk.CTkLabel(info, text=filename, font=ctk.CTkFont(size=13, weight="bold"), anchor="w").pack(fill="x")
        ctk.CTkLabel(info, text=subtitle, font=ctk.CTkFont(size=11), text_color="gray", anchor="w").pack(fill="x")

        btn_box = ctk.CTkFrame(card, fg_color="transparent")
        btn_box.pack(side="right", padx=10)
        for text, color, cmd in actions:
            ctk.CTkButton(btn_box, text=text, width=68, height=30, fg_color=color, command=cmd).pack(side="left", padx=3)

    def _refresh_received_files(self):
        for w in self.recv_scroll.winfo_children():
            w.destroy()

        if not os.path.isdir(self.receive_dir):
            self.recv_count_lbl.configure(text="Receive folder not found")
            return

        entries = []
        try:
            for name in os.listdir(self.receive_dir):
                full = os.path.join(self.receive_dir, name)
                if os.path.isfile(full):
                    entries.append((full, os.path.getmtime(full), os.path.getsize(full)))
        except OSError as exc:
            self._log(f"Could not scan receive folder: {exc}")
            return

        entries.sort(key=lambda item: item[1], reverse=True)
        total_files = len(entries)
        shown = entries[:50]

        suffix = " (showing latest 50)" if total_files > 50 else ""
        self.recv_count_lbl.configure(text=f"{total_files} file{'s' if total_files != 1 else ''} in receive folder{suffix}")

        if not shown:
            ctk.CTkLabel(self.recv_scroll, text="No files received yet. Scan the QR code from your phone to send files.", text_color="gray").pack(pady=36)
            return

        for full, mtime, size in shown:
            peer, ts = self.received_meta.get(full, ("Local folder", time.strftime("%d %b %H:%M", time.localtime(mtime))))
            sub = f"{format_bytes(size)}  •  {peer}  •  {ts}"
            actions = [
                ("Open", "#2563eb", lambda p=full: self._handle_open_file(p)),
                ("Locate", "#475569", lambda p=full: self._handle_locate_file(p)),
            ]
            self._create_file_card(self.recv_scroll, full, sub, actions)

    def _open_receive_folder(self):
        os.makedirs(self.receive_dir, exist_ok=True)
        if sys.platform.startswith("win"):
            os.startfile(self.receive_dir)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", self.receive_dir])
        else:
            subprocess.Popen(["xdg-open", self.receive_dir])

    def _handle_open_file(self, filepath: str):
        if not open_with_default_app(filepath):
            self._log(f"File no longer exists: {filepath}")
            self._refresh_received_files()

    def _handle_locate_file(self, filepath: str):
        if not reveal_in_folder(filepath):
            self._log(f"File no longer exists: {filepath}")
            self._refresh_received_files()

    def _refresh_shared_list(self):
        for w in self.share_scroll.winfo_children():
            w.destroy()

        if not self.server:
            self.share_count_lbl.configure(text="Server stopped")
            ctk.CTkLabel(self.share_scroll, text="Start the server to share files with mobile devices.", text_color="gray").pack(pady=36)
            return

        items = self.server.get_shared_files_meta()
        self.share_count_lbl.configure(text=f"{len(items)} file{'s' if len(items) != 1 else ''} shared to mobile")

        if not items:
            ctk.CTkLabel(self.share_scroll, text="Click '+ Add Files' to make PC files downloadable on your phone.", text_color="gray").pack(pady=36)
            return

        for item in items:
            with self.server.lock:
                filepath = self.server.shared_files.get(item["id"], "")
            if not filepath:
                continue
            sub = f"{item['size_human']}  •  Ready on mobile portal"
            actions = [
                ("Open", "#2563eb", lambda p=filepath: self._handle_open_file(p)),
                ("Remove", "#dc2626", lambda fid=item["id"]: self._remove_shared_item(fid)),
            ]
            self._create_file_card(self.share_scroll, filepath, sub, actions)

    def _remove_shared_item(self, fid: str):
        if self.server:
            self.server.remove_shared_file(fid)
            self._refresh_shared_list()

    def _refresh_sent_history(self):
        for w in self.sent_scroll.winfo_children():
            w.destroy()

        self.sent_count_lbl.configure(text=f"{len(self.sent_history)} file{'s' if len(self.sent_history) != 1 else ''} sent this session")
        if not self.sent_history:
            ctk.CTkLabel(self.sent_scroll, text="No files downloaded by mobile devices yet.", text_color="gray").pack(pady=36)
            return

        for entry in self.sent_history:
            sub = f"{format_bytes(entry['size'])}  •  To {entry['peer']}  •  {entry['speed']:.1f} MB/s  •  {entry['time']}"
            actions = [
                ("Open", "#2563eb", lambda p=entry["filepath"]: self._handle_open_file(p)),
                ("Locate", "#475569", lambda p=entry["filepath"]: self._handle_locate_file(p)),
            ]
            self._create_file_card(self.sent_scroll, entry["filepath"], sub, actions)

    def _clear_sent_history(self):
        self.sent_history.clear()
        self._refresh_sent_history()

    def _restart_with_new_mode(self):
        saved_files = self._stop_server_sync()
        self._start_server(preserved_files=saved_files)

    def _start_server(self, preserved_files: list[str] | None = None):
        if self.server is not None:
            return
        self.toggle_btn.configure(state="disabled", text="Starting...")
        use_https = self.https_var.get() and HAS_CRYPTO

        started = False
        for port in (8080, 8000, 8888, 0):
            try:
                self.server = PyDropHTTPServer(
                    ("0.0.0.0", port),
                    self.receive_dir,
                    self.event_queue,
                    use_https=use_https,
                )
                started = True
                break
            except OSError:
                continue

        if not started or self.server is None:
            self.toggle_btn.configure(state="normal", text="Start Server", fg_color="#16a34a", hover_color="#15803d")
            self._log("Failed to bind any local port.")
            return

        if preserved_files:
            for p in preserved_files:
                if os.path.isfile(p):
                    self.server.add_shared_file(p)

        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

        self.toggle_btn.configure(state="normal", text="Stop Server", fg_color="#dc2626", hover_color="#b91c1c")
        self._update_qr()
        self._refresh_shared_list()
        self._last_device_snapshot = []
        self._render_devices([])
        proto = "HTTPS" if self.server.use_https else "HTTP"
        self._log(f"{proto} server listening on port {self.server.server_address[1]} (Receive dir: {self.receive_dir})")

    def _stop_server_sync(self) -> list[str]:
        if self.server is None:
            return []
        srv = self.server
        self.server = None
        with srv.lock:
            saved_files = list(srv.shared_files.values())
        srv.shutdown()
        srv.server_close()
        if self.server_thread and self.server_thread.is_alive():
            self.server_thread.join(timeout=2.0)
        self.server_thread = None
        return saved_files

    def _stop_server(self):
        if self.server is None:
            return
        self.toggle_btn.configure(state="disabled", text="Stopping...")
        srv = self.server
        self.server = None
        with srv.lock:
            self._saved_shared_files = list(srv.shared_files.values())

        def _worker():
            try:
                srv.shutdown()
                srv.server_close()
            finally:
                self.after(0, self._on_server_stopped)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_server_stopped(self):
        self.server_thread = None
        self.qr_ctk_image = None
        self.qr_label.configure(image="", text="Server Stopped")
        self.url_label.configure(text="")
        self.toggle_btn.configure(state="normal", text="Start Server", fg_color="#16a34a", hover_color="#15803d")
        self._last_device_snapshot = []
        self._render_devices([])
        self._refresh_shared_list()
        self._log("Server stopped.")

    def _toggle_server(self):
        if self.server is not None:
            self._stop_server()
        else:
            self._start_server(preserved_files=self._saved_shared_files)

    def _update_qr(self):
        if not self.server:
            return
        ip = self.ip_var.get()
        port = self.server.server_address[1]
        scheme = "https" if self.server.use_https else "http"
        url = f"{scheme}://{ip}:{port}/?token={self.server.token}"
        self.url_label.configure(text=url)

        qr = qrcode.QRCode(border=2, box_size=8)
        qr.add_data(url)
        qr.make(fit=True)
        pil_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        pil_img = pil_img.resize((220, 220), Image.Resampling.NEAREST)

        self.qr_ctk_image = ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=(220, 220))
        self.qr_label.configure(image=self.qr_ctk_image, text="")

    def _choose_receive_dir(self):
        selected = filedialog.askdirectory(initialdir=self.receive_dir)
        if selected:
            self.receive_dir = selected
            self.dir_label.configure(text=selected)
            if self.server:
                self.server.receive_dir = selected
            self._refresh_received_files()
            self._log(f"Receive folder updated: {selected}")

    def _add_shared_files(self):
        if not self.server:
            self._log("Start the server before sharing files.")
            return
        paths = filedialog.askopenfilenames(title="Select files to share with mobile devices")
        for p in paths:
            self.server.add_shared_file(p)
        self._refresh_shared_list()

    def _clear_shared(self):
        if self.server:
            self.server.clear_shared_files()
            self._refresh_shared_list()

    def _log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{ts}] {msg}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _poll_events(self):
        try:
            while not self.event_queue.empty():
                ev = self.event_queue.get_nowait()
                etype = ev["type"]

                if etype == "device_connected":
                    self._log(f"Device connected: {ev['name']} ({ev['ip']}) [{ev['platform']}]")
                    if self.server:
                        self._render_devices(self.server.get_active_devices())

                elif etype == "device_disconnected":
                    self._log(f"Device disconnected: {ev['name']} ({ev['ip']}) — {ev.get('reason', 'offline')}")
                    if self.server:
                        self._render_devices(self.server.get_active_devices())

                elif etype == "transfer_start":
                    if self.idle_transfer_lbl.winfo_ismapped():
                        self.idle_transfer_lbl.pack_forget()
                    tid = ev["tid"]
                    arrow = "↑ Receiving" if ev["direction"] == "UP" else "↓ Sending"
                    row = ctk.CTkFrame(self.transfers_scroll)
                    row.pack(fill="x", padx=4, pady=3)
                    lbl = ctk.CTkLabel(row, text=f"{arrow} {ev['filename']} ({format_bytes(ev['total'])}) — {ev['peer']}", anchor="w")
                    lbl.pack(fill="x", padx=8, pady=(4, 2))
                    bar = ctk.CTkProgressBar(row)
                    bar.set(0)
                    bar.pack(fill="x", padx=8, pady=(0, 6))
                    self.active_transfer_widgets[tid] = (row, lbl, bar)

                elif etype == "transfer_progress":
                    tid = ev["tid"]
                    if tid in self.active_transfer_widgets:
                        _, lbl, bar = self.active_transfer_widgets[tid]
                        total = max(ev["total"], 1)
                        pct = ev["done"] / total
                        bar.set(pct)
                        speed_mb = ev["speed"] / (1024 * 1024)
                        base_text = lbl.cget("text").split(" | ")[0]
                        lbl.configure(text=f"{base_text} | {pct*100:.0f}% ({speed_mb:.1f} MB/s)")

                elif etype == "transfer_done":
                    tid = ev["tid"]
                    if tid in self.active_transfer_widgets:
                        row, _, _ = self.active_transfer_widgets.pop(tid)
                        row.destroy()
                    if not self.active_transfer_widgets:
                        self.idle_transfer_lbl.pack(pady=16)

                    direction = "Received" if ev["direction"] == "UP" else "Sent"
                    if ev["ok"]:
                        speed_mb = ev["speed"] / (1024 * 1024)
                        self._log(f"{direction} '{ev['filename']}' ({format_bytes(ev['total'])}) at {speed_mb:.1f} MB/s [{ev['peer']}]")
                        filepath = ev.get("filepath") or os.path.join(self.receive_dir, ev["filename"])

                        if ev["direction"] == "UP":
                            self.received_meta[filepath] = (f"From {ev['peer']}", time.strftime("%H:%M:%S"))
                            self._refresh_received_files()
                            self.tabs.set("Received Files")
                        else:
                            self.sent_history.insert(0, {
                                "filepath": filepath,
                                "size": ev["total"],
                                "peer": ev["peer"],
                                "speed": speed_mb,
                                "time": time.strftime("%H:%M:%S"),
                            })
                            self._refresh_sent_history()
                            self.tabs.set("Sent Files")
                    else:
                        self._log(f"Transfer failed '{ev['filename']}' [{ev['peer']}]: {ev.get('error', 'Unknown error')}")
        except Exception as exc:
            self._log(f"GUI update error: {exc}")
        finally:
            self.after(100, self._poll_events)

    def _on_close(self):
        self._stop_server_sync()
        self.destroy()


if __name__ == "__main__":
    app = PyDropApp()
    app.mainloop()
