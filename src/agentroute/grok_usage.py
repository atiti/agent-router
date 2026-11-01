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


def account_key(credential):
    data = credential._read()
    identity = data.get("user_id") or data["key"]
    return hashlib.sha256(identity.encode()).hexdigest()


def read_snapshot(credential):
    try:
        data = json.loads((agentroute_home() / "grok-usage.json").read_text())
        if not isinstance(data, dict):
            return {}
        observed = data.get("observed_at", 0)
        if not isinstance(observed, (int, float)) or not math.isfinite(observed):
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


def parse_billing(payload):
    config = payload.get("config") or {}
    used = config.get("creditUsagePercent")
    if used is None:
        limit = (config.get("monthlyLimit") or {}).get("val", 0)
        if limit > 0:
            used = (config.get("used") or {}).get("val", 0) / limit * 100
    if used is not None:
        used = float(used)
        if not math.isfinite(used) or used < 0:
            raise ValueError("Invalid usage percentage")
    period = config.get("currentPeriod") or {}
    end = period.get("end") or config.get("billingPeriodEnd")
    reset = int(datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp()) if end else None
    return {"used_percent": used, "resets_at": reset}


def fetch_usage(credential, version, *, force=False):
    from .grok_bridge import UPSTREAM, open_upstream, request_headers

    previous = read_snapshot(credential)
    if previous.get("blocked_until", 0) > time.time():
        return previous
    if not force and time.time() - previous.get("observed_at", 0) < TTL:
        return previous
    try:
        headers = request_headers(credential, version, "grok-4.7")
        account = account_key(credential)
        headers["x-userid"] = credential._read().get("user_id", "")
        with open_upstream(
            urllib.request.Request(UPSTREAM + "/billing?format=credits", headers=headers), timeout=8
        ) as response:
            result = parse_billing(json.load(response))
        return write_snapshot(credential, result, account=account)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
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
                if result.get("error"):
                    return {}
                data = read_snapshot(credential)
            except (OSError, ValueError):
                return {}
    if time.time() - data.get("observed_at", 0) > TTL * 2:
        return {}
    return data
