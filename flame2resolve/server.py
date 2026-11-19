"""Password-gated, single-origin Flame archive conversion service."""

from collections import defaultdict, deque
from dataclasses import dataclass, field
import hashlib
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import mimetypes
from pathlib import Path
import re
import secrets
import shutil
import socket
from threading import BoundedSemaphore, Event, Lock, Thread
import time
from urllib.parse import quote

from . import archives, conversion
from .auth import ROUNDS, parse_verifier

LOG = logging.getLogger("flame2resolve")
STATIC = Path(__file__).with_name("static")
MAX_TOTAL = 1_000_000_000
CHUNK = 8 * 1024 ** 2
TTL = 3600
COOKIE = "flame2resolve"
JOB_API = re.compile(r"/api/upload/([a-f0-9]{32})(?:/(\d{1,2}|inspect|export|download))?")


@dataclass
class Upload:
    owner: str
    workspace: Path
    entries: list
    header: str
    updated: float
    lock: Lock = field(default_factory=Lock)
    plans: dict = field(default_factory=dict)
    listing: dict | None = None
    result: tuple | None = None


class Application(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, host, port, workspace, verifier, origin):
        super().__init__((host, port), Handler)
        self.origin = origin.rstrip("/")
        self.workspace = workspace
        self.salt, self.verifier = parse_verifier(verifier)
        self.guard = Lock()
        self.slots = BoundedSemaphore(12)
        self.inspect_slot = BoundedSemaphore(1)
        self.sessions = {}
        self.uploads = {}
        self.attempts = defaultdict(deque)
        self.global_attempts = deque()
        self.done = Event()
        self.workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
        for folder in self.workspace.iterdir():
            if folder.is_dir() and re.fullmatch(r"[a-f0-9]{32}", folder.name):
                shutil.rmtree(folder)
        Thread(target=self.reap, daemon=True).start()

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            try:
                request.settimeout(1)
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def remove_job(self, key, job):
        del self.uploads[key]
        shutil.rmtree(job.workspace)

    def reap(self):
        while not self.done.wait(60):
            now = time.monotonic()
            with self.guard:
                for session, expires in list(self.sessions.items()):
                    if expires < now:
                        del self.sessions[session]
                for ip, attempts in list(self.attempts.items()):
                    if not attempts or now - attempts[-1] > 600:
                        del self.attempts[ip]
                for key, job in list(self.uploads.items()):
                    if (now-job.updated > TTL or job.owner not in self.sessions) and job.lock.acquire(False):
                        try:
                            self.remove_job(key, job)
                        finally:
                            job.lock.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "Flame2Resolve"
    sys_version = ""

    def setup(self):
        self.request.settimeout(30)
        super().setup()

    def log_message(self, format, *args):
        # Upload names, project paths and bearer cookies are private.
        return

    def reply(self, data, status=200, content_type="application/json; charset=utf-8", extra=()):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        headers = (
            ("Content-Type", content_type),
            ("Content-Length", str(len(data))),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
            ("X-Frame-Options", "DENY"),
            ("Referrer-Policy", "no-referrer"),
            ("Strict-Transport-Security", "max-age=31536000"),
            ("Content-Security-Policy", "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"),
            *extra,
        )
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def body(self, max_size, expected_type):
        if self.headers.get("Transfer-Encoding") or self.headers.get_content_type() != expected_type:
            raise Rejected(415, "Unexpected request format.")
        sizes = self.headers.get_all("Content-Length", [])
        if len(sizes) != 1 or not re.fullmatch(r"\d{1,10}", sizes[0]):
            raise Rejected(411, "A valid request size is required.")
        size = int(sizes[0])
        if size > max_size:
            raise Rejected(413, "Request exceeds the upload limit.")
        data = self.rfile.read(size)
        if len(data) != size:
            raise Rejected(400, "Upload was interrupted.")
        return data

    def json(self):
        data = json.loads(self.body(48*1024, "application/json"))
        if not isinstance(data, dict):
            raise Rejected(400, "Expected a request object.")
        return data

    def session(self):
        jar = SimpleCookie(self.headers.get("Cookie", ""))
        token = jar[COOKIE].value if COOKIE in jar else ""
        with self.server.guard:
            if self.server.sessions.get(token, -1) < time.monotonic():
                return ""
        return token

    def cookie(self, token):
        secure = "; Secure" if self.server.origin.startswith("https:") else ""
        return "Set-Cookie", f"{COOKIE}={token}; Max-Age={TTL if token else 0}; Path=/; HttpOnly; SameSite=Strict{secure}"

    def login(self):
        password = self.json().get("password")
        if not isinstance(password,str) or len(password) > 512:
            raise Rejected(400, "Enter the development password.")
        ip = self.client_address[0]
        now = time.monotonic()
        with self.server.guard:
            attempts = self.server.attempts[ip]
            if len(self.server.attempts) > 2048:
                self.server.attempts.pop(next(iter(self.server.attempts)))
            for records in (attempts, self.server.global_attempts):
                while records and now-records[0] > 300:
                    records.popleft()
            if len(attempts) >= 10 or len(self.server.global_attempts) >= 120:
                raise Rejected(429, "Too many attempts. Try again in a few minutes.")
            attempts.append(now)
            self.server.global_attempts.append(now)
        verifier = hashlib.pbkdf2_hmac("sha256", password.encode(), self.server.salt, ROUNDS)
        if not secrets.compare_digest(verifier, self.server.verifier):
            raise Rejected(401, "That password didn’t match.")
        token = secrets.token_hex(32)
        with self.server.guard:
            self.server.sessions[token] = now + TTL
            self.server.attempts.pop(ip, None)
        self.reply({"authenticated": True}, extra=(self.cookie(token),))

    def start_upload(self, token):
        entries = self.json().get("files")
        header = archives.validate_files(entries)
        if sum(entry["size"] for entry in entries) > MAX_TOTAL:
            raise Rejected(413, "All archive files combined must be 1 GB or smaller.")
        with self.server.guard:
            if sum(job.owner == token for job in self.server.uploads.values()) >= 3 or len(self.server.uploads) >= 6:
                raise Rejected(429, "The upload queue is full. Remove an earlier upload or try again later.")
            key = secrets.token_hex(16)
            folder = self.server.workspace / key
            folder.mkdir(mode=0o700)
            self.server.uploads[key] = Upload(token, folder, entries, header, time.monotonic())
        self.reply({"id": key, "chunk": CHUNK}, 201)

    def add_chunk(self, job, index):
        if index >= len(job.entries) or job.listing is not None:
            raise Rejected(409, "This archive can no longer accept files.")
        entry = job.entries[index]
        target = job.workspace / entry["name"]
        current = target.stat().st_size if target.exists() else 0
        if self.headers.get("X-Offset") != str(current):
            raise Rejected(409, "File offset differs. Restart the upload.")
        data = self.body(min(CHUNK, entry["size"] - current), "application/octet-stream")
        if not data:
            raise Rejected(400, "Empty upload chunk.")
        if not current:
            signatures = {job.header: b"\x80\x82\xa7\xd6", job.header+"_1.seg": b"\x1f\x8b\x08"}
            magic = signatures.get(entry["name"], b"# Discreet flame_archive log file - ")
            if not data.startswith(magic):
                raise Rejected(415, "File signature does not match a native Flame archive.")
        with target.open("ab") as output:
            output.write(data)
        self.reply({"received": current + len(data)})

    def inspect(self, job):
        if job.listing is None:
            for entry in job.entries:
                file = job.workspace / entry["name"]
                if not file.exists() or file.stat().st_size != entry["size"]:
                    raise Rejected(400, "Finish uploading the archive files first.")
            if not self.server.inspect_slot.acquire(False):
                raise Rejected(503, "Another archive is being read. Try again shortly.")
            try:
                entries, job.plans = archives.inspect(job.workspace / job.header)
                job.listing = {"timelines": entries, "bases": archives.source_bases(job.plans.values())}
                for file in job.workspace.iterdir():
                    file.unlink()
            finally:
                self.server.inspect_slot.release()
        self.reply(job.listing)

    def export(self, job, key):
        request = self.json()
        if not isinstance(request.get("timeline"), str) or request["timeline"] not in job.plans:
            raise Rejected(400, "Select a recoverable timeline.")
        plan = job.plans[request["timeline"]]
        mode = request.get("scale")
        enabled = request.get("remap") is True
        rules = archives.mappings(request.get("mappings", [])) if enabled else []
        binary, report = conversion.convert(plan, mode, rules, enabled)
        same_name = sum(item.name == plan.name for item in job.plans.values())
        frame_count = max(cut.record + cut.duration for cut in plan.cuts)
        filename = f"{plan.name} - {frame_count} frames" if same_name > 1 else plan.name
        job.result = (filename, binary)
        self.reply({"url": f"/api/upload/{key}/download", **report})

    def route_job(self, match, method, token):
        key, action = match.groups()
        with self.server.guard:
            job = self.server.uploads.get(key)
            if job is None or job.owner != token:
                raise Rejected(404, "Archive upload expired. Upload the files again.")
            if not job.lock.acquire(False):
                raise Rejected(409, "This archive is already processing a request.")
        try:
            job.updated = time.monotonic()
            if method == "DELETE" and action is None:
                with self.server.guard:
                    self.server.remove_job(key, job)
                return self.reply({"removed": True})
            if method == "POST" and action and action.isdigit():
                return self.add_chunk(job, int(action))
            if method == "POST" and action == "inspect":
                return self.inspect(job)
            if method == "POST" and action == "export":
                return self.export(job, key)
            if method == "GET" and action == "download" and job.result:
                name, data = job.result
                safe = re.sub(r"[^\w .-]", "-", name)[:110].strip(" .") or "timeline"
                return self.reply(data, content_type="application/octet-stream",
                                  extra=(("Content-Disposition", f"attachment; filename*=UTF-8''{quote(safe)}.drt"),))
            raise Rejected(404, "Unknown upload operation.")
        finally:
            job.lock.release()

    def route(self, method):
        path = self.path.split("?", 1)[0]
        if self.server.origin.startswith("https:") and self.headers.get("X-Forwarded-Proto") == "http":
            return self.reply(b"", 308, extra=(("Location", self.server.origin + "/"),))
        if method == "GET" and path in ("/", "/app.js", "/style.css", "/icon.svg"):
            file = STATIC / ("index.html" if path == "/" else path[1:])
            return self.reply(file.read_bytes(), content_type=mimetypes.guess_type(file)[0] or "text/plain")
        if not path.startswith("/api/"):
            raise Rejected(404, "Page not found.")
        if method != "GET" and self.headers.get("Origin") != self.server.origin:
            raise Rejected(403, "Open the site directly before submitting a request.")
        if method == "POST" and path == "/api/login":
            return self.login()
        token = self.session()
        if method == "GET" and path == "/api/session":
            return self.reply({"authenticated": bool(token)})
        if not token:
            raise Rejected(401, "Enter the development password.")
        if method == "POST" and path == "/api/logout":
            with self.server.guard:
                del self.server.sessions[token]
                for key, job in list(self.server.uploads.items()):
                    if job.owner == token and job.lock.acquire(False):
                        try:
                            self.server.remove_job(key, job)
                        finally:
                            job.lock.release()
            return self.reply({"authenticated": False}, extra=(self.cookie(""),))
        if method == "POST" and path == "/api/upload":
            return self.start_upload(token)
        if match := JOB_API.fullmatch(path):
            return self.route_job(match, method, token)
        raise Rejected(404, "Unknown request.")

    def dispatch(self, method):
        try:
            self.route(method)
        except Rejected as error:
            self.reply({"error": str(error)}, error.status)
        except archives.ARCHIVE_ERRORS as error:
            if isinstance(error, (socket.timeout, BrokenPipeError, ConnectionResetError)):
                return
            self.reply({"error": str(error)[:300] or "Invalid archive contents."}, 400)
        except Exception as error:
            LOG.error("Unexpected %s in %s request", type(error).__name__, method)
            self.reply({"error": "The converter could not finish this request."}, 500)

    def do_GET(self):
        self.dispatch("GET")

    def do_HEAD(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_DELETE(self):
        self.dispatch("DELETE")


class Rejected(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = HTTPStatus(status)
