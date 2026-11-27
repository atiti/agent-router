"""Provider-free HTTP fixtures for effects, identity, secrecy and artifact custody."""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/meeting_recall.py"
SPEC = importlib.util.spec_from_file_location("meeting_recall", SCRIPT)
recall = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recall)

OPERATION = "d94b0d47-3b7b-4fb1-88d4-309a9017946c"
BOT = "327b5f2b-ef2c-4ec4-9c94-83aad773fb91"
RECORDING = "e6a5b2bb-cb9f-4dc2-9929-dd7a238fed66"
API_KEY = "secret-recall-rest-key-for-tests"
PAGE_KEY = "S" * 43
PAGE = f"https://meeting.example.com/#access={PAGE_KEY}"


def secure(path: Path, contents: bytes = b"") -> Path:
    path.write_bytes(contents)
    path.chmod(0o600)
    return path


@pytest.fixture
def inputs(tmp_path):
    root, artifacts = tmp_path / "state", tmp_path / "artifacts"
    root.mkdir(mode=0o700)
    artifacts.mkdir(mode=0o700)
    page = secure(tmp_path / "meeting.key", PAGE_KEY.encode())
    args = argparse.Namespace(
        operation=OPERATION,
        region="us-west-2",
        workspace_api_version="1.11",
        meeting_url="https://meet.google.com/abc-defg-hij",
        page_url="https://meeting.example.com/",
        meeting_key_file=page,
    )
    return root, artifacts, args


class Response:
    def __init__(self, response, original_url):
        self.response, self.original_url = response, original_url
        self.status, self.headers = response.status, response.headers

    def geturl(self):
        return self.original_url

    def read(self, size=-1):
        return self.response.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.response.close()


class FixtureHTTP:
    def __init__(self):
        self.calls = []
        self.routes = {}
        self.fallback = None
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def handle_request(self):
                length = int(self.headers.get("Content-Length", "0"))
                call = {
                    "method": self.command,
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body": self.rfile.read(length),
                }
                fixture.calls.append(call)
                route = fixture.routes.get((self.command, self.path), fixture.fallback)
                value = route(call) if callable(route) else route
                if value is None:
                    value = (404, {"error": "missing test route"})
                status, body, *extra = value
                headers = extra[0] if extra else {}
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                for key, header_value in headers.items():
                    self.send_header(key, header_value)
                if "Content-Length" not in headers:
                    self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            do_GET = handle_request
            do_POST = handle_request

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), recall.NoRedirect()
        )

    def open(self, request, timeout):
        parsed = urlsplit(request.full_url)
        assert parsed.scheme == "https"
        url = f"http://127.0.0.1:{self.server.server_port}{parsed.path}"
        if parsed.query:
            url += "?" + parsed.query
        forwarded = urllib.request.Request(
            url, request.data, dict(request.headers), method=request.get_method()
        )
        return Response(self.opener.open(forwarded, timeout=timeout), request.full_url)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)

    def client(self, timeout=2):
        return recall.RecallHTTP("us-west-2", API_KEY, opener=self, timeout=timeout)


@pytest.fixture
def http():
    fixture = FixtureHTTP()
    try:
        yield fixture
    finally:
        fixture.close()


def bot_response(payload, *, bot_id=BOT, state="in_call_recording"):
    bot = copy.deepcopy(payload)
    bot.update(
        id=bot_id, status_changes=[{"code": state, "message": "private message"}], recordings=[]
    )
    # The real Retrieve Bot response normalizes the original URL to a meeting object.
    bot["meeting_url"] = {"platform": "google_meet", "meeting_id": "abc-defg-hij"}
    return bot


@contextmanager
def operation(inputs):
    root, _, args = inputs
    op = recall.Operation(root, OPERATION, "us-west-2", "1.11")
    with op.locked():
        yield op, recall.payload_for(args)


def create(http, op, prepared):
    payload, _ = prepared
    bot = bot_response(payload)
    http.routes[("POST", "/api/v1/bot/")] = (201, bot)
    http.routes[("GET", f"/api/v1/bot/{BOT}/")] = (200, bot)
    return op.create(http.client(), *prepared), bot


