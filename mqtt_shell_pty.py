"""PTY and terminal-size management, independent of message transport."""
import fcntl
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import threading
from pathlib import Path


class Shell:
    def __init__(self, on_output, on_exit, initial_size=(24, 80)):
        self.on_output = on_output
        self.on_exit = on_exit
        self.master, slave = pty.openpty()
        self.lock = threading.Lock()
        self.resize(*initial_size, fd=slave)
        shell = os.environ.get('SHELL', '/bin/bash')
        if not os.path.isfile(shell):
            shell = '/bin/sh'
        command = [sys.executable, str(Path(__file__).with_name('mqtt_shell_tty_exec.py')), shell]
        if shell.endswith('bash'):
            command.append('-l')
        env = os.environ.copy()
        env['TERM'] = env.get('TERM') or 'xterm-256color'
        try:
            self.process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave,
                                            start_new_session=True, close_fds=True, env=env,
                                            cwd=os.path.expanduser('~'))
        except BaseException:
            os.close(self.master)
            os.close(slave)
            raise
        os.close(slave)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def resize(self, rows, cols, fd=None):
        if not (1 <= rows <= 1000 and 1 <= cols <= 1000):
            raise ValueError('Invalid terminal dimensions')
        fcntl.ioctl(self.master if fd is None else fd, termios.TIOCSWINSZ,
                    struct.pack('HHHH', rows, cols, 0, 0))
        if hasattr(self, 'process') and self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGWINCH)

    def write(self, payload):
        with self.lock:
            if self.process.poll() is None:
                os.write(self.master, payload)

    def _read(self):
        try:
            while True:
                ready, _, _ = select.select([self.master], [], [], 0.2)
                if ready:
                    try:
                        data = os.read(self.master, 4096)
                    except OSError:  # EIO is normal once PTY slave closes
                        break
                    if not data:
                        break
                    self.on_output(data)
                elif self.process.poll() is not None:
                    # Keep draining buffered stdout before reporting shell-exited.
                    continue
        finally:
            code = self.process.wait()
            try:
                os.close(self.master)
            except OSError:
                pass
            self.on_exit(code)

    def close(self):
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGHUP)
            except ProcessLookupError:
                pass
