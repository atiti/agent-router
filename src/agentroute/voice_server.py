"""Serve a local WebRTC microphone and speaker for the shared Codex voice thread."""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
from concurrent.futures import Future
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from importlib import resources
from pathlib import Path

from .dashboard import Collector
from .dashboard_server import Handler
from .shared_server import RpcClient, socket_path
from .voice import INSTRUCTIONS, TOOLS, TRANSPORT_ERRORS, VoicePilot

LEASE_SECONDS = 90
CONNECT_SECONDS = 45
VOICE_PATH = "/api/voice"



class VoiceSession:
    def __init__(self, cwd: Path, sdp: str):
        self.stop = threading.Event()
        self.answer: Future[str] = Future()
        self.id = secrets.token_urlsafe(24)
        self.last_seen = time.monotonic()
        self.error = ""
        self.worker = threading.Thread(target=self.run, args=(cwd, sdp), daemon=True)
        self.worker.start()

    def run(self, cwd: Path, sdp: str) -> None:
        pilot = VoicePilot(Collector())
        try:
            with RpcClient(socket_path(), name="agentroute_voice", timeout=10) as client:
                thread = client.request("thread/start", {
                    "cwd": str(cwd), "sandbox": "read-only", "ephemeral": True,
                    "modelProvider": "openai",
                    "developerInstructions": INSTRUCTIONS, "dynamicTools": TOOLS,
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
            if method == "thread/realtime/sdp" and not self.answer.done():
                self.answer.set_result(params["sdp"])
            elif method in {"thread/realtime/error", "thread/realtime/closed"}:
                self.error = "Codex ended the voice conversation. Start a new one to continue."
                return
            pilot.event(client, event)
            pilot.update(client)


class VoiceServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, cwd: Path):
        super().__init__(("127.0.0.1", 0), VoiceHandler)
        self.cwd = cwd
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.session: VoiceSession | None = None


class VoiceHandler(Handler):
    server: VoiceServer

    @property
    def action_token(self) -> str:
        return self.server.token

    def do_GET(self) -> None:
        if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
            self._json({"error": "Open the local voice page."}, HTTPStatus.FORBIDDEN)
        elif self.path == "/":
            page = resources.files("agentroute").joinpath("dashboard_assets/voice.html")
            self._page(page.read_bytes())
        else:
            self._json({"error": "Unknown page."}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        self.close_connection = True
        if self.path != VOICE_PATH:
            self._json({"error": "Unknown action."}, HTTPStatus.NOT_FOUND)
            return
        if not self._action_allowed() or not self.headers.get("Origin"):
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
                            "error": session.error})
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


def serve(cwd: Path) -> None:
    with VoiceServer(cwd) as server:
        print(f"Open voice on this Mac: http://127.0.0.1:{server.server_port}/#access={server.token}",
              flush=True)
        try:
            server.serve_forever()
        finally:
            if server.session is not None:
                server.session.stop.set()
                server.session.worker.join(timeout=12)
