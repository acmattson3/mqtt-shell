import os
import queue
import re
import threading
import time
import unittest

from mqtt_shell_protocol import Cipher, session_material, nonce
from mqtt_shell_client import Client
from mqtt_shell_agent import Agent


class Hub:
    def __init__(self):
        self.peers = []
        self.frames = []
        self.lock = threading.Lock()

    def add(self, callback, subscriptions):
        t = MemoryTransport(self, callback, subscriptions)
        self.peers.append(t)
        return t

    def publish(self, topic, payload):
        with self.lock:
            self.frames.append((topic, payload))
            peers = list(self.peers)
        for peer in peers:
            if any(match(pattern, topic) for pattern in peer.subscriptions):
                peer.callback(topic, payload)


class MemoryTransport:
    def __init__(self, hub, callback, subscriptions):
        self.hub = hub
        self.callback = callback
        self.subscriptions = subscriptions

    def start(self):
        return

    def publish(self, topic, payload):
        self.hub.publish(topic, payload)

    def close(self):
        pass


def match(pattern, topic):
    p, t = pattern.split('/'), topic.split('/')
    return len(p) == len(t) and all(a == '+' or a == b for a, b in zip(p, t))


def collect(client, pattern, timeout=4):
    deadline = time.monotonic() + timeout
    raw = bytearray()
    while time.monotonic() < deadline:
        try:
            raw.extend(client.stdout.get(timeout=0.1))
        except queue.Empty:
            pass
        if pattern in raw:
            return bytes(raw)
    raise AssertionError(f'Output not found: {pattern!r}, got {bytes(raw)!r}')


class ProtocolTests(unittest.TestCase):
    def test_cipher_authentication_replay_and_direction(self):
        proof1, k1 = session_material('good-password', 'a' * 32, nonce(), nonce())
        self.assertEqual(len(proof1), 64)
        client = Cipher(k1, 'a' * 32, sender='client')
        agent = Cipher(k1, 'a' * 32, sender='agent')
        frame = client.seal('I', b'hello world')
        self.assertNotIn(b'hello', frame)
        self.assertEqual(agent.open(frame), ('I', b'hello world'))
        self.assertIsNone(agent.open(frame))
        wrong = Cipher(b'\0'*32, 'a'*32, sender='agent')
        with self.assertRaises(Exception):
            wrong.open(frame)
        altered = bytearray(client.seal('I', b'two'))
        altered[-1] ^= 1
        with self.assertRaises(Exception):
            agent.open(bytes(altered))

    def test_out_of_order_rejected(self):
        key = b'a' * 32
        c = Cipher(key, 's' * 32, 'client')
        a = Cipher(key, 's' * 32, 'agent')
        c.seal('I', b'lost')
        with self.assertRaisesRegex(ValueError, 'Out-of-order'):
            a.open(c.seal('I', b'next'))


class ShellTests(unittest.TestCase):
    def setUp(self):
        self.hub = Hub()
        self.agent = Agent('robot', 'strong-secret', None)
        self.agent.transport = self.hub.add(self.agent.on_message,
            [self.agent.root + '/hello', self.agent.root + '/session/+/auth', self.agent.root + '/session/+/in'])
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close()
        for _, shell in list(self.agent.sessions.values()):
            if shell:
                shell.close()
                shell.reader.join(timeout=2)

    def client(self, password='strong-secret'):
        c = Client('robot', password)
        c.transport = self.hub.add(c.on_message,
            [f'{c.root}/session/{c.sid}/control', f'{c.root}/session/{c.sid}/out'])
        self.clients.append(c)
        return c

    def test_pty_job_control_and_resize(self):
        c = self.client()
        c.start(rows=42, cols=99)
        collect(c, b'# ', timeout=4)
        c.send('I', b'tty; stty size; printf "DONE\\n"\n')
        data = collect(c, b'\r\nDONE\r\n')
        self.assertRegex(data, rb'/dev/pts/\d+')
        self.assertIn(b'42 99', data)
        c.resize(30, 100)
        c.send('I', b'stty size\n')
        self.assertIn(b'30 100', collect(c, b'30 100'))

    def test_two_sessions_isolated(self):
        a, b = self.client(), self.client()
        a.start()
        b.start()
        collect(a, b'# ', timeout=4)
        collect(b, b'# ', timeout=4)
        a.send('I', b'export TEST_ISOLATED=unique123; echo $TEST_ISOLATED\n')
        self.assertIn(b'unique123', collect(a, b'\runique123\r\n'))
        b.send('I', b'printf "[%s]\\n" "$TEST_ISOLATED"\n')
        data = collect(b, b'[]')
        self.assertNotIn(b'unique123', data)

    def test_wrong_password_rejected(self):
        c = self.client('wrong')
        with self.assertRaises(PermissionError):
            c.start()
        self.assertFalse(self.agent.sessions)

    def test_final_output_before_exit(self):
        c = self.client()
        c.start()
        collect(c, b'# ', timeout=4)
        c.send('I', b'printf "LAST-OUTPUT\\n"; exit 17\n')
        self.assertTrue(c.exited.wait(5), 'Shell never exited')
        text = b''.join([entry for entry in list(c.stdout.queue)])
        self.assertIn(b'LAST-OUTPUT', text)
        self.assertEqual(c.exit_code, 17)

    def test_input_replay_is_ignored(self):
        c = self.client()
        c.start()
        collect(c, b'# ', timeout=4)
        c.send('I', b'printf "ONE-TIME\\n"\n')
        topic, payload = [x for x in self.hub.frames if x[0].endswith('/in')][-1]
        self.hub.publish(topic, payload)
        data = collect(c, b'\rONE-TIME\r\n')
        time.sleep(.2)
        data += b''.join(list(c.stdout.queue))
        self.assertEqual(data.count(b'ONE-TIME'), 2)  # command echo and printf output


if __name__ == '__main__':
    unittest.main()
