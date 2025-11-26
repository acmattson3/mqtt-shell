#!/usr/bin/env python3
import os
import sys
import termios
import tty
import queue
import threading
import getpass
import paho.mqtt.client as mqtt

# ==== STATIC CONFIG (things you probably won't change often) ====
BROKER_HOST = os.environ.get("MQTT_HOST")
BROKER_PORT = int(os.environ.get("MQTT_PORT", "1883"))
USE_TLS     = False
# ================================================================

# These will be set at runtime after parsing the ssh-style target
USERNAME    = None
SESSION_ID  = None
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD") or os.environ.get("MQTT_PASS")
AGENT_PASSWORD = os.environ.get("MQTT_AGENT_PASSWORD") or os.environ.get("AGENT_PASSWORD")

client = None
stdout_queue: "queue.Queue[bytes]" = queue.Queue()
connected_event = threading.Event()
auth_ok_event = threading.Event()

# These are computed after SESSION_ID is known
TOPIC_BASE  = None
TOPIC_STDIN = None
TOPIC_STDOUT = None
TOPIC_STATUS = None
TOPIC_AUTH = None


def build_topics():
    global TOPIC_BASE, TOPIC_STDIN, TOPIC_STDOUT, TOPIC_STATUS, TOPIC_AUTH
    if SESSION_ID is None:
        raise RuntimeError("SESSION_ID not set before building topics")
    TOPIC_BASE   = f"mqtt-shell/{SESSION_ID}"
    TOPIC_STDIN  = TOPIC_BASE + "/stdin"
    TOPIC_STDOUT = TOPIC_BASE + "/stdout"
    TOPIC_STATUS = TOPIC_BASE + "/status"
    TOPIC_AUTH   = TOPIC_BASE + "/auth"


def parse_target_arg(argv):
    """
    Parse ssh-like target: [user@]host

    - user -> MQTT username
    - host -> SESSION_ID (remote agent ID)
    """
    if len(argv) < 2:
        print("Usage: mqtt_shell_client.py [user@]session_id", file=sys.stderr)
        sys.exit(1)

    target = argv[1]

    if "@" in target:
        user, host = target.split("@", 1)
    else:
        # default username = local user
        user = getpass.getuser()
        host = target

    return user, host


def on_connect(mqttc, userdata, flags, reason_code, properties=None):
    print(f"[mqtt-shell] Connected to broker (code {reason_code}).", file=sys.stderr)
    connected_event.set()
    mqttc.subscribe([(TOPIC_STDOUT, 0), (TOPIC_STATUS, 1)])


def on_message(mqttc, userdata, msg):
    if msg.topic == TOPIC_STDOUT:
        stdout_queue.put(msg.payload)
    elif msg.topic == TOPIC_STATUS:
        try:
            text = msg.payload.decode("utf-8", errors="replace")
        except Exception:
            text = repr(msg.payload)
        print(f"[status] {text}", file=sys.stderr)
        if text == "auth-ok":
            auth_ok_event.set()


def setup_mqtt():
    global client
    client = mqtt.Client(client_id="mqtt-shell-client", protocol=mqtt.MQTTv5)

    if USERNAME:
        client.username_pw_set(USERNAME, MQTT_PASSWORD)

    if USE_TLS:
        client.tls_set()

    client.on_connect = on_connect
    client.on_message = on_message

    client.connect(BROKER_HOST, BROKER_PORT, keepalive=60)
    client.loop_start()


def writer_loop():
    """Pull data from stdout_queue and write to local stdout."""
    while True:
        data = stdout_queue.get()
        if data is None:
            break
        os.write(sys.stdout.fileno(), data)
        sys.stdout.flush()


def main():
    global USERNAME, SESSION_ID, MQTT_PASSWORD, AGENT_PASSWORD

    # Parse [user@]session_id from command line
    USERNAME, SESSION_ID = parse_target_arg(sys.argv)

    if not MQTT_PASSWORD:
        MQTT_PASSWORD = getpass.getpass("MQTT broker password: ")
    if not AGENT_PASSWORD:
        AGENT_PASSWORD = getpass.getpass(f"Agent password for {SESSION_ID}: ")

    # Now that SESSION_ID is known, build topics
    build_topics()

    setup_mqtt()

    if not connected_event.wait(timeout=5):
        print("Failed to connect to MQTT broker; giving up.", file=sys.stderr)
        sys.exit(1)

    # Authenticate to the remote agent before sending any input
    client.publish(TOPIC_AUTH, AGENT_PASSWORD.encode("utf-8"), qos=1)

    if not auth_ok_event.wait(timeout=5):
        print("Agent authentication failed or timed out.", file=sys.stderr)
        client.loop_stop()
        client.disconnect()
        sys.exit(1)

    # Put terminal into raw mode so ^C, arrows, etc. go through as bytes
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    tty.setraw(fd)

    writer_thread = threading.Thread(target=writer_loop, daemon=True)
    writer_thread.start()

    try:
        while True:
            data = os.read(fd, 1024)
            if not data:
                break
            client.publish(TOPIC_STDIN, data, qos=0)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        stdout_queue.put(None)
        if client:
            client.loop_stop()
            client.disconnect()


if __name__ == "__main__":
    main()
