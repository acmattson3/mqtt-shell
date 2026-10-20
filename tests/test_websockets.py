"""MQTT WebSocket configuration tests without a network or real credentials."""
import sys
import types

import pytest

from mqtt_shell_transport import MQTTTransport


class FakeMQTTClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.path = None
        self.tls_enabled = False
        self.host = None
        self.port = None
        self.username = None
        self.password = None
        self.subscriptions = []

    def ws_set_options(self, *, path):
        self.path = path

    def username_pw_set(self, user, password):
        self.username, self.password = user, password

    def tls_set(self):
        self.tls_enabled = True

    def connect(self, host, port, keepalive):
        self.host, self.port = host, port

    def loop_start(self):
        self.on_connect(self, None, None, 0)

    def subscribe(self, topic, qos):
        self.subscriptions.append((topic, qos))
        self.on_subscribe(self, None, len(self.subscriptions), [0])

    def disconnect(self):
        pass

    def loop_stop(self):
        pass


@pytest.fixture
def fake_mqtt(monkeypatch):
    instances = []

    def factory(**kwargs):
        instance = FakeMQTTClient(**kwargs)
        instances.append(instance)
        return instance

    package = types.ModuleType('paho')
    mqtt_package = types.ModuleType('paho.mqtt')
    client = types.ModuleType('paho.mqtt.client')
    client.Client = factory
    client.MQTTv5 = 5
    client.CallbackAPIVersion = types.SimpleNamespace(VERSION2=2)
    package.mqtt = mqtt_package
    mqtt_package.client = client
    monkeypatch.setitem(sys.modules, 'paho', package)
    monkeypatch.setitem(sys.modules, 'paho.mqtt', mqtt_package)
    monkeypatch.setitem(sys.modules, 'paho.mqtt.client', client)
    return instances


def test_wss_setup(monkeypatch, fake_mqtt):
    monkeypatch.setenv('MQTT_HOST', 'mqtt.sssn.us')
    monkeypatch.setenv('MQTT_TRANSPORT', 'websockets')
    monkeypatch.setenv('MQTT_TLS', '1')
    monkeypatch.delenv('MQTT_PORT', raising=False)
    monkeypatch.setenv('MQTT_WS_PATH', '/mqtt')
    monkeypatch.setenv('MQTT_USER', 'testuser')
    monkeypatch.setenv('MQTT_PASSWORD', 'testpassword')
    connection = MQTTTransport(lambda topic, body: None, ['test/topic'], 'test')
    try:
        connection.start()
        client = fake_mqtt[0]
        assert client.kwargs['transport'] == 'websockets'
        assert client.host == 'mqtt.sssn.us'
        assert client.port == 443
        assert client.tls_enabled
        assert client.path == '/mqtt'
        assert client.username == 'testuser'
        assert client.password == 'testpassword'
        assert client.subscriptions == [('test/topic', 1)]
    finally:
        connection.close()


def test_custom_path_and_port(monkeypatch, fake_mqtt):
    monkeypatch.setenv('MQTT_HOST', 'test.local')
    monkeypatch.setenv('MQTT_TRANSPORT', 'websockets')
    monkeypatch.setenv('MQTT_TLS', '1')
    monkeypatch.setenv('MQTT_PORT', '8443')
    monkeypatch.setenv('MQTT_WS_PATH', '/')
    connection = MQTTTransport(lambda topic, body: None, [], 'test')
    try:
        connection.start()
        assert fake_mqtt[0].path == '/'
        assert fake_mqtt[0].port == 8443
    finally:
        connection.close()


def test_paho_v5_reason_code_object(monkeypatch, fake_mqtt):
    monkeypatch.setenv('MQTT_HOST', 'broker.local')
    monkeypatch.setenv('MQTT_TRANSPORT', 'websockets')

    def loop_start(client):
        client.on_connect(client, None, None, types.SimpleNamespace(value=0))

    monkeypatch.setattr(FakeMQTTClient, 'loop_start', loop_start)
    connection = MQTTTransport(lambda topic, body: None, [], 'test')
    try:
        connection.start()
    finally:
        connection.close()


def test_plain_mqtt_defaults(monkeypatch, fake_mqtt):
    monkeypatch.setenv('MQTT_HOST', 'broker.local')
    monkeypatch.delenv('MQTT_TRANSPORT', raising=False)
    monkeypatch.delenv('MQTT_PORT', raising=False)
    monkeypatch.delenv('MQTT_TLS', raising=False)
    connection = MQTTTransport(lambda topic, body: None, [], 'test')
    try:
        connection.start()
        assert fake_mqtt[0].kwargs['transport'] == 'tcp'
        assert fake_mqtt[0].port == 1883
        assert not fake_mqtt[0].tls_enabled
    finally:
        connection.close()


def test_invalid_websocket_path(monkeypatch, fake_mqtt):
    monkeypatch.setenv('MQTT_HOST', 'broker.local')
    monkeypatch.setenv('MQTT_TRANSPORT', 'websockets')
    monkeypatch.setenv('MQTT_WS_PATH', 'mqtt')
    with pytest.raises(ValueError, match='begin with'):
        MQTTTransport(lambda topic, body: None, [], 'test').start()
    assert fake_mqtt == []
