"""Versioned, authenticated MQTT-shell protocol; no shell bytes go out in cleartext."""
import base64
import hashlib
import hmac
import json
import secrets
import struct
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

VERSION = 2
MAX_DATA = 16384


def encode(obj):
    return json.dumps(obj, separators=(',', ':')).encode('utf-8')


def decode(payload):
    if len(payload) > 4096:
        raise ValueError('Control message too large')
    obj = json.loads(payload)
    if not isinstance(obj, dict) or obj.get('v') != VERSION:
        raise ValueError('Incompatible protocol version')
    return obj


def nonce():
    return secrets.token_hex(16)


def session_material(secret, session_id, client_nonce, server_nonce):
    """Derive separate authentication proof and traffic key."""
    transcript = b'\x00'.join([str(VERSION).encode(), session_id.encode(),
                              bytes.fromhex(client_nonce), bytes.fromhex(server_nonce)])
    key = secret.encode('utf-8')
    proof = hmac.new(key, b'mqtt-shell auth\x00' + transcript, hashlib.sha256).hexdigest()
    traffic_key = hmac.new(key, b'mqtt-shell traffic\x00' + transcript, hashlib.sha256).digest()
    return proof, traffic_key


class Cipher:
    """Directional authenticated encryption, with ordered, replay-resistant frames."""
    def __init__(self, key, session_id, sender):
        self.aead = ChaCha20Poly1305(key)
        self.aad = (str(VERSION) + ':' + session_id).encode()
        self.send_prefix = b'CLNT' if sender == 'client' else b'AGNT'
        self.recv_prefix = b'AGNT' if sender == 'client' else b'CLNT'
        self.send_seq = 0
        self.recv_seq = -1

    def seal(self, kind, data=b''):
        if not isinstance(data, bytes) or len(data) > MAX_DATA:
            raise ValueError('Invalid frame size')
        if self.send_seq == 2**64 - 1:
            raise ValueError('Frame counter exhausted')
        counter = self.send_seq
        self.send_seq += 1
        n = self.send_prefix + struct.pack('>Q', counter)
        return struct.pack('>Q', counter) + self.aead.encrypt(n, kind.encode('ascii') + data, self.aad)

    def open(self, frame):
        if len(frame) < 25 or len(frame) > MAX_DATA + 25:
            raise ValueError('Invalid encrypted frame size')
        counter = struct.unpack('>Q', frame[:8])[0]
        if counter <= self.recv_seq:
            return None  # duplicated/replayed QoS1 delivery
        if counter != self.recv_seq + 1:
            raise ValueError('Out-of-order encrypted frame')
        n = self.recv_prefix + frame[:8]
        plain = self.aead.decrypt(n, frame[8:], self.aad)
        self.recv_seq = counter
        return plain[:1].decode('ascii'), plain[1:]