def test_create_claims_immutable_request_before_http_and_returns_no_urls(inputs, http):
    with operation(inputs) as (op, prepared):
        payload, binding = prepared

        def provider(call):
            state = op.load()
            assert state["create"]["phase"] == "held"
            assert state["request_sha256"] == recall.digest(call["body"])
            assert call["headers"]["Idempotency-Key"] == state["create"]["idempotency_key"]
            assert call["headers"]["Authorization"] == f"Token {API_KEY}"
            return 201, bot_response(payload)

        http.routes[("POST", "/api/v1/bot/")] = provider
        receipt = op.create(http.client(), *prepared)
        assert receipt["bot_id"] == BOT
        assert receipt["create"]["phase"] == "created"
        assert op.create(http.client(), *prepared) == receipt
        assert len(http.calls) == 1
        assert payload["metadata"][recall.BINDING] == binding
        assert payload["recording_config"]["include_bot_in_recording"] == {"audio": True}
        assert "recallai_streaming" in payload["recording_config"]["transcript"]["provider"]
        assert "audio" not in payload["output_media"]
        assert PAGE_KEY not in json.dumps(receipt)
        assert PAGE_KEY not in op.journal.read_text()
        assert PAGE_KEY in recall.read_private(op.request).decode()
        for path in op.directory.iterdir():
            assert path.stat().st_mode & 0o777 == 0o600


def test_changed_payload_region_or_request_bytes_never_recreates(inputs, http):
    with operation(inputs) as (op, prepared):
        create(http, op, prepared)
        changed = copy.deepcopy(prepared[0])
        changed["meeting_url"] = "https://meet.google.com/xyz-abcd-efg"
        with pytest.raises(recall.OperatorError, match="request_binding_changed"):
            op.create(http.client(), changed, prepared[1])
        wrong_region = recall.Operation(inputs[0], OPERATION, "eu-central-1", "1.11")
        with pytest.raises(recall.OperatorError, match="operation_binding_changed"):
            wrong_region.load()
        op.request.write_bytes(b"{}")
        with pytest.raises(recall.OperatorError, match="saved_request_changed"):
            op.load()
        assert len(http.calls) == 1


def test_create_timeout_holds_even_after_hour_and_reconciles_all_pages(inputs, http):
    with operation(inputs) as (op, prepared):
        payload, _ = prepared

        def delayed(_):
            time.sleep(0.15)
            return 201, bot_response(payload)

        http.routes[("POST", "/api/v1/bot/")] = delayed
        with pytest.raises(recall.OperatorError, match="provider_outcome_unconfirmed"):
            op.create(http.client(timeout=0.03), *prepared)
        state = op.load()
        state["create"]["attempted_at"] -= 7200
        op.save(state)
        with pytest.raises(recall.OperatorError, match="create_held"):
            op.create(http.client(), *prepared)
        assert len(http.calls) == 1
        path = f"/api/v1/bot/?metadata__{recall.OPERATION}={OPERATION}"
        next_page = path + "&page=2"
        http.routes[("GET", path)] = (200, {"results": [], "next": next_page})
        http.routes[("GET", next_page)] = (200, {"results": [bot_response(payload)], "next": None})
        http.routes[("GET", f"/api/v1/bot/{BOT}/")] = (200, bot_response(payload))
        receipt = op.reconcile(http.client())
        assert receipt["create"]["reason"] == "exact_bot_reconciled"
        assert receipt["bot_id"] == BOT
        assert [call["method"] for call in http.calls].count("POST") == 1


@pytest.mark.parametrize("failure", ["none", "duplicates", "wrong_binding", "wrong_page"])
def test_reconcile_cannot_adopt_unproven_or_multiple_bot(inputs, http, failure):
    with operation(inputs) as (op, prepared):
        state = op.prepare(*prepared)
        state["create"]["phase"] = "held"
        op.save(state)
        bot = bot_response(prepared[0])
        path = f"/api/v1/bot/?metadata__{recall.OPERATION}={OPERATION}"
        if failure == "none":
            result = {"results": [], "next": None}
        elif failure == "duplicates":
            result = {
                "results": [bot, bot_response(prepared[0], bot_id=str(uuid4()))],
                "next": None,
            }
        elif failure == "wrong_binding":
            bot["metadata"][recall.BINDING] = "not-the-request"
            result = {"results": [bot], "next": None}
        else:
            result = {"results": [], "next": "/api/v1/bot/?page=2"}
        http.routes[("GET", path)] = (200, result)
        with pytest.raises(recall.OperatorError):
            op.reconcile(http.client())
        assert op.load()["create"]["phase"] == "held"
        assert all(call["method"] == "GET" for call in http.calls)


