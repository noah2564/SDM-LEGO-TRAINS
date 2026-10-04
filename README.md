# Depot — LEGO train control

Control a fleet of LEGO trains from a browser. Each train carries a Raspberry
Pi Pico W that talks MQTT over Wi-Fi to a Mosquitto broker; a backend turns
that into a REST API and a live WebSocket feed for the web UI.

```
User → Web UI → Backend → Mosquitto → Wi-Fi → Pico W → LEGO hub → train
                   ↑                              │
                   └──────── telemetry, status ───┘
```

One Pico W drives exactly one train. Adding the tenth train is the same work
as adding the second: register it in the UI, flash a Pico with its train id.

---

## Contents

- [Quick start](#quick-start)
- [Architecture](#architecture)
- [MQTT topics](#mqtt-topics)
- [Message formats](#message-formats)
- [REST API](#rest-api)
- [Adding a train](#adding-a-train)
- [Configuring a Pico W](#configuring-a-pico-w)
- [The LEGO hub (City Hub, LWP3)](#the-lego-hub-city-hub-lwp3)
- [Safety model](#safety-model)
- [Running the simulator](#running-the-simulator)
- [Debugging with MQTT](#debugging-with-mqtt)
- [Tests](#tests)
- [Operations](#operations-logs-restarts-backups)
- [Security](#security)
- [Design decisions](#design-decisions)

---

## Quick start

Requirements: Docker Engine 24+ with the Compose plugin. Nothing else — no
Mosquitto, Python or Node on the host.

```bash
git clone <this-repo>

cp .env.example .env          # edit if ports 8080/8000/1883 are taken
docker compose up -d --build                  # broker + backend + web UI
docker compose --profile sim up -d --build   # three simulated trains
```

Open **http://localhost:8080**. You should see the dashboard with an empty
fleet and a green “Live” lamp in the top right.

Start three simulated trains and watch them appear:

```bash
docker compose --profile sim up -d
```

The UI shows a banner listing unregistered devices it has heard on MQTT
(`train-001`, `train-002`, `train-003`). Click **Register** on one, confirm the
name, and it turns online. Drag the throttle and watch the simulator log:

```bash
docker compose logs -f simulator
```

To stop everything: `docker compose down` (add `-v` to delete the data
volumes too).

---

## Architecture

| Service | Image / build | Port | Purpose |
|---|---|---|---|
| `mosquitto` | `eclipse-mosquitto:2.0` | 1883, 9001 | MQTT broker; the only thing devices talk to |
| `backend` | `./backend` (Python 3.12, FastAPI) | 8000 | Validation, persistence, MQTT bridge, REST + WebSocket |
| `frontend` | `./frontend` (nginx + static JS) | 8080 | Web UI; also reverse-proxies `/api` and `/ws` |
| `simulator` | `./simulator` (profile `sim`) | — | Fake Pico Ws for development |

```
project/
├── docker-compose.yml
├── .env.example
├── mosquitto/config/mosquitto.conf, aclfile.example
├── backend/app/           protocol, models, repository, train_service, api
├── backend/tests/         unit + integration tests
├── frontend/public/       index.html, app.js, styles.css
├── firmware/pico_w/       main.py, config.py, mqtt_client.py,
│                          train_controller.py, lego_hub.py
└── simulator/simulator.py
```

**Data flow.** The Pico publishes status, telemetry and events. The backend
subscribes to `trains/+/…`, sanitises every payload, writes the result to the
database and pushes it to every open browser over a WebSocket. Commands travel
the other way: the browser POSTs to the REST API, the backend validates and
publishes to that train's command topic. **The browser never speaks MQTT and
never receives broker credentials.**

**Persistence.** Live state (speed, direction, status, last seen, last
telemetry) is written to the database, not held in memory, so a backend
restart loses nothing. Retained MQTT status messages mean the backend learns
each train's state the moment it reconnects.

---

## MQTT topics

| Topic | Direction | QoS | Retained | Payload |
|---|---|---|---|---|
| `trains/{train_id}/status` | device → backend | 1 | yes | online/offline, also the LWT |
| `trains/{train_id}/telemetry` | device → backend | 0 | no | speed, direction, battery, … |
| `trains/{train_id}/event` | device → backend | 1 | no | boot, faults, safety trips |
| `trains/{train_id}/command` | backend → device | 1 | no | one command |
| `system/command` | backend → all | 1 | no | fleet-wide emergency stop |
| `system/heartbeat` | backend → all | 0 | no | backend liveness beacon |

The backend subscribes to `trains/+/status`, `trains/+/telemetry` and
`trains/+/event`. Each Pico subscribes only to its own
`trains/{its id}/command`, plus the two `system/` topics.

**Why the two extra `system/` topics.** A global emergency stop published once
on `system/command` reaches the whole fleet in one broker round trip instead
of N sequential publishes — the difference matters when the reason you pressed
the button is that something is about to hit the floor. (The backend *also*
publishes to each train individually, so firmware that only subscribes to its
own topic still stops, and so the database reflects it.) `system/heartbeat`
lets a device notice that the **backend** has died, not merely that the broker
socket is up; without it, a crashed backend would leave trains running.

Status is retained so a restarted backend immediately knows who is out there.
Telemetry is not retained and uses QoS 0: it is superseded every two seconds,
and a stale queued reading is worse than no reading.

---

## Message formats

Every message is a JSON object carrying `"v"` (protocol version). **Consumers
ignore unknown keys**, which is what makes the format extensible: adding a
track sensor, a light or a sound field breaks nothing. Unrecognised telemetry
fields are preserved by the backend under `extra` and shown in the detail view,
so a new sensor is visible in the UI before any backend change.

### Status (device → backend, retained)

```json
{"v": 1, "train_id": "train-001", "device_id": "pico-001",
 "status": "online", "firmware": "1.0.0"}
```

`status` is one of `online`, `offline`, `connecting`, `unknown`. The Last Will
registered at connect time is the same shape with `"status": "offline"` and
`"reason": "lwt"`.

### Telemetry (device → backend)

```json
{"v": 1, "train_id": "train-001", "timestamp": "2026-09-17T10:04:11Z",
 "speed": 45, "direction": "forward", "battery": 7.4,
 "connected": true, "emergency_stop": false,
 "hub_connected": true, "uptime_s": 812.4, "rssi": -58}
```

Speed is a magnitude `0–100` with a separate `direction` rather than a signed
value: it maps directly onto both the throttle slider and motor PWM, and makes
“stopped” unambiguous.

### Events (device → backend)

```json
{"v": 1, "train_id": "train-001", "type": "safety_timeout",
 "message": "Stopped: no contact with the control server", "severity": "error"}
```

`severity` is `info`, `warning` or `error`.

### Commands (backend → device)

The backend adds `v`, `command_id`, `train_id` and `ts` to every command:

```json
{"v": 1, "command_id": "9f2c1a7b4e01", "train_id": "train-001",
 "ts": "2026-09-17T10:04:12.418+00:00", "command": "set_speed", "speed": 50}
```

| Command | Fields | Notes |
|---|---|---|
| `set_speed` | `speed` 0–100, optional `direction` | Rejected above the train's speed limit |
| `set_direction` | `direction` `forward`\|`backward` | Firmware stops before reversing |
| `stop` | — | Normal stop; the train can drive again at once |
| `emergency_stop` | optional `reason` | Cuts power and **latches** |
| `clear_emergency` | — | Releases the latch |
| `set_config` | `safety_timeout_s`, `telemetry_interval_s` | Retune a device without reflashing |
| `ping` | — | Liveness probe |

Anything else is rejected with HTTP 422 and never reaches the broker.

---

## REST API

Base URL `http://localhost:8000/api` (or `/api` through the web UI).
Interactive docs at http://localhost:8000/docs.

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/api/health` | Liveness, database and broker state |
| `GET` | `/api/stats` | Fleet counters |
| `GET` | `/api/discovered` | Train ids heard on MQTT but not registered |
| `GET` | `/api/trains` | All trains with live state |
| `GET` | `/api/trains/{id}` | One train |
| `POST` | `/api/trains` | Register a train |
| `PUT` | `/api/trains/{id}` | Edit name, device id, description, speed limit |
| `DELETE` | `/api/trains/{id}` | Remove a train and its history |
| `GET` | `/api/trains/{id}/events` | Recent events (`?limit=`, max 200) |
| `POST` | `/api/trains/{id}/command` | Any validated command |
| `POST` | `/api/trains/{id}/stop` | Normal stop |
| `POST` | `/api/trains/{id}/emergency-stop` | Emergency stop, optional `{"reason": "..."}` |
| `POST` | `/api/trains/{id}/clear-emergency` | Release the latch |
| `POST` | `/api/emergency-stop` | Stop the entire fleet |

Status codes: `404` unknown train, `409` conflict (duplicate id/device, broker
unreachable, or driving a latched train), `422` invalid command or field.

WebSocket at `ws://localhost:8080/ws`. On connect it sends a
`snapshot`; afterwards it pushes `train.created`, `train.updated`,
`train.deleted`, `event`, `discovery` and `fleet.emergency_stop`.

```bash
curl localhost:8000/api/health
curl localhost:8000/api/trains
curl -X POST localhost:8000/api/trains -H 'Content-Type: application/json' \
  -d '{"id":"train-001","name":"ICE","device_id":"pico-001"}'
curl -X POST localhost:8000/api/trains/train-001/command \
  -H 'Content-Type: application/json' -d '{"command":"set_speed","speed":45}'
curl -X POST localhost:8000/api/emergency-stop \
  -H 'Content-Type: application/json' -d '{"reason":"cat on the layout"}'
```

---

## Adding a train

In the UI, press **Add a train** (or **Register** on a discovered device) and
fill in:

- **Train id** — `train-001`. Used verbatim in MQTT topics. Must match
  `TRAIN_ID` in the Pico's `config.py`. Letters, digits, `-`, `_`, `.` only.
- **Name** — what you call it: `ICE`, `Freight`.
- **Device id** — `pico-001`. Which Pico drives this train; unique across the
  fleet.
- **Speed limit** — per-train ceiling. The backend refuses higher values, so a
  short-radius branch line can be capped at 40 regardless of the UI.

---

## Configuring a Pico W

1. Flash MicroPython (1.22+) onto the Pico W.
2. Install the MQTT library on the board:
   ```python
   import mip; mip.install("umqtt.simple")
   ```
3. Copy `firmware/pico_w/*.py` to the board (Thonny, `mpremote fs cp`, …).
4. Edit `config.py` on the board:

   ```python
   TRAIN_ID  = "train-001"      # must match the UI
   DEVICE_ID = "pico-001"

   WIFI_SSID     = "your-ssid"
   WIFI_PASSWORD = "your-password"
   WIFI_COUNTRY  = "DK"

   MQTT_HOST = "192.168.1.10"   # the host running Docker; not "localhost"
   MQTT_PORT = 1883
   MQTT_USERNAME = None         # fill in when broker auth is enabled
   MQTT_PASSWORD = None

   SAFETY_TIMEOUT_S = 6.0
   HUB_DRIVER = "pwm"           # or "mock" to test with no motor attached
   ```
5. Reset the board. It prints its Wi-Fi address, connects, and appears in the
   UI within a second or two.

**Wiring for the `pwm` driver** (a DRV8833/L298N-style H-bridge):

```
MOTOR_PWM_PIN   (GP15) → ENA / nSLEEP
MOTOR_DIR_A_PIN (GP14) → IN1
MOTOR_DIR_B_PIN (GP13) → IN2
motor                  → OUT1 / OUT2
```

**Using the real LEGO hub.** `lego_hub.py` defines one interface —
`apply(speed, direction)`, `coast()`, `brake()`, `battery_voltage()`,
`connected()` — and currently ships two implementations: the H-bridge driver
above and a mock that logs instead of driving. The hub we actually have is a
Powered Up City Hub that speaks LWP3 over Bluetooth Low Energy; what it
reports and which commands work is documented in
[The LEGO hub](#the-lego-hub-city-hub-lwp3). **A driver for it is not written
yet** — it would be a third class in `lego_hub.py` implementing the same
interface, selected with `HUB_DRIVER`. Nothing else in the firmware, and
nothing in the backend or UI, needs to change.

---

## The LEGO hub (City Hub, LWP3)

Everything in this section was observed on our own hub with `lwp3_probe.py`
(see [below](#probing-the-hub)), except where marked *untested*.

### What we have

| | |
|---|---|
| Hub | LEGO Powered Up **City Hub (88009)**, advertised name `HUB NO.4` |
| Manufacturer string | `LEGO System A/S` |
| Protocol | LWP3 (LEGO Wireless Protocol 3.0), reported version bytes `00 03` |
| Firmware / hardware | raw bytes `00 00 01 10` / `00 00 00 01` (not decoded) |
| Transport | Bluetooth Low Energy, hub is the peripheral |
| Advertisement | LEGO manufacturer id `0x0397`; system type byte `0x41` |

The hub is not a Wi-Fi device. Whatever drives it — a Pico W, an ESP32 or the
PC directly — must act as a BLE central and write to one GATT characteristic.

### What is plugged in

Reported by the hub itself in its "Hub Attached I/O" messages on connect:

| Port | Id | Device | Notes |
|---|---|---|---|
| 0 (A) | `0x02` | train motor | moves the train |
| 1 (B) | `0x08` | light | LEGO light |
| 50 (`0x32`) | `0x17` | hub RGB LED | built in |
| 59 (`0x3B`) | `0x15` | current sensor | built in, not read yet |
| 60 (`0x3C`) | `0x14` | voltage sensor | built in, not read yet |

Both external ports are occupied. There is **no position sensing** (no
colour/distance sensor): the hub only knows its own battery voltage and motor
current, and the system has no way to know where a train is on the track.

### Connecting

```
service         00001623-1212-efde-1623-785feabcd123
characteristic  00001624-1212-efde-1623-785feabcd123   write, write-without-response, read, notify
```

Subscribe to notifications on the characteristic, then write commands to it.
Every packet is `[length] [hub id = 0x00] [message type] [payload…]`, where the
length byte counts the whole packet including itself.

Windows note: the hub may be hidden by a stale GATT cache. `bleak` is told to
ignore the cache with `winrt={"use_cached_services": False}`.

### Commands

Seen working or not working on this hub:

| Command | Bytes | Status |
|---|---|---|
| Motor, forward at 50 | `08 00 81 00 11 51 00 32` | **works** |
| Motor stop | `08 00 81 00 11 51 00 00` | *untested* (expected: coast) |
| Motor reverse at 50 | `08 00 81 00 11 51 00 CE` | *untested* (`0xCE` = −50 as a signed byte) |
| Motor brake | `08 00 81 00 11 51 00 7F` | *untested* (`0x7F` = 127 = brake) |
| Light on port B at 50% | `08 00 81 01 11 51 00 32` | *untested* (brightness 0–100) |
| Hub LED mode: colour index | `0A 00 41 32 00 01 00 00 00 00` | works, then send colour below |
| Hub LED colour | `08 00 81 32 11 51 00 <c>` | **works** (`c`: 0 off, 1 pink, 2 purple, 3 blue, 4 light blue, 5 cyan, 6 green, 7 yellow, 8 orange, 9 red, 10 white) |
| Hub LED mode: RGB | `0A 00 41 32 01 01 00 00 00 00` | *untested*; then `0A 00 81 32 11 51 01 <r> <g> <b>` |
| Request a hub property | `05 00 01 <prop> 05` | **works** |
| `StartPower` (`… 81 <port> 11 01 <power>`) | `07 00 81 00 11 01 32` | **does not move the motor** — the hub acknowledges it but nothing happens; use the `51 00` form |

The general form of a motor/light write is
`08 00 81 <port> 11 51 <mode> <data>`; mode 0 on a basic motor or light is
direct power/brightness, as a signed byte for motors.

**Replies.** After each output command the hub answers
`05 00 82 <port> <flags>`; we always saw flags `0A`. As far as we know this
means "command completed, hub idle". It does **not** prove the motor moved
(see `StartPower` above), so never treat it as confirmation of motion.

Hub properties (`prop`): `01` name, `03` firmware version, `04` hardware
version, `06` battery %, `08` manufacturer, `0A` LWP version, `0B` system type.
Observed on connect: name `HUB NO.4`, battery 100 %, system type `0x41`.

### What the hub can do

- Drive the train motor forward and backward at a chosen power (confirmed
  forward only).
- Switch/dim the light on port B (untested).
- Set its own LED colour, which doubles as a status lamp.
- Report battery state, and — once subscribed to ports 59/60 — voltage and
  motor current. Current is a candidate "train is blocked/stalled" signal.
- Announce when something is plugged in or removed from a port.

### What it cannot do (as set up)

- Tell us where it is: no position, speed or distance feedback. "Speed" in
  telemetry is only the last commanded value.
- Join Wi-Fi or speak MQTT. It needs a BLE bridge (see above).
- Run our safety watchdog. **What the hub does to a running motor when the BLE
  link drops is not known yet** and must be tested before relying on it: start
  the motor at a low power, close the controlling program or leave BLE range,
  and watch whether the train stops.

### Probing the hub

`lwp3_probe.py` (repo root) is a standalone tool for exploring the hub. It
needs only `bleak`:

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install bleak
python lwp3_probe.py          # scan, connect, print everything the hub says
```

Turn the hub on (green button, LED blinking) and close the official LEGO app
first; the hub accepts one connection at a time. At the `lwp3>` prompt,
`raw <hex bytes>` sends exactly what you type. Its `power`/`brake` helpers
currently use the `StartPower` form that does **not** move the motor — use
`raw` with the `51 00` commands above until that is changed.

---

## Safety model

Four independent mechanisms, because relying on the browser to keep a train
safe is how a train ends up on the floor.

1. **Device watchdog (the important one).** The Pico stops the motor if it has
   heard nothing from the server for `SAFETY_TIMEOUT_S` (default 6 s). The
   backend publishes `system/heartbeat` every 2 s, so a dead backend, a dead
   broker, a dead Wi-Fi link or a dead router all stop the fleet.
2. **Immediate stop on link loss.** Losing Wi-Fi or the MQTT connection brakes
   the motor at once, without waiting for the timeout.
3. **Latching emergency stop.** `emergency_stop` cuts power and sets a latch in
   both the device and the database. Speed and direction commands are refused
   — HTTP 409 from the API, ignored by the firmware — until `clear_emergency`.
   This is what gives emergency stop priority over normal speed commands, even
   one already in flight.
4. **Last Will and Testament.** Every device registers a retained `offline`
   status at connect time, so a Pico that loses power is marked offline by the
   broker within roughly 1.5× its keepalive. The backend also runs a staleness
   sweep: a train silent for `OFFLINE_TIMEOUT_SECONDS` becomes “no signal”, and
   then offline, even if the LWT never fires.

Firmware errors are not swallowed: an unhandled exception in the main loop
triggers an emergency stop, and a fatal error resets the board — a rebooting
Pico is safer than a stuck one.

---

## Running the simulator

The simulator speaks exactly the protocol the firmware speaks — retained
status, LWT, telemetry, the same safety timeout. The backend cannot tell the
difference, and there is no simulator-specific code anywhere else in the
system.

```bash
# Three trains via Compose
docker compose --profile sim up -d
docker compose logs -f simulator

# Or directly, for finer control
cd simulator && pip install -r requirements.txt
python simulator.py --host localhost --train-id train-001 --device-id pico-001
python simulator.py --count 5                    # train-001 … train-005
python simulator.py --train-id train-001 --die-after 20   # test LWT → offline
```

`--die-after N` closes the socket without a goodbye after N seconds, which is
precisely what a Pico losing battery power does: watch the train flip to
offline in the UI. `docker compose stop simulator` does the same thing for the
whole simulated fleet.

---

## Debugging with MQTT

The broker container ships with the Mosquitto client tools:

```bash
# Watch everything the fleet says
docker compose exec mosquitto mosquitto_sub -h localhost -t 'trains/#' -v

# Watch one train
docker compose exec mosquitto mosquitto_sub -h localhost -t 'trains/train-001/#' -v

# Drive a train by hand, bypassing the backend (development only)
docker compose exec mosquitto mosquitto_pub -h localhost \
  -t trains/train-001/command -m '{"command":"set_speed","speed":40}'

docker compose exec mosquitto mosquitto_pub -h localhost \
  -t trains/train-001/command -m '{"command":"emergency_stop","reason":"manual"}'

# Fake a device coming online (retained, like real firmware)
docker compose exec mosquitto mosquitto_pub -h localhost -r -q 1 \
  -t trains/train-009/status \
  -m '{"v":1,"train_id":"train-009","device_id":"pico-009","status":"online"}'

# Clear a stuck retained status
docker compose exec mosquitto mosquitto_pub -h localhost -r -t trains/train-009/status -m ''
```

Add `-u "$MQTT_USERNAME" -P "$MQTT_PASSWORD"` once authentication is on.

---

## Tests

```bash
cd backend
pip install -r requirements-dev.txt
python -m pytest -v
```

68 tests covering command validation, MQTT ingest and payload sanitising,
online/offline state management, the train API, emergency stop, persistence,
and a full integration test driving `simulated Pico → MQTT → backend →
WebSocket/API`. They run without a broker: MQTT sits behind a `Publisher`
interface and a fake broker moves messages both ways.

To also exercise a real Mosquitto:

```bash
docker compose up -d mosquitto
cd backend && MQTT_TEST_HOST=localhost python -m pytest tests/test_broker_integration.py -v
```

Inside Docker: `docker compose exec backend python -m pytest` (the image ships
without the test dependencies, so install them first or run on the host).

---

## Operations: logs, restarts, backups

```bash
docker compose ps                       # what is running and healthy
docker compose logs -f backend          # follow one service
docker compose logs --tail=100          # everything, recent
docker compose restart backend          # restart one service
docker compose up -d --build            # rebuild after a code change
docker compose down                     # stop, keep data
docker compose down -v                  # stop and delete the data volumes
```

Mosquitto also writes to a file inside its volume:

```bash
docker compose exec mosquitto tail -f /mosquitto/log/mosquitto.log
```

**Backups.** Two volumes hold state: `backend-data` (the SQLite database) and
`mosquitto-data` (retained messages and queued QoS 1 messages).

```bash
mkdir -p backups
# Database
docker compose exec -T backend cat /data/trains.db > backups/trains-$(date +%F).db
# Both volumes as tarballs
docker run --rm -v lego-train-control_backend-data:/data -v "$PWD/backups":/backup \
  alpine tar czf /backup/backend-data-$(date +%F).tar.gz -C /data .
docker run --rm -v lego-train-control_mosquitto-data:/data -v "$PWD/backups":/backup \
  alpine tar czf /backup/mosquitto-data-$(date +%F).tar.gz -C /data .
```

Restore by stopping the stack and untarring back into the volume.

**Verifying persistence:** set a train's name, `docker compose restart backend`,
reload the UI — the train, its history and its last known state are still
there.

---

## Security

The shipped configuration is for a **trusted home network**. It is documented
as such in `mosquitto.conf` and enforced nowhere else, so read this before
putting the stack on a shared network.

- **Do not forward port 1883 to the internet.** MQTT without TLS is plaintext.
- **The frontend never receives broker credentials** and cannot publish MQTT.
  Every command goes through backend validation; the browser only has HTTP.
- **Devices are not trusted.** Telemetry is clamped and type-checked before it
  is stored: an impossible speed is capped, a nonsense direction is dropped, a
  chatty device cannot bloat the database.
- **Commands are validated** against a strict schema — unknown commands,
  unknown fields, out-of-range speeds and speeds above a train's own limit are
  all rejected before publishing.

### Turning on authentication

```bash
# 1. Create broker users
docker compose exec mosquitto mosquitto_passwd -c -b /mosquitto/config/passwd backend 'strong-password'
docker compose exec mosquitto mosquitto_passwd -b /mosquitto/config/passwd train-001 'another-password'

# 2. Restrict what each user may do
cp mosquitto/config/aclfile.example mosquitto/config/aclfile

# 3. In mosquitto/config/mosquitto.conf: set allow_anonymous false and
#    uncomment the password_file and acl_file lines.

# 4. Put the backend's credentials in .env
#    MQTT_USERNAME=backend
#    MQTT_PASSWORD=strong-password

# 5. Set MQTT_USERNAME / MQTT_PASSWORD in each Pico's config.py, then:
docker compose restart mosquitto backend
```

### Enabling TLS later

`mosquitto.conf` contains a commented TLS listener on 8883. Add it as a
*second* listener rather than replacing 1883, so devices migrate one at a
time; then point `MQTT_PORT` at 8883 and add the CA certificate to each Pico.
No application code changes — the broker address and port are configuration on
both sides.

---

## Design decisions

**FastAPI + aiomqtt in one process.** HTTP, WebSocket and MQTT share one
asyncio loop, so there is no queue, no worker process and no inter-process
state to keep consistent. For a fleet of tens of trains this is ample.

**SQLite, PostgreSQL-ready.** Only portable SQLAlchemy constructs are used and
the connection string is a single environment variable, so moving to Postgres
is `DATABASE_URL=postgresql+asyncpg://…`, uncommenting one dependency and
adding a `db` service — no code changes.

**Vanilla JS on nginx.** No build step, no toolchain, no `node_modules`;
editing `app.js` and reloading is the whole development loop. nginx also
reverse-proxies `/api` and `/ws`, so the UI is same-origin and CORS never
comes into it. If the UI grows past what this comfortably supports, it can be
replaced wholesale — it consumes nothing but the documented REST and
WebSocket interfaces.

**MQTT for devices, HTTP for the browser.** Devices get QoS, retained state
and Last Will — none of which HTTP polling would provide — while the browser
gets a plain REST API and a single push socket, and no broker credentials.

**Speed as magnitude plus direction.** See [Message formats](#message-formats).

**Optimistic UI state, telemetry as truth.** A sent command updates the stored
state immediately so the UI responds instantly, but the next telemetry message
overwrites it. What you see is what the train reports, not what you asked for.
