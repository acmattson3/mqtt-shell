#!/usr/bin/env python3
"""Minimal authenticated MQTT pub/sub smoke test; no remote shell is launched."""
import os
import secrets
import sys
import threading

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mqtt_shell_transport import MQTTTransport


def probe(timeout=15):
    host = os.environ.get('MQTT_HOST', '').strip()
    if not host:
        raise ValueError('Set MQTT_HOST')
    if not os.environ.get('MQTT_USER'):
        raise ValueError('Set MQTT_USER')
    if not os.environ.get('MQTT_PASSWORD'):
        raise ValueError('Set MQTT_PASSWORD (use a password prompt, not a committed file)')
    token = secrets.token_hex(12)
    topic = 'mqtt-shell-probe/' + token
    received = threading.Event()

    def on_message(name, payload):
        if name == topic and payload == token.encode('ascii'):
            received.set()

    transport = MQTTTransport(on_message, [topic], 'probe')
    try:
        transport.start()
        transport.publish(topic, token.encode('ascii'))
        if not received.wait(timeout):
            raise TimeoutError('No matching MQTT echo; broker subscribe or publish ACL may block this topic')
    finally:
        transport.close()
    print('PASS: TLS/WebSocket MQTT connect, authenticate, subscribe, publish and receive')


if __name__ == '__main__':
    try:
        probe()
    except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
        print(f'FAIL: {exc}', file=sys.stderr)
        raise SystemExit(1)