def test_pagination_cannot_send_key_to_other_origin_and_redirects_are_refused(inputs, http):
    with operation(inputs) as (op, prepared):
        op.prepare(*prepared)
        path = f"/api/v1/bot/?metadata__{recall.OPERATION}={OPERATION}"
        http.routes[("GET", path)] = (
            200,
            {"results": [], "next": f"https://leak.example.com{path}&page=2"},
        )
        with pytest.raises(recall.OperatorError, match="api_origin_or_path"):
            op.reconcile(http.client())
        assert len(http.calls) == 1
        http.routes[("GET", path)] = (302, b"", {"Location": "/api/v1/leaked/"})
        with pytest.raises(recall.OperatorError, match="provider_http_302"):
            op.reconcile(http.client())
        assert len(http.calls) == 2
        with pytest.raises(recall.OperatorError, match="api_origin_or_path"):
            http.client().json("GET", "https://evil.example.com/api/v1/bot/")
        assert len(http.calls) == 2


def test_lock_serializes_distinct_operator_instances(inputs):
    with operation(inputs):
        duplicate = recall.Operation(inputs[0], OPERATION, "us-west-2", "1.11")
        with pytest.raises(recall.OperatorError, match="operation_busy"), duplicate.locked():
            pytest.fail("second operator acquired an existing lock")


def test_leave_is_once_exact_bot_and_timeout_requires_read_reconciliation(inputs, http):
    with operation(inputs) as (op, prepared):
        _, bot = create(http, op, prepared)

        def delayed(call):
            state = op.load()
            assert state["leave"]["phase"] == "held"
            assert state["leave"]["bot_id"] == BOT
            assert call["headers"]["Idempotency-Key"] == state["leave"]["idempotency_key"]
            time.sleep(0.15)
            return 200, {"id": BOT}

        http.routes[("POST", f"/api/v1/bot/{BOT}/leave_call/")] = delayed
        with pytest.raises(recall.OperatorError, match="provider_outcome_unconfirmed"):
            op.leave(http.client(timeout=0.03))
        with pytest.raises(recall.OperatorError, match="leave_held"):
            op.leave(http.client())
        with pytest.raises(recall.OperatorError, match="leave_outcome_still"):
            op.reconcile(http.client(), effect="leave")
        bot["status_changes"].append({"code": "call_ended"})
        receipt = op.reconcile(http.client(), effect="leave")
        assert receipt["leave"]["phase"] == "observed_ended"
        assert op.leave(http.client())["leave"]["phase"] == "observed_ended"
        assert len([call for call in http.calls if call["method"] == "POST"]) == 2
        assert not any(call["method"] == "DELETE" for call in http.calls)


def ready_recording(http, payload, bot, recording_id=RECORDING):
    bot["status_changes"].append({"code": "done"})
    bot["recordings"] = [{"id": recording_id, "status": {"code": "done"}}]
    recording = {
        "id": recording_id,
        "bot": {"id": BOT},
        "status": {"code": "done"},
        "metadata": payload["recording_config"]["metadata"].copy(),
        "media_shortcuts": {},
    }
    for name, file in recall.MEDIA.items():
        path = f"/downloads/{recording_id}/{file}?signature=secret-signed-query"
        content = b'[{"speaker":"Aura","text":"Done"}]' if name == "transcript" else b"media-bytes"
        http.routes[("GET", path)] = (200, content)
        recording["media_shortcuts"][name] = {
            "id": str(uuid4()),
            "status": {"code": "done"},
            "data": {"download_url": "https://recordings.example.com" + path},
            "format": "mp4" if name == "video_mixed" else "mp3",
        }
    http.routes[("GET", f"/api/v1/recording/{recording_id}/")] = (200, recording)
    return recording


