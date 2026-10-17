import queue
import threading
import time
import unittest

from mqtt_shell_agent import Agent
from mqtt_shell_client import Client
from mqtt_shell_transport import SerialTransport, slip, unslip, verify_wire, wire


class Port:
    def __init__(self):
        self.inbox = queue.Queue()
        self.peer = None
        self.drop_once = False
        self.drop_count = 0

    def read(self, size):
        try:
            return self.inbox.get(timeout=.05)
        except queue.Empty:
            return b''

    def write(self, data):
        # Simulate dropping a complete LoRa burst without delivering bytes.
        if self.drop_once and self.drop_count == 0:
            self.drop_count += 1
            return len(data)
        for byte in data:
            self.peer.inbox.put(bytes([byte]))
        return len(data)

    def close(self):
        pass


def ports():
    a, b = Port(), Port()
    a.peer, b.peer = b, a
    return a, b


class RadioTests(unittest.TestCase):
    def test_slip_binary_crc(self):
        original = b'one\0' + bytes([0xc0, 0xdb, 0x01, 0x00])
        self.assertEqual(unslip(slip(original)[1:-1]), original)
        self.assertEqual(verify_wire(wire(original)[1:-1]), original)
        damaged = bytearray(wire(original)[1:-1])
        damaged[2] ^= 1
        with self.assertRaises(ValueError):
            verify_wire(damaged)

    def test_serial_retries_and_fragmentation(self):
        a, b = ports()
        a.drop_once = True
        received = []
        t1 = SerialTransport(lambda *_: None, ['x'], 'left', a)
        t2 = SerialTransport(lambda topic, payload: received.append((topic, payload)),
                             ['x'], 'right', b)
        t1.timeout = t2.timeout = .1
        t1.start()
        t2.start()
        try:
            t1.publish('x', b'A'*1000 + bytes([0xc0, 0xdb]))
            until = time.monotonic()+8
            while not received and time.monotonic() < until:
                time.sleep(.05)
            self.assertEqual(received, [('x', b'A'*1000 + bytes([0xc0, 0xdb]))])
            self.assertEqual(a.drop_count, 1)
        finally:
            t1.close()
            t2.close()

    def test_encrypted_shell_over_simulated_radio(self):
        a, b = ports()
        agent = Agent('remote', 'radio-password')
        client = Client('remote', 'radio-password')
        agent.transport = SerialTransport(agent.on_message,
            [agent.root+'/hello', agent.root+'/session/+/auth', agent.root+'/session/+/in'], 'agent', a)
        client.transport = SerialTransport(client.on_message,
            [f'{client.root}/session/{client.sid}/control',
             f'{client.root}/session/{client.sid}/out'], 'client', b)
        agent.transport.timeout = .1
        client.transport.timeout = .1
        agent.transport.start()
        try:
            client.start()
            client.send('I', b'printf "RADIO-SHELL\\n"\n')
            output = b''
            end = time.monotonic()+8
            while b'RADIO-SHELL\r\n' not in output and time.monotonic() < end:
                try:
                    output += client.stdout.get(timeout=.1)
                except queue.Empty:
                    pass
            self.assertIn(b'RADIO-SHELL\r\n', output)
        finally:
            client.close()
            for _, shell in list(agent.sessions.values()):
                if shell:
                    shell.close()
            agent.transport.close()


if __name__ == '__main__':
    unittest.main()
