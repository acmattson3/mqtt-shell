#!/usr/bin/env python3
import os
import sys
import pty
import select
import subprocess
import threading
import signal
import time
import paho.mqtt.client as mqtt
from typing import Optional

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
shell_thread: Optional[threading.Thread] = None
authenticated = False
auth_notice_sent = False
AGENT_PASSWORD_BYTES = AGENT_PASSWORD.encode("utf-8") if AGENT_PASSWORD else None
shell_lock = threading.Lock()

def choose_start_dir():
    """
    Decide where to start the shell and what to use for $HOME.

    Prefer the user's HOME if it is executable/readable; otherwise fall back to
    the current working directory to avoid permission-denied errors on startup.
    """
    home_dir = os.environ.get("HOME") or os.path.expanduser("~")
    if home_dir and os.access(home_dir, os.X_OK | os.R_OK):
        return home_dir, home_dir, False

    fallback = os.getcwd()
    return fallback, fallback, True


def start_shell():
    """Start a fresh PTY-backed shell after successful auth."""
    global master_fd, shell_proc, shell_thread

    with shell_lock:
        # Avoid starting multiple shells if auth races
        if shell_proc and shell_proc.poll() is None:
            return

        # Create PTY
        master_fd, slave_fd = pty.openpty()

        # Spawn default shell
        shell = os.environ.get("SHELL", "/bin/bash")
        start_dir, home_for_env, skip_rc = choose_start_dir()

        env = os.environ.copy()
        env.setdefault("TERM", "xterm-256color")
        env["HOME"] = home_for_env

        shell_cmd = [shell]
        if skip_rc and shell.endswith("bash"):
            # Skip rc files when $HOME is not readable to avoid noisy errors.
            shell_cmd.extend(["--noprofile", "--norc"])

        shell_proc = subprocess.Popen(
            shell_cmd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            preexec_fn=os.setsid,  # give the shell a session and controlling TTY
            env=env,
            cwd=start_dir,
        )
        os.close(slave_fd)

        shell_thread = threading.Thread(target=shell_reader, daemon=True)
        shell_thread.start()

def shell_reader():
    """Read from PTY and publish to MQTT."""
    global master_fd, shell_proc, client, shell_thread, authenticated, auth_notice_sent

    bufsize = 1024
    try:
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
    finally:
        with shell_lock:
            if master_fd is not None:
                try:
                    os.close(master_fd)
                except OSError:
                    pass
            master_fd = None
            shell_proc = None
            shell_thread = None
            authenticated = False
            auth_notice_sent = False

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
            start_shell()
            # Nudge the PTY so the user sees a prompt even if the shell
            # started before they connected.
            if master_fd is not None:
                try:
                    os.write(master_fd, b"\n")
                except OSError:
                    pass
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
    callback_api = getattr(mqtt, "CallbackAPIVersion", None)
    client_kwargs = {
        "client_id": f"mqtt-shell-agent-{SESSION_ID}",
        "protocol": mqtt.MQTTv5,
    }
    if callback_api:
        client_kwargs["callback_api_version"] = callback_api.VERSION2

    client = mqtt.Client(**client_kwargs)

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

    try:
        # Wait for MQTT loop; shells are spawned after auth. This thread just
        # idles to keep the process alive.
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        if shell_proc and shell_proc.poll() is None:
            shell_proc.terminate()
        if shell_thread and shell_thread.is_alive():
            shell_thread.join(timeout=1)
        if client:
            client.loop_stop()
            client.disconnect()

if __name__ == "__main__":
    main()