def test_collect_requires_complete_exact_artifacts_and_never_sends_auth_to_download(inputs, http):
    with operation(inputs) as (op, prepared):
        _, bot = create(http, op, prepared)
        ready_recording(http, prepared[0], bot)
        receipt = op.collect(http.client(), inputs[1])
        assert receipt["collect"]["phase"] == "complete"
        manifest_path = Path(receipt["collect"]["manifest"])
        manifest = json.loads(manifest_path.read_text())
        assert set(manifest["recordings"][0]["media"]) == set(recall.MEDIA)
        assert PAGE_KEY not in manifest_path.read_text()
        assert "secret-signed-query" not in manifest_path.read_text()
        assert "secret-signed-query" not in op.journal.read_text()
        for call in http.calls:
            if call["path"].startswith("/downloads/"):
                assert not any(k.lower() == "authorization" for k in call["headers"])
        for path in (inputs[1] / OPERATION).rglob("*"):
            assert path.stat().st_mode & 0o777 == (0o700 if path.is_dir() else 0o600)
        before = len(http.calls)
        op.collect(http.client(), inputs[1])
        assert len(http.calls) == before + 2
        assert all(call["method"] == "GET" for call in http.calls[before:])
        path = manifest_path.parent / RECORDING / "audio.mp3"
        path.write_bytes(b"changed")
        with pytest.raises(recall.OperatorError, match="artifact_integrity_failed"):
            op.collect(http.client(), inputs[1])
        assert op.load()["collect"]["phase"] == "incomplete"


@pytest.mark.parametrize("command", ["status", "collect"])
@pytest.mark.parametrize("change", ["new_recording", "media_id", "not_ended", "failed_media"])
def test_completed_collection_is_held_when_provider_coverage_changes(
    inputs, http, command, change
):
    with operation(inputs) as (op, prepared):
        _, bot = create(http, op, prepared)
        recording = ready_recording(http, prepared[0], bot)
        receipt = op.collect(http.client(), inputs[1])
        manifest = Path(receipt["collect"]["manifest"])
        original = manifest.read_bytes()
        if change == "new_recording":
            bot["recordings"].append({"id": str(uuid4()), "status": {"code": "done"}})
        elif change == "media_id":
            recording["media_shortcuts"]["audio_mixed"]["id"] = str(uuid4())
        elif change == "not_ended":
            bot["status_changes"].append({"code": "in_call_recording"})
        else:
            recording["media_shortcuts"]["transcript"]["status"]["code"] = "failed"
        with pytest.raises(recall.OperatorError):
            if command == "status":
                op.status(http.client())
            else:
                op.collect(http.client(), inputs[1])
        assert op.load()["collect"]["phase"] == "incomplete"
        assert manifest.read_bytes() == original
        assert len([call for call in http.calls if call["method"] == "POST"]) == 1


def test_reconcile_requires_explicit_pagination_end_even_with_exact_match(inputs, http):
    with operation(inputs) as (op, prepared):
        state = op.prepare(*prepared)
        state["create"]["phase"] = "held"
        op.save(state)
        query = "/api/v1/bot/?metadata__agentroute_meeting_operation=" + OPERATION
        http.routes[("GET", query)] = (200, {"results": [bot_response(prepared[0])]})
        with pytest.raises(recall.OperatorError, match="reconcile_pagination_unconfirmed"):
            op.reconcile(http.client())
        assert op.load()["create"]["phase"] == "held"
        assert "bot_id" not in op.load()
        assert all(call["method"] == "GET" for call in http.calls)


@pytest.mark.parametrize(
    "failure",
    [
        "recording_processing",
        "artifact_processing",
        "wrong_bot",
        "wrong_binding",
        "expired",
        "truncated",
        "bad_transcript",
    ],
)
def test_collection_failures_never_publish_complete_or_partial_artifacts(inputs, http, failure):
    with operation(inputs) as (op, prepared):
        _, bot = create(http, op, prepared)
        recording = ready_recording(http, prepared[0], bot)
        media = recording["media_shortcuts"]
        if failure == "recording_processing":
            recording["status"]["code"] = "processing"
        elif failure == "artifact_processing":
            media["transcript"]["status"]["code"] = "processing"
        elif failure == "wrong_bot":
            recording["bot"]["id"] = str(uuid4())
        elif failure == "wrong_binding":
            recording["metadata"][recall.BINDING] = "other"
        else:
            name = "transcript" if failure == "bad_transcript" else "audio_mixed"
            url = urlsplit(media[name]["data"]["download_url"])
            path = url.path + "?" + url.query
            http.routes[("GET", path)] = {
                "expired": (403, {"error": "secret-signed-query expired"}),
                "truncated": (200, b"part", {"Content-Length": "100"}),
                "bad_transcript": (200, b"not json"),
            }[failure]
        with pytest.raises((recall.OperatorError, OSError)):
            op.collect(http.client(timeout=0.1), inputs[1])
        assert op.load()["collect"]["phase"] == "incomplete"
        assert not (inputs[1] / OPERATION).exists()
        assert not list(inputs[1].iterdir())
        assert "secret-signed-query" not in op.journal.read_text()
        assert len([call for call in http.calls if call["method"] == "POST"]) == 1


