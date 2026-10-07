"""Prepare process limits in the child before replacing it with the shared server."""

import os
import sys

MIN_OPEN_FILES = 8192


def prepare_open_file_limit() -> None:
    # Desktop launches can inherit macOS's 256-file soft limit. A persistent
    # owner retains sockets and watchers for many tasks, so use the allowed
    # headroom without changing the machine or the launching parent's limits.
    try:
        import resource
    except ImportError:
        return
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft == resource.RLIM_INFINITY or soft >= MIN_OPEN_FILES:
        return
    target = MIN_OPEN_FILES if hard == resource.RLIM_INFINITY else min(MIN_OPEN_FILES, hard)
    if target <= soft:
        return
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except (OSError, ValueError) as error:
        print(
            f"AgentRoute: could not raise shared server open-file limit: {error}", file=sys.stderr
        )


def main() -> None:
    argv = sys.argv[1:]
    if not argv:
        raise SystemExit("usage: python -m agentroute.server_process BINARY [ARGS...]")
    prepare_open_file_limit()
    os.execv(argv[0], argv)


if __name__ == "__main__":
    main()
