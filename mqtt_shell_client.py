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
BROKER_HOST = "sssn.us"
BROKER_PORT = 1883
USE_TLS     = True
# ================================================================

# These will be set at runtime after parsing the ssh-style target
USERNAME    = None
PASSWORD    = None
SESSION_ID  = None

client = None
stdout_queue: "queue.Queue[bytes]" = queue.Queue()

# These are computed after SESSION_ID is known
TOPIC_BASE  = None
TOPIC_STDIN = None
TOPIC_STDOUT = None
TOPIC_STATUS = None


def build_topics():
    global TOPIC_BASE, TOPIC_STDIN, TOPIC_STDOUT, TOPIC_STATUS
    if SESSION_ID is None:
        raise RuntimeError("SESSION_ID not set before building topics")
    TOPIC_BASE   = f"mqtt-shell/{SESSION_ID}"
    TOPIC_STDIN  = TOPIC_BASE + "/stdin"
    TOPIC_STDOUT = TOPIC_BASE + "/stdout"
    TOPIC_STATUS = TOPIC_BASE + "/status"


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


def setup_mqtt():
    global client
    client = mqtt.Client(client_id="mqtt-shell-client", protocol=mqtt.MQTTv5)

    if USERNAME:
        client.username_pw_set(USERNAME, PASSWORD)

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
    global USERNAME, PASSWORD, SESSION_ID

    # Parse [user@]session_id from command line
    USERNAME, SESSION_ID = parse_target_arg(sys.argv)
    PASSWORD = getpass.getpass(f"{USERNAME}@{SESSION_ID} password: ")

    # Now that SESSION_ID is known, build topics
    build_topics()

    setup_mqtt()

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