def test_expired_download_can_refresh_by_read_without_repeating_effect(inputs, http):
    with operation(inputs) as (op, prepared):
        _, bot = create(http, op, prepared)
        recording = ready_recording(http, prepared[0], bot)
        path = f"/downloads/{RECORDING}/audio.mp3?signature=secret-signed-query"
        http.routes[("GET", path)] = (403, b"expired")
        with pytest.raises(recall.OperatorError, match="download_http_403"):
            op.collect(http.client(), inputs[1])
        recording["media_shortcuts"]["audio_mixed"]["data"]["download_url"] = (
            "https://recordings.example.com/downloads/new?signature=refreshed"
        )
        http.routes[("GET", "/downloads/new?signature=refreshed")] = (200, b"audio")
        assert op.collect(http.client(), inputs[1])["collect"]["phase"] == "complete"
        assert len([call for call in http.calls if call["method"] == "POST"]) == 1


@pytest.mark.parametrize(
    "bad",
    [
        "http://meet.google.com/abc-defg-hij",
        "https://meet.google.com.evil.com/abc-defg-hij",
        "https://user:password@meet.google.com/abc-defg-hij",
        "https://meet.google.com/abc-defg-hij?secret=value",
    ],
)
def test_invalid_meeting_inputs_are_rejected_without_echoing_url(inputs, bad):
    inputs[2].meeting_url = bad
    with pytest.raises(recall.OperatorError) as error:
        recall.payload_for(inputs[2])
    assert bad not in str(error.value)


def test_private_inputs_symlink_and_modes_and_legacy_api_are_rejected(inputs, tmp_path):
    args = inputs[2]
    args.meeting_key_file.chmod(0o644)
    with pytest.raises(recall.OperatorError, match="file_must_be_owner_only"):
        recall.payload_for(args)
    args.meeting_key_file.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(args.meeting_key_file)
    args.meeting_key_file = link
    with pytest.raises(recall.OperatorError, match="private_file_unavailable"):
        recall.payload_for(args)
    args.workspace_api_version = "1.10"
    with pytest.raises(recall.OperatorError, match="verify_workspace"):
        recall.payload_for(args)
    inputs[0].chmod(0o755)
    with pytest.raises(recall.OperatorError, match="directory_must_be_owner_only"):
        recall.Operation(inputs[0], OPERATION, "us-west-2", "1.11")


def test_cli_preflight_needs_no_rest_key_network_or_state_effect(inputs, monkeypatch, capsys):
    root, _, args = inputs
    monkeypatch.delenv("RECALL_API_KEY", raising=False)
    command = [
        "--state-dir",
        str(root),
        "--operation",
        OPERATION,
        "--region",
        "us-west-2",
        "--workspace-api-version",
        "1.11",
        "preflight",
        "--meeting-url",
        args.meeting_url,
        "--page-url",
        args.page_url,
        "--meeting-key-file",
        str(args.meeting_key_file),
    ]
    assert recall.main(command) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["provider_calls"] == 0
    assert PAGE_KEY not in json.dumps(result)
    assert list(root.iterdir()) == []
    command[command.index("1.11")] = "1.10"
    assert recall.main(command) == 2
    assert "verify_workspace_api_version_1_11" in capsys.readouterr().err
    assert recall.main(["--unknown", PAGE]) == 2
    assert PAGE_KEY not in capsys.readouterr().err


def test_clean_page_can_use_existing_meeting_key_without_url_secret_argument(inputs, tmp_path):
    args = inputs[2]
    args.page_url = "https://meeting.example.com/"
    args.meeting_key_file = secure(tmp_path / "meeting.key", PAGE_KEY.encode())
    assert recall.payload_for(args)[0]["output_media"]["camera"]["config"]["url"] == PAGE
    args.page_url += "?credential=bad"
    with pytest.raises(recall.OperatorError, match="clean_page_url"):
        recall.payload_for(args)


def test_reconcile_checks_later_pages_even_after_an_apparent_match(inputs, http):
    with operation(inputs) as (op, prepared):
        op.prepare(*prepared)
        path = f"/api/v1/bot/?metadata__{recall.OPERATION}={OPERATION}"
        second = path + "&page=2"
        http.routes[("GET", path)] = (200, {"results": [bot_response(prepared[0])], "next": second})
        http.routes[("GET", second)] = (
            200,
            {"results": [bot_response(prepared[0], bot_id=str(uuid4()))], "next": None},
        )
        with pytest.raises(recall.OperatorError, match="multiple_bots"):
            op.reconcile(http.client())
        assert op.load()["create"]["phase"] == "held"
        assert len(http.calls) == 2


