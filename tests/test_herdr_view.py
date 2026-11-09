"""Exercise the real PTY boundary: output and resize pass, human input doesn't."""
import errno
import fcntl
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import time


def test_watch_resizes_and_drops_human_keyboard_input(tmp_path):
    child = tmp_path / "child.py"
    child.write_text('''import os, select, time, tty

tty.setraw(0)
print("INITIAL", os.get_terminal_size(0).columns, flush=True)
time.sleep(.6)
ready, _, _ = select.select([0], [], [], .1)
print("INPUT_BLOCKED" if not ready else "INPUT_LEAKED", flush=True)
print("RESIZED", os.get_terminal_size(0).columns, flush=True)
''')
    runner = tmp_path / "runner.py"
    runner.write_text('''import os, sys
from agentroute.herdr_view import watch
raise SystemExit(watch([sys.executable, sys.argv[1]], dict(os.environ)))
''')
    master, slave = pty.openpty()
    process = None
    try:
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        process = subprocess.Popen([sys.executable, str(runner), str(child)],
                                   stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        slave = -1
        output = b""
        changed = False
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], .2)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError as error:
                    if error.errno == errno.EIO:
                        break
                    raise
                if not chunk:
                    break
                output += chunk
            if b"INITIAL 80" in output and not changed:
                os.write(master, b"human input must not reach the worker\n")
                fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
                changed = True
            if process.poll() is not None:
                break
        assert process.wait(timeout=2) == 0
        assert b"INPUT_BLOCKED" in output
        assert b"INPUT_LEAKED" not in output
        assert b"RESIZED 100" in output
    finally:
        os.close(master)
        if slave >= 0:
            os.close(slave)
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
