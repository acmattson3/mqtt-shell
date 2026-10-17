#!/usr/bin/env python3
"""Local interactive terminal client for MQTT shell protocol v2."""
import fcntl
import getpass
import os
import queue
import select
import shutil
import signal
import struct
import sys
import termios
import threading
import tty
from mqtt_shell_protocol import VERSION, Cipher, decode, encode, nonce, session_material
from mqtt_shell_transport import make_transport


class Client:
    def __init__(self, agent_id, secret, transport=None):
        if not agent_id or '/' in agent_id:
            raise ValueError('Invalid agent ID')
        self.root = f'mqtt-shell/{agent_id}'
        self.sid = nonce()
        self.cn = nonce()
        self.secret = secret
        self.cipher = None
        self.ready = threading.Event()
        self.exited = threading.Event()
        self.error = None
        self.exit_code = None
        self.stdout = queue.Queue()
        self.lock = threading.Lock()
        self.transport = transport or make_transport(self.on_message,
            [f'{self.root}/session/{self.sid}/control',
             f'{self.root}/session/{self.sid}/out'], 'client')

    def on_message(self, topic, payload):
        if topic == f'{self.root}/session/{self.sid}/control':
            message = decode(payload)
            if message.get('type') == 'challenge':
                proof, key = session_material(self.secret, self.sid, self.cn, message['nonce'])
                with self.lock:
                    self.cipher = Cipher(key, self.sid, sender='client')
                self.transport.publish(f'{self.root}/session/{self.sid}/auth',
                                       encode({'v': VERSION, 'proof': proof}))
            elif message.get('type') == 'fail':
                self.error = 'Authentication failed or PTY unavailable'
                self.ready.set()
        elif topic == f'{self.root}/session/{self.sid}/out':
            with self.lock:
                if self.cipher is None:
                    return
                packet = self.cipher.open(payload)
            if packet is None:
                return
            kind, data = packet
            if kind == 'K':
                self.ready.set()
            elif kind == 'O':
                self.stdout.put(data)
            elif kind == 'X':
                if len(data) == 4:
                    self.exit_code = struct.unpack('>i', data)[0]
                self.exited.set()

    def start(self, rows=24, cols=80):
        self.transport.start()
        self.transport.publish(self.root + '/hello', encode({
            'v': VERSION, 'id': self.sid, 'nonce': self.cn,
            'rows': rows, 'cols': cols}))
        if not self.ready.wait(timeout=20):
            raise TimeoutError('Agent did not respond to handshake')
        if self.error:
            raise PermissionError(self.error)

    def send(self, kind, payload=b''):
        with self.lock:
            if self.cipher is None or not self.ready.is_set() or self.error:
                raise RuntimeError('Session not authenticated')
            packet = self.cipher.seal(kind, payload)
            self.transport.publish(f'{self.root}/session/{self.sid}/in', packet)

    def resize(self, rows, cols):
        self.send('R', struct.pack('>HH', rows, cols))

    def close(self):
        if self.ready.is_set() and not self.exited.is_set() and not self.error:
            try:
                self.send('C')
            except Exception:
                pass
        self.transport.close()


def parse_target(value):
    parts = value.split('@')
    if len(parts) == 2:
        user, agent, host = None, parts[0], parts[1]
    elif len(parts) == 3:
        user, agent, host = parts
    else:
        raise ValueError('Expected [mqtt-user@]agent-id@broker-host')
    if not agent or not host:
        raise ValueError('Agent ID and broker host required')
    return user, agent, host


def terminal_size(fd):
    try:
        packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, b'\0' * 8)
        rows, cols, _, _ = struct.unpack('HHHH', packed)
        return max(rows, 1), max(cols, 1)
    except OSError:
        size = shutil.get_terminal_size()
        return size.lines, size.columns


def interactive(client):
    fd = sys.stdin.fileno()
    if not os.isatty(fd):
        raise RuntimeError('Interactive client requires a TTY on stdin')
    old_settings = termios.tcgetattr(fd)
    old_sigwinch = signal.getsignal(signal.SIGWINCH)
    stop_writer = object()

    def writer():
        while True:
            chunk = client.stdout.get()
            if chunk is stop_writer:
                break
            os.write(sys.stdout.fileno(), chunk)

    worker = threading.Thread(target=writer, daemon=True)
    worker.start()
    try:
        tty.setraw(fd)

        def resized(sig=None, frame=None):
            client.resize(*terminal_size(fd))

        signal.signal(signal.SIGWINCH, resized)
        resized()
        while not client.exited.is_set():
            ready, _, _ = select.select([fd], [], [], 0.1)
            if ready:
                data = os.read(fd, 1024)
                if not data:
                    break
                client.send('I', data)
    finally:
        signal.signal(signal.SIGWINCH, old_sigwinch)
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        client.stdout.put(stop_writer)
        worker.join(timeout=3)


def main():
    if len(sys.argv) != 2:
        raise SystemExit('Usage: mqtt_shell_client.py [mqtt-user@]agent-id@broker-host (or agent-id in serial mode)')
    if os.environ.get('SHELL_TRANSPORT', 'mqtt').lower() == 'serial':
        agent = sys.argv[1]
    else:
        username, agent, host = parse_target(sys.argv[1])
        os.environ['MQTT_HOST'] = host
        if username:
            os.environ['MQTT_USER'] = username
        if not os.environ.get('MQTT_USER'):
            os.environ['MQTT_USER'] = input('MQTT broker username: ').strip()
        if not os.environ.get('MQTT_PASSWORD'):
            os.environ['MQTT_PASSWORD'] = getpass.getpass('MQTT broker password: ')
    secret = os.environ.get('MQTT_AGENT_PASSWORD') or os.environ.get('AGENT_PASSWORD')
    if not secret:
        secret = getpass.getpass(f'Agent password for {agent}: ')
    client = Client(agent, secret)
    try:
        client.start(*terminal_size(sys.stdin.fileno()))
        interactive(client)
    finally:
        client.close()
    if client.exit_code not in (None, 0):
        raise SystemExit(client.exit_code if client.exit_code > 0 else 1)


if __name__ == '__main__':
    main()
