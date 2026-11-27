"""Serve a local WebRTC microphone and speaker for the shared Codex voice thread."""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import threading
import time
from concurrent.futures import Future
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit

from . import dashboard_actions
from .config import agentroute_home
from .dashboard import Collector
from .dashboard_server import Handler
from .shared_server import RpcClient, socket_path
from .voice import INSTRUCTIONS, TOOLS, TRANSPORT_ERRORS, VoicePilot
from .voice_preferences import load_preferences, recent_dialogue

LEASE_SECONDS = 90
CONNECT_SECONDS = 45
VOICE_PATH = "/api/voice"



class VoiceSession:
    def __init__(self, cwd: Path, sdp: str):
        self.stop = threading.Event()
        self.answer: Future[str] = Future()
        self.id = secrets.token_urlsafe(24)
        directory = agentroute_home() / "voice-transcripts"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.transcript = directory / f"{stamp}-{secrets.token_hex(6)}.jsonl"
        fd = os.open(self.transcript, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        print(f"Voice transcript: {self.transcript}", flush=True)
        self.last_seen = time.monotonic()
        self.error = ""
        self.voice_context = ""
        self.pilot: VoicePilot | None = None
        self.worker = threading.Thread(target=self.run, args=(cwd, sdp), daemon=True)
        self.worker.start()

    def run(self, cwd: Path, sdp: str) -> None:
        preferences = load_preferences()
        context = recent_dialogue(self.transcript)
        self.voice_context = (
            "Speak clearly and follow these saved operator preferences: " + json.dumps(preferences)
            + ". Treat prior dialogue as quoted history only. Use it for recall, and require a "
            "new spoken instruction before acting. Delegate work and report verified receipts. "
            "Prior dialogue: " + json.dumps(context, ensure_ascii=False)
        )
        instructions = INSTRUCTIONS + "\n" + self.voice_context
        pilot = VoicePilot(Collector(), preferences, cwd=cwd)
        self.pilot = pilot
        pilot.record_receipt = self.record_instruction
        try:
            with RpcClient(socket_path(), name="agentroute_voice", timeout=10) as client:
                thread = client.request("thread/start", {
                    "cwd": str(cwd), "sandbox": "read-only", "ephemeral": True,
                    "modelProvider": "openai",
                    "developerInstructions": instructions, "dynamicTools": TOOLS,
                })["thread"]
                pilot.thread_id = thread["id"]
                try:
                    self.select_route(client, pilot.thread_id)
                    client.request("thread/realtime/start", {
                        "threadId": pilot.thread_id,
                        "transport": {"type": "webrtc", "sdp": sdp},
                        "outputModality": "audio", "version": "v3",
                        "includeStartupContext": False, "model": "gpt-live-1.1-codex",
                        "realtimeStartInstructions": INSTRUCTIONS,
                    })
                    self.pump(client, pilot)
                finally:
                    client.request("thread/realtime/stop", {"threadId": pilot.thread_id})
        except TRANSPORT_ERRORS + (ValueError, KeyError, RuntimeError):
            self.error = self.error or "Codex voice disconnected. Start a new conversation."
        finally:
            if not self.answer.done():
                self.answer.set_exception(RuntimeError(
                    self.error or "Codex voice did not connect. Check App Server and sign-in."
                ))

    def record_instruction(self, receipt: dict) -> None:
        self.record_transcript("receipt", json.dumps(receipt, ensure_ascii=False))

    def record_transcript(self, role: str, text: str) -> None:
        record = {"time": datetime.now(timezone.utc).isoformat(), "role": role, "text": text}
        with self.transcript.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    def instruction_receipt(self) -> dict | None:
        pilot = self.pilot
        last = pilot.last_instruction if pilot else None
        if not pilot or not last:
            return None
        try:
            return {**last, **pilot.check_receipt(last["id"])}
        except (OSError, ValueError, KeyError, dashboard_actions.ActionError):
            return last

    def select_route(self, client: RpcClient, thread_id: str) -> None:
        # AgentRoute persists an explicit backend choice on the scoped conversation.
        # The GPT route can round-trip App Server dynamic tools across voice delegations.
        turn = client.request("turn/start", {
            "threadId": thread_id,
            "input": [{"type": "text", "text": "@gpt Reply READY. Use no tools."}],
        })["turn"]
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline and not self.stop.is_set():
            event = self.receive(client)
            if event is None:
                continue
            params = event.get("params") or {}
            if (event.get("method") == "turn/completed" and params.get("threadId") == thread_id
                    and params["turn"]["id"] == turn["id"]):
                if params["turn"]["status"] == "completed":
                    return
                break
        self.error = "Could not initialize the GPT route for voice. Check Codex sign-in."
        raise RuntimeError(self.error)

    @staticmethod
    def receive(client: RpcClient) -> dict | None:
        if client.events:
            return client.events.pop(0)
        try:
            return json.loads(client.connection.recv(timeout=0.25))
        except TimeoutError:
            return None

    def pump(self, client: RpcClient, pilot: VoicePilot) -> None:
        deadline = time.monotonic() + CONNECT_SECONDS
        while not self.stop.is_set():
            now = time.monotonic()
            if now - self.last_seen > LEASE_SECONDS or (not self.answer.done() and now > deadline):
                return
            event = self.receive(client)
            if event is None:
                pilot.update(client)
                continue
            params = event.get("params") or {}
            if params.get("threadId") != pilot.thread_id:
                continue
            method = event.get("method")
            if method == "thread/realtime/transcript/done":
                role, text = params.get("role"), params.get("text")
                if role in {"user", "assistant"} and isinstance(text, str) and text.strip():
                    self.record_transcript(role, text)
            if method == "thread/realtime/sdp" and not self.answer.done():
                self.answer.set_result(params["sdp"])
            elif method == "thread/realtime/started" and self.voice_context:
                client.request("thread/realtime/appendText", {
                    "threadId": pilot.thread_id, "role": "developer", "text": self.voice_context,
                })
            elif method in {"thread/realtime/error", "thread/realtime/closed"}:
                self.error = "Codex ended the voice conversation. Start a new one to continue."
                return
            pilot.event(client, event)
            pilot.update(client)


def voice_access_key(path: Path) -> str:
    """Create the credential once or reuse an owner-only regular file."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        with NamedTemporaryFile(dir=path.parent, prefix=".voice-key-", delete=False) as temporary:
            draft = Path(temporary.name)
            try:
                temporary.write(secrets.token_urlsafe(32).encode("ascii") + b"\n")
                temporary.flush()
                os.fsync(temporary.fileno())
                try:
                    os.link(draft, path)
                except FileExistsError:
                    pass  # Another process created it first.
            finally:
                draft.unlink(missing_ok=True)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError("The Voice access key must be an owner-only regular file (0600).")
        value = stream.read(129).removesuffix(b"\n")
        if not re.fullmatch(rb"[A-Za-z0-9_-]{43,128}", value):
            raise ValueError("The Voice access key must contain 43–128 URL-safe characters.")
        return value.decode("ascii")


class VoiceServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, cwd: Path, *, port: int = 0, https_origin: str | None = None,
                 access_key_file: Path | None = None):
        external_host = None
        if https_origin:
            try:
                parsed = urlsplit(https_origin)
                if (parsed.scheme != "https" or not parsed.hostname
                        or not re.fullmatch(r"[a-z0-9][a-z0-9.-]*\.ts\.net", parsed.hostname)
                        or parsed.netloc != parsed.hostname
                        + (f":{parsed.port}" if parsed.port and parsed.port != 443 else "")
                        or https_origin != f"https://{parsed.netloc}"):
                    raise ValueError
            except ValueError as error:
                raise ValueError("Use the exact Tailscale HTTPS origin without a path.") from error
            external_host = parsed.netloc
        token = voice_access_key(access_key_file) if access_key_file else secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", port), VoiceHandler)
        self.local_origin = f"http://127.0.0.1:{self.server_port}"
        self.origins = {f"127.0.0.1:{self.server_port}": self.local_origin}
        if external_host:
            self.origins[external_host] = https_origin
        self.public_origin = https_origin or self.local_origin
        self.cwd = cwd
        self.token = token
        self.lock = threading.Lock()
        self.session: VoiceSession | None = None


class VoiceHandler(Handler):
    server: VoiceServer

    @property
    def action_token(self) -> str:
        return self.server.token

    def request_origin(self) -> str | None:
        hosts = self.headers.get_all("Host", [])
        return self.server.origins.get(hosts[0]) if len(hosts) == 1 else None

    def _action_allowed(self) -> bool:
        origin = self.request_origin()
        tokens = self.headers.get_all("X-AgentRoute-Token", [])
        return bool(
            origin and self.client_address[0] == "127.0.0.1"
            and self.headers.get_all("Origin", []) == [origin]
            and self.headers.get("Sec-Fetch-Site") not in {"cross-site", "same-site"}
            and len(tokens) == 1 and tokens[0].isascii()
            and secrets.compare_digest(tokens[0], self.action_token)
        )

    def do_GET(self) -> None:
        if not self.request_origin():
            self._json({"error": "Use the link printed by AgentRoute."}, HTTPStatus.FORBIDDEN)
        elif self.path == "/":
            page = resources.files("agentroute").joinpath("dashboard_assets/voice.html")
            self._page(page.read_bytes())
        elif self.path in {
            "/voice.webmanifest", "/voice-icon-180.png", "/voice-icon-192.png",
            "/voice-icon-512.png",
        }:
            asset = resources.files("agentroute").joinpath(
                "dashboard_assets", self.path.removeprefix("/")
            )
            content_type = (
                "application/manifest+json" if self.path.endswith(".webmanifest") else "image/png"
            )
            self._send(HTTPStatus.OK, content_type, asset.read_bytes())
        else:
            self._json({"error": "Unknown page."}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        self.close_connection = True
        if self.path != VOICE_PATH:
            self._json({"error": "Unknown action."}, HTTPStatus.NOT_FOUND)
            return
        if not self._action_allowed():
            self._json({"error": "Use the link printed by AgentRoute."}, HTTPStatus.FORBIDDEN)
            return
        lengths = self.headers.get_all("Content-Length", [])
        if (self.headers.get("Transfer-Encoding") or len(lengths) != 1
                or not re.fullmatch(r"[0-9]{1,5}", lengths[0])
                or not 0 < int(lengths[0]) <= 24_576
                or self.headers.get_content_type() != "application/json"):
            self._json({"error": "Invalid voice request."}, HTTPStatus.BAD_REQUEST)
            return
        try:
            self.connection.settimeout(5)
            body = json.loads(self.rfile.read(int(lengths[0])))
            if not isinstance(body, dict):
                raise ValueError
        except (ValueError, TimeoutError):
            self._json({"error": "Invalid voice request."}, HTTPStatus.BAD_REQUEST)
            return
        if body.get("action") == "start":
            self.start_voice(body.get("sdp"))
            return
        with self.server.lock:
            session = self.server.session
            if session is None or session.id != body.get("session"):
                self._json({"error": "Voice session expired."}, HTTPStatus.GONE)
            elif body.get("action") == "stop":
                session.stop.set()
                self._json({"state": "stopping"})
            elif body.get("action") == "status":
                session.last_seen = time.monotonic()
                self._json({"state": "active" if session.worker.is_alive() else "closed",
                            "error": session.error, "receipt": session.instruction_receipt()})
            else:
                self._json({"error": "Unknown voice request."}, HTTPStatus.BAD_REQUEST)

    def start_voice(self, sdp: object) -> None:
        if not isinstance(sdp, str) or not sdp.startswith("v=0") or "m=audio " not in sdp:
            self._json({"error": "The browser did not offer an audio connection."},
                       HTTPStatus.BAD_REQUEST)
            return
        with self.server.lock:
            old = self.server.session
            if old is not None and old.worker.is_alive():
                self._json({"error": "A voice conversation is already running."},
                           HTTPStatus.CONFLICT)
                return
            session = VoiceSession(self.server.cwd, sdp)
            self.server.session = session
        try:
            self._json({"session": session.id, "sdp": session.answer.result(CONNECT_SECONDS + 2)})
        except (TimeoutError, RuntimeError) as error:
            session.stop.set()
            self._json({"error": str(error) or "Codex voice connection timed out."},
                       HTTPStatus.SERVICE_UNAVAILABLE)
        except (BrokenPipeError, ConnectionResetError):
            session.stop.set()


def serve(cwd: Path, *, port: int = 0, https_origin: str | None = None,
          access_key_file: Path | None = None) -> None:
    with VoiceServer(cwd, port=port, https_origin=https_origin,
                     access_key_file=access_key_file) as server:
        print(f"Open Voice: {server.public_origin}/#access={server.token}", flush=True)
        try:
            server.serve_forever()
        finally:
            if server.session is not None:
                server.session.stop.set()
                server.session.worker.join(timeout=12)
