#!/usr/bin/env python3
"""Remote PTY agent, using isolated authenticated sessions over MQTT."""
import hmac
import os
import struct
import threading
import time
from dataclasses import dataclass
from mqtt_shell_protocol import VERSION, Cipher, decode, encode, nonce, session_material
from mqtt_shell_pty import Shell
from mqtt_shell_transport import make_transport

MAX_SESSIONS = 8
PENDING_TTL = 30


@dataclass
class Pending:
    client_nonce: str
    server_nonce: str
    rows: int
    cols: int
    created: float


class Agent:
    def __init__(self, agent_id, password, transport=None):
        if not agent_id or '/' in agent_id or '+' in agent_id or '#' in agent_id:
            raise ValueError('Invalid MQTT_ID')
        if not password:
            raise ValueError('MQTT_AGENT_PASSWORD is required')
        self.root = f'mqtt-shell/{agent_id}'
        self.secret = password
        self.pending = {}
        self.sessions = {}
        self.lock = threading.RLock()
        self.transport = transport or make_transport(self.on_message,
            [self.root + '/hello', self.root + '/session/+/auth',
             self.root + '/session/+/in'], 'agent-' + agent_id)

    def control(self, sid, message):
        self.transport.publish(f'{self.root}/session/{sid}/control', encode(message))

    def _send(self, sid, kind, payload=b''):
        with self.lock:  # protects both nonce counters and publication ordering
            item = self.sessions.get(sid)
            if item is not None:
                cipher, shell = item
                frame = cipher.seal(kind, payload)
                self.transport.publish(f'{self.root}/session/{sid}/out', frame)

    def _exit(self, sid, code):
        self._send(sid, 'X', struct.pack('>i', code))
        with self.lock:
            self.sessions.pop(sid, None)

    def _hello(self, body):
        sid = body.get('id', '')
        cn = body.get('nonce', '')
        if not (isinstance(sid, str) and len(sid) == 32 and
                isinstance(cn, str) and len(cn) == 32):
            return
        try:
            bytes.fromhex(sid)
            bytes.fromhex(cn)
            rows, cols = int(body.get('rows', 24)), int(body.get('cols', 80))
            if not (1 <= rows <= 1000 and 1 <= cols <= 1000):
                return
        except (ValueError, TypeError):
            return
        with self.lock:
            self.pending = {k: v for k, v in self.pending.items()
                            if time.monotonic() - v.created < PENDING_TTL}
            if sid in self.sessions or len(self.pending) + len(self.sessions) >= MAX_SESSIONS:
                return
            challenge = nonce()
            self.pending[sid] = Pending(cn, challenge, rows, cols, time.monotonic())
        self.control(sid, {'v': VERSION, 'type': 'challenge', 'nonce': challenge})

    def _auth(self, sid, body):
        proof = body.get('proof', '')
        if not isinstance(proof, str):
            return
        with self.lock:
            pending = self.pending.pop(sid, None)
            if pending is None or time.monotonic() - pending.created >= PENDING_TTL:
                return
            expected, key = session_material(self.secret, sid, pending.client_nonce,
                                             pending.server_nonce)
            if not hmac.compare_digest(expected, proof):
                self.control(sid, {'v': VERSION, 'type': 'fail'})
                return
            cipher = Cipher(key, sid, sender='agent')
            self.sessions[sid] = (cipher, None)
        try:
            shell = Shell(lambda data: self._send(sid, 'O', data),
                          lambda code: self._exit(sid, code),
                          initial_size=(pending.rows, pending.cols))
            with self.lock:
                if sid in self.sessions:
                    self.sessions[sid] = (cipher, shell)
            self._send(sid, 'K')
        except Exception:
            with self.lock:
                self.sessions.pop(sid, None)
            self.control(sid, {'v': VERSION, 'type': 'fail'})
            raise

    def _input(self, sid, payload):
        with self.lock:
            item = self.sessions.get(sid)
            if item is None:
                return
            cipher, shell = item
            packet = cipher.open(payload)
        if packet is None or shell is None:
            return
        kind, data = packet
        if kind == 'I':
            shell.write(data)
        elif kind == 'R' and len(data) == 4:
            rows, cols = struct.unpack('>HH', data)
            shell.resize(rows, cols)
        elif kind == 'C':
            shell.close()

    def on_message(self, topic, payload):
        if topic == self.root + '/hello':
            self._hello(decode(payload))
            return
        prefix = self.root + '/session/'
        if not topic.startswith(prefix):
            return
        parts = topic[len(prefix):].split('/')
        if len(parts) != 2:
            return
        sid, action = parts
        if action == 'auth':
            self._auth(sid, decode(payload))
        elif action == 'in':
            self._input(sid, payload)

    def run(self):
        self.transport.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            with self.lock:
                shells = [shell for _, shell in self.sessions.values() if shell]
            for shell in shells:
                shell.close()
            self.transport.close()


def main():
    agent = Agent(os.environ.get('MQTT_ID'), os.environ.get('MQTT_AGENT_PASSWORD') or
                  os.environ.get('AGENT_PASSWORD'))
    agent.run()


if __name__ == '__main__':
    main()