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
BROKER_HOST = os.environ.get("MQTT_HOST")      # your Fairbanks broker
BROKER_PORT = 1883           # or 1883 if you're not using TLS
USERNAME = os.environ.get("MQTT_USER")
PASSWORD = os.environ.get("MQTT_PASS")
USE_TLS     = False           # set False if you're not using TLS

SESSION_ID  = os.environ.get("MQTT_ID")   # identifying string for this machine
TOPIC_BASE  = f"mqtt-shell/{SESSION_ID}"
TOPIC_STDIN = TOPIC_BASE + "/stdin"
TOPIC_STDOUT = TOPIC_BASE + "/stdout"
TOPIC_STATUS = TOPIC_BASE + "/status"
# ==============

client = None
master_fd = None
shell_proc = None

def start_shell():
    global master_fd, shell_proc

    # Create PTY
    master_fd, slave_fd = pty.openpty()

    # Spawn default shell
    shell = os.environ.get("SHELL", "/bin/bash")
    shell_proc = subprocess.Popen(
        [shell],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True
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
    mqttc.subscribe(TOPIC_STDIN, qos=0)
    mqttc.publish(TOPIC_STATUS, "agent-online".encode("utf-8"), qos=1)

def on_message(mqttc, userdata, msg):
    global master_fd, shell_proc
    if msg.topic == TOPIC_STDIN and master_fd is not None:
        # Write raw bytes into PTY
        try:
            os.write(master_fd, msg.payload)
        except OSError as e:
            print("Error writing to PTY:", e, file=sys.stderr)

def setup_mqtt():
    global client
    client = mqtt.Client(client_id=f"mqtt-shell-agent-{SESSION_ID}", protocol=mqtt.MQTTv5)

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
