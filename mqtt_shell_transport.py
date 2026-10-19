"""Message-based transport: MQTT now, transparent serial radios as an extension point."""
import os
import threading
import uuid


class MQTTTransport:
    def __init__(self, callback, subscriptions, client_tag):
        self.callback = callback
        self.subscriptions = subscriptions
        self.ready = threading.Event()
        self.subscriptions_ready = threading.Event()
        self.client = None
        self.client_tag = client_tag

    def start(self):
        import paho.mqtt.client as mqtt
        host = os.environ.get('MQTT_HOST', '').strip()
        if not host:
            raise ValueError('MQTT_HOST is required')
        mode = os.environ.get('MQTT_TRANSPORT', 'tcp').strip().lower()
        if mode not in ('tcp', 'websockets'):
            raise ValueError('MQTT_TRANSPORT must be tcp or websockets')
        tls = os.environ.get('MQTT_TLS') == '1'
        default_port = (443 if tls else 80) if mode == 'websockets' else (8883 if tls else 1883)
        port = int(os.environ.get('MQTT_PORT', str(default_port)))
        ws_path = os.environ.get('MQTT_WS_PATH', '/mqtt')
        if mode == 'websockets' and not ws_path.startswith('/'):
            raise ValueError('MQTT_WS_PATH must begin with /')
        user = os.environ.get('MQTT_USER') or os.environ.get('MQTT_USERNAME')
        password = os.environ.get('MQTT_PASSWORD') or os.environ.get('MQTT_PASS')
        kwargs = {'client_id': f'mqtt-shell-{self.client_tag}-{uuid.uuid4().hex[:10]}',
                  'protocol': mqtt.MQTTv5, 'transport': mode}
        if hasattr(mqtt, 'CallbackAPIVersion'):
            kwargs['callback_api_version'] = mqtt.CallbackAPIVersion.VERSION2
        self.client = mqtt.Client(**kwargs)
        if mode == 'websockets':
            self.client.ws_set_options(path=ws_path)
        if user:
            if password is None:
                raise ValueError('MQTT_PASSWORD is required when MQTT_USER is set')
            self.client.username_pw_set(user, password)
        if tls:
            self.client.tls_set()

        def connected(client, userdata, flags, reason_code, properties=None):
            if int(reason_code) != 0:
                return
            if not self.subscriptions:
                self.subscriptions_ready.set()
            for topic in self.subscriptions:
                client.subscribe(topic, qos=1)
            self.ready.set()

        def subscribed(client, userdata, mid, reason_codes, properties=None):
            # Paho invokes on_subscribe once for each SUBSCRIBE packet. Treat any
            # rejected filter as a failed startup rather than publishing into a
            # subscription that the broker never accepted.
            if any(getattr(code, 'is_failure', False) for code in reason_codes):
                return
            if not self.subscriptions:
                self.subscriptions_ready.set()
                return
            self._suback_count += 1
            if self._suback_count >= len(self.subscriptions):
                self.subscriptions_ready.set()

        def received(client, userdata, msg):
            try:
                self.callback(msg.topic, bytes(msg.payload))
            except Exception as exc:
                import logging
                logging.exception('Bad message on %s: %s', msg.topic, exc)

        self.client.on_connect = connected
        self._suback_count = 0
        self.client.on_subscribe = subscribed
        self.client.on_message = received
        self.client.connect(host, port, keepalive=60)
        self.client.loop_start()
        if not self.ready.wait(timeout=10):
            self.close()
            raise TimeoutError('Cannot connect to MQTT broker')
        if not self.subscriptions_ready.wait(timeout=10):
            self.close()
            raise TimeoutError('Cannot subscribe to MQTT topics')

    def publish(self, topic, payload):
        if self.client is None:
            raise RuntimeError('Transport not running')
        info = self.client.publish(topic, payload, qos=1, retain=False)
        if info.rc != 0:
            raise ConnectionError(f'MQTT publish failed: {info.rc}')

    def close(self):
        if self.client:
            self.client.disconnect()
            self.client.loop_stop()
            self.client = None


# Serial mode is intended for transparent UART radio modems. It does not drive
# an SX127x/SX126x LoRa transceiver directly; an MCU or modem must do that.
import binascii
import queue
import struct
import time
import logging

END, ESC, ESC_END, ESC_ESC = 0xc0, 0xdb, 0xdc, 0xdd
CHUNK = 90  # bounded fragments for small LoRa radio packets


def slip(data):
    return bytes([END]) + data.replace(bytes([ESC]), bytes([ESC, ESC_ESC])).replace(
        bytes([END]), bytes([ESC, ESC_END])) + bytes([END])


def unslip(data):
    out = bytearray()
    i = 0
    while i < len(data):
        v = data[i]
        if v == ESC:
            i += 1
            if i >= len(data) or data[i] not in (ESC_END, ESC_ESC):
                raise ValueError('Malformed SLIP escape')
            v = END if data[i] == ESC_END else ESC
        out.append(v)
        i += 1
    return bytes(out)


def wire(body):
    return slip(body + struct.pack('>H', binascii.crc_hqx(body, 0xffff)))


def verify_wire(frame):
    decoded = unslip(frame)
    if len(decoded) < 4:
        raise ValueError('Truncated radio frame')
    body, checksum = decoded[:-2], decoded[-2:]
    if binascii.crc_hqx(body, 0xffff) != struct.unpack('>H', checksum)[0]:
        raise ValueError('CRC mismatch')
    return body


def matches(pattern, topic):
    p, t = pattern.split('/'), topic.split('/')
    return len(p) == len(t) and all(a == b or a == '+' for a, b in zip(p, t))


