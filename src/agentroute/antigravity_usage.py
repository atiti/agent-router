"""Antigravity's weekly allowance, read from the CLI that owns it.

Google gates the Code Assist quota API (``retrieveUserQuota`` and friends) to its
own clients; an ordinary token receives ``403 SUBSCRIPTION_REQUIRED``. The
Antigravity CLI already prints the numbers it is entitled to, so asking it is
both the cheapest and the only honest route.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

CACHE_NAME = "antigravity-usage.json"
CACHE_TTL_SECONDS = 600.0


def cache_path() -> Path:
    home = Path(os.environ.get("AGENTROUTE_HOME") or (Path.home() / ".agentroute"))
    return home / CACHE_NAME


def parse_usage_output(text: str) -> list[dict[str, object]]:
    """Parse ``agy -p "/usage"`` rows: group, metric, remaining percent, reset."""
    groups: list[dict[str, object]] = []
    for line in text.splitlines():
        fields = [part.strip() for part in line.split("\t")]
        if len(fields) < 3:
            continue
        try:
            remaining = float(fields[2].rstrip("%"))
        except ValueError:
            continue
        groups.append(
            {
                "name": fields[0],
                "metric": fields[1],
                "remaining_percent": remaining,
                "resets_at": fields[3] if len(fields) > 3 and fields[3] else None,
            }
        )
    return groups


def fetch_limits(
    *, max_age: float = CACHE_TTL_SECONDS, path: Path | None = None, binary: str | None = None
) -> dict[str, object] | None:
    """Return the subscription allowance, cached because agy takes seconds to start."""
    path = path or cache_path()
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - float(cached.get("observed_epoch") or 0) < max_age:
            return cached
    except (OSError, ValueError, TypeError):
        pass
    if binary is None:
        import shutil

        binary = shutil.which("agy")
    if not binary:
        return None
    try:
        result = subprocess.run(
            [binary, "-p", "/usage"],
            capture_output=True,
            text=True,
            timeout=90,
            cwd=os.environ.get("HOME") or "/tmp",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    groups = parse_usage_output(result.stdout)
    if not groups:
        return None
    observed = time.time()
    payload: dict[str, object] = {
        "source": "antigravity-cli",
        "observed_epoch": observed,
        "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(observed)),
        "groups": groups,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass
    return payload
