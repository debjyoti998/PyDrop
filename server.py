import html
import ipaddress
import json
import mimetypes
import os
import queue
import re
import secrets
import socket
import ssl
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

CHUNK_SIZE = 1024 * 1024  # 1 MB fixed streaming buffer
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PORTAL_HTML_PATH = os.path.join(BASE_DIR, "port site.html")
DEVICE_TIMEOUT_SEC = 12.0


def parse_device_label(user_agent: str) -> tuple[str, str]:
    """Extract a concise human-readable device name and platform from User-Agent."""
    ua = user_agent or ""
    if "iPhone" in ua:
        return ("iPhone", "iOS Safari")
    if "iPad" in ua:
        return ("iPad", "iPadOS")
    if "Android" in ua:
        model = "Android Device"
        m = re.search(r"Android [^;)]+;\s*([^;)]+)", ua)
        if m:
            candidate = m.group(1).split("Build/")[0].strip()
            if candidate and candidate.lower() not in ("k", "wv", "linux"):
                model = candidate
        browser = "Chrome" if "Chrome" in ua else ("Firefox" if "Firefox" in ua else "Browser")
        return (model, f"Android • {browser}")
    if "Windows" in ua:
        return ("Windows PC", "Desktop Browser")
    if "Macintosh" in ua or "Mac OS X" in ua:
        return ("Mac", "macOS Browser")
    if "Linux" in ua:
        return ("Linux PC", "Desktop Browser")
    return ("Connected Device", "Web Portal")


def get_local_ipv4_addresses() -> list[str]:
    """Detect local LAN IPv4 addresses completely offline."""
    ips = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            primary_ip = s.getsockname()[0]
            if primary_ip and not primary_ip.startswith("127."):
                ips.append(primary_ip)
    except OSError:
        pass

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except socket.gaierror:
        pass

    if not ips:
        ips.append("127.0.0.1")
    return ips


def generate_local_tls_cert(ip_addresses: list[str]) -> tuple[str, str]:
    """Generate an offline self-signed X.509 certificate with LAN IP SANs."""
    cert_dir = os.path.join(os.path.expanduser("~"), ".pydrop", "certs")
    os.makedirs(cert_dir, exist_ok=True)
    cert_path = os.path.join(cert_dir, "pydrop_cert.pem")
    key_path = os.path.join(cert_dir, "pydrop_key.pem")

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "PyDrop Local")])

    san_entries = [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    for addr in ip_addresses:
        try:
            san_entries.append(x509.IPAddress(ipaddress.ip_address(addr)))
        except ValueError:
            pass

    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    with open(key_path, "wb") as f:
        f.write(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))

    return cert_path, key_path


def format_bytes(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} B"
        size /= 1024
    return f"{size:.1f} PB"


def safe_unique_path(directory: str, filename: str) -> str:
    """Prevent path traversal and auto-rename colliding filenames."""
    clean_name = os.path.basename(filename).strip() or f"upload_{int(time.time())}"
    base, ext = os.path.splitext(clean_name)
    candidate = os.path.join(directory, clean_name)
    counter = 1
    while os.path.exists(candidate):
        candidate = os.path.join(directory, f"{base} ({counter}){ext}")
        counter += 1
    return candidate


def render_portal_html(token: str, template_path: str = PORTAL_HTML_PATH) -> str:
    """Load external HTML portal template and inject the active session token."""
    with open(template_path, "r", encoding="utf-8") as f:
        template = f.read()
    return template.replace("__SESSION_TOKEN__", html.escape(token))


class PyDropHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address,
        receive_dir: str,
        event_queue: queue.Queue,
        use_https: bool = False,
        portal_path: str = PORTAL_HTML_PATH,
    ):
        if not os.path.isfile(portal_path):
            raise FileNotFoundError(f"Portal HTML file not found: {portal_path}")

        self.receive_dir = receive_dir
        self.event_queue = event_queue
        self.portal_path = portal_path
        self.token = secrets.token_urlsafe(6)
        self.shared_files: dict[str, str] = {}
        self.connected_devices: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.use_https = False

        super().__init__(server_address, PyDropRequestHandler)

        if use_https and HAS_CRYPTO:
            cert_path, key_path = generate_local_tls_cert(get_local_ipv4_addresses())
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
            self.socket = ctx.wrap_socket(self.socket, server_side=True)
            self.use_https = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[0]
        if exc and issubclass(exc, (ssl.SSLError, ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)

    def register_device_activity(self, ip: str, user_agent: str, cid: str = ""):
        device_key = f"{ip}:{cid}" if cid else ip
        name, platform = parse_device_label(user_agent)
        now = time.monotonic()
        is_new = False

        with self.lock:
            existing = self.connected_devices.get(device_key)
            if existing is None:
                is_new = True
                self.connected_devices[device_key] = {
                    "key": device_key,
                    "ip": ip,
                    "name": name,
                    "platform": platform,
                    "connected_at": time.strftime("%H:%M:%S"),
                    "last_seen": now,
                    "active_transfers": 0,
                }
            else:
                existing["last_seen"] = now
                if existing["name"] == "Connected Device" and name != "Connected Device":
                    existing["name"] = name
                    existing["platform"] = platform

        if is_new:
            self.emit("device_connected", ip=ip, name=name, platform=platform)

    def adjust_device_transfers(self, ip: str, cid: str, delta: int):
        device_key = f"{ip}:{cid}" if cid else ip
        with self.lock:
            dev = self.connected_devices.get(device_key)
            if dev:
                dev["active_transfers"] = max(0, dev["active_transfers"] + delta)
                dev["last_seen"] = time.monotonic()

    def remove_device(self, ip: str, cid: str = ""):
        device_key = f"{ip}:{cid}" if cid else ip
        removed = None
        with self.lock:
            removed = self.connected_devices.pop(device_key, None)
        if removed:
            self.emit("device_disconnected", ip=removed["ip"], name=removed["name"], reason="closed portal")

    def get_active_devices(self) -> list[dict]:
        now = time.monotonic()
        expired = []
        active = []
        with self.lock:
            for key, dev in list(self.connected_devices.items()):
                if dev["active_transfers"] == 0 and (now - dev["last_seen"]) > DEVICE_TIMEOUT_SEC:
                    expired.append(self.connected_devices.pop(key))
                else:
                    active.append({
                        "ip": dev["ip"],
                        "name": dev["name"],
                        "platform": dev["platform"],
                        "connected_at": dev["connected_at"],
                        "active_transfers": dev["active_transfers"],
                        "idle_sec": int(now - dev["last_seen"]),
                    })
        for dev in expired:
            self.emit("device_disconnected", ip=dev["ip"], name=dev["name"], reason="timed out")
        return active

    def add_shared_file(self, filepath: str) -> str:
        with self.lock:
            for fid, path in self.shared_files.items():
                if path == filepath:
                    return fid
            fid = secrets.token_hex(4)
            self.shared_files[fid] = filepath
            return fid

    def remove_shared_file(self, fid: str):
        with self.lock:
            self.shared_files.pop(fid, None)

    def clear_shared_files(self):
        with self.lock:
            self.shared_files.clear()

    def get_shared_files_meta(self) -> list[dict]:
        with self.lock:
            items = []
            for fid, path in list(self.shared_files.items()):
                if os.path.isfile(path):
                    size = os.path.getsize(path)
                    items.append({
                        "id": fid,
                        "name": os.path.basename(path),
                        "size": size,
                        "size_human": format_bytes(size),
                    })
                else:
                    self.shared_files.pop(fid, None)
            return items

    def emit(self, event_type: str, **payload):
        self.event_queue.put({"type": event_type, **payload})


class PyDropRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def _authenticate(self, query_params: dict) -> bool:
        token = query_params.get("token", [""])[0]
        return secrets.compare_digest(token, self.server.token)

    def _track_peer(self, query_params: dict) -> str:
        cid = query_params.get("cid", [""])[0][:48]
        ua = self.headers.get("User-Agent", "")
        self.server.register_device_activity(self.client_address[0], ua, cid)
        return cid

    def _send_json(self, data, status=HTTPStatus.OK):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def do_HEAD(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)

        if not self._authenticate(query):
            self.send_error(HTTPStatus.UNAUTHORIZED)
            return

        cid = self._track_peer(query)

        if parsed.path.startswith("/api/download/"):
            fid = parsed.path.removeprefix("/api/download/")
            with self.server.lock:
                filepath = self.server.shared_files.get(fid)
            if not filepath or not os.path.isfile(filepath):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._stream_download(filepath, head_only=True, cid=cid)
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)

        if not self._authenticate(query):
            self.send_error(HTTPStatus.UNAUTHORIZED, "Invalid or missing session token. Please scan the QR code.")
            return

        cid = self._track_peer(query)

        if parsed.path == "/":
            try:
                page = render_portal_html(self.server.token, self.server.portal_path)
            except OSError as exc:
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"Failed to load portal HTML: {exc}")
                return
            body = page.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            return

        if parsed.path == "/api/ping":
            self._send_json({"ok": True})
            return

        if parsed.path == "/api/files":
            self._send_json(self.server.get_shared_files_meta())
            return

        if parsed.path.startswith("/api/download/"):
            fid = parsed.path.removeprefix("/api/download/")
            with self.server.lock:
                filepath = self.server.shared_files.get(fid)
            if not filepath or not os.path.isfile(filepath):
                self.send_error(HTTPStatus.NOT_FOUND, "File no longer available")
                return
            self._stream_download(filepath, head_only=False, cid=cid)
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)

        if not self._authenticate(query):
            self.send_error(HTTPStatus.UNAUTHORIZED, "Invalid session token")
            return

        cid = query.get("cid", [""])[0][:48]

        if parsed.path == "/api/bye":
            content_len = int(self.headers.get("Content-Length", "0") or "0")
            if content_len > 0:
                self.rfile.read(min(content_len, 4096))
            self.server.remove_device(self.client_address[0], cid)
            self._send_json({"ok": True})
            return

        self._track_peer(query)

        if parsed.path == "/api/upload":
            self._stream_upload(cid=cid)
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def _stream_upload(self, cid: str = ""):
        try:
            content_length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
            return

        if content_length < 0:
            self.send_error(HTTPStatus.LENGTH_REQUIRED, "Content-Length required")
            return

        raw_name = self.headers.get("X-Filename", "uploaded_file")
        filename = urllib.parse.unquote(raw_name)
        dest_path = safe_unique_path(self.server.receive_dir, filename)
        final_name = os.path.basename(dest_path)
        transfer_id = secrets.token_hex(4)
        client_ip = self.client_address[0]

        self.server.adjust_device_transfers(client_ip, cid, 1)
        self.server.emit(
            "transfer_start",
            tid=transfer_id,
            direction="UP",
            filename=final_name,
            total=content_length,
            peer=client_ip,
        )

        received = 0
        start_time = time.monotonic()
        last_ui_update = start_time

        try:
            with open(dest_path, "wb") as f:
                while received < content_length:
                    to_read = min(CHUNK_SIZE, content_length - received)
                    chunk = self.rfile.read(to_read)
                    if not chunk:
                        raise ConnectionResetError("Client disconnected before upload finished")
                    f.write(chunk)
                    received += len(chunk)

                    now = time.monotonic()
                    if now - last_ui_update >= 0.2:
                        speed = received / max(now - start_time, 0.01)
                        self.server.adjust_device_transfers(client_ip, cid, 0)
                        self.server.emit("transfer_progress", tid=transfer_id, done=received, total=content_length, speed=speed)
                        last_ui_update = now

            elapsed = max(time.monotonic() - start_time, 0.01)
            self.server.emit(
                "transfer_done",
                tid=transfer_id,
                direction="UP",
                filename=final_name,
                filepath=dest_path,
                total=content_length,
                speed=content_length / elapsed,
                peer=client_ip,
                ok=True,
            )
            self._send_json({"status": "ok", "saved_as": final_name})

        except Exception as exc:
            if os.path.exists(dest_path):
                try:
                    os.remove(dest_path)
                except OSError:
                    pass
            self.server.emit(
                "transfer_done",
                tid=transfer_id,
                direction="UP",
                filename=final_name,
                filepath=dest_path,
                total=content_length,
                speed=0,
                peer=client_ip,
                ok=False,
                error=str(exc),
            )
            try:
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            except OSError:
                pass
        finally:
            self.server.adjust_device_transfers(client_ip, cid, -1)

    def _stream_download(self, filepath: str, head_only: bool = False, cid: str = ""):
        file_size = os.path.getsize(filepath)
        filename = os.path.basename(filepath)
        mime_type, _ = mimetypes.guess_type(filepath)
        mime_type = mime_type or "application/octet-stream"
        client_ip = self.client_address[0]
        transfer_id = secrets.token_hex(4)

        start = 0
        end = file_size - 1
        status = HTTPStatus.OK
        range_header = self.headers.get("Range")

        if range_header and range_header.startswith("bytes="):
            try:
                spec = range_header.removeprefix("bytes=").split(",")[0].strip()
                s_str, e_str = spec.split("-", 1)
                start = int(s_str) if s_str else 0
                end = int(e_str) if e_str else file_size - 1
                if start > end or end >= file_size:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{file_size}")
                    self.end_headers()
                    return
                status = HTTPStatus.PARTIAL_CONTENT
            except ValueError:
                pass

        bytes_to_send = max(0, end - start + 1)
        self.send_response(status)
        self.send_header("Content-Type", mime_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(bytes_to_send))
        quoted_name = urllib.parse.quote(filename)
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quoted_name}")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.end_headers()

        if head_only:
            return

        self.server.adjust_device_transfers(client_ip, cid, 1)
        self.server.emit(
            "transfer_start",
            tid=transfer_id,
            direction="DOWN",
            filename=filename,
            total=bytes_to_send,
            peer=client_ip,
        )

        sent = 0
        start_time = time.monotonic()
        last_ui_update = start_time

        try:
            with open(filepath, "rb") as f:
                f.seek(start)
                while sent < bytes_to_send:
                    chunk = f.read(min(CHUNK_SIZE, bytes_to_send - sent))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    sent += len(chunk)

                    now = time.monotonic()
                    if now - last_ui_update >= 0.2:
                        speed = sent / max(now - start_time, 0.01)
                        self.server.adjust_device_transfers(client_ip, cid, 0)
                        self.server.emit("transfer_progress", tid=transfer_id, done=sent, total=bytes_to_send, speed=speed)
                        last_ui_update = now

            self.wfile.flush()
            elapsed = max(time.monotonic() - start_time, 0.01)
            self.server.emit(
                "transfer_done",
                tid=transfer_id,
                direction="DOWN",
                filename=filename,
                filepath=filepath,
                total=bytes_to_send,
                speed=bytes_to_send / elapsed,
                peer=client_ip,
                ok=True,
            )
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
            self.server.emit(
                "transfer_done",
                tid=transfer_id,
                direction="DOWN",
                filename=filename,
                filepath=filepath,
                total=bytes_to_send,
                speed=0,
                peer=client_ip,
                ok=False,
                error="Peer disconnected or download cancelled",
            )
        finally:
            self.server.adjust_device_transfers(client_ip, cid, -1)
