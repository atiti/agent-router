"""Interactive, resizable terminal attachment for Herdr worker panes."""
from __future__ import annotations

import errno
import fcntl
import os
import pty
import select
import struct
import subprocess
import termios
import tty


def watch(argv: list[str], environment: dict[str, str]) -> int:
    """Relay input, output and size; Ctrl+\\ closes only the attachment."""
    input_fd, output_fd = 0, 1
    previous = termios.tcgetattr(input_fd)
    master, slave = pty.openpty()
    process = None
    last_size = None
    detached = False

    def resize():
        nonlocal last_size
        size = os.get_terminal_size(output_fd)
        if size != last_size:
            fcntl.ioctl(master, termios.TIOCSWINSZ,
                        struct.pack("HHHH", size.lines, size.columns, 0, 0))
            last_size = size

    try:
        resize()
        tty.setraw(input_fd)
        process = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave,
                                   env=environment, start_new_session=True)
        os.close(slave)
        slave = -1
        while True:
            resize()
            ready, _, _ = select.select([master, input_fd], [], [], .2)
            if master in ready:
                try:
                    data = os.read(master, 65536)
                except OSError as error:
                    if error.errno != errno.EIO:
                        raise
                    break
                if not data:
                    break
                while data:
                    data = data[os.write(output_fd, data):]
            if input_fd in ready:
                keys = os.read(input_fd, 65536)
                if not keys or b"\x1c" in keys or b"\x1b[92;5u" in keys:
                    detached = True
                    break
                while keys:
                    keys = keys[os.write(master, keys):]
            if process.poll() is not None:
                break
    finally:
        termios.tcsetattr(input_fd, termios.TCSADRAIN, previous)
        if slave >= 0:
            os.close(slave)
        os.close(master)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    return 0 if detached or process is None else (process.returncode or 0)
