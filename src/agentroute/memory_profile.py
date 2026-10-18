"""Read process memory counters without collecting arguments, environment, or heap contents."""

import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .config import agentroute_home

# Public Darwin rusage_info_v4 fields, through the lifetime footprint peak.
_DARWIN_FIELDS = (
    "user_time system_time pkg_idle_wkups interrupt_wkups pageins wired_size resident_size "
    "phys_footprint proc_start_abstime proc_exit_abstime child_user_time child_system_time "
    "child_pkg_idle_wkups child_interrupt_wkups child_pageins child_elapsed_abstime "
    "diskio_bytesread diskio_byteswritten cpu_time_qos_default cpu_time_qos_maintenance "
    "cpu_time_qos_background cpu_time_qos_utility cpu_time_qos_legacy cpu_time_qos_user_initiated "
    "cpu_time_qos_user_interactive billed_system_time serviced_system_time logical_writes "
    "lifetime_max_phys_footprint instructions cycles billed_energy serviced_energy "
    "interval_max_phys_footprint runnable_time"
).split()


class _DarwinUsage(ctypes.Structure):
    _fields_ = [("uuid", ctypes.c_uint8 * 16)] + [
        (name, ctypes.c_uint64) for name in _DARWIN_FIELDS
    ]


def parse_processes(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) != 4 or not parts[0].isdigit():
            continue
        pid, parent, rss, executable = parts
        rows.append(
            {
                "pid": int(pid),
                "ppid": int(parent),
                "rss_bytes": int(rss) * 1024,
                "executable": executable,
            }
        )
    return rows


def snapshot() -> dict:
    if sys.platform not in {"darwin", "linux"}:
        raise ValueError("memory profiling supports macOS and Linux")
    output = subprocess.check_output(["ps", "-axo", "pid=,ppid=,rss=,comm="], text=True)
    rows = parse_processes(output)
    if sys.platform == "darwin":
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        for row in rows:
            usage = _DarwinUsage()
            if library.proc_pid_rusage(row["pid"], 4, ctypes.byref(usage)) == 0:
                row["footprint_bytes"] = usage.phys_footprint
                row["peak_footprint_bytes"] = usage.lifetime_max_phys_footprint
                row["identity"] = usage.proc_start_abstime
    by_pid = {row["pid"]: row for row in rows}
    for row in rows:
        root = row
        visited = set()
        while root["ppid"] in by_pid and root["ppid"] not in visited:
            visited.add(root["pid"])
            root = by_pid[root["ppid"]]
        row["root_pid"] = root["pid"]
    return {"timestamp": time.time(), "platform": sys.platform, "processes": rows}


def watch(*, duration: float, interval: float, threshold_bytes: int, output: Path) -> dict:
    if not 0 < duration <= 86400 or not 1 <= interval <= 300:
        raise ValueError("duration must be at most one day and interval between 1 and 300 seconds")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    captured = set()
    samples = 0
    deadline = time.monotonic() + duration
    stream = output / "samples.jsonl"
    descriptor = os.open(stream, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "a") as handle:
        while True:
            sample = snapshot()
            # Persist only coding-runtime processes, not arbitrary application inventory.
            relevant = [
                row
                for row in sample["processes"]
                if any(
                    marker in row["executable"]
                    for marker in ("codex", "agentroute", "node", "Python")
                )
            ]
            handle.write(json.dumps({**sample, "processes": relevant}) + "\n")
            handle.flush()
            samples += 1
            for row in relevant:
                identity = (row["pid"], row.get("identity"))
                size = row.get("footprint_bytes", row["rss_bytes"])
                if (
                    "codex-bin" not in row["executable"]
                    or size < threshold_bytes
                    or identity in captured
                ):
                    continue
                captured.add(identity)
                if sys.platform == "darwin":
                    report = subprocess.run(
                        ["vmmap", "-summary", str(row["pid"])],
                        capture_output=True,
                        text=True,
                        timeout=20,
                    )
                    path = output / f"spike-{row['pid']}-{int(sample['timestamp'])}.txt"
                    path.write_text(report.stdout + report.stderr)
                    path.chmod(0o600)
            if time.monotonic() >= deadline:
                break
            time.sleep(min(interval, max(0, deadline - time.monotonic())))
    return {"samples": samples, "spikes": len(captured), "output": str(output)}


def default_output() -> Path:
    return agentroute_home() / "profiling" / "memory" / time.strftime("%Y%m%d-%H%M%S")
