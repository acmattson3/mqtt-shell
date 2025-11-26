#!/usr/bin/env python3
import os
import sys
import pty
import select
import subprocess
import threading
import signal
import paho.mqtt.client as mqtt

# ==== CONFIG ====
_raw_host = os.environ.get("MQTT_HOST")
# Guard against missing/blank host to avoid paho's "Invalid host" error.
BROKER_HOST = _raw_host.strip() if _raw_host else None
BROKER_PORT = int(os.environ.get("MQTT_PORT", "1883"))  # or 1883 if you're not using TLS
USERNAME = os.environ.get("MQTT_USER")
PASSWORD = os.environ.get("MQTT_PASSWORD") or os.environ.get("MQTT_PASS")
AGENT_PASSWORD = os.environ.get("MQTT_AGENT_PASSWORD") or os.environ.get("AGENT_PASSWORD")
USE_TLS     = False           # set False if you're not using TLS

SESSION_ID  = os.environ.get("MQTT_ID")   # identifying string for this machine
TOPIC_BASE  = f"mqtt-shell/{SESSION_ID}"
TOPIC_STDIN = TOPIC_BASE + "/stdin"
TOPIC_STDOUT = TOPIC_BASE + "/stdout"
TOPIC_STATUS = TOPIC_BASE + "/status"
TOPIC_AUTH = TOPIC_BASE + "/auth"
# ==============

client = None
master_fd = None
shell_proc = None
authenticated = False
auth_notice_sent = False
AGENT_PASSWORD_BYTES = AGENT_PASSWORD.encode("utf-8") if AGENT_PASSWORD else None

def start_shell():
    global master_fd, shell_proc

    # Create PTY
    master_fd, slave_fd = pty.openpty()

    # Spawn default shell
    shell = os.environ.get("SHELL", "/bin/bash")
    env = os.environ.copy()
    env.setdefault("TERM", "xterm-256color")
    shell_proc = subprocess.Popen(
        [shell],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
        preexec_fn=os.setsid,  # give the shell a session and controlling TTY
        env=env,
    )
    os.close(slave_fd)

def shell_reader():
    """Read from PTY and publish to MQTT."""
    global master_fd, shell_proc, client

    bufsize = 1024
    while True:
        if shell_proc.poll() is not None:
            # Shell has exited
            client.publish(TOPIC_STATUS, "shell-exited".encode("utf-8"), qos=1)
            break

        rlist, _, _ = select.select([master_fd], [], [], 0.1)
        if master_fd in rlist:
            try:
                data = os.read(master_fd, bufsize)
            except OSError:
                break

            if not data:
                break

            client.publish(TOPIC_STDOUT, data, qos=0)

def on_connect(mqttc, userdata, flags, reason_code, properties=None):
    print("Connected to broker with code:", reason_code)
    mqttc.subscribe([(TOPIC_STDIN, 0), (TOPIC_AUTH, 1)])
    mqttc.publish(TOPIC_STATUS, "agent-online".encode("utf-8"), qos=1)

def on_message(mqttc, userdata, msg):
    global master_fd, shell_proc, authenticated, auth_notice_sent
    if msg.topic == TOPIC_AUTH:
        if msg.payload == AGENT_PASSWORD_BYTES:
            authenticated = True
            auth_notice_sent = False
            mqttc.publish(TOPIC_STATUS, "auth-ok".encode("utf-8"), qos=1)
        else:
            mqttc.publish(TOPIC_STATUS, "auth-fail".encode("utf-8"), qos=1)
        return

    if msg.topic == TOPIC_STDIN and master_fd is not None:
        if not authenticated:
            if not auth_notice_sent:
                mqttc.publish(TOPIC_STATUS, "auth-required".encode("utf-8"), qos=1)
                auth_notice_sent = True
            return
        # Write raw bytes into PTY
        try:
            os.write(master_fd, msg.payload)
        except OSError as e:
            print("Error writing to PTY:", e, file=sys.stderr)

def setup_mqtt():
    global client
    client = mqtt.Client(client_id=f"mqtt-shell-agent-{SESSION_ID}", protocol=mqtt.MQTTv5)

    if not BROKER_HOST:
        print("MQTT_HOST is not set or is empty; please configure a broker host.", file=sys.stderr)
        sys.exit(1)

    if not SESSION_ID:
        print("MQTT_ID is not set; please provide a unique identifier for this agent.", file=sys.stderr)
        sys.exit(1)

    if not PASSWORD:
        print("MQTT_PASSWORD is not set; refusing to connect without broker credentials.", file=sys.stderr)
        sys.exit(1)

    if not AGENT_PASSWORD:
        print("MQTT_AGENT_PASSWORD is not set; refusing to start without an agent password.", file=sys.stderr)
        sys.exit(1)

    if USERNAME:
        client.username_pw_set(USERNAME, PASSWORD)

    if USE_TLS:
        client.tls_set()  # uses system CA; customize if needed

    client.on_connect = on_connect
    client.on_message = on_message

    client.connect(BROKER_HOST, BROKER_PORT, keepalive=60)
    client.loop_start()

def main():
    signal.signal(signal.SIGINT, signal.SIG_DFL)  # let systemd handle signals normally

    setup_mqtt()
    start_shell()

    reader_thread = threading.Thread(target=shell_reader, daemon=True)
    reader_thread.start()

    # Just keep the main thread alive while MQTT loop and reader thread run
    try:
        while True:
            if shell_proc.poll() is not None:
                break
            signal.pause()
    except KeyboardInterrupt:
        pass
    finally:
        if shell_proc and shell_proc.poll() is None:
            shell_proc.terminate()
        if client:
            client.loop_stop()
            client.disconnect()

if __name__ == "__main__":
    main()
