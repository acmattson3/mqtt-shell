# mqtt-shell

An SSH-style **remote terminal**, not an SSH server. The agent launches a real Linux PTY. A client connects over either MQTT or a **transparent serial link**, including a serial LoRa modem or MCU radio bridge.

## What changed in protocol v2

- Independent sessions with unpredictable IDs, so two clients no longer share a shell or authentication state.
- Challenge-response authentication: the agent password is **never transmitted directly**.
- ChaCha20-Poly1305 authenticated encryption of stdin, stdout, resizing, and exit messages; frame counters reject duplicates and replays.
- A proper controlling terminal, terminal resizing (`SIGWINCH`), orderly exit status, and stdout drain before session close.
- MQTT QoS 1 for commands and output, or a small serial radio protocol with packet framing, CRC-16, 90-byte fragments, acknowledgments, and retries.
- Functional integration tests that run actual Bash instances and simulate dropped serial packets.

**Breaking change:** v2 clients cannot speak to v1 agents. Upgrade both endpoints together.

## Install

Linux and Python 3.9+ are required for the agent. Client terminal support also targets Unix/Linux/macOS.

```sh
python3 -m pip install -r requirements.txt
```

For MQTT, both endpoints need broker connectivity, and `paho-mqtt`. For serial, both endpoints need `pyserial` and a **bidirectional transparent UART connection**. Connecting two raw SX1262/SX1276 SPI modules directly is *not* supported. Those modules need suitable radio bridge firmware. A transparent serial LoRa modem works without custom host-side radio drivers.

## MQTT mode

Agent:

```sh
export MQTT_HOST=broker.example.com MQTT_PORT=8883 MQTT_TLS=1
export MQTT_USER=your-broker-user MQTT_PASSWORD=your-broker-secret
export MQTT_ID=agent-01
export MQTT_AGENT_PASSWORD="$(openssl rand -hex 32)"  # save this same secret for client
python3 mqtt_shell_agent.py
```

Client (use the *same* `MQTT_AGENT_PASSWORD` as the agent):

```sh
export MQTT_PORT=8883 MQTT_TLS=1
export MQTT_USER=your-broker-user MQTT_PASSWORD=your-broker-secret
export MQTT_AGENT_PASSWORD=the-agent-secret
python3 mqtt_shell_client.py agent-01@broker.example.com
```

`MQTT_HOST` is taken from the client target. Agent ID is a single topic segment. MQTT broker credentials and agent credentials are distinct. **TLS is strongly recommended**, and broker ACLs should restrict who may publish/subscribe. Without TLS, MQTT topics and timing remain visible and a weak agent password can be subjected to offline guesses, although PTY traffic is encrypted. This is a custom protocol, **not** a drop-in replacement for audited SSH.

`MQTT_TLS=1` enables system-CA TLS validation. For TCP, the default ports are 8883 with TLS and 1883 without TLS. Configure hostnames and trusted certificates appropriately.

### MQTT over secure WebSockets (WSS)

Set these variables on **both** agent and client when your broker is behind an HTTPS reverse proxy (such as Cloudflare):

```sh
export MQTT_HOST=mqtt.sssn.us MQTT_TRANSPORT=websockets MQTT_TLS=1 MQTT_PORT=443
export MQTT_WS_PATH=/mqtt  # change to / if that is your broker's exposed WSS path
export MQTT_USER=acmattson3
# supply MQTT_PASSWORD securely at runtime; do not commit broker secrets
```

Paho creates a WebSocket connection to `wss://mqtt.sssn.us:443/mqtt`, performs the normal MQTT broker login **inside** that connection, and uses the same MQTT topics as TCP mode. A WebSocket endpoint must be configured in the broker and correctly routed through the reverse proxy. The WebSocket path differs between deployments; the default `/mqtt` is not a verified path for your server.

Run the broker smoke test to check authentication **and** publish/subscribe permissions before launching an agent:

```sh
python3 tools/probe_broker.py
```

The probe uses an ephemeral, unpredictable topic under `mqtt-shell-probe/` and waits for its own published message to be delivered. It does not expose a shell. A successful probe only establishes MQTT WebSocket connectivity with your credentials, not agent functionality. The test and application never print the broker password. Do not run this against brokers you do not own or have permission to test.

## Serial / transparent LoRa mode (experimental)

Connect a transparent full-duplex UART tunnel between Linux computers using supported serial radios or two MCUs running compatible radio bridge firmware. Each end uses its **own** serial device path.

Agent:

```sh
export SHELL_TRANSPORT=serial SHELL_SERIAL_PORT=/dev/ttyUSB0
export SHELL_SERIAL_BAUD=9600 MQTT_ID=agent-01
export MQTT_AGENT_PASSWORD=your-shared-high-entropy-secret
python3 mqtt_shell_agent.py
```

Client:

```sh
export SHELL_TRANSPORT=serial SHELL_SERIAL_PORT=/dev/ttyUSB0
export SHELL_SERIAL_BAUD=9600 MQTT_AGENT_PASSWORD=your-shared-high-entropy-secret
python3 mqtt_shell_client.py agent-01
```

Serial settings:

- `SHELL_SERIAL_ACK_TIMEOUT` defaults to 4 seconds. Increase for slow LoRa spreading factors.
- `SHELL_SERIAL_RETRIES` defaults to 12 retries per fragment. If exhausted, the link stops; reconnect/restart both endpoints.
- Stop-and-wait retransmission makes output reliable under moderate packet loss, but deliberately limits throughput.
- This adapter assumes transparent **byte-stream** radio behavior and is not optimized for LoRaWAN, duty-cycle limits, or SX-series SPI modules. Confirm applicable radio regulations and module limits before transmitting. Physical LoRa links have not been tested here.

## Running as a systemd service

The included `mqtt-shell-agent.service` uses `/etc/mqtt-shell-agent.env` for secrets and a dedicated `mqttshell` user. Create that user and its home directory, install this repository under `/opt/mqtt-shell`, and secure the environment file (`chmod 600`). Adjust paths and service user for your setup. The shell executes with the service account's permissions, **not as the MQTT client username**. Avoid running the agent as root.

## Tests

```sh
python3 -m pytest -q
```

These tests cover cryptography, authentication rejection, replay protection, session isolation, PTY and resizing, exit output, binary radio framing, dropped frames, and a real shell carried over a simulated radio. They do **not** establish that the implementation is security-audited or that a specific physical LoRa module works.

## Limitations

- The session is terminated if the shell or agent exits. Resume after network loss is not yet implemented.
- QoS 1 helps MQTT recover transient transmission loss, but broker outages can still interrupt an interactive session. The custom serial transport retries each fragment and gives up after a configured limit.
- Protocol v2 supports a PTY, not SSH file transfer, SSH agent forwarding, port forwarding, or native SSH authentication.
- Full-screen interactive editors can be slow over low-bit-rate LoRa. Keep command output small and prefer simple text commands.