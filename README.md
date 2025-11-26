# mqtt-shell

A purely MQTT-based SSH-style shell that uses MQTT for transport and a per-agent password for authorization.

## What this does
- An agent runs on the remote machine and exposes a pseudo-TTY over MQTT topics.
- A client connects to the same broker, authenticates to the agent with an agent-specific password, and streams stdin/stdout over MQTT.
- MQTT broker credentials (`MQTT_USER`/`MQTT_PASSWORD`) are shared across all agents, while each agent has its own `MQTT_AGENT_PASSWORD`.

## Prerequisites
- Python 3.9+ on both agent and client hosts.
- `paho-mqtt` installed: `pip install paho-mqtt`
- An MQTT broker reachable by both sides.

## Configuration
### Environment variables (agent)
- `MQTT_HOST` (required): Broker hostname/IP.
- `MQTT_PORT` (optional, default `1883`): Broker port.
- `MQTT_USER` (required): MQTT username.
- `MQTT_PASSWORD` (required): MQTT password (shared across all agents/clients).
- `MQTT_ID` (required): Unique agent/session identifier; used to build topics.
- `MQTT_AGENT_PASSWORD` (required): Per-agent secret used to authorize clients.

### Environment variables (client)
- `MQTT_HOST` / `MQTT_PORT`: Defaults for broker host/port; can be overridden in the CLI target.
- `MQTT_USER`: MQTT username. If omitted, you'll be prompted.
- `MQTT_PASSWORD`: MQTT broker password. If omitted, you'll be prompted.
- `MQTT_AGENT_PASSWORD`: Agent password. If omitted, you will be prompted per connection.

## Running the agent
### Quick run
```bash
export MQTT_HOST=broker.example.com
export MQTT_PORT=1883
export MQTT_USER=your-broker-user
export MQTT_PASSWORD=your-broker-password
export MQTT_AGENT_PASSWORD=unique-agent-secret
export MQTT_ID=agent-01

python3 mqtt_shell_agent.py
```

### systemd service
1) Copy `mqtt-shell-agent.service` to `/etc/systemd/system/mqtt-shell-agent.service`.
2) Update the `Environment=` lines to match your broker, agent ID, and passwords.
3) Reload and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mqtt-shell-agent
sudo systemctl status mqtt-shell-agent
```
The bundled service file is tuned for an SSH-like experience (no `NoNewPrivileges`, `ProtectHome=false`, `ProtectSystem=off`) so `sudo`, login shell config, and colors work as expected. If you want a more locked-down service, re-enable those hardening directives after testing.

## Running the client
```bash
export MQTT_HOST=broker.example.com
export MQTT_PORT=1883
export MQTT_PASSWORD=your-broker-password   # shared MQTT secret
# export MQTT_USER=your-broker-user         # if not set, you'll be prompted
# export MQTT_AGENT_PASSWORD=unique-agent-secret  # if not set, you'll be prompted

python3 mqtt_shell_client.py agent-01@broker.example.com
# or with an explicit MQTT username (optional prefix):
# python3 mqtt_shell_client.py mqttuser@agent-01@broker.example.com
```

The client will:
1) Connect to the MQTT broker at `<broker-host>` using `MQTT_USER` (from env or the optional prefix) and `MQTT_PASSWORD`.
2) Send the `MQTT_AGENT_PASSWORD` to the agent over a dedicated auth topic for `<agent-id>`.
3) Enter a raw TTY session once it receives `auth-ok`. Incorrect or missing passwords result in `auth-fail`.

## Topics
- `mqtt-shell/<MQTT_ID>/stdin` – client -> agent (shell input)
- `mqtt-shell/<MQTT_ID>/stdout` – agent -> client (shell output)
- `mqtt-shell/<MQTT_ID>/status` – status messages (`agent-online`, `auth-ok`, `auth-fail`, `shell-exited`, etc.)
- `mqtt-shell/<MQTT_ID>/auth` – client -> agent (agent password)

## Notes & tips
- Each agent must have a unique `MQTT_ID`; clients connect with `[user@]<MQTT_ID>`.
- The MQTT broker password is shared; the agent password is unique per agent for an extra layer of protection.
- If you see `auth-required` in status, ensure the agent password matches between client and agent.
- TLS is supported by setting `USE_TLS=True` in code; customize certificates as needed.
- The agent launches your shell in a real PTY session (`TERM` defaults to `xterm-256color`) so prompts, colors, and job control behave like SSH.
- When the remote shell exits, the client now exits cleanly after printing the `shell-exited` status message.
