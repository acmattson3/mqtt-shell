#!/usr/bin/env python3
"""Exec helper: safely acquire the controlling TTY after Popen(start_new_session=True)."""
import fcntl
import os
import sys
import termios


def main():
    if len(sys.argv) < 2:
        raise SystemExit('Usage: mqtt_shell_tty_exec.py SHELL [ARG...]')
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == '__main__':
    main()