class SerialTransport:
    """Small-packet, retrying, half-duplex-friendly message adapter.

    Each direction independently uses stop-and-wait ARQ. Only use over a
    transparent serial radio pair (or a compatible firmware bridge).
    """
    def __init__(self, callback, subscriptions, client_tag, serial_port=None):
        self.callback = callback
        self.subscriptions = subscriptions
        self.port = serial_port
        self.owns_port = serial_port is None
        self.outbox = queue.Queue(maxsize=256)
        self.stop = threading.Event()
        self.ack_event = threading.Event()
        self.write_lock = threading.Lock()
        self.send_seq = 0
        self.recv_seq = 0
        self.message_id = 0
        self.fragments = {}
        self.timeout = float(os.environ.get('SHELL_SERIAL_ACK_TIMEOUT', '4'))
        self.retries = int(os.environ.get('SHELL_SERIAL_RETRIES', '12'))
        self.threads = []
        self.failed = False

    def start(self):
        if self.port is None:
            try:
                import serial
            except ImportError as exc:
                raise RuntimeError('Serial mode requires pip install pyserial') from exc
            device = os.environ.get('SHELL_SERIAL_PORT')
            if not device:
                raise ValueError('SHELL_SERIAL_PORT must name the UART device')
            self.port = serial.Serial(device,
                                      baudrate=int(os.environ.get('SHELL_SERIAL_BAUD', '9600')),
                                      timeout=0.1, write_timeout=5)
        self.threads = [threading.Thread(target=self._receive, daemon=True),
                        threading.Thread(target=self._transmit, daemon=True)]
        for thread in self.threads:
            thread.start()

    def publish(self, topic, payload):
        if self.failed:
            raise ConnectionError('Serial link failed to acknowledge packets')
        message = topic.encode('utf-8') + b'\0' + payload
        if len(message) > 20000:
            raise ValueError('Serial message too large')
        self.outbox.put(message, timeout=5)

    def _write(self, body):
        with self.write_lock:
            self.port.write(wire(body))

    def _transmit(self):
        while not self.stop.is_set():
            try:
                message = self.outbox.get(timeout=0.1)
            except queue.Empty:
                continue
            if message is None:
                return
            count = (len(message) + CHUNK - 1) // CHUNK
            self.message_id = (self.message_id + 1) & 0xffff
            for index in range(count):
                frag = message[index * CHUNK:(index+1) * CHUNK]
                body = struct.pack('>BHHBB', 1, self.send_seq, self.message_id,
                                   index, count) + frag
                acknowledged = False
                for attempt in range(self.retries):
                    if self.stop.is_set():
                        return
                    self.ack_event.clear()
                    self._write(body)
                    if self.ack_event.wait(timeout=self.timeout):
                        acknowledged = True
                        break
                if not acknowledged:
                    logging.error('Serial fragment %d not acknowledged; stopping link', self.send_seq)
                    self.failed = True
                    return
                self.send_seq = (self.send_seq + 1) & 0xffff

    def _receive(self):
        in_frame = bytearray()
        while not self.stop.is_set():
            try:
                chunk = self.port.read(1)
            except Exception:
                if not self.stop.is_set():
                    logging.exception('Serial port read failure')
                return
            if not chunk:
                continue
            v = chunk[0]
            if v != END:
                if len(in_frame) < 256:
                    in_frame.append(v)
                else:
                    in_frame.clear()
            elif in_frame:
                try:
                    self._incoming(verify_wire(in_frame))
                except (ValueError, UnicodeError):
                    pass  # damaged RF packet, sender will retry
                except Exception:
                    logging.exception('Serial message processing failure')
                in_frame.clear()

    def _incoming(self, body):
        kind = body[0]
        if kind == 2 and len(body) == 3:
            seq = struct.unpack('>H', body[1:3])[0]
            if seq == self.send_seq:
                self.ack_event.set()
            return
        if kind != 1 or len(body) < 7:
            return
        seq, msgid, index, count = struct.unpack('>HHBB', body[1:7])
        # Retransmissions still need an acknowledgement.
        if seq == (self.recv_seq - 1) & 0xffff:
            self._write(struct.pack('>BH', 2, seq))
            return
        if seq != self.recv_seq or not count or index >= count:
            return
        self._write(struct.pack('>BH', 2, seq))
        self.recv_seq = (self.recv_seq + 1) & 0xffff
        if index == 0:
            self.fragments[msgid] = [count, bytearray()]
            # A single serial link only has one in-flight message in each
            # direction, but old incomplete messages can be discarded.
            for key in list(self.fragments):
                if key != msgid:
                    del self.fragments[key]
        pending = self.fragments.get(msgid)
        if pending is None or pending[0] != count:
            return
        pending[1].extend(body[7:])
        if index == count - 1:
            message = bytes(pending[1])
            del self.fragments[msgid]
            topic_bytes, divider, payload = message.partition(b'\0')
            if divider:
                topic = topic_bytes.decode('utf-8')
                if any(matches(pattern, topic) for pattern in self.subscriptions):
                    # Callbacks may generate reply messages, so never block
                    # the receive/ACK thread waiting on another ACK.
                    self.callback(topic, payload)

    def close(self):
        self.stop.set()
        self.ack_event.set()
        try:
            self.outbox.put_nowait(None)
        except queue.Full:
            pass
        for thread in self.threads:
            thread.join(timeout=2)
        if self.port and self.owns_port:
            self.port.close()


def make_transport(callback, subscriptions, tag):
    mode = os.environ.get('SHELL_TRANSPORT', 'mqtt').lower()
    if mode == 'mqtt':
        return MQTTTransport(callback, subscriptions, tag)
    if mode == 'serial':
        return SerialTransport(callback, subscriptions, tag)
    raise ValueError('SHELL_TRANSPORT must be mqtt or serial')
