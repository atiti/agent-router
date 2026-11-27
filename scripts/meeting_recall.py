#!/usr/bin/env python3
"""Explicit, private Recall bot operations for the scoped meeting prototype.

This operator never provisions accounts, grants access, runs workers or configures
ingress. A journal claims each effect before HTTP; an uncertain effect stays held.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import http.client
import ipaddress
import json
import os
import re
import secrets
import shutil
import stat
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from uuid import UUID, uuid4

REGIONS = {
    name: f"https://{name}.recall.ai"
    for name in ("us-west-2", "us-east-1", "eu-central-1", "ap-northeast-1")
}
OPERATION = "agentroute_meeting_operation"
BINDING = "agentroute_meeting_request_sha256"
MAX_JSON = 4 * 1024 * 1024
MAX_PAGES = 100
MAX_DOWNLOAD = 512 * 1024 * 1024
MAX_TRANSCRIPT = 32 * 1024 * 1024
TERMINAL = {"call_ended", "done", "fatal", "media_expired", "analysis_done", "analysis_failed"}
BOT_STATES = TERMINAL | {
    "ready",
    "joining_call",
    "in_waiting_room",
    "in_call_not_recording",
    "in_call_recording",
    "recording_done",
    "recording_permission_allowed",
    "recording_permission_denied",
}
ARTIFACT_STATES = {"processing", "paused", "done", "failed", "deleted"}
MEDIA = {"video_mixed": "video.mp4", "audio_mixed": "audio.mp3", "transcript": "transcript.json"}


class OperatorError(Exception):
    """Only fixed, credential-free codes cross the command output boundary."""


def require(condition: bool, code: str) -> None:
    if not condition:
        raise OperatorError(code)


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def identifier(value: object) -> str:
    try:
        require(isinstance(value, str) and str(UUID(value)) == value, "invalid_identifier")
    except (ValueError, TypeError, AttributeError) as error:
        raise OperatorError("invalid_identifier") from error
    return value


def private_dir(path: Path, *, create: bool = False) -> Path:
    path = path.expanduser().absolute()
    if create:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
    try:
        directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(directory)
            require(
                info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700,
                "directory_must_be_owner_only",
            )
        finally:
            os.close(directory)
    except (OSError, ValueError) as error:
        raise OperatorError("private_directory_unavailable") from error
    return path


def check_file(fd: int) -> None:
    info = os.fstat(fd)
    require(
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) == 0o600
        and info.st_nlink == 1,
        "file_must_be_owner_only",
    )


def read_private(path: Path, limit: int = MAX_JSON) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            check_file(stream.fileno())
            data = stream.read(limit + 1)
        require(len(data) <= limit, "private_file_too_large")
        return data
    except OSError as error:
        raise OperatorError("private_file_unavailable") from error


def sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_private(path: Path, data: bytes, *, exclusive: bool = False) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            os.link(temporary, path, follow_symlinks=False)
            temporary.unlink()
        else:
            os.replace(temporary, path)
        sync_dir(path.parent)
    except OSError as error:
        raise OperatorError("private_state_write_failed") from error
    finally:
        temporary.unlink(missing_ok=True)


def decode(data: bytes) -> dict:
    try:
        value = json.loads(data)
    except (ValueError, UnicodeError) as error:
        raise OperatorError("invalid_json_response_or_state") from error
    require(isinstance(value, dict), "invalid_json_response_or_state")
    return value


def https_url(value: str, *, fragment: bool = False) -> str:
    try:
        require(
            isinstance(value, str)
            and 0 < len(value) <= 8192
            and not re.search(r"[\s\x00-\x1f\x7f\\]", value),
            "invalid_https_url",
        )
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        require(
            parsed.scheme == "https"
            and host
            and not parsed.username
            and not parsed.password
            and parsed.port in {None, 443}
            and (fragment or not parsed.fragment),
            "invalid_https_url",
        )
        require(
            not host.endswith((".localhost", ".local", ".internal")) and host != "localhost",
            "private_url_rejected",
        )
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            require(
                "." in host and re.fullmatch(r"[a-zA-Z0-9.-]+", host) is not None,
                "invalid_https_url",
            )
        else:
            require(address.is_global, "private_url_rejected")
    except (ValueError, TypeError) as error:
        raise OperatorError("invalid_https_url") from error
    return value


def meeting_url(value: str) -> str:
    https_url(value)
    require(
        re.fullmatch(r"https://meet\.google\.com/[a-z]{3}-[a-z]{4}-[a-z]{3}", value) is not None,
        "use_canonical_google_meet_url",
    )
    return value


def secret_file(path: Path) -> str:
    try:
        value = read_private(path, 8192).decode("ascii").strip()
    except UnicodeError as error:
        raise OperatorError("invalid_private_input") from error
    require(value and not re.search(r"[\s\x00-\x1f\x7f]", value), "invalid_private_input")
    return value


def participant_url(args: argparse.Namespace) -> str:
    page = https_url(args.page_url)
    require(not urlsplit(page).query, "use_clean_page_url_with_key_file")
    token = secret_file(args.meeting_key_file)
    require(re.fullmatch(r"[A-Za-z0-9_-]{43,128}", token) is not None, "invalid_meeting_key")
    return f"{page}#access={token}"


def payload_for(args: argparse.Namespace) -> tuple[dict, str]:
    require(args.workspace_api_version == "1.11", "verify_workspace_api_version_1_11")
    require(args.region in REGIONS, "unsupported_region")
    operation = identifier(args.operation)
    metadata = {OPERATION: operation}
    payload = {
        "meeting_url": meeting_url(args.meeting_url),
        "bot_name": "Aura",
        "variant": {"google_meet": "web_4_core"},
        "output_media": {"camera": {"kind": "webpage", "config": {"url": participant_url(args)}}},
        "recording_config": {
            "video_mixed_mp4": {},
            "audio_mixed_mp3": {},
            "include_bot_in_recording": {"audio": True},
            "transcript": {
                "provider": {
                    "recallai_streaming": {
                        "mode": "prioritize_accuracy",
                        "language_code": "auto",
                    }
                },
                "diarization": {"use_separate_streams_when_available": True},
            },
            "retention": {"type": "timed", "hours": 168},
            "metadata": metadata.copy(),
        },
        "automatic_leave": {
            "waiting_room_timeout": 180,
            "noone_joined_timeout": 180,
            "in_call_not_recording_timeout": 180,
            "in_call_recording_timeout": 900,
        },
        "metadata": metadata,
    }
    binding = request_binding(payload, args.region)
    payload["metadata"][BINDING] = binding
    payload["recording_config"]["metadata"][BINDING] = binding
    return payload, binding


def request_binding(payload: dict, region: str) -> str:
    original = decode(canonical(payload))
    original["metadata"].pop(BINDING, None)
    original["recording_config"]["metadata"].pop(BINDING, None)
    return digest(canonical({"region": region, "api_version": "1.11", "payload": original}))


def contains_request(actual: object, expected: object) -> bool:
    """Recall adds defaults/metadata to responses; every requested field must survive."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and contains_request(actual[key], value)
            for key, value in expected.items()
        )
    return type(actual) is type(expected) and actual == expected


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class RecallHTTP:
    def __init__(self, region: str, key: str, *, timeout: float = 30, opener=None):
        require(region in REGIONS, "unsupported_region")
        require(
            isinstance(key, str)
            and 16 <= len(key) <= 4096
            and key.isascii()
            and not re.search(r"[\s\x00-\x1f\x7f]", key),
            "rest_api_key_required",
        )
        self.region, self.origin, self.key, self.timeout = region, REGIONS[region], key, timeout
        # Never forward REST credentials through redirects or an inherited proxy.
        self.opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )

    def api_url(self, target: str) -> str:
        url = urljoin(self.origin, target)
        https_url(url)
        parsed = urlsplit(url)
        require(
            f"{parsed.scheme}://{parsed.netloc}" == self.origin
            and parsed.path.startswith("/api/v1/"),
            "api_origin_or_path_rejected",
        )
        return url

    def json(
        self,
        method: str,
        target: str,
        *,
        body: bytes | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        headers = {"Authorization": f"Token {self.key}", "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        request = urllib.request.Request(self.api_url(target), body, headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                require(200 <= response.status < 300, "provider_response_not_success")
                require(response.geturl() == request.full_url, "provider_redirect_rejected")
                data = response.read(MAX_JSON + 1)
        except urllib.error.HTTPError as error:
            raise OperatorError(f"provider_http_{error.code}") from None
        except (OSError, urllib.error.URLError, http.client.HTTPException) as error:
            raise OperatorError("provider_outcome_unconfirmed") from error
        require(len(data) <= MAX_JSON, "provider_response_too_large")
        return decode(data) if data else {}

    def download(self, url: str, target: Path, max_bytes: int) -> dict:
        request = urllib.request.Request(https_url(url), headers={"Accept": "*/*"})
        total, checksum = 0, hashlib.sha256()
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                require(
                    response.status == 200 and response.geturl() == url,
                    "download_response_rejected",
                )
                length = response.headers.get("Content-Length")
                require(
                    length is None or (length.isdigit() and 0 < int(length) <= max_bytes),
                    "download_size_rejected",
                )
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as output:
                    while chunk := response.read(1024 * 1024):
                        total += len(chunk)
                        require(total <= max_bytes, "download_size_rejected")
                        checksum.update(chunk)
                        output.write(chunk)
                    require(
                        total > 0 and (length is None or total == int(length)),
                        "download_incomplete",
                    )
                    output.flush()
                    os.fsync(output.fileno())
        except urllib.error.HTTPError as error:
            raise OperatorError(f"download_http_{error.code}") from None
        except (OSError, urllib.error.URLError, http.client.HTTPException) as error:
            raise OperatorError("download_incomplete") from error
        return {"bytes": total, "sha256": checksum.hexdigest()}


class Operation:
    def __init__(self, root: Path, operation: str, region: str, api_version: str):
        require(api_version == "1.11", "verify_workspace_api_version_1_11")
        require(region in REGIONS, "unsupported_region")
        self.id, self.region = identifier(operation), region
        self.directory = private_dir(private_dir(root) / self.id, create=True)
        self.journal = self.directory / "operation.json"
        self.request = self.directory / "request.private.json"

    @contextmanager
    def locked(self):
        fd = os.open(
            self.directory / "operation.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
        )
        try:
            check_file(fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise OperatorError("operation_busy") from error
            yield
        finally:
            os.close(fd)

    def load(self) -> dict:
        state = decode(read_private(self.journal))
        require(
            state.get("schema") == 1
            and state.get("operation") == self.id
            and state.get("region") == self.region
            and state.get("api_version") == "1.11",
            "operation_binding_changed",
        )
        require(
            digest(read_private(self.request)) == state.get("request_sha256"),
            "saved_request_changed",
        )
        return state

    def save(self, state: dict) -> None:
        write_private(self.journal, canonical(state))

    def prepare(self, payload: dict, binding: str) -> dict:
        require(
            payload["metadata"].get(OPERATION) == self.id
            and payload["metadata"].get(BINDING) == binding
            and payload["recording_config"]["metadata"] == payload["metadata"]
            and request_binding(payload, self.region) == binding,
            "request_binding_changed",
        )
        data = canonical(payload)
        if self.journal.exists():
            state = self.load()
            require(
                state["request_sha256"] == digest(data)
                and state["request_binding_sha256"] == binding,
                "request_changed",
            )
            return state
        require(not self.request.exists(), "incomplete_preparation_held")
        state = {
            "schema": 1,
            "operation": self.id,
            "region": self.region,
            "api_version": "1.11",
            "request_sha256": digest(data),
            "request_binding_sha256": binding,
            "create": {"phase": "prepared", "idempotency_key": str(uuid4())},
        }
        write_private(self.request, data, exclusive=True)
        self.save(state)
        return state

    def receipt(self, state: dict) -> dict:
        return {
            key: state[key]
            for key in (
                "operation",
                "region",
                "api_version",
                "request_sha256",
                "create",
                "bot_id",
                "leave",
                "collect",
            )
            if key in state
        }

    def client_matches(self, client: RecallHTTP) -> None:
        require(client.region == self.region, "client_region_changed")

    def bound_bot(
        self, client: RecallHTTP, state: dict, bot_id: str, *, full: bool = False
    ) -> dict:
        self.client_matches(client)
        bot = client.json("GET", f"/api/v1/bot/{identifier(bot_id)}/")
        self.verify_bot(state, bot, bot_id, full=full)
        return bot

    def verify_bot(self, state: dict, bot: dict, bot_id: str, *, full: bool = False) -> None:
        require(bot.get("id") == bot_id, "bot_identity_mismatch")
        metadata = bot.get("metadata") or {}
        require(
            metadata.get(OPERATION) == self.id
            and metadata.get(BINDING) == state["request_binding_sha256"],
            "bot_binding_mismatch",
        )
        if full:
            expected = decode(read_private(self.request))
            source = bot.get("meeting_url")
            require(
                source == expected["meeting_url"]
                or source
                == {
                    "platform": "google_meet",
                    "meeting_id": expected["meeting_url"].rsplit("/", 1)[1],
                },
                "bot_meeting_mismatch",
            )
            output = bot.get("output_media") or {}
            require(
                not output.get("audio") and not output.get("screenshare"),
                "bot_media_mismatch",
            )
            # The meeting URL is normalized above; all other requested fields,
            # including timeouts, transcript/own-audio settings and the page, match.
            require(
                contains_request(
                    bot, {key: value for key, value in expected.items() if key != "meeting_url"}
                ),
                "bot_request_mismatch",
            )

    def create(self, client: RecallHTTP, payload: dict, binding: str) -> dict:
        self.client_matches(client)
        state = self.prepare(payload, binding)
        if state["create"]["phase"] == "created":
            return self.receipt(state)
        require(state["create"]["phase"] == "prepared", "create_held_run_reconcile")
        state["create"].update(phase="held", attempted_at=time.time(), reason="outcome_unconfirmed")
        self.save(state)  # Durable before the first byte of any provider effect.
        try:
            bot = client.json(
                "POST",
                "/api/v1/bot/",
                body=read_private(self.request),
                idempotency_key=state["create"]["idempotency_key"],
            )
            bot_id = identifier(bot.get("id"))
            self.verify_bot(state, bot, bot_id, full=True)
        except OperatorError as error:
            state["create"]["reason"] = str(error)
            self.save(state)
            raise
        state["bot_id"] = bot_id
        state["create"].update(phase="created", reason="provider_response_verified")
        self.save(state)
        return self.receipt(state)

    def reconcile(self, client: RecallHTTP, *, effect: str = "create") -> dict:
        self.client_matches(client)
        state = self.load()
        if effect == "leave":
            require(state.get("bot_id") and state.get("leave"), "no_leave_to_reconcile")
            bot = self.bound_bot(client, state, state["bot_id"])
            require(bot_status(bot) in TERMINAL, "leave_outcome_still_unconfirmed")
            state["leave"].update(phase="observed_ended", observed_at=time.time())
            self.save(state)
            return self.receipt(state)
        if state["create"]["phase"] == "created":
            self.bound_bot(client, state, state["bot_id"])
            return self.receipt(state)
        state["create"].update(phase="held", reason="reconciliation_pending")
        self.save(state)
        target = "/api/v1/bot/?" + urlencode({f"metadata__{OPERATION}": self.id})
        seen, matches = set(), set()
        for _ in range(MAX_PAGES):
            url = client.api_url(target)
            parsed = urlsplit(url)
            require(
                parsed.path == "/api/v1/bot/"
                and parse_qs(parsed.query).get(f"metadata__{OPERATION}") == [self.id],
                "reconcile_page_scope_changed",
            )
            require(url not in seen, "reconcile_pagination_loop")
            seen.add(url)
            page = client.json("GET", url)
            require(isinstance(page.get("results"), list), "reconcile_invalid_page")
            require("next" in page, "reconcile_pagination_unconfirmed")
            for row in page["results"]:
                require(
                    isinstance(row, dict) and (row.get("metadata") or {}).get(OPERATION) == self.id,
                    "reconcile_result_scope_changed",
                )
                require(
                    (row.get("metadata") or {}).get(BINDING) == state["request_binding_sha256"],
                    "reconcile_request_mismatch",
                )
                matches.add(identifier(row.get("id")))
            require(len(matches) <= 1, "reconcile_multiple_bots_held")
            target = page.get("next")
            if target is None:
                break
            require(isinstance(target, str) and bool(target), "reconcile_invalid_next_page")
        else:
            raise OperatorError("reconcile_page_limit_held")
        require(len(matches) == 1, "reconcile_no_exact_bot_held")
        bot_id = matches.pop()
        self.bound_bot(client, state, bot_id, full=True)
        state["bot_id"] = bot_id
        state["create"].update(phase="created", reason="exact_bot_reconciled")
        self.save(state)
        return self.receipt(state)

    def status(self, client: RecallHTTP) -> dict:
        state = self.load()
        require(state.get("bot_id"), "create_held_run_reconcile")
        bot = self.bound_bot(client, state, state["bot_id"])
        if (state.get("collect") or {}).get("phase") == "complete":
            self.verify_collection(state)
            self.verify_current_collection(client, state, bot)
        recordings = bot.get("recordings") or []
        require(isinstance(recordings, list), "invalid_recording_list")
        return {
            **self.receipt(state),
            "bot_status": bot_status(bot),
            "recordings": [
                {"id": identifier(row.get("id")), "status": artifact_status(row)}
                for row in recordings
            ],
        }

    def leave(self, client: RecallHTTP) -> dict:
        self.client_matches(client)
        state = self.load()
        require(state.get("bot_id"), "create_held_run_reconcile")
        previous = state.get("leave")
        if previous:
            require(previous["phase"] != "held", "leave_held_run_reconcile")
            return self.receipt(state)
        bot = self.bound_bot(client, state, state["bot_id"])
        state["leave"] = {"idempotency_key": str(uuid4()), "bot_id": state["bot_id"]}
        if bot_status(bot) in TERMINAL:
            state["leave"].update(phase="observed_ended", observed_at=time.time())
        else:
            state["leave"].update(
                phase="held", attempted_at=time.time(), reason="outcome_unconfirmed"
            )
            self.save(state)
            try:
                result = client.json(
                    "POST",
                    f"/api/v1/bot/{state['bot_id']}/leave_call/",
                    body=b"{}",
                    idempotency_key=state["leave"]["idempotency_key"],
                )
                require(not result or result.get("id") == state["bot_id"], "leave_bot_mismatch")
            except OperatorError as error:
                state["leave"]["reason"] = str(error)
                self.save(state)
                raise
            state["leave"].update(phase="accepted", reason="provider_accepted_leave")
        self.save(state)
        return self.receipt(state)

    def collect(self, client: RecallHTTP, artifacts: Path) -> dict:
        state = self.load()
        self.client_matches(client)
        require(state.get("bot_id"), "create_held_run_reconcile")
        artifacts = private_dir(artifacts)
        previous = state.get("collect") or {}
        require(
            previous.get("artifact_root", str(artifacts)) == str(artifacts),
            "artifact_directory_changed",
        )
        destination = artifacts / self.id
        if destination.exists():
            require(previous.get("recording_ids"), "unbound_existing_artifact_directory")
            self.verify_collection(state)
            self.verify_current_collection(client, state)
            state["collect"] = {
                **previous,
                "phase": "complete",
                "artifact_root": str(artifacts),
                "manifest": str(destination / "manifest.json"),
            }
            self.save(state)
            return self.receipt(state)
        state["collect"] = {"phase": "incomplete", "artifact_root": str(artifacts)}
        self.save(state)
        staging = None
        try:
            bot = self.bound_bot(client, state, state["bot_id"])
            require(bot_status(bot) in TERMINAL, "bot_not_ended")
            rows = bot.get("recordings")
            require(isinstance(rows, list) and bool(rows), "recordings_unavailable")
            ids = [identifier(row.get("id")) for row in rows]
            require(len(set(ids)) == len(ids), "duplicate_recording_ids")
            recordings = []
            for recording_id in sorted(ids):
                recording = client.json("GET", f"/api/v1/recording/{recording_id}/")
                require(
                    recording.get("id") == recording_id
                    and (recording.get("bot") or {}).get("id") == state["bot_id"],
                    "recording_bot_mismatch",
                )
                require(
                    (recording.get("metadata") or {}).get(OPERATION) == self.id
                    and (recording.get("metadata") or {}).get(BINDING)
                    == state["request_binding_sha256"],
                    "recording_binding_mismatch",
                )
                require(artifact_status(recording) == "done", "recording_not_done")
                for name in MEDIA:
                    media = (recording.get("media_shortcuts") or {}).get(name) or {}
                    require(artifact_status(media) == "done", "artifact_not_done")
                    identifier(media.get("id"))
                    https_url((media.get("data") or {}).get("download_url"))
                    if name in {"video_mixed", "audio_mixed"}:
                        require(
                            media.get("format") == ("mp4" if name == "video_mixed" else "mp3"),
                            "artifact_format_mismatch",
                        )
                recordings.append(recording)
            state["collect"]["recording_ids"] = sorted(ids)
            state["collect"]["media_ids"] = {
                row["id"]: {name: row["media_shortcuts"][name]["id"] for name in MEDIA}
                for row in recordings
            }
            self.save(state)
            staging = private_dir(artifacts / f".{self.id}.collect-{uuid4()}", create=True)
            manifest = {
                "schema": 1,
                "operation": self.id,
                "region": self.region,
                "request_sha256": state["request_sha256"],
                "bot_id": state["bot_id"],
                "completed_at": time.time(),
                "recordings": [],
            }
            for recording in recordings:
                folder = private_dir(staging / recording["id"], create=True)
                entry = {"id": recording["id"], "media": {}}
                for name, filename in MEDIA.items():
                    media = recording["media_shortcuts"][name]
                    result = client.download(
                        media["data"]["download_url"],
                        folder / filename,
                        MAX_TRANSCRIPT if name == "transcript" else MAX_DOWNLOAD,
                    )
                    if name == "transcript":
                        try:
                            transcript = json.loads(read_private(folder / filename, MAX_TRANSCRIPT))
                        except (ValueError, UnicodeError) as error:
                            raise OperatorError("invalid_transcript_artifact") from error
                        require(isinstance(transcript, list), "invalid_transcript_artifact")
                    entry["media"][name] = {
                        "id": media["id"],
                        "file": f"{recording['id']}/{filename}",
                        **result,
                    }
                sync_dir(folder)
                manifest["recordings"].append(entry)
            latest = self.bound_bot(client, state, state["bot_id"])
            require(
                bot_status(latest) in TERMINAL
                and sorted(identifier(row.get("id")) for row in (latest.get("recordings") or []))
                == sorted(ids),
                "recording_set_changed",
            )
            write_private(staging / "manifest.json", canonical(manifest), exclusive=True)
            verify_manifest(staging, state)
            os.rename(staging, destination)
            staging = None
            sync_dir(artifacts)
        except OperatorError as error:
            state["collect"]["reason"] = str(error)
            self.save(state)
            raise
        finally:
            if staging is not None:
                shutil.rmtree(staging)
        state["collect"].update(phase="complete", manifest=str(destination / "manifest.json"))
        self.save(state)
        return self.receipt(state)

    def verify_collection(self, state: dict) -> None:
        collection = state["collect"]
        try:
            verify_manifest(Path(collection["artifact_root"]) / self.id, state)
        except (OperatorError, OSError, ValueError, KeyError, TypeError) as error:
            collection.update(phase="incomplete", reason="artifact_integrity_failed")
            self.save(state)
            raise OperatorError("artifact_integrity_failed") from error

    def verify_current_collection(
        self, client: RecallHTTP, state: dict, bot: dict | None = None
    ) -> None:
        """Stored bytes cannot certify a later, different provider recording set."""
        collection = state["collect"]
        try:
            bot = bot if bot is not None else self.bound_bot(client, state, state["bot_id"])
            require(bot_status(bot) in TERMINAL, "bot_not_ended")
            rows = bot.get("recordings")
            require(isinstance(rows, list), "invalid_recording_list")
            ids = [identifier(row.get("id")) for row in rows]
            require(
                len(ids) == len(set(ids)) and sorted(ids) == collection["recording_ids"],
                "recording_set_changed",
            )
            for recording_id in ids:
                recording = client.json("GET", f"/api/v1/recording/{recording_id}/")
                require(
                    recording.get("id") == recording_id
                    and (recording.get("bot") or {}).get("id") == state["bot_id"],
                    "recording_bot_mismatch",
                )
                metadata = recording.get("metadata") or {}
                require(
                    metadata.get(OPERATION) == self.id
                    and metadata.get(BINDING) == state["request_binding_sha256"],
                    "recording_binding_mismatch",
                )
                require(artifact_status(recording) == "done", "recording_not_done")
                for name in MEDIA:
                    media = (recording.get("media_shortcuts") or {}).get(name) or {}
                    require(artifact_status(media) == "done", "artifact_not_done")
                    require(
                        media.get("id") == collection["media_ids"][recording_id][name],
                        "recording_media_changed",
                    )
        except OperatorError as error:
            collection.update(phase="incomplete", reason=str(error))
            self.save(state)
            raise


def artifact_status(value: dict) -> str:
    code = (value.get("status") or {}).get("code")
    return code if code in ARTIFACT_STATES else "unknown"


def bot_status(bot: dict) -> str:
    changes = bot.get("status_changes") or []
    code = changes[-1].get("code") if changes else None
    return code if code in BOT_STATES else "unknown"


def verify_manifest(folder: Path, state: dict) -> None:
    private_dir(folder)
    manifest = decode(read_private(folder / "manifest.json"))
    require(
        manifest.get("schema") == 1
        and manifest.get("operation") == state["operation"]
        and manifest.get("region") == state["region"]
        and manifest.get("bot_id") == state["bot_id"]
        and manifest.get("request_sha256") == state["request_sha256"],
        "manifest_binding_mismatch",
    )
    recordings = manifest.get("recordings")
    require(isinstance(recordings, list) and bool(recordings), "incomplete_manifest")
    expected = state.get("collect") or {}
    require(
        sorted(identifier(row.get("id")) for row in recordings) == expected.get("recording_ids"),
        "manifest_recording_set_mismatch",
    )
    seen = set()
    for recording in recordings:
        recording_id = identifier(recording.get("id"))
        require(recording_id not in seen, "duplicate_recording_ids")
        seen.add(recording_id)
        private_dir(folder / recording_id)
        media = recording.get("media") or {}
        require(set(media) == set(MEDIA), "incomplete_manifest")
        for name, filename in MEDIA.items():
            entry = media[name]
            identifier(entry.get("id"))
            require(
                entry["id"] == (expected.get("media_ids", {}).get(recording_id) or {}).get(name),
                "manifest_media_id_mismatch",
            )
            require(entry.get("file") == f"{recording_id}/{filename}", "manifest_path_rejected")
            target = folder / recording_id / filename
            checksum, length = hashlib.sha256(), 0
            fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                check_file(stream.fileno())
                while chunk := stream.read(1024 * 1024):
                    length += len(chunk)
                    checksum.update(chunk)
            require(
                length > 0
                and length == entry.get("bytes")
                and checksum.hexdigest() == entry.get("sha256"),
                "artifact_integrity_failed",
            )


class QuietParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally echoes invalid values, which could contain a page token.
        raise OperatorError("invalid_arguments_see_help")


def parser() -> argparse.ArgumentParser:
    result = QuietParser(description=__doc__)
    result.add_argument(
        "--state-dir", type=Path, required=True, help="Existing owner-only directory"
    )
    result.add_argument("--operation", required=True, help="Stable canonical UUID for this bot")
    result.add_argument("--region", required=True, choices=sorted(REGIONS))
    result.add_argument(
        "--workspace-api-version",
        required=True,
        help="Explicitly verified workspace version; only 1.11 is supported",
    )
    result.add_argument("--api-key-file", type=Path, help="Otherwise inject RECALL_API_KEY")
    commands = result.add_subparsers(dest="command", required=True, parser_class=QuietParser)
    for name in ("preflight", "create"):
        command = commands.add_parser(name)
        command.add_argument("--meeting-url", required=True)
        command.add_argument("--page-url", required=True, help="Clean HTTPS participant page URL")
        command.add_argument("--meeting-key-file", required=True, type=Path)
    commands.add_parser("status")
    commands.add_parser("leave")
    reconcile = commands.add_parser("reconcile")
    reconcile.add_argument("--effect", choices=("create", "leave"), default="create")
    collect = commands.add_parser("collect")
    collect.add_argument("--artifact-dir", required=True, type=Path)
    return result


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        require(args.workspace_api_version == "1.11", "verify_workspace_api_version_1_11")
        if args.command == "preflight":
            private_dir(args.state_dir)
            payload, binding = payload_for(args)
            print(
                json.dumps(
                    {
                        "operation": args.operation,
                        "region": args.region,
                        "api_version": "1.11",
                        "request_sha256": digest(canonical(payload)),
                        "request_binding_sha256": binding,
                        "provider_calls": 0,
                        "state": "local_inputs_validated",
                    }
                )
            )
            return 0
        if args.api_key_file:
            key = secret_file(args.api_key_file)
        else:
            key = os.environ.get("RECALL_API_KEY", "")
        client = RecallHTTP(args.region, key)
        operation = Operation(
            args.state_dir, args.operation, args.region, args.workspace_api_version
        )
        with operation.locked():
            if args.command == "create":
                result = operation.create(client, *payload_for(args))
            elif args.command == "reconcile":
                result = operation.reconcile(client, effect=args.effect)
            elif args.command == "collect":
                result = operation.collect(client, args.artifact_dir)
            else:
                result = getattr(operation, args.command)(client)
        print(json.dumps(result))
        return 0
    except OperatorError as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 2
    except (Exception, KeyboardInterrupt):
        # Provider bodies, URLs and exception strings are deliberately never logged.
        print(json.dumps({"error": "operator_failed_inspect_private_state"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
