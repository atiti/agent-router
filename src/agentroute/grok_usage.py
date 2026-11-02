"""Account-bound, short-lived Grok subscription allowance snapshots."""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import tempfile
import time
import urllib.request
from datetime import datetime
from urllib.parse import urlparse

from .config import agentroute_home

TTL = 60


def account_key(credential, *, login=None):
    data = credential._read() if login is None else login
    identity = data.get("user_id") or data["key"]
    if not isinstance(identity, str):
        raise ValueError("Invalid Grok account identity")
    return hashlib.sha256(identity.encode()).hexdigest()


def read_snapshot(credential):
    try:
        data = json.loads((agentroute_home() / "grok-usage.json").read_text())
        if not isinstance(data, dict):
            return {}
        observed = data.get("observed_at", 0)
        if not valid_number(observed) or observed > time.time() + TTL:
            return {}
        for field in ("used_percent", "resets_at", "blocked_until"):
            value = data.get(field)
            if field == "blocked_until" and field in data and value is None:
                return {}
            if value is not None and not valid_number(value):
                return {}
        if data.get("account") == account_key(credential):
            return data
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return {}


def write_snapshot(credential, data, *, account=None):
    data = dict(data, account=account or account_key(credential), observed_at=time.time())
    home = agentroute_home()
    home.mkdir(parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix=".grok-usage-", dir=home)
    try:
        with os.fdopen(fd, "w") as file:
            json.dump(data, file)
        os.replace(path, home / "grok-usage.json")
    finally:
        if os.path.exists(path):
            os.unlink(path)
    return data


def valid_number(value):
    try:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value >= 0
        )
    except OverflowError:
        return False


def _object(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Invalid Grok billing object")
    return value


def _number(value):
    if isinstance(value, bool):
        raise ValueError("Invalid Grok billing measurement")
    value = float(value)
    if not valid_number(value):
        raise ValueError("Invalid Grok billing measurement")
    return value


def parse_billing(payload):
    config = _object(_object(payload).get("config"))
    used = config.get("creditUsagePercent")
    if used is None:
        limit = _number(_object(config.get("monthlyLimit")).get("val", 0))
        if limit > 0:
            consumed = _object(config.get("used")).get("val")
            if consumed is not None:
                used = _number(consumed) / limit * 100
    if used is not None:
        used = _number(used)
    period = _object(config.get("currentPeriod"))
    end = period.get("end") or config.get("billingPeriodEnd")
    reset = None
    if end is not None:
        if not isinstance(end, str):
            raise ValueError("Invalid Grok billing reset")
        timestamp = datetime.fromisoformat(end.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError("Invalid Grok billing reset timezone")
        reset = int(timestamp.timestamp())
        if not valid_number(reset):
            raise ValueError("Invalid Grok billing reset")
    return {"used_percent": used, "resets_at": reset}


def fetch_usage(credential, version, *, force=False):
    if getattr(credential, "mode", "subscription") == "api-key":
        return {"error": "API billing telemetry unavailable"}
    from .grok_bridge import UPSTREAM, open_upstream, request_headers

    previous = read_snapshot(credential)
    if previous.get("blocked_until", 0) > time.time():
        return previous
    if not force and time.time() - previous.get("observed_at", 0) < TTL:
        return previous
    try:
        login = credential.current()
        headers = request_headers(credential, version, "grok-4.7", login=login)
        account = account_key(credential, login=login)
        headers["x-userid"] = login.get("user_id", "")
        with open_upstream(
            urllib.request.Request(UPSTREAM + "/billing?format=credits", headers=headers), timeout=8
        ) as response:
            result = parse_billing(json.load(response))
        return write_snapshot(credential, result, account=account)
    except (OSError, ValueError, KeyError, TypeError, OverflowError, subprocess.SubprocessError):
        # Never extend a stale successful observation after a failed read.
        return {"error": "subscription telemetry unavailable"}


def capacity_snapshot(base_url, *, offline=False):
    from .grok_bridge import GrokCredential

    credential = GrokCredential("grok")
    data = read_snapshot(credential)
    if data.get("blocked_until", 0) > time.time():
        return data
    if not offline and time.time() - data.get("observed_at", 0) >= TTL:
        url = urlparse(base_url or "")
        if url.scheme == "http" and url.hostname in {"127.0.0.1", "localhost"}:
            try:
                with urllib.request.urlopen(
                    f"{url.scheme}://{url.netloc}/usage", timeout=10
                ) as response:
                    result = json.loads(response.read(65536))
                if not isinstance(result, dict) or result.get("error"):
                    return {}
                data = read_snapshot(credential)
            except (OSError, ValueError):
                return {}
    if time.time() - data.get("observed_at", 0) > TTL * 2:
        return {}
    return data