@pytest.mark.parametrize("response", [b"not-json", {"id": BOT}, {"detail": PAGE}])
def test_malformed_success_response_holds_instead_of_repeating_create(inputs, http, response):
    with operation(inputs) as (op, prepared):
        http.routes[("POST", "/api/v1/bot/")] = (201, response)
        with pytest.raises(recall.OperatorError):
            op.create(http.client(), *prepared)
        assert op.load()["create"]["phase"] == "held"
        with pytest.raises(recall.OperatorError, match="create_held"):
            op.create(http.client(), *prepared)
        assert len(http.calls) == 1
        assert PAGE_KEY not in op.journal.read_text()


def test_status_sanitizes_unknown_provider_status_and_never_returns_provider_content(inputs, http):
    with operation(inputs) as (op, prepared):
        _, bot = create(http, op, prepared)
        bot["status_changes"] = [{"code": PAGE, "message": API_KEY}]
        bot["recordings"] = [{"id": RECORDING, "status": {"code": PAGE}}]
        result = op.status(http.client())
        assert result["bot_status"] == result["recordings"][0]["status"] == "unknown"
        serialized = json.dumps(result)
        assert PAGE_KEY not in serialized and API_KEY not in serialized
        bot["id"] = str(uuid4())
        with pytest.raises(recall.OperatorError, match="bot_identity_mismatch"):
            op.status(http.client())


def test_completed_collection_restores_only_exact_full_manifest_after_crash(inputs, http):
    with operation(inputs) as (op, prepared):
        _, bot = create(http, op, prepared)
        first = ready_recording(http, prepared[0], bot)
        second_id = str(uuid4())
        ready_recording(http, prepared[0], bot, second_id)
        bot["recordings"].append({"id": first["id"], "status": {"code": "done"}})
        receipt = op.collect(http.client(), inputs[1])
        manifest_path = Path(receipt["collect"]["manifest"])
        manifest = json.loads(manifest_path.read_text())
        assert sorted(x["id"] for x in manifest["recordings"]) == sorted([RECORDING, second_id])
        state = op.load()
        state["collect"]["phase"] = "incomplete"
        op.save(state)
        count = len(http.calls)
        assert op.collect(http.client(), inputs[1])["collect"]["phase"] == "complete"
        assert len(http.calls) == count + 3
        assert all(call["method"] == "GET" for call in http.calls[count:])
        manifest["recordings"].pop()
        manifest_path.write_text(json.dumps(manifest))
        with pytest.raises(recall.OperatorError, match="artifact_integrity_failed"):
            op.collect(http.client(), inputs[1])
        assert op.load()["collect"]["phase"] == "incomplete"


def test_download_refuses_redirect_without_following_or_sending_authorization(inputs, http):
    with operation(inputs) as (op, prepared):
        _, bot = create(http, op, prepared)
        ready_recording(http, prepared[0], bot)
        path = f"/downloads/{RECORDING}/video.mp4?signature=secret-signed-query"
        http.routes[("GET", path)] = (302, b"", {"Location": "https://leak.example.com/file"})
        with pytest.raises(recall.OperatorError, match="download_http_302"):
            op.collect(http.client(), inputs[1])
        requests = [call for call in http.calls if call["path"].startswith("/downloads/")]
        assert len(requests) == 1
        assert "Authorization" not in requests[0]["headers"]
        assert not list(inputs[1].iterdir())


def test_recording_set_change_during_download_withholds_final_manifest(inputs, http):
    with operation(inputs) as (op, prepared):
        _, bot = create(http, op, prepared)
        ready_recording(http, prepared[0], bot)
        path = f"/downloads/{RECORDING}/transcript.json?signature=secret-signed-query"

        def changed(_):
            bot["recordings"].append({"id": str(uuid4())})
            return 200, b"[]"

        http.routes[("GET", path)] = changed
        with pytest.raises(recall.OperatorError, match="recording_set_changed"):
            op.collect(http.client(), inputs[1])
        assert not list(inputs[1].iterdir())
        assert op.load()["collect"]["phase"] == "incomplete"